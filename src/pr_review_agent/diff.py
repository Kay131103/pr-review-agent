"""Parsing of the per-file unified-diff ``patch`` strings returned by the GitHub files API."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class DiffLine:
    kind: str  # "+", "-", " ", or "@" for a hunk header
    content: str
    new_line: int | None  # line number in the new file (None for removed lines)
    old_line: int | None  # line number in the old file (None for added lines)


@dataclass
class FileDiff:
    path: str
    status: str
    lines: list[DiffLine] = field(default_factory=list)

    @property
    def added(self) -> list[DiffLine]:
        return [line for line in self.lines if line.kind == "+"]

    @property
    def commentable_lines(self) -> set[int]:
        """New-file lines GitHub accepts review comments on (``side=RIGHT``)."""
        return {line.new_line for line in self.lines if line.new_line is not None}

    def annotated(self) -> str:
        """Render the diff with new-file line numbers, for an LLM to cite exact lines."""
        out: list[str] = []
        for line in self.lines:
            if line.kind == "@":
                out.append(line.content)
                continue
            number = line.new_line if line.new_line is not None else ""
            out.append(f"{number:>6} {line.kind}{line.content}")
        return "\n".join(out)


def parse_patch(path: str, patch: str | None, status: str = "modified") -> FileDiff:
    diff = FileDiff(path=path, status=status)
    if not patch:
        return diff
    old_no = new_no = 0
    in_hunk = False
    for raw in patch.splitlines():
        match = _HUNK_RE.match(raw)
        if match:
            old_no, new_no = int(match.group(1)), int(match.group(3))
            in_hunk = True
            diff.lines.append(DiffLine("@", raw, None, None))
            continue
        if not in_hunk or raw.startswith("\\"):  # "\ No newline at end of file"
            continue
        kind, content = (raw[0], raw[1:]) if raw else (" ", "")
        if kind == "+":
            diff.lines.append(DiffLine("+", content, new_no, None))
            new_no += 1
        elif kind == "-":
            diff.lines.append(DiffLine("-", content, None, old_no))
            old_no += 1
        else:
            diff.lines.append(DiffLine(" ", content, new_no, old_no))
            new_no += 1
            old_no += 1
    return diff
