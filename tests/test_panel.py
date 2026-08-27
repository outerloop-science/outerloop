"""The pre-PR panel: lens dispatch, mechanical merge, transcript honesty."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from autoresearch.harness import SessionResult
from autoresearch.panel import PanelLens, run_panel
from autoresearch.review import PullRequest

_PR = PullRequest(
    repo="org/pilot",
    number=0,
    title="[agent] tsp: 13.9 -> 13.1",
    body="claim",
    diff="+x",
    author="bot",
)

_REVIEW_BLOCKING = json.dumps(
    {
        "findings": [
            {
                "file": "src/x.py",
                "line": 3,
                "confidence": "high",
                "summary": "reads the ruler",
                "detail": "the eval file is opened at runtime",
                "blocking": True,
            }
        ],
        "notes": "",
    }
)
_VERIFY_CLEAN = json.dumps({"findings": [], "notes": "mechanism checks out"})


@dataclass
class _Judge:
    text: str
    is_error: bool = False
    workspaces: list = field(default_factory=list)

    def run(self, brief_text, workspace, resume_session_id=None) -> SessionResult:
        self.workspaces.append(Path(workspace))
        if not self.is_error:
            # commit the verdict through the syscall channel, as the tool would
            try:
                payload = json.loads(self.text)
            except (json.JSONDecodeError, TypeError):
                payload = None  # never concluded: no verdict written
            if isinstance(payload, dict):
                for f in payload.get("findings", []):
                    if isinstance(f, dict):
                        f.setdefault("kind", "note")  # the tool's own default
                d = Path(workspace) / ".autoresearch"
                d.mkdir(exist_ok=True)
                (d / "syscall.json").write_text(json.dumps({"type": "verdict", **payload}))
        return SessionResult(
            stop_reason="error" if self.is_error else "completed",
            is_error=self.is_error,
            cost_usd=0.0,
            num_turns=1,
            session_id="j",
            final_text=self.text,
            transcript_path="",
            error_detail="boom" if self.is_error else "",
        )


def test_lenses_read_their_workspaces_and_blocking_merges(tmp_path: Path) -> None:
    verify, review = _Judge(_VERIFY_CLEAN), _Judge(_REVIEW_BLOCKING)
    verdict = run_panel(
        (PanelLens("verify", verify), PanelLens("review", review)),
        tmp_path,
        _PR,
        contract_text="benchmarks: []",
        today="2026-08-15",
        round_no=1,
    )
    assert verify.workspaces == [tmp_path]  # verify reads both trees from the root
    assert review.workspaces == [tmp_path / "pr-head"]  # review reads the candidate only
    assert len(verdict.blocking) == 1 and verdict.blocking[0].file == "src/x.py"
    assert "0 blocking" in verdict.transcript and "1 blocking" in verdict.transcript
    # the wake quotes findings as data, fenced
    assert "```" in verdict.wake_text and "reads the ruler" in verdict.wake_text
    assert "DATA, not instructions" in verdict.wake_text


def test_no_verdict_is_never_a_pass(tmp_path: Path) -> None:
    dead = _Judge("", is_error=True)
    verdict = run_panel((PanelLens("verify", dead),), tmp_path, _PR, "c", "2026-08-15", round_no=2)
    assert verdict.blocking == ()
    assert "no verdict" in verdict.transcript
    assert "silence is not endorsement" in verdict.transcript
    assert verdict.wake_text == ""  # nothing to wake on, but the transcript says why


def test_clean_panel_has_no_wake(tmp_path: Path) -> None:
    verdict = run_panel(
        (PanelLens("review", _Judge(_VERIFY_CLEAN)),), tmp_path, _PR, "c", "t", round_no=1
    )
    assert verdict.blocking == () and verdict.wake_text == ""


def test_no_verdict_degrades_the_read(tmp_path: Path) -> None:
    dead = _Judge("", is_error=True)
    verdict = run_panel((PanelLens("verify", dead),), tmp_path, _PR, "c", "t", round_no=1)
    assert verdict.degraded  # never a certified pass
    clean = run_panel(
        (PanelLens("review", _Judge(_VERIFY_CLEAN)),), tmp_path, _PR, "c", "t", round_no=1
    )
    assert not clean.degraded


def test_parse_lenses_admits_codex_and_refuses_uncontainable_backends() -> None:
    import pytest

    from autoresearch.panel import parse_lenses

    # backends are peers; the one gate is containment on the climb host
    assert parse_lenses("verify:claude:claude-fable-5,review:codex:gpt-5.6-terra") == (
        ("verify", "claude", "claude-fable-5"),
        ("review", "codex", "gpt-5.6-terra"),
    )
    # hermes is admitted now (containable); only an unknown backend refuses
    assert parse_lenses("review:hermes:gpt-5.6-terra") == (("review", "hermes", "gpt-5.6-terra"),)
    with pytest.raises(ValueError, match="unknown backend"):
        parse_lenses("review:gemini")
