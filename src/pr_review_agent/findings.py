from __future__ import annotations

from dataclasses import dataclass

from pr_review_agent.config import Severity

_ICONS = {Severity.ERROR: "🔴", Severity.WARNING: "🟡", Severity.INFO: "🔵"}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int | None  # None = file-level finding (reported in the review summary)
    severity: Severity
    rule: str
    title: str
    body: str = ""
    source: str = "rules"  # "rules" or "llm"

    @property
    def key(self) -> tuple[str, int | None, str, str]:
        return (self.path, self.line, self.rule, self.title)

    def render(self) -> str:
        text = f"{_ICONS[self.severity]} **{self.title}** `[{self.rule}]`"
        if self.body:
            text += f"\n\n{self.body}"
        return text


def sort_key(finding: Finding) -> tuple[int, str, int]:
    return (-finding.severity.rank, finding.path, finding.line or 0)
