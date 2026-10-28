from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pr_review_agent.github import GitHubClient
from pr_review_agent.runtime import Runtime
from pr_review_agent.server import create_app, verify_signature
from pr_review_agent.store import ReviewStore
from tests.conftest import FakeGitHub, make_settings

SECRET = "s3cret"


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def client(github_client: GitHubClient):
    settings = make_settings(workers=0)  # no workers: requests stay queued for inspection
    app = create_app(
        settings, lambda: Runtime(settings, github=github_client, store=ReviewStore(":memory:"))
    )
    with TestClient(app) as test_client:
        yield test_client


def post(client: TestClient, event: str, payload: dict[str, Any], signature: str | None = None):
    body = json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "X-Hub-Signature-256": signature or sign(body)}
    return client.post("/webhook", content=body, headers=headers)


def pr_event(action: str = "opened") -> dict[str, Any]:
    return {
        "action": action,
        "repository": {"full_name": "acme/widgets"},
        "installation": {"id": 7},
        "pull_request": {"number": 5},
    }


def queue_depth(client: TestClient) -> int:
    return client.get("/healthz").json()["queue_depth"]


def test_verify_signature() -> None:
    assert verify_signature(SECRET, b"x", sign(b"x"))
    assert not verify_signature(SECRET, b"x", sign(b"y"))
    assert not verify_signature(SECRET, b"x", None)
    assert not verify_signature(SECRET, b"x", "sha1=abc")


def test_rejects_bad_signature(client: TestClient) -> None:
    assert post(client, "pull_request", pr_event(), signature="sha256=bad").status_code == 401


def test_ping(client: TestClient) -> None:
    assert post(client, "ping", {"zen": "hi"}).json() == {"result": "pong"}


def test_pull_request_is_queued_once(client: TestClient) -> None:
    first = post(client, "pull_request", pr_event())
    assert first.status_code == 202 and first.json() == {"result": "queued"}
    assert post(client, "pull_request", pr_event("synchronize")).json() == {"result": "duplicate"}
    assert queue_depth(client) == 1


def test_irrelevant_actions_ignored(client: TestClient) -> None:
    assert post(client, "pull_request", pr_event("labeled")).json() == {"result": "ignored"}
    assert post(client, "push", {"repository": {"full_name": "a/b"}}).json() == {
        "result": "ignored"
    }
    assert queue_depth(client) == 0


def comment_event(body: str, association: str = "MEMBER") -> dict[str, Any]:
    return {
        "action": "created",
        "repository": {"full_name": "acme/widgets"},
        "issue": {"number": 5, "pull_request": {}},
        "comment": {"id": 99, "body": body, "author_association": association},
    }


def test_review_command_from_collaborator(client: TestClient, fake_github: FakeGitHub) -> None:
    response = post(client, "issue_comment", comment_event("/review please"))
    assert response.json() == {"result": "queued"}
    assert fake_github.reactions == [{"content": "eyes"}]


def test_review_command_from_outsider_ignored(client: TestClient) -> None:
    response = post(client, "issue_comment", comment_event("/review", association="NONE"))
    assert response.json()["result"].startswith("ignored")
    assert post(client, "issue_comment", comment_event("nice work")).json() == {"result": "ignored"}
    assert queue_depth(client) == 0


def test_missing_secret_refuses_webhooks(github_client: GitHubClient) -> None:
    settings = make_settings(webhook_secret=None, workers=0)
    app = create_app(
        settings, lambda: Runtime(settings, github=github_client, store=ReviewStore(":memory:"))
    )
    with TestClient(app) as test_client:
        assert post(test_client, "ping", {}).status_code == 503
