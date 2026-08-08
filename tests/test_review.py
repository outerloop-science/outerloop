import json
from typing import Any

import pytest

from autoresearch.review import (
    ADVISORY_HEADER,
    MARKER,
    MAX_DIFF_CHARS,
    OPT_OUT_LABEL,
    PullRequest,
    ReviewResult,
    build_prompt,
    format_comment,
    review,
    skip_reason,
)

BOT = "agentic-learning-bot"


class FakeCompleter:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def complete(self, system: str, prompt: str, schema: dict[str, Any]) -> str:
        self.calls.append((system, prompt, schema))
        return json.dumps(self.payload)


def make_pr(**overrides: Any) -> PullRequest:
    base = {
        "repo": "org/repo",
        "number": 7,
        "title": "Fix off-by-one",
        "body": "Fixes the loop bound.",
        "diff": "--- a/x.py\n+++ b/x.py\n@@\n-for i in range(n-1):\n+for i in range(n):\n",
        "author": "human-dev",
        "labels": (),
    }
    return PullRequest(**{**base, **overrides})


ONE_FINDING = {
    "findings": [
        {
            "file": "x.py",
            "line": 12,
            "confidence": "high",
            "summary": "Loop still skips the last element",
            "detail": "range(n) with a zero-based index is right, but the caller passes len-1.",
        }
    ],
    "notes": "Reviewed 1 file.",
}


def test_bot_authored_prs_are_never_reviewed() -> None:
    pr = make_pr(author=BOT)
    assert skip_reason(pr, BOT) is not None
    completer = FakeCompleter(ONE_FINDING)
    result = review(pr, completer, BOT)
    assert completer.calls == [], "must not call the model on its own PR"
    assert result.findings == []
    assert format_comment(result) is None


def test_bot_check_is_case_insensitive() -> None:
    assert skip_reason(make_pr(author="Agentic-Learning-Bot"), BOT) is not None


def test_opt_out_label_skips() -> None:
    pr = make_pr(labels=(OPT_OUT_LABEL,))
    completer = FakeCompleter(ONE_FINDING)
    assert review(pr, completer, BOT).skipped is not None
    assert completer.calls == []


def test_empty_diff_skips() -> None:
    assert skip_reason(make_pr(diff="   \n"), BOT) is not None


def test_review_parses_findings() -> None:
    result = review(make_pr(), FakeCompleter(ONE_FINDING), BOT)
    assert len(result.findings) == 1
    assert result.findings[0].confidence == "high"
    assert result.findings[0].line == 12
    assert result.notes == "Reviewed 1 file."


def test_null_line_is_allowed() -> None:
    payload = {
        "findings": [
            {
                "file": "x.py",
                "line": None,
                "confidence": "low",
                "summary": "s",
                "detail": "d",
            }
        ],
        "notes": "",
    }
    result = review(make_pr(), FakeCompleter(payload), BOT)
    assert result.findings[0].line is None


def test_comment_carries_marker_and_advisory_header() -> None:
    body = format_comment(review(make_pr(), FakeCompleter(ONE_FINDING), BOT))
    assert body is not None
    assert body.startswith(MARKER)
    assert ADVISORY_HEADER in body


@pytest.mark.parametrize("word", ["approve", "approving", "lgtm", "merge this"])
def test_comment_never_tells_humans_to_merge(word: str) -> None:
    body = format_comment(review(make_pr(), FakeCompleter(ONE_FINDING), BOT))
    assert body is not None
    assert word not in body.casefold()


def test_findings_sorted_by_confidence() -> None:
    payload = {
        "findings": [
            {"file": "a", "line": 1, "confidence": "low", "summary": "L", "detail": "d"},
            {"file": "b", "line": 2, "confidence": "high", "summary": "H", "detail": "d"},
            {"file": "c", "line": 3, "confidence": "medium", "summary": "M", "detail": "d"},
        ],
        "notes": "",
    }
    body = format_comment(review(make_pr(), FakeCompleter(payload), BOT))
    assert body is not None
    assert body.index("**H.**") < body.index("**M.**") < body.index("**L.**")


def test_no_findings_still_posts_a_clean_report() -> None:
    body = format_comment(review(make_pr(), FakeCompleter({"findings": [], "notes": ""}), BOT))
    assert body is not None
    assert "No defects found" in body


def test_large_diffs_are_truncated_and_flagged() -> None:
    pr = make_pr(diff="x" * (MAX_DIFF_CHARS + 5_000))
    prompt = build_prompt(pr)
    assert "truncated" in prompt
    assert len(prompt) < MAX_DIFF_CHARS + 2_000


def test_schema_forbids_extra_keys() -> None:
    completer = FakeCompleter(ONE_FINDING)
    review(make_pr(), completer, BOT)
    _, _, schema = completer.calls[0]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["findings"]["items"]["additionalProperties"] is False


def test_skipped_result_renders_nothing() -> None:
    assert format_comment(ReviewResult(findings=[], notes="", skipped="because")) is None


def test_explicit_request_overrides_bot_skip() -> None:
    """A maintainer-added re-request label reviews a bot PR; the automatic
    path still never does."""
    pr = make_pr(author=BOT)
    assert skip_reason(pr, BOT) is not None  # automatic: skipped
    assert skip_reason(pr, BOT, explicit_request=True) is None  # asked: reviewed


def test_opt_out_still_wins_over_explicit_request() -> None:
    """Contradictory labels resolve to silence, not to a review."""
    pr = make_pr(author=BOT, labels=("autoresearch:no-review",))
    reason = skip_reason(pr, BOT, explicit_request=True)
    assert reason is not None and "opted out" in reason
