"""Command-line entry point: ``serve``, ``watch`` and one-off ``review``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import re
import signal
import sys

from pr_review_agent import __version__
from pr_review_agent.config import Settings
from pr_review_agent.logging_setup import configure_logging

_PR_RE = re.compile(r"^(?P<repo>[\w.-]+/[\w.-]+)#(?P<number>\d+)$")
_PR_URL_RE = re.compile(r"github\.com/(?P<repo>[\w.-]+/[\w.-]+)/pull/(?P<number>\d+)")


def _parse_pr(value: str) -> tuple[str, int]:
    match = _PR_RE.match(value) or _PR_URL_RE.search(value)
    if not match:
        raise argparse.ArgumentTypeError("expected owner/repo#123 or a pull request URL")
    return match["repo"], int(match["number"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pr-review-agent", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the webhook server (and watcher, if configured)")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)

    watch = sub.add_parser("watch", help="poll repositories for pull requests without webhooks")
    watch.add_argument("repos", nargs="*", help="owner/repo (defaults to PR_REVIEW_WATCH_REPOS)")
    watch.add_argument("--once", action="store_true", help="poll once, review, and exit")

    review = sub.add_parser("review", help="review a single pull request now")
    review.add_argument("pr", type=_parse_pr, help="owner/repo#123 or a pull request URL")
    review.add_argument("--dry-run", action="store_true", help="print findings; post nothing")
    review.add_argument("--no-ai", action="store_true", help="skip the Claude review pass")

    args = parser.parse_args(argv)
    overrides: dict[str, object] = {}
    if args.command == "watch" and args.repos:
        overrides["watch_repos"] = args.repos
    if args.command == "review" and args.no_ai:
        overrides["llm_enabled"] = False
    settings = Settings(**overrides)  # type: ignore[arg-type]
    configure_logging(settings.log_level, settings.log_json)

    if args.command == "serve":
        return _serve(settings, args.host, args.port)
    if args.command == "watch":
        if not settings.watch_repos:
            parser.error("no repositories given (pass them or set PR_REVIEW_WATCH_REPOS)")
        return asyncio.run(_watch(settings, args.once))
    repo, number = args.pr
    return asyncio.run(_review_one(settings, repo, number, args.dry_run))


def _serve(settings: Settings, host: str | None, port: int | None) -> int:
    import uvicorn

    from pr_review_agent.server import create_app

    uvicorn.run(
        create_app(settings),
        host=host or settings.host,
        port=port or settings.port,
        log_config=None,  # keep our logging configuration
        proxy_headers=True,
    )
    return 0


async def _watch(settings: Settings, once: bool) -> int:
    from pr_review_agent.runtime import Runtime

    runtime = Runtime(settings)
    assert runtime.watcher is not None
    if once:
        runtime.queue.start()
        try:
            await runtime.watcher.poll_once()
            await runtime.queue.join()
        finally:
            await runtime.stop()
        return 0

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # not supported on Windows
            loop.add_signal_handler(sig, stop.set)
    await runtime.start()
    try:
        await stop.wait()
    finally:
        await runtime.stop()
    return 0


async def _review_one(settings: Settings, repo: str, number: int, dry_run: bool) -> int:
    from pr_review_agent.github import RepoRef
    from pr_review_agent.reviewer import ReviewRequest
    from pr_review_agent.runtime import Runtime

    runtime = Runtime(settings)
    try:
        result = await runtime.reviewer.review(
            ReviewRequest(RepoRef.parse(repo), number, force=True, dry_run=dry_run)
        )
    finally:
        await runtime.stop()
    if result.skipped_reason:
        print(f"Skipped: {result.skipped_reason}")
        return 0
    print(result.body)
    if dry_run:
        for finding in result.findings:
            where = f"{finding.path}:{finding.line}" if finding.line else finding.path
            print(f"[{finding.severity}] {where} {finding.rule}: {finding.title}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
