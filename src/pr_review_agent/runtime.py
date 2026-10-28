"""Wires the long-lived components together and manages their lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from pr_review_agent.config import Settings
from pr_review_agent.github import GitHubClient
from pr_review_agent.llm import ClaudeReviewer
from pr_review_agent.reviewer import Reviewer
from pr_review_agent.store import ReviewStore
from pr_review_agent.watcher import Watcher
from pr_review_agent.work_queue import ReviewQueue

log = logging.getLogger(__name__)


class Runtime:
    def __init__(
        self,
        settings: Settings,
        github: GitHubClient | None = None,
        store: ReviewStore | None = None,
        llm: ClaudeReviewer | None = None,
    ) -> None:
        self.settings = settings
        self.github = github or GitHubClient(settings)
        self.store = store or ReviewStore(settings.db_path)
        if llm is None and settings.llm_active:
            llm = ClaudeReviewer(settings)
        self.llm = llm
        self.reviewer = Reviewer(settings, self.github, self.store, self.llm)
        self.queue = ReviewQueue(self.reviewer, settings.workers, settings.queue_size)
        self.watcher: Watcher | None = None
        if settings.watch_repos:
            self.watcher = Watcher(
                self.github,
                self.store,
                self.queue,
                settings.watch_repos,
                settings.poll_interval_seconds,
            )
        self._watch_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        log.info(
            "starting: auth=%s, ai_review=%s, watching=%d repo(s)",
            "github-app" if self.settings.uses_github_app else "token",
            "on" if self.llm else "off",
            len(self.settings.watch_repos),
        )
        self.queue.start()
        if self.watcher:
            self._watch_task = asyncio.create_task(self.watcher.run(), name="watcher")

    async def stop(self) -> None:
        if self.watcher and self._watch_task:
            self.watcher.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch_task
        await self.queue.stop()
        await self.github.aclose()
        if self.llm:
            await self.llm.aclose()
        self.store.close()
