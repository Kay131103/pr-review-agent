"""Bounded in-process work queue with a fixed pool of review workers."""

from __future__ import annotations

import asyncio
import logging

from pr_review_agent.reviewer import Reviewer, ReviewRequest

log = logging.getLogger(__name__)


class ReviewQueue:
    def __init__(self, reviewer: Reviewer, workers: int, maxsize: int) -> None:
        self._reviewer = reviewer
        self._queue: asyncio.Queue[ReviewRequest] = asyncio.Queue(maxsize=maxsize)
        self._pending: set[tuple[str, int]] = set()
        self._worker_count = workers
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    def submit(self, request: ReviewRequest) -> bool:
        """Enqueue a review. Returns False if it is already queued or the queue is full."""
        key = (str(request.repo), request.number)
        if key in self._pending and not request.force:
            return False
        try:
            self._queue.put_nowait(request)
        except asyncio.QueueFull:
            log.error("review queue full; dropping %s#%s", request.repo, request.number)
            return False
        self._pending.add(key)
        return True

    async def join(self) -> None:
        """Wait until every queued review has been processed."""
        await self._queue.join()

    def start(self) -> None:
        for index in range(self._worker_count):
            self._tasks.append(asyncio.create_task(self._worker(), name=f"review-worker-{index}"))

    async def stop(self, drain_seconds: float = 30.0) -> None:
        """Let in-flight work finish (up to ``drain_seconds``), then cancel the workers."""
        if not self._tasks:
            return
        try:
            await asyncio.wait_for(self._queue.join(), drain_seconds)
        except TimeoutError:
            log.warning("shutdown timeout with %d review(s) still queued", self.depth)
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _worker(self) -> None:
        while True:
            request = await self._queue.get()
            self._pending.discard((str(request.repo), request.number))
            try:
                result = await self._reviewer.review(request)
                if result.skipped_reason:
                    log.info("skipped %s#%s: %s", result.repo, result.number, result.skipped_reason)
                else:
                    log.info(
                        "reviewed %s#%s: %d finding(s), %s",
                        result.repo,
                        result.number,
                        len(result.findings),
                        "passed" if result.passed else "failed",
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("review of %s#%s failed", request.repo, request.number)
            finally:
                self._queue.task_done()
