import pytest

from pr_review_agent.checks import LARGE_FILE_ADDITIONS, run_checks
from pr_review_agent.config import Severity
from pr_review_agent.diff import parse_patch
from tests.conftest import patch_of

# Fake credentials are assembled at runtime so this file never contains a literal secret.
AWS_KEY = "AKIA" + "Z" * 16
GH_TOKEN = "ghp_" + "a1" * 18


def rules_for(path: str, *lines: str) -> list[str]:
    return [f.rule for f in run_checks([parse_patch(path, patch_of(*lines))])]


@pytest.mark.parametrize(
    ("line", "rule"),
    [
        (f'key = "{AWS_KEY}"', "secret-aws-key"),
        (f"TOKEN={GH_TOKEN}", "secret-github-token"),
        ("-----BEGIN RSA PRIVATE KEY-----", "secret-private-key"),
        ('password = "hunter22"', "secret-hardcoded-password"),
        ("<<<<<<< HEAD", "merge-conflict-marker"),
        ("=======", "merge-conflict-marker"),
        ("# TODO: handle errors", "todo-comment"),
    ],
)
def test_line_rules_fire(line: str, rule: str) -> None:
    assert rule in rules_for("src/app.py", line)


def test_language_specific_rules() -> None:
    assert "debug-console-log" in rules_for("web/app.ts", "console.log(x)")
    assert "debug-console-log" not in rules_for("app.py", "console.log(x)")
    assert "debug-breakpoint" in rules_for("app.py", "    breakpoint()")
    assert "debug-debugger" in rules_for("app.js", "  debugger;")


def test_clean_code_has_no_findings() -> None:
    assert rules_for("app.py", "def add(a, b):", "    return a + b") == []


def test_only_added_lines_are_checked() -> None:
    patch = "@@ -1,2 +1,1 @@\n-<<<<<<< HEAD\n ok = True"
    assert run_checks([parse_patch("a.py", patch)]) == []


def test_finding_points_at_new_line_number() -> None:
    [finding] = run_checks([parse_patch("a.py", patch_of("x = 1", "breakpoint()", start=10))])
    assert (finding.line, finding.severity) == (11, Severity.ERROR)


def test_disabled_rules_are_skipped() -> None:
    diff = parse_patch("a.py", patch_of("# TODO later"))
    assert run_checks([diff], disabled=["todo-comment"]) == []


def test_large_change_is_file_level() -> None:
    diff = parse_patch("big.py", patch_of(*(["x = 1"] * LARGE_FILE_ADDITIONS)))
    [finding] = run_checks([diff])
    assert finding.rule == "large-change" and finding.line is None
