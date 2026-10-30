from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from pr_review_agent.diff import parse_patch
from pr_review_agent.llm import ClaudeReviewer, LLMReviewError
from tests.conftest import make_settings, patch_of


class FakeMessages:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self.response


def fake_client(text: str | None, stop_reason: str = "end_turn") -> Any:
    content = [] if text is None else [SimpleNamespace(type="text", text=text)]
    messages = FakeMessages(SimpleNamespace(stop_reason=stop_reason, content=content))
    return SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages))


DIFFS = [parse_patch("a.py", patch_of("x = 1", "y = 2"))]


async def test_parses_findings_and_validates_lines() -> None:
    payload = {
        "summary": "Two notes.",
        "findings": [
            {"path": "a.py", "line": 2, "severity": "error", "title": "Bug", "body": "b"},
            {"path": "a.py", "line": 99, "severity": "warning", "title": "Far", "body": ""},
            {"path": "ghost.py", "line": 1, "severity": "info", "title": "Nope", "body": ""},
        ],
    }
    client = fake_client(json.dumps(payload))
    review = await ClaudeReviewer(make_settings(), client).review("t", "d", DIFFS)

    assert review.summary == "Two notes."
    assert [(f.line, f.title) for f in review.findings] == [(2, "Bug"), (None, "Far")]
    kwargs = client.messages.kwargs
    assert kwargs["model"] == "qwen3.7-max"
    assert "output_config" not in kwargs
    assert "thinking" not in kwargs
    assert "fallbacks" not in kwargs


@pytest.mark.parametrize(
    ("text", "stop_reason", "message"),
    [
        ("{}", "refusal", "declined"),
        ("{", "max_tokens", "token limit"),
        ("not json", "end_turn", "malformed"),
        (None, "end_turn", "no review content"),
    ],
)
async def test_bad_responses_raise(text: str | None, stop_reason: str, message: str) -> None:
    reviewer = ClaudeReviewer(make_settings(), fake_client(text, stop_reason))
    with pytest.raises(LLMReviewError, match=message):
        await reviewer.review("t", "d", DIFFS)


async def test_oversized_files_are_reported_not_sent() -> None:
    big = parse_patch("big.py", patch_of(*(["z = 0"] * 50)))
    client = fake_client(json.dumps({"summary": "", "findings": []}))
    reviewer = ClaudeReviewer(make_settings(max_patch_chars=400), client)
    review = await reviewer.review("t", "d", [*DIFFS, big])
    assert review.skipped_files == ["big.py"]
    prompt = client.messages.kwargs["messages"][0]["content"]
    assert 'path="a.py"' in prompt and "big.py" not in prompt
