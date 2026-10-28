"""Polls configured repositories for open pull requests whose head commit hasn't been reviewed."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random

from pr_review_agent.github import GitHubClient, RepoRef
from pr_review_agent.reviewer import ReviewRequest
from pr_review_agent.store import ReviewStore
from pr_review_agent.work_queue import ReviewQueue

log = logging.getLogger(__name__)


class Watcher:
    def __init__(
        self,
        github: GitHubClient,
        store: ReviewStore,
        queue: ReviewQueue,
        repos: list[str],
        interval: float,
    ) -> None:
        self._github = github
        self._store = store
        self._queue = queue
        self._repos = [RepoRef.parse(repo) for repo in repos]
        self._interval = interval
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def poll_once(self) -> int:
        submitted = 0
        for repo in self._repos:
            try:
                pulls = await self._github.list_open_pulls(repo)
            except Exception:
                log.exception("failed to list pull requests for %s", repo)
                continue
            for pull in pulls:
                number, sha = pull["number"], pull["head"]["sha"]
                if await self._store.has_reviewed(str(repo), number, sha):
                    continue
                if self._queue.submit(ReviewRequest(repo, number)):
                    submitted += 1
        return submitted

    async def run(self) -> None:
        log.info("watching %s every %ss", ", ".join(map(str, self._repos)), self._interval)
        while not self._stop.is_set():
            submitted = await self.poll_once()
            if submitted:
                log.info("queued %d pull request(s) for review", submitted)
            delay = self._interval * random.uniform(0.9, 1.1)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
