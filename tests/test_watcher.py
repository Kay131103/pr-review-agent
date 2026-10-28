from __future__ import annotations

from pr_review_agent.github import GitHubClient
from pr_review_agent.reviewer import Reviewer
from pr_review_agent.store import ReviewStore
from pr_review_agent.watcher import Watcher
from pr_review_agent.work_queue import ReviewQueue
from tests.conftest import FakeGitHub, make_settings, patch_of


async def test_watcher_reviews_new_heads_only(
    fake_github: FakeGitHub, github_client: GitHubClient
) -> None:
    fake_github.add_pull(1, [{"filename": "a.py", "status": "added", "patch": patch_of("x = 1")}])
    fake_github.add_pull(2, [{"filename": "b.py", "status": "added", "patch": patch_of("y = 2")}])
    store = ReviewStore(":memory:")
    queue = ReviewQueue(Reviewer(make_settings(), github_client, store), workers=2, maxsize=10)
    watcher = Watcher(github_client, store, queue, ["acme/widgets"], interval=60)
    queue.start()
    try:
        assert await watcher.poll_once() == 2
        await queue.join()
        assert sorted(r["number"] for r in fake_github.reviews) == [1, 2]

        assert await watcher.poll_once() == 0  # nothing new

        fake_github.pulls[1]["head"]["sha"] = "f" * 40  # author pushed a new commit
        assert await watcher.poll_once() == 1
        await queue.join()
        assert len(fake_github.reviews) == 3
    finally:
        await queue.stop()
