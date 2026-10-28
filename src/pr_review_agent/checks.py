"""Deterministic, rule-based checks run against the lines a pull request adds."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

from pr_review_agent.config import Severity
from pr_review_agent.diff import DiffLine, FileDiff
from pr_review_agent.findings import Finding


@dataclass(frozen=True)
class LineRule:
    id: str
    severity: Severity
    title: str
    pattern: re.Pattern[str]
    body: str = ""
    # Restrict to these file suffixes (lower-case, with dot); None means every file.
    suffixes: frozenset[str] | None = None

    def applies_to(self, path: str) -> bool:
        return self.suffixes is None or PurePosixPath(path).suffix.lower() in self.suffixes


_JS = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte"})
_PY = frozenset({".py"})

_SECRET_BODY = (
    "This looks like a credential. Remove it from the change, rotate it, and load it from a "
    "secret store or environment variable instead. Rewriting history may be required."
)

LINE_RULES: tuple[LineRule, ...] = (
    LineRule(
        "secret-private-key",
        Severity.ERROR,
        "Private key committed",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY"),
        _SECRET_BODY,
    ),
    LineRule(
        "secret-aws-key",
        Severity.ERROR,
        "AWS access key ID",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        _SECRET_BODY,
    ),
    LineRule(
        "secret-github-token",
        Severity.ERROR,
        "GitHub token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b"),
        _SECRET_BODY,
    ),
    LineRule(
        "secret-anthropic-key",
        Severity.ERROR,
        "Anthropic API key",
        re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
        _SECRET_BODY,
    ),
    LineRule(
        "secret-slack-token",
        Severity.ERROR,
        "Slack token",
        re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
        _SECRET_BODY,
    ),
    LineRule(
        "secret-hardcoded-password",
        Severity.WARNING,
        "Possible hard-coded password",
        re.compile(
            r"""(?i)\b(?:password|passwd|pwd|secret|api[_-]?key)\b\s*[:=]\s*["'][^"'\s]{6,}["']"""
        ),
        "Avoid committing credentials. If this is a test fixture, consider a clearly fake value.",
    ),
    LineRule(
        "merge-conflict-marker",
        Severity.ERROR,
        "Unresolved merge conflict marker",
        re.compile(r"^(?:<{7}|>{7})(?:\s|$)|^={7}$"),
        "Resolve the conflict before merging.",
    ),
    LineRule(
        "debug-breakpoint",
        Severity.ERROR,
        "Debugger breakpoint left in code",
        re.compile(r"\b(?:pdb\.set_trace|ipdb\.set_trace|breakpoint)\(\)"),
        suffixes=_PY,
    ),
    LineRule(
        "debug-debugger",
        Severity.ERROR,
        "`debugger` statement left in code",
        re.compile(r"^\s*debugger\s*;?\s*$"),
        suffixes=_JS,
    ),
    LineRule(
        "debug-console-log",
        Severity.WARNING,
        "`console.log` left in code",
        re.compile(r"\bconsole\.log\("),
        "Use the project's logger or remove before merging.",
        suffixes=_JS,
    ),
    LineRule(
        "todo-comment",
        Severity.INFO,
        "New TODO/FIXME",
        re.compile(r"(?:#|//|/\*|<!--)\s*(?:TODO|FIXME|XXX|HACK)\b"),
        "Consider tracking this in an issue so it is not forgotten.",
    ),
)

FileRule = Callable[[FileDiff], Iterable[Finding]]

LARGE_FILE_ADDITIONS = 1000


def _large_addition(diff: FileDiff) -> Iterable[Finding]:
    added = len(diff.added)
    if added >= LARGE_FILE_ADDITIONS:
        yield Finding(
            diff.path,
            None,
            Severity.INFO,
            "large-change",
            f"Large change ({added} added lines)",
            "Large files are hard to review; consider splitting them up.",
        )


FILE_RULES: dict[str, FileRule] = {"large-change": _large_addition}


def run_checks(diffs: Iterable[FileDiff], disabled: Iterable[str] = ()) -> list[Finding]:
    disabled_set = set(disabled)
    line_rules = [rule for rule in LINE_RULES if rule.id not in disabled_set]
    findings: list[Finding] = []
    for diff in diffs:
        applicable = [rule for rule in line_rules if rule.applies_to(diff.path)]
        for line in diff.added:
            findings.extend(_check_line(diff.path, line, applicable))
        for rule_id, file_rule in FILE_RULES.items():
            if rule_id not in disabled_set:
                findings.extend(file_rule(diff))
    return findings


def _check_line(path: str, line: DiffLine, rules: list[LineRule]) -> Iterable[Finding]:
    for rule in rules:
        if rule.pattern.search(line.content):
            yield Finding(path, line.new_line, rule.severity, rule.id, rule.title, rule.body)
