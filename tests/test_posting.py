"""Shared posting helpers (`posting.py`): round numbering and the skip stub.
The fake client is deliberately local (no model/llm dependency), matching what
the helpers actually call.
"""

from __future__ import annotations

from typing import Any

from autoresearch import posting
from autoresearch.github import GitHubError

_MARKER = "<!-- test-marker -->"


class _FakeClient:
    """Records posts and serves them back split by kind — the minimal surface
    the posting helpers touch (list_comments, list_pr_reviews, comment)."""

    def __init__(self) -> None:
        self.posted: list[dict] = []

    def list_comments(self, repo: str, number: int, max_pages: int = 20) -> list[dict]:
        return [c for c in self.posted if c.get("kind") != "review"]

    def list_pr_reviews(self, repo: str, number: int, max_pages: int = 10) -> list[dict]:
        return [c for c in self.posted if c.get("kind") == "review"]

    def comment(self, repo: str, number: int, body: str) -> None:
        self.posted.append({"body": body, "kind": "comment"})

    def create_pr_review(self, repo: str, number: int, body: str, comments: Any = None) -> None:
        self.posted.append({"body": body, "kind": "review", "inline": comments or []})


def test_round_numbering_spans_comments_and_reviews() -> None:
    """Prior rounds are counted across BOTH issue comments and review bodies, so
    switching a role between posting styles never resets its numbering."""
    client = _FakeClient()
    client.posted.append({"body": f"{_MARKER}\nold round", "kind": "comment"})
    client.posted.append({"body": f"{_MARKER}\nolder", "kind": "review", "inline": []})
    _stamp, label = posting._round_stamp(
        client,  # type: ignore[arg-type]
        "org/repo",
        1,
        _MARKER,
        {"head": {"sha": "abc"}},
    )
    assert label == "**Round 3**"


def test_round_stamp_falls_back_when_count_fails(caplog: Any) -> None:
    """A GitHub failure counting prior rounds must not cost the round itself —
    it falls back to a labeled 'New round', never raises."""

    class _Blind(_FakeClient):
        def list_comments(self, repo: str, number: int, max_pages: int = 20) -> list[dict]:
            raise GitHubError(500, "/repos/org/repo", "server error")

    _stamp, label = posting._round_stamp(
        _Blind(),  # type: ignore[arg-type]
        "org/repo",
        1,
        _MARKER,
        {"head": {"sha": "abc"}},
    )
    assert "New round" in label
    assert "could not count prior rounds" in caplog.text


def test_skip_stub_posting_failure_is_swallowed(caplog: Any) -> None:
    """post_skip_stub must never raise — a failed stub post is logged, not fatal
    (an advisory role never reds the target's CI)."""

    class _Refusing(_FakeClient):
        def comment(self, repo: str, number: int, body: str) -> None:
            raise GitHubError(403, "/repos/org/repo", "forbidden")

    posting.post_skip_stub(
        _Refusing(),  # type: ignore[arg-type]
        "org/repo",
        1,
        "advisory review",
        ValueError("x"),
    )
    assert "could not post the skip stub" in caplog.text


def test_skip_stub_carries_its_own_marker_and_reason() -> None:
    client = _FakeClient()
    posting.post_skip_stub(
        client,  # type: ignore[arg-type]
        "org/repo",
        1,
        "advisory review",
        ValueError("credit balance"),
    )
    (stub,) = [c["body"] for c in client.posted]
    assert stub.lstrip().startswith(posting.SKIP_MARKER)
    assert "could not run" in stub and "credit balance" in stub


def test_post_round_posts_one_numbered_stamped_comment() -> None:
    client = _FakeClient()
    label = posting.post_round(
        client,  # type: ignore[arg-type]
        "org/repo",
        1,
        _MARKER,
        f"{_MARKER}\nfindings",
        {"head": {"sha": "abcd1234ef"}},
    )
    assert label == "**Round 1**"
    (posted,) = client.posted
    assert posted["body"].lstrip().startswith(_MARKER)
    assert "reviewed head `abcd1234`" in posted["body"]  # 8-char stamp


def test_post_round_review_posts_inline_on_success() -> None:
    client = _FakeClient()
    label = posting.post_round_review(
        client,  # type: ignore[arg-type]
        "org/repo",
        1,
        _MARKER,
        f"{_MARKER}\nsummary",
        [{"path": "x.py", "line": 1, "body": "note"}],
        {"head": {"sha": "abcd"}},
        fallback_body=f"{_MARKER}\nfull findings",
    )
    assert label == "**Round 1**"
    (posted,) = client.posted
    assert posted["kind"] == "review" and posted["inline"]


def test_post_round_review_falls_back_to_a_comment_carrying_full_findings() -> None:
    class _NoInline(_FakeClient):
        def create_pr_review(self, repo: str, number: int, body: str, comments: Any = None) -> None:
            raise GitHubError(422, "/repos/org/repo", "unprocessable")

    client = _NoInline()
    label = posting.post_round_review(
        client,  # type: ignore[arg-type]
        "org/repo",
        1,
        _MARKER,
        f"{_MARKER}\nsummary only",
        [{"path": "x.py", "line": 1}],
        {"head": {"sha": "abcd"}},
        fallback_body=f"{_MARKER}\nFULL findings in the fallback",
    )
    assert label == "**Round 1**"
    (posted,) = client.posted  # the failed inline review fell back to an issue comment
    assert posted["kind"] == "comment" and "FULL findings in the fallback" in posted["body"]


def test_skip_stub_redacts_the_provided_key_any_provider() -> None:
    # backend-agnostic: whatever key the caller passes (the harness owns its
    # own key) is scrubbed before the error text — which can echo request
    # material — is posted.
    client = _FakeClient()
    posting.post_skip_stub(
        client,  # type: ignore[arg-type]
        "org/repo",
        1,
        "advisory review",
        ValueError("401 Unauthorized: key sk-or-LEAK9 rejected"),
        secrets=("sk-or-LEAK9",),
    )
    (stub,) = [c["body"] for c in client.posted]
    assert "sk-or-LEAK9" not in stub
    assert "[redacted]" in stub


def test_rounds_count_per_reviewer() -> None:
    # two standing opinions must not inflate each other's round numbers
    from autoresearch.posting import _round_stamp

    class _C:
        def list_comments(self, repo, number):
            stamp = "**Round 1** — reviewed head `aaaa1111` — reviewer `hermes/terra`."
            return [{"body": f"<!-- m -->\n{stamp}\nx"}]

        def list_pr_reviews(self, repo, number):
            return []

    pr_data = {"head": {"sha": "aaaa1111bbbb"}}
    # claude's FIRST round stays Round 1 despite terra's prior round
    stamp, label = _round_stamp(_C(), "o/r", 1, "<!-- m -->", pr_data, "claude/claude-opus-5")  # type: ignore[arg-type]
    assert label == "**Round 1**"
    assert "(re-run" not in stamp
    # terra's next round increments ITS OWN count and sees its own same-head re-run
    stamp, label = _round_stamp(_C(), "o/r", 1, "<!-- m -->", pr_data, "hermes/terra")  # type: ignore[arg-type]
    assert "**Round 2**" in label and "(re-run on the same head)" in label


def test_round_stamp_fits_a_panel_attribution_without_mid_word_cut() -> None:
    # the summarizer's reviewed_by names every lens: it must render whole,
    # not truncate mid-word (regression: a 60-char cap cut '...+de')
    who = "summarizer:hermes/gpt-5.6-terra over general+credentials+deployment+lifecycle+coverage"
    stamp, _ = posting._round_stamp(
        _FakeClient(),  # type: ignore[arg-type]
        "org/repo",
        1,
        _MARKER,
        {"head": {"sha": "abc"}},
        who,
    )
    assert "deployment" in stamp and "coverage" in stamp and "…" not in stamp


def test_round_stamp_ellipsizes_a_hostile_overlong_attribution() -> None:
    stamp, _ = posting._round_stamp(
        _FakeClient(),  # type: ignore[arg-type]
        "org/repo",
        1,
        _MARKER,
        {"head": {}},
        "x" * 400,
    )
    assert "…" in stamp  # bounded, and legibly so
