"""SQLite record of which pull request head commits have already been reviewed."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    repo        TEXT    NOT NULL,
    number      INTEGER NOT NULL,
    head_sha    TEXT    NOT NULL,
    state       TEXT    NOT NULL,
    findings    INTEGER NOT NULL,
    reviewed_at REAL    NOT NULL,
    PRIMARY KEY (repo, number, head_sha)
)
"""


class ReviewStore:
    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)
        self._lock = asyncio.Lock()

    async def has_reviewed(self, repo: str, number: int, head_sha: str) -> bool:
        async with self._lock:
            row = await asyncio.to_thread(
                lambda: self._conn.execute(
                    "SELECT 1 FROM reviews WHERE repo=? AND number=? AND head_sha=?",
                    (repo, number, head_sha),
                ).fetchone()
            )
        return row is not None

    async def mark_reviewed(
        self, repo: str, number: int, head_sha: str, state: str, findings: int
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "INSERT OR REPLACE INTO reviews VALUES (?, ?, ?, ?, ?, ?)",
                (repo, number, head_sha, state, findings, time.time()),
            )

    def close(self) -> None:
        self._conn.close()
