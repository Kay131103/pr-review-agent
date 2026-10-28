"""Minimal async GitHub REST client with token/App authentication, retries and pagination."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
import jwt

from pr_review_agent import __version__
from pr_review_agent.config import Settings

log = logging.getLogger(__name__)

_RETRY_STATUSES = {500, 502, 503, 504}
_MAX_ATTEMPTS = 5


class GitHubError(Exception):
    def __init__(self, status: int, message: str, payload: Any = None) -> None:
        super().__init__(f"GitHub API error {status}: {message}")
        self.status = status
        self.payload = payload


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str

    @classmethod
    def parse(cls, full_name: str) -> RepoRef:
        owner, _, name = full_name.partition("/")
        if not owner or not name or "/" in name:
            raise ValueError(f"expected 'owner/name', got {full_name!r}")
        return cls(owner, name)

    def __str__(self) -> str:
        return f"{self.owner}/{self.name}"


class TokenProvider:
    """Returns a bearer token for API calls against a given repository."""

    async def token_for(self, repo: RepoRef, installation_id: int | None = None) -> str:
        raise NotImplementedError


class StaticTokenProvider(TokenProvider):
    def __init__(self, token: str) -> None:
        self._token = token

    async def token_for(self, repo: RepoRef, installation_id: int | None = None) -> str:
        return self._token


class AppTokenProvider(TokenProvider):
    """Mints GitHub App installation tokens and caches them until shortly before expiry."""

    def __init__(self, app_id: str, private_key: str, http: httpx.AsyncClient) -> None:
        self._app_id = app_id
        self._private_key = private_key
        self._http = http
        self._installations: dict[str, int] = {}
        self._tokens: dict[int, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    def _app_jwt(self) -> str:
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": self._app_id}
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    async def _app_request(self, method: str, url: str) -> dict[str, Any]:
        response = await self._http.request(
            method, url, headers={"Authorization": f"Bearer {self._app_jwt()}"}
        )
        if response.status_code >= 400:
            raise GitHubError(response.status_code, response.text)
        return response.json()

    async def token_for(self, repo: RepoRef, installation_id: int | None = None) -> str:
        async with self._lock:
            if installation_id is None:
                installation_id = self._installations.get(str(repo))
            if installation_id is None:
                data = await self._app_request("GET", f"/repos/{repo}/installation")
                installation_id = int(data["id"])
            self._installations[str(repo)] = installation_id

            cached = self._tokens.get(installation_id)
            if cached and cached[1] - time.time() > 300:
                return cached[0]
            data = await self._app_request(
                "POST", f"/app/installations/{installation_id}/access_tokens"
            )
            expires_at = _parse_github_time(data.get("expires_at")) or time.time() + 3000
            self._tokens[installation_id] = (data["token"], expires_at)
            return data["token"]


def _parse_github_time(value: str | None) -> float | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class GitHubClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        token_provider: TokenProvider | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=settings.github_api_url,
            timeout=httpx.Timeout(30.0, connect=10.0),
            transport=transport,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": f"pr-review-agent/{__version__}",
            },
        )
        if token_provider is not None:
            self._tokens = token_provider
        elif settings.uses_github_app:
            assert settings.github_app_id is not None
            self._tokens = AppTokenProvider(
                settings.github_app_id, settings.app_private_key(), self._http
            )
        elif settings.github_token is not None:
            self._tokens = StaticTokenProvider(settings.github_token.get_secret_value())
        else:
            raise ValueError("Configure PR_REVIEW_GITHUB_TOKEN or GitHub App credentials")

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(
        self,
        method: str,
        repo: RepoRef,
        path: str,
        *,
        installation_id: int | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        token = await self._tokens.token_for(repo, installation_id)
        headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await self._http.request(method, path, headers=headers, **kwargs)
            except httpx.TransportError as exc:
                if attempt == _MAX_ATTEMPTS:
                    raise
                delay = _backoff(attempt)
                log.warning("GitHub transport error %s; retrying in %.1fs", exc, delay)
                await asyncio.sleep(delay)
                continue

            delay = _retry_delay(response, attempt)
            if delay is None:
                if response.status_code >= 400:
                    raise GitHubError(
                        response.status_code, _error_message(response), _json(response)
                    )
                return response
            log.warning(
                "GitHub %s %s -> %s; retrying in %.1fs", method, path, response.status_code, delay
            )
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def paginate(
        self,
        repo: RepoRef,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        installation_id: int | None = None,
        limit: int = 3000,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        url: str | None = path
        query: dict[str, Any] | None = {"per_page": 100, **(params or {})}
        while url and len(items) < limit:
            response = await self.request(
                "GET", repo, url, installation_id=installation_id, params=query
            )
            items.extend(response.json())
            next_link = response.links.get("next")
            url = next_link["url"] if next_link else None
            query = None  # the "next" URL already carries the query string
        return items[:limit]

    # --- Endpoints used by the reviewer ---

    async def get_pull(self, repo: RepoRef, number: int, **kw: Any) -> dict[str, Any]:
        return (await self.request("GET", repo, f"/repos/{repo}/pulls/{number}", **kw)).json()

    async def list_pull_files(self, repo: RepoRef, number: int, **kw: Any) -> list[dict[str, Any]]:
        return await self.paginate(repo, f"/repos/{repo}/pulls/{number}/files", **kw)

    async def list_open_pulls(self, repo: RepoRef, **kw: Any) -> list[dict[str, Any]]:
        return await self.paginate(repo, f"/repos/{repo}/pulls", params={"state": "open"}, **kw)

    async def create_review(
        self,
        repo: RepoRef,
        number: int,
        *,
        commit_id: str,
        body: str,
        event: str,
        comments: list[dict[str, Any]],
        **kw: Any,
    ) -> dict[str, Any]:
        payload = {"commit_id": commit_id, "body": body, "event": event, "comments": comments}
        response = await self.request(
            "POST", repo, f"/repos/{repo}/pulls/{number}/reviews", json=payload, **kw
        )
        return response.json()

    async def set_status(
        self,
        repo: RepoRef,
        sha: str,
        *,
        state: str,
        description: str,
        context: str,
        **kw: Any,
    ) -> None:
        payload = {"state": state, "description": description[:140], "context": context}
        await self.request("POST", repo, f"/repos/{repo}/statuses/{sha}", json=payload, **kw)

    async def add_reaction(self, repo: RepoRef, comment_id: int, content: str, **kw: Any) -> None:
        await self.request(
            "POST",
            repo,
            f"/repos/{repo}/issues/comments/{comment_id}/reactions",
            json={"content": content},
            **kw,
        )


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _error_message(response: httpx.Response) -> str:
    data = _json(response)
    if isinstance(data, dict):
        message = str(data.get("message", ""))
        errors = data.get("errors")
        return f"{message} {errors}" if errors else message
    return response.text[:500]


def _backoff(attempt: int) -> float:
    return min(60.0, 2.0 ** (attempt - 1)) + random.uniform(0, 1)


def _retry_delay(response: httpx.Response, attempt: int) -> float | None:
    """Seconds to wait before retrying, or None if the response should not be retried."""
    if attempt >= _MAX_ATTEMPTS:
        return None
    status = response.status_code
    if status in _RETRY_STATUSES:
        return _backoff(attempt)
    if status in (403, 429):
        retry_after = response.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), 300.0)
        if response.headers.get("x-ratelimit-remaining") == "0":
            reset = response.headers.get("x-ratelimit-reset")
            if reset and reset.isdigit():
                wait = int(reset) - time.time()
                return max(1.0, wait) if wait <= 300 else None
        if status == 429 or "secondary rate limit" in response.text.lower():
            return _backoff(attempt) + 30
    return None
