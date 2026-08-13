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


def test_result_from_data_parses_and_defaults_kind() -> None:
    from autoresearch.review import result_from_data

    def item(**extra: Any) -> dict[str, Any]:
        return {
            "file": "x.py",
            "line": 1,
            "confidence": "low",
            "summary": "s",
            "detail": "d",
            **extra,
        }

    data: dict[str, Any] = {
        "findings": [
            item(kind="change"),
            item(kind="bogus"),  # invalid -> note
            item(),  # missing -> note
        ],
        "notes": "",
    }
    kinds = [f.kind for f in result_from_data(data).findings]
    assert kinds == ["change", "note", "note"]


def test_result_from_data_drops_malformed_items_without_crashing() -> None:
    from autoresearch.review import result_from_data

    good = {
        "file": "x.py",
        "line": 1,
        "confidence": "high",
        "summary": "ok",
        "detail": "d",
        "blocking": False,
        "kind": "note",
    }
    data: dict[str, Any] = {
        "findings": [
            good,
            {"file": "y.py"},  # missing summary/detail -> dropped
            "not a dict",  # non-dict -> dropped
            {"summary": "no file", "detail": "d"},  # missing file -> dropped
        ],
        "notes": None,  # non-string notes must not crash
    }
    result = result_from_data(data)
    assert [f.file for f in result.findings] == ["x.py"]
    assert result.notes == ""

    # findings null or a non-list must not raise, either
    assert result_from_data({"findings": None, "notes": ""}).findings == []
    assert result_from_data({"findings": 7, "notes": ""}).findings == []
    assert result_from_data({}).findings == []


def test_boolean_line_is_not_treated_as_a_line_number() -> None:
    from autoresearch.review import result_from_data

    data: dict[str, Any] = {
        "findings": [
            {"file": "x.py", "line": True, "confidence": "low", "summary": "s", "detail": "d"}
        ],
        "notes": "",
    }
    # bool is an int subclass; `line: true` must not become line 1
    assert result_from_data(data).findings[0].line is None


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
            {
                "file": "a",
                "line": 1,
                "confidence": "low",
                "summary": "L",
                "detail": "d",
                "blocking": True,
            },
            {
                "file": "b",
                "line": 2,
                "confidence": "high",
                "summary": "H",
                "detail": "d",
                "blocking": True,
            },
            {
                "file": "c",
                "line": 3,
                "confidence": "medium",
                "summary": "M",
                "detail": "d",
                "blocking": True,
            },
        ],
        "notes": "",
    }
    body = format_comment(review(make_pr(), FakeCompleter(payload), BOT))
    assert body is not None
    assert body.index("**H.**") < body.index("**M.**") < body.index("**L.**")


def test_verdict_leads_and_blocking_split_from_advisory() -> None:
    from autoresearch.review import Finding, ReviewResult, format_comment

    r = ReviewResult(
        findings=[
            Finding(
                file="a.py",
                line=3,
                confidence="high",
                summary="Real bug",
                detail="Skips a row.",
                blocking=True,
            ),
            Finding(
                file="b.py",
                line=None,
                confidence="low",
                summary="Stale comment",
                detail="Says v1.",
                blocking=False,
            ),
        ],
        notes="",
    )
    body = format_comment(r)
    assert body is not None
    # verdict leads
    assert "**Verdict: 1 blocking, 1 advisory.**" in body
    # blocking shown in full, advisory as a compact bullet under its own heading
    assert "**Real bug.** Skips a row." in body
    assert "**Advisory (non-blocking):**" in body
    assert "- Stale comment (`b.py`; low)" in body


def test_verdict_says_mergeable_when_nothing_blocks() -> None:
    from autoresearch.review import Finding, ReviewResult, format_comment

    r = ReviewResult(
        findings=[
            Finding(
                file="a.py", line=1, confidence="low", summary="Nit", detail="x.", blocking=False
            )
        ],
        notes="",
    )
    body = format_comment(r)
    assert body is not None
    assert "nothing blocking" in body and "1 advisory note" in body


def test_no_findings_still_posts_a_clean_report() -> None:
    body = format_comment(review(make_pr(), FakeCompleter({"findings": [], "notes": ""}), BOT))
    assert body is not None
    assert "no defects found" in body.lower()


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


def test_advisory_header_semantics_are_pinned() -> None:
    """Same pin as the verifier's: the constant itself must keep the
    advisory/code-owner-decides framing through any future rewording."""
    assert "Advisory" in ADVISORY_HEADER
    assert "code owner decides" in ADVISORY_HEADER
    assert OPT_OUT_LABEL in ADVISORY_HEADER  # posted instructions track the constant


def test_backticked_filename_cannot_break_the_reference_span() -> None:
    from autoresearch.review import Finding, ReviewResult
    from autoresearch.review import format_comment as _fc

    body = _fc(
        ReviewResult(
            findings=[
                Finding(
                    file="a`](http://evil)`.py",
                    line=1,
                    confidence="low",
                    summary="s",
                    detail="d",
                )
            ],
            notes="",
        )
    )
    assert body is not None
    # the injected backtick is gone, so the span cannot be closed early;
    # whatever remains of the path renders as inert code-span text
    assert "a](http://evil).py" in body  # inside the span, backtick-free
    assert "`a](http://evil).py`" in body


DIFF = """\
--- a/src/x.py
+++ b/src/x.py
@@ -10,3 +10,4 @@
 context_line_ten
-removed_eleven
+added_eleven
+added_twelve
 context_thirteen
"""


def test_commentable_lines_maps_the_new_side() -> None:
    from autoresearch.review import commentable_lines

    lines = commentable_lines(DIFF)
    # new side: 10 (context), 11-12 (added), 13 (context); nothing else
    assert lines == {"src/x.py": {10, 11, 12, 13}}


def test_format_review_splits_anchored_from_body() -> None:
    from autoresearch.review import Finding, ReviewResult, format_review

    anchored = Finding(
        file="src/x.py",
        line=11,
        confidence="high",
        summary="Bad add",
        detail="It breaks.",
        blocking=True,
    )
    off_diff = Finding(
        file="src/other.py", line=5, confidence="low", summary="Stale doc", detail="Old text."
    )
    no_line = Finding(
        file="src/x.py", line=None, confidence="medium", summary="Global", detail="Everywhere."
    )
    rendered = format_review(ReviewResult([anchored, off_diff, no_line], notes=""), DIFF)
    assert rendered is not None
    body, inline = rendered
    # only the blocking finding anchors inline
    (item,) = inline
    assert item["path"] == "src/x.py" and item["line"] == 11 and item["side"] == "RIGHT"
    assert "Bad add" in item["body"] and "high confidence" in item["body"]
    # advisory findings stay in the body as a compact list, references intact
    assert "Stale doc" in body and "`src/other.py`:5" in body
    assert "Global" in body and "Bad add" not in body
    assert "1 finding attached to the lines below." in body
    assert "Verdict:" in body and body.lstrip().startswith(MARKER)


def test_nonblocking_suggestion_anchors_inline_with_label() -> None:
    from autoresearch.review import Finding, ReviewResult, format_review

    sugg = Finding(
        file="src/x.py",
        line=11,
        confidence="medium",
        summary="Rename for clarity",
        detail="`tmp` hides intent.",
        blocking=False,
        kind="suggestion",
    )
    _body, inline = format_review(ReviewResult([sugg], notes=""), DIFF)  # type: ignore[misc]
    (item,) = inline
    assert item["line"] == 11
    assert item["body"].startswith("**Suggestion.**")


def test_inline_lead_distinguishes_blocking_from_change() -> None:
    from autoresearch.review import Finding, ReviewResult, format_review

    blocking = Finding(
        file="src/x.py",
        line=11,
        confidence="high",
        summary="Bug",
        detail="Crashes.",
        blocking=True,
        kind="change",
    )
    change = Finding(
        file="src/x.py",
        line=12,
        confidence="medium",
        summary="Tidy",
        detail="Clearer name.",
        blocking=False,
        kind="change",
    )
    _body, inline = format_review(ReviewResult([blocking, change], notes=""), DIFF)  # type: ignore[misc]
    by_line = {c["line"]: c["body"] for c in inline}
    assert by_line[11].startswith("**Blocking.**")
    assert not by_line[12].startswith("**Blocking.**")


def test_local_question_and_note_stay_in_body() -> None:
    from autoresearch.review import Finding, ReviewResult, format_review

    question = Finding(
        file="src/x.py",
        line=11,
        confidence="low",
        summary="Why drop the guard",
        detail="The old code checked n.",
        blocking=False,
        kind="question",
    )
    note = Finding(
        file="src/x.py",
        line=12,
        confidence="low",
        summary="Uses tabs here",
        detail="Rest of file is spaces.",
        blocking=False,
        kind="note",
    )
    body, inline = format_review(ReviewResult([question, note], notes=""), DIFF)  # type: ignore[misc]
    assert inline == []  # neither floods the diff
    assert "Question: Why drop the guard" in body
    assert "Uses tabs here" in body


def test_commentable_lines_survives_header_lookalike_content() -> None:
    """An added line whose CONTENT starts with '++ b/' arrives as
    '+++ b/...' and must not rebind the file mid-hunk (review finding:
    file content is contributor-controlled)."""
    from autoresearch.review import commentable_lines

    tricky = (
        "diff --git a/real.py b/real.py\n"
        "--- a/real.py\n"
        "+++ b/real.py\n"
        "@@ -1,1 +1,3 @@\n"
        " kept\n"
        "+++ b/fake.py\n"  # added content: '++ b/fake.py'
        "+normal\n"
    )
    lines = commentable_lines(tricky)
    assert lines == {"real.py": {1, 2, 3}}
    assert "fake.py" not in lines


def test_format_review_all_anchored_says_so() -> None:
    from autoresearch.review import Finding, ReviewResult, format_review

    f = Finding(
        file="src/x.py",
        line=12,
        confidence="high",
        summary="Real",
        detail="Breaks.",
        blocking=True,
    )
    rendered = format_review(ReviewResult([f], notes=""), DIFF)
    assert rendered is not None
    body, inline = rendered
    assert len(inline) == 1
    assert "1 finding attached to the lines below." in body


def test_commentable_lines_stops_at_file_boundaries() -> None:
    """Inter-file headers must not inflate the previous file's anchors
    (review finding: they read as context lines past the last hunk)."""
    from autoresearch.review import commentable_lines

    two_files = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1,1 +1,2 @@\n"
        " kept\n"
        "+added\n"
        "diff --git a/b.py b/b.py\n"
        "index 123..456 100644\n"
        "--- a/b.py\n"
        "+++ b/b.py\n"
        "@@ -5,1 +5,1 @@\n"
        "+only\n"
    )
    lines = commentable_lines(two_files)
    assert lines == {"a.py": {1, 2}, "b.py": {5}}  # headers counted nowhere
