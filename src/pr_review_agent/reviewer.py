"""Orchestrates one review: fetch the PR, run checks, post inline comments and a commit status."""

from __future__ import annotations

import asyncio
import fnmatch
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from pr_review_agent.checks import run_checks
from pr_review_agent.config import Settings, Severity
from pr_review_agent.diff import FileDiff, parse_patch
from pr_review_agent.findings import Finding, sort_key
from pr_review_agent.github import GitHubClient, GitHubError, RepoRef
from pr_review_agent.llm import LLMReview, LLMReviewError
from pr_review_agent.store import ReviewStore

log = logging.getLogger(__name__)

MARKER = "<!-- pr-review-agent -->"


class LLM(Protocol):
    async def review(self, title: str, description: str, diffs: list[FileDiff]) -> LLMReview: ...


@dataclass(frozen=True)
class ReviewRequest:
    repo: RepoRef
    number: int
    installation_id: int | None = None
    force: bool = False  # re-review even if this head commit was already reviewed
    dry_run: bool = False  # compute findings but don't write anything to GitHub


@dataclass
class ReviewResult:
    repo: str
    number: int
    head_sha: str = ""
    skipped_reason: str | None = None
    findings: list[Finding] = field(default_factory=list)
    passed: bool = True
    body: str = ""


class Reviewer:
    def __init__(
        self,
        settings: Settings,
        github: GitHubClient,
        store: ReviewStore,
        llm: LLM | None = None,
    ) -> None:
        self._settings = settings
        self._github = github
        self._store = store
        self._llm = llm
        self._locks: dict[tuple[str, int], tuple[asyncio.Lock, int]] = {}

    async def review(self, request: ReviewRequest) -> ReviewResult:
        # Serialise reviews of the same PR; the lock is dropped once nobody holds or awaits it.
        key = (str(request.repo), request.number)
        lock, users = self._locks.get(key, (asyncio.Lock(), 0))
        self._locks[key] = (lock, users + 1)
        try:
            async with lock:
                return await self._review(request)
        finally:
            lock, users = self._locks[key]
            if users <= 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, users - 1)

    async def _review(self, req: ReviewRequest) -> ReviewResult:
        s = self._settings
        kw: dict[str, Any] = {"installation_id": req.installation_id}
        result = ReviewResult(str(req.repo), req.number)

        pull = await self._github.get_pull(req.repo, req.number, **kw)
        head_sha = pull["head"]["sha"]
        result.head_sha = head_sha
        if pull.get("state") != "open":
            result.skipped_reason = "pull request is not open"
            return result
        if pull.get("draft") and not s.review_drafts and not req.force:
            result.skipped_reason = "pull request is a draft"
            return result
        if not req.force and await self._store.has_reviewed(str(req.repo), req.number, head_sha):
            result.skipped_reason = f"commit {head_sha[:7]} already reviewed"
            return result

        log.info("reviewing %s#%s at %s", req.repo, req.number, head_sha[:7])
        if not req.dry_run:
            await self._status(req, head_sha, "pending", "Review in progress", kw)

        try:
            files = await self._github.list_pull_files(req.repo, req.number, **kw)
            diffs, notes = self._select_files(files)
            findings = run_checks(diffs, s.disabled_rules)

            llm_summary = ""
            if self._llm is not None and diffs:
                try:
                    llm_review = await self._llm.review(
                        pull.get("title", ""), pull.get("body") or "", diffs
                    )
                    llm_summary = llm_review.summary
                    findings.extend(llm_review.findings)
                    if llm_review.skipped_files:
                        notes.append(
                            "Too large for AI review: "
                            + ", ".join(f"`{p}`" for p in llm_review.skipped_files)
                        )
                except LLMReviewError as exc:
                    log.warning("AI review failed for %s#%s: %s", req.repo, req.number, exc)
                    notes.append(f"AI review unavailable: {exc}. Rule-based checks still ran.")

            findings = _dedupe(findings)
            result.findings = findings
            result.passed = not any(f.severity.rank >= s.fail_on.rank for f in findings)

            inline, overflow = self._split_inline(findings)
            result.body = self._render_body(findings, overflow, llm_summary, notes, result.passed)
            if req.dry_run:
                return result

            event = "COMMENT"
            if not result.passed and s.request_changes_on_fail:
                event = "REQUEST_CHANGES"
            await self._post_review(req, head_sha, result.body, event, inline, kw)
            state, description = _status_for(findings, result.passed)
            await self._status(req, head_sha, state, description, kw)
            await self._store.mark_reviewed(
                str(req.repo), req.number, head_sha, state, len(findings)
            )
            return result
        except Exception:
            if not req.dry_run:
                await self._status(req, head_sha, "error", "Review failed; see agent logs", kw)
            raise

    def _select_files(self, files: list[dict[str, Any]]) -> tuple[list[FileDiff], list[str]]:
        s = self._settings
        diffs: list[FileDiff] = []
        ignored: list[str] = []
        no_patch: list[str] = []
        for item in files:
            path = item["filename"]
            if item.get("status") == "removed":
                continue
            if any(fnmatch.fnmatch(path, pattern) for pattern in s.ignore_paths):
                ignored.append(path)
                continue
            if not item.get("patch"):
                no_patch.append(path)  # binary, or too large for GitHub to render a diff
                continue
            diffs.append(parse_patch(path, item["patch"], item.get("status", "modified")))

        notes: list[str] = []
        if len(diffs) > s.max_files:
            notes.append(
                f"Only the first {s.max_files} of {len(diffs)} changed files were reviewed."
            )
            diffs = diffs[: s.max_files]
        if no_patch:
            notes.append(f"{len(no_patch)} file(s) had no text diff (binary or too large).")
        if ignored:
            notes.append(f"{len(ignored)} file(s) skipped by ignore patterns.")
        return diffs, notes

    def _split_inline(self, findings: list[Finding]) -> tuple[list[Finding], list[Finding]]:
        inline: list[Finding] = []
        overflow: list[Finding] = []
        for finding in findings:  # already sorted most-severe first
            if finding.line is not None and len(inline) < self._settings.max_inline_comments:
                inline.append(finding)
            else:
                overflow.append(finding)
        return inline, overflow

    def _render_body(
        self,
        findings: list[Finding],
        overflow: list[Finding],
        llm_summary: str,
        notes: list[str],
        passed: bool,
    ) -> str:
        counts = {sev: sum(1 for f in findings if f.severity == sev) for sev in Severity}
        verdict = "✅ No blocking issues found." if passed else "❌ Blocking issues found."
        lines = [
            MARKER,
            "## PR review",
            "",
            f"{verdict} {counts[Severity.ERROR]} error(s), {counts[Severity.WARNING]} "
            f"warning(s), {counts[Severity.INFO]} note(s).",
        ]
        if llm_summary:
            lines += ["", llm_summary]
        if overflow:
            lines += ["", "### Other findings", ""]
            for f in overflow:
                where = f"`{f.path}:{f.line}`" if f.line else f"`{f.path}`"
                detail = f" — {f.body}" if f.body else ""
                lines.append(f"- {where} {f.render().splitlines()[0]}{detail}")
        if notes:
            lines += ["", "### Notes", ""] + [f"- {note}" for note in notes]
        return "\n".join(lines)

    async def _post_review(
        self,
        req: ReviewRequest,
        head_sha: str,
        body: str,
        event: str,
        inline: list[Finding],
        kw: dict[str, Any],
    ) -> None:
        comments = [
            {"path": f.path, "line": f.line, "side": "RIGHT", "body": f.render()} for f in inline
        ]
        try:
            await self._github.create_review(
                req.repo,
                req.number,
                commit_id=head_sha,
                body=body,
                event=event,
                comments=comments,
                **kw,
            )
        except GitHubError as exc:
            if exc.status != 422 or not comments:
                raise
            # A comment GitHub can't anchor (e.g. the PR moved on) fails the whole review.
            # Fall back to a summary-only review so the author still gets feedback.
            log.warning("inline comments rejected (%s); posting summary only", exc)
            fallback = (
                body
                + "\n\n### Inline findings\n\n"
                + "\n".join(f"- `{f.path}:{f.line}` {f.render().splitlines()[0]}" for f in inline)
            )
            await self._github.create_review(
                req.repo,
                req.number,
                commit_id=head_sha,
                body=fallback,
                event=event,
                comments=[],
                **kw,
            )

    async def _status(
        self, req: ReviewRequest, sha: str, state: str, description: str, kw: dict[str, Any]
    ) -> None:
        try:
            await self._github.set_status(
                req.repo,
                sha,
                state=state,
                description=description,
                context=self._settings.status_context,
                **kw,
            )
        except GitHubError as exc:
            # Missing statuses permission shouldn't block the review itself.
            log.warning("could not set commit status on %s@%s: %s", req.repo, sha[:7], exc)


def _dedupe(findings: list[Finding]) -> list[Finding]:
    seen: dict[tuple[str, int | None, str, str], Finding] = {}
    for finding in findings:
        current = seen.get(finding.key)
        if current is None or finding.severity.rank > current.severity.rank:
            seen[finding.key] = finding
    return sorted(seen.values(), key=sort_key)


def _status_for(findings: list[Finding], passed: bool) -> tuple[str, str]:
    if not findings:
        return "success", "No issues found"
    errors = sum(1 for f in findings if f.severity == Severity.ERROR)
    warnings = sum(1 for f in findings if f.severity == Severity.WARNING)
    summary = f"{errors} error(s), {warnings} warning(s)"
    return ("success" if passed else "failure"), summary
