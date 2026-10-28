from __future__ import annotations

import pytest

from pr_review_agent.config import Severity
from pr_review_agent.diff import FileDiff
from pr_review_agent.findings import Finding
from pr_review_agent.github import GitHubClient, RepoRef
from pr_review_agent.llm import LLMReview, LLMReviewError
from pr_review_agent.reviewer import MARKER, Reviewer, ReviewRequest
from pr_review_agent.store import ReviewStore
from tests.conftest import FakeGitHub, make_settings, patch_of

REPO = RepoRef("acme", "widgets")


class FakeLLM:
    def __init__(self, review: LLMReview | None = None, error: str | None = None) -> None:
        self._review = review or LLMReview()
        self._error = error
        self.calls = 0

    async def review(self, title: str, description: str, diffs: list[FileDiff]) -> LLMReview:
        self.calls += 1
        if self._error:
            raise LLMReviewError(self._error)
        return self._review


def make_reviewer(github: GitHubClient, llm: FakeLLM | None = None, **settings) -> Reviewer:
    return Reviewer(make_settings(**settings), github, ReviewStore(":memory:"), llm)


async def test_posts_inline_comments_and_failure_status(
    fake_github: FakeGitHub, github_client: GitHubClient
) -> None:
    fake_github.add_pull(
        1,
        [{"filename": "app.py", "status": "modified", "patch": patch_of("x = 1", "breakpoint()")}],
    )
    result = await make_reviewer(github_client).review(ReviewRequest(REPO, 1))

    assert not result.passed
    [review] = fake_github.reviews
    assert review["event"] == "COMMENT"
    assert review["body"].startswith(MARKER)
    [comment] = review["comments"]
    assert (comment["path"], comment["line"], comment["side"]) == ("app.py", 2, "RIGHT")
    assert [s["state"] for s in fake_github.statuses] == ["pending", "failure"]


async def test_clean_pr_passes(fake_github: FakeGitHub, github_client: GitHubClient) -> None:
    fake_github.add_pull(1, [{"filename": "a.py", "status": "added", "patch": patch_of("x = 1")}])
    result = await make_reviewer(github_client).review(ReviewRequest(REPO, 1))
    assert result.passed and result.findings == []
    assert fake_github.statuses[-1]["state"] == "success"


async def test_same_head_is_not_reviewed_twice(
    fake_github: FakeGitHub, github_client: GitHubClient
) -> None:
    fake_github.add_pull(1, [{"filename": "a.py", "status": "added", "patch": patch_of("x = 1")}])
    reviewer = make_reviewer(github_client)
    await reviewer.review(ReviewRequest(REPO, 1))
    second = await reviewer.review(ReviewRequest(REPO, 1))
    assert second.skipped_reason and "already reviewed" in second.skipped_reason
    await reviewer.review(ReviewRequest(REPO, 1, force=True))
    assert len(fake_github.reviews) == 2


async def test_drafts_are_skipped_by_default(
    fake_github: FakeGitHub, github_client: GitHubClient
) -> None:
    fake_github.add_pull(1, [], draft=True)
    result = await make_reviewer(github_client).review(ReviewRequest(REPO, 1))
    assert result.skipped_reason == "pull request is a draft"
    assert fake_github.reviews == [] and fake_github.statuses == []


async def test_dry_run_writes_nothing(fake_github: FakeGitHub, github_client: GitHubClient) -> None:
    fake_github.add_pull(
        1, [{"filename": "a.py", "status": "added", "patch": patch_of("breakpoint()")}]
    )
    result = await make_reviewer(github_client).review(ReviewRequest(REPO, 1, dry_run=True))
    assert len(result.findings) == 1
    assert fake_github.reviews == [] and fake_github.statuses == []


async def test_ignored_and_binary_files(
    fake_github: FakeGitHub, github_client: GitHubClient
) -> None:
    fake_github.add_pull(
        1,
        [
            {
                "filename": "package-lock.json",
                "status": "modified",
                "patch": patch_of("breakpoint()"),
            },
            {"filename": "logo.png", "status": "added"},
        ],
    )
    result = await make_reviewer(github_client).review(ReviewRequest(REPO, 1))
    assert result.findings == []
    assert "skipped by ignore patterns" in result.body and "no text diff" in result.body


async def test_falls_back_to_summary_when_inline_rejected(
    fake_github: FakeGitHub, github_client: GitHubClient
) -> None:
    fake_github.reject_inline = True
    fake_github.add_pull(
        1, [{"filename": "a.py", "status": "added", "patch": patch_of("breakpoint()")}]
    )
    await make_reviewer(github_client).review(ReviewRequest(REPO, 1))
    [review] = fake_github.reviews
    assert review["comments"] == [] and "`a.py:1`" in review["body"]


async def test_inline_comment_cap_moves_rest_to_summary(
    fake_github: FakeGitHub, github_client: GitHubClient
) -> None:
    lines = ["breakpoint()"] * 5
    fake_github.add_pull(1, [{"filename": "a.py", "status": "added", "patch": patch_of(*lines)}])
    await make_reviewer(github_client, max_inline_comments=2).review(ReviewRequest(REPO, 1))
    [review] = fake_github.reviews
    assert len(review["comments"]) == 2
    assert review["body"].count("`a.py:") == 3


async def test_request_changes_when_configured(
    fake_github: FakeGitHub, github_client: GitHubClient
) -> None:
    fake_github.add_pull(
        1, [{"filename": "a.py", "status": "added", "patch": patch_of("breakpoint()")}]
    )
    await make_reviewer(github_client, request_changes_on_fail=True).review(ReviewRequest(REPO, 1))
    assert fake_github.reviews[0]["event"] == "REQUEST_CHANGES"


async def test_llm_findings_are_merged(
    fake_github: FakeGitHub, github_client: GitHubClient
) -> None:
    fake_github.add_pull(1, [{"filename": "a.py", "status": "added", "patch": patch_of("x = 1")}])
    llm = FakeLLM(
        LLMReview(
            summary="Looks mostly fine.",
            findings=[
                Finding("a.py", 1, Severity.WARNING, "ai-review", "Off-by-one", "Use <=", "llm")
            ],
        )
    )
    result = await make_reviewer(github_client, llm).review(ReviewRequest(REPO, 1))
    assert llm.calls == 1 and result.passed  # warnings don't fail with fail_on=error
    assert "Looks mostly fine." in result.body
    assert fake_github.reviews[0]["comments"][0]["body"].startswith("🟡 **Off-by-one**")


async def test_llm_failure_does_not_block_rules(
    fake_github: FakeGitHub, github_client: GitHubClient
) -> None:
    fake_github.add_pull(
        1, [{"filename": "a.py", "status": "added", "patch": patch_of("breakpoint()")}]
    )
    result = await make_reviewer(github_client, FakeLLM(error="rate limited")).review(
        ReviewRequest(REPO, 1)
    )
    assert len(result.findings) == 1
    assert "AI review unavailable: rate limited" in result.body


async def test_error_status_on_unexpected_failure(
    fake_github: FakeGitHub, github_client: GitHubClient
) -> None:
    fake_github.add_pull(1, [{"filename": "a.py", "status": "added", "patch": patch_of("x")}])
    del fake_github.files[1]  # the fake now raises KeyError when files are fetched
    with pytest.raises(Exception):  # noqa: B017
        await make_reviewer(github_client).review(ReviewRequest(REPO, 1))
    assert [s["state"] for s in fake_github.statuses] == ["pending", "error"]
