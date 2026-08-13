"""Agent-review orchestration (run_agent_review) with a fake harness and a fake
GitHub client — no real session, no network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from autoresearch.harness import SessionResult
from autoresearch.review import build_agent_brief
from autoresearch.review_agent import run_agent_review

_WORKSPACE = Path("/tmp/checkout")

_FINDINGS = json.dumps(
    {
        "findings": [
            {
                "file": "models/encoder.py",
                "line": 2,
                "confidence": "high",
                "summary": "smells like a per-layer LR",
                "detail": "belongs in the initializer seam",
                "blocking": True,
            }
        ],
        "notes": "one finding",
    }
)

_DIFF = (
    "diff --git a/models/encoder.py b/models/encoder.py\n"
    "--- a/models/encoder.py\n"
    "+++ b/models/encoder.py\n"
    "@@ -1,2 +1,3 @@\n"
    " class TanhMLP:\n"
    "+    input_gain = 4.0\n"
    "     pass\n"
)


class _Harness:
    def __init__(self, final_text: str, *, is_error: bool = False, detail: str = "") -> None:
        self._text, self._err, self._detail = final_text, is_error, detail
        self.briefs: list[str] = []

    def run(
        self, brief_text: str, workspace: Path, resume_session_id: str | None = None
    ) -> SessionResult:
        self.briefs.append(brief_text)
        return SessionResult(
            stop_reason="error" if self._err else "completed",
            is_error=self._err,
            cost_usd=0.0,
            num_turns=1,
            session_id="s",
            final_text=self._text,
            transcript_path="",
            error_detail=self._detail,
        )


class _Client:
    """Minimal GitHubClient stand-in recording posts."""

    _UNSET = object()

    def __init__(self, *, author: str = "alice", labels: Any = _UNSET) -> None:
        # labels passed as None models the API returning "labels": null
        self._author = author
        self._labels = [] if labels is _Client._UNSET else labels
        self.reviews: list[tuple[str, list[dict]]] = []
        self.comments: list[str] = []

    def get_pull_request(self, repo: str, number: int) -> dict:
        return {
            "title": "improve encoder",
            "body": "raise the probe",
            "user": {"login": self._author},
            "labels": self._labels,
            "head": {"sha": "deadbeef1234"},
        }

    def get_pull_request_diff(self, repo: str, number: int) -> str:
        return _DIFF

    def list_comments(self, repo: str, number: int) -> list[dict]:
        return []

    def list_pr_reviews(self, repo: str, number: int) -> list[dict]:
        return []

    def create_pr_review(self, repo: str, number: int, body: str, inline: list[dict]) -> None:
        self.reviews.append((body, inline))

    def comment(self, repo: str, number: int, body: str) -> None:
        self.comments.append(body)


def test_clean_review_posts_inline() -> None:
    client, harness = _Client(), _Harness(_FINDINGS)
    label = run_agent_review(
        client,  # type: ignore[arg-type]
        "org/repo",
        7,
        harness,
        _WORKSPACE,
        bot_login="autoresearch-bot",
    )
    assert label is not None
    assert len(client.reviews) == 1
    _body, inline = client.reviews[0]
    assert any(c.get("path") == "models/encoder.py" for c in inline)
    # the brief carried the read-only investigation instruction
    assert "checked out read-only" in harness.briefs[0]


def test_null_labels_do_not_crash() -> None:
    client, harness = _Client(labels=None), _Harness(_FINDINGS)
    label = run_agent_review(
        client,  # type: ignore[arg-type]
        "org/repo",
        7,
        harness,
        _WORKSPACE,
        bot_login="autoresearch-bot",
    )
    assert label is not None  # posts normally; null labels are not a crash


def test_bot_authored_pr_is_skipped() -> None:
    client, harness = _Client(author="autoresearch-bot"), _Harness(_FINDINGS)
    label = run_agent_review(
        client,  # type: ignore[arg-type]
        "org/repo",
        7,
        harness,
        _WORKSPACE,
        bot_login="autoresearch-bot",
    )
    assert label is None
    assert client.reviews == [] and client.comments == []
    assert harness.briefs == []  # skipped before the session ran


def test_outage_posts_skip_stub_no_review() -> None:
    client = _Client()
    harness = _Harness("", is_error=True, detail="rate_limit_error: slow down")
    label = run_agent_review(
        client,  # type: ignore[arg-type]
        "org/repo",
        7,
        harness,
        _WORKSPACE,
        bot_login="autoresearch-bot",
    )
    assert label is None
    assert client.reviews == []
    assert len(client.comments) == 1
    assert "could not run" in client.comments[0]


def test_malformed_output_posts_nothing() -> None:
    client = _Client()
    harness = _Harness("not json, no verdict")
    label = run_agent_review(
        client,  # type: ignore[arg-type]
        "org/repo",
        7,
        harness,
        _WORKSPACE,
        bot_login="autoresearch-bot",
    )
    assert label is None
    assert client.reviews == [] and client.comments == []


def test_build_agent_brief_reuses_rubric_and_diff() -> None:
    from autoresearch.review import PullRequest

    pr = PullRequest("org/repo", 1, "t", "b", _DIFF, "alice")
    brief = build_agent_brief(pr, today="2026-08-13")
    assert "reviewing a pull request" in brief.lower()
    assert "input_gain" in brief  # the diff is embedded
    assert "2026-08-13" in brief
