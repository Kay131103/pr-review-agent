from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from pr_review_agent.config import Settings
from pr_review_agent.github import GitHubClient, StaticTokenProvider


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "github_token": "test-token",
        "webhook_secret": "s3cret",
        "anthropic_api_key": None,
        "db_path": ":memory:",
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)


@dataclass
class FakeGitHub:
    """In-memory stand-in for the parts of the GitHub REST API the agent uses."""

    pulls: dict[int, dict[str, Any]] = field(default_factory=dict)
    files: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    reviews: list[dict[str, Any]] = field(default_factory=list)
    statuses: list[dict[str, Any]] = field(default_factory=list)
    reactions: list[dict[str, Any]] = field(default_factory=list)
    reject_inline: bool = False
    requests: list[httpx.Request] = field(default_factory=list)

    def add_pull(self, number: int, files: list[dict[str, Any]], **fields: Any) -> None:
        self.pulls[number] = {
            "number": number,
            "state": "open",
            "draft": False,
            "title": f"PR {number}",
            "body": "",
            "head": {"sha": f"sha{number:037d}"},
            **fields,
        }
        self.files[number] = files

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["authorization"] == "Bearer test-token"
        path = request.url.path
        if m := re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/(\d+)", path):
            return httpx.Response(200, json=self.pulls[int(m[1])])
        if m := re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/(\d+)/files", path):
            return httpx.Response(200, json=self.files[int(m[1])])
        if re.fullmatch(r"/repos/[^/]+/[^/]+/pulls", path):
            return httpx.Response(200, json=list(self.pulls.values()))
        if m := re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/(\d+)/reviews", path):
            payload = json.loads(request.content)
            if self.reject_inline and payload["comments"]:
                return httpx.Response(422, json={"message": "Unprocessable Entity"})
            self.reviews.append({"number": int(m[1]), **payload})
            return httpx.Response(200, json={"id": len(self.reviews)})
        if re.fullmatch(r"/repos/[^/]+/[^/]+/statuses/\w+", path):
            self.statuses.append(json.loads(request.content))
            return httpx.Response(201, json={})
        if re.fullmatch(r"/repos/[^/]+/[^/]+/issues/comments/\d+/reactions", path):
            self.reactions.append(json.loads(request.content))
            return httpx.Response(201, json={})
        return httpx.Response(404, json={"message": f"no fake for {path}"})


@pytest.fixture
def fake_github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def github_client(fake_github: FakeGitHub) -> GitHubClient:
    return GitHubClient(
        make_settings(),
        transport=httpx.MockTransport(fake_github.handler),
        token_provider=StaticTokenProvider("test-token"),
    )


def patch_of(*added: str, start: int = 1) -> str:
    return f"@@ -0,0 +{start},{len(added)} @@\n" + "\n".join(f"+{line}" for line in added)
