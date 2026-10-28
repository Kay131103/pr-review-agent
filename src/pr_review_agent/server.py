"""FastAPI app that receives GitHub webhooks and queues reviews."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from pr_review_agent import __version__
from pr_review_agent.config import Settings
from pr_review_agent.github import RepoRef
from pr_review_agent.reviewer import ReviewRequest
from pr_review_agent.runtime import Runtime

log = logging.getLogger(__name__)

PR_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
MAX_BODY_BYTES = 25 * 1024 * 1024  # GitHub caps webhook payloads at 25 MB


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


def create_app(settings: Settings, runtime_factory: Callable[[], Runtime] | None = None) -> FastAPI:
    factory = runtime_factory or (lambda: Runtime(settings))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = factory()
        app.state.runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="pr-review-agent", version=__version__, lifespan=lifespan)

    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        runtime: Runtime = request.app.state.runtime
        return {"status": "ok", "version": __version__, "queue_depth": runtime.queue.depth}

    @app.post("/webhook")
    async def webhook(
        request: Request,
        x_github_event: str = Header(default=""),
        x_hub_signature_256: str | None = Header(default=None),
        x_github_delivery: str = Header(default=""),
    ) -> JSONResponse:
        if settings.webhook_secret is None:
            raise HTTPException(503, "webhook secret is not configured")
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(413, "payload too large")
        if not verify_signature(
            settings.webhook_secret.get_secret_value(), body, x_hub_signature_256
        ):
            raise HTTPException(401, "invalid signature")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "invalid JSON") from exc

        runtime: Runtime = request.app.state.runtime
        outcome = await _dispatch(runtime, settings, x_github_event, payload)
        log.info("delivery %s event=%s -> %s", x_github_delivery, x_github_event, outcome)
        return JSONResponse({"result": outcome}, status_code=202 if outcome == "queued" else 200)

    return app


async def _dispatch(
    runtime: Runtime, settings: Settings, event: str, payload: dict[str, Any]
) -> str:
    if event == "ping":
        return "pong"
    installation_id = (payload.get("installation") or {}).get("id")
    repo_name = (payload.get("repository") or {}).get("full_name")
    if not repo_name:
        return "ignored"
    repo = RepoRef.parse(repo_name)

    if event == "pull_request":
        if payload.get("action") not in PR_ACTIONS:
            return "ignored"
        number = payload["pull_request"]["number"]
        queued = runtime.queue.submit(ReviewRequest(repo, number, installation_id))
        return "queued" if queued else "duplicate"

    if event == "issue_comment":
        issue = payload.get("issue") or {}
        comment = payload.get("comment") or {}
        if (
            payload.get("action") != "created"
            or "pull_request" not in issue
            or not comment.get("body", "").strip().startswith(settings.review_command)
        ):
            return "ignored"
        if comment.get("author_association") not in TRUSTED_ASSOCIATIONS:
            return "ignored: commenter is not a collaborator"
        queued = runtime.queue.submit(
            ReviewRequest(repo, issue["number"], installation_id, force=True)
        )
        if queued:
            try:
                await runtime.github.add_reaction(
                    repo, comment["id"], "eyes", installation_id=installation_id
                )
            except Exception as exc:  # cosmetic; never fail the delivery over it
                log.debug("could not react to comment: %s", exc)
        return "queued" if queued else "duplicate"

    return "ignored"
