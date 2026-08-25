"""The reviewer result-policy end to end (faked harness): an agent hands back
findings JSON, the kernel turns it into a ReviewResult and renders inline
comments — proving "hand back data; kernel posts inline" without a live session
or GitHub."""

from __future__ import annotations

import json
from pathlib import Path

from autoresearch.harness import SessionResult
from autoresearch.review import format_review
from autoresearch.role_runner import RoleResult, run_role
from autoresearch.roles import review_result_from_role, reviewer_spec

# a diff whose new side has an anchorable line in models/encoder.py
_DIFF = (
    "diff --git a/models/encoder.py b/models/encoder.py\n"
    "--- a/models/encoder.py\n"
    "+++ b/models/encoder.py\n"
    "@@ -1,2 +1,3 @@\n"
    " class TanhMLP:\n"
    "+    input_gain = 4.0\n"
    "     pass\n"
)

_AGENT_FINDINGS = {
    "findings": [
        {
            "file": "models/encoder.py",
            "line": 2,
            "confidence": "high",
            "summary": "input_gain is a per-layer LR in disguise",
            "detail": "This belongs in the initializer/LR seam, not forward().",
            "blocking": True,
            "kind": "change",
        }
    ],
    "notes": "one blocking finding",
}


class _Harness:
    """A judge session: commits a verdict through the syscall channel (as the
    tool would), or commits nothing when given None."""

    def __init__(self, verdict: dict | None) -> None:
        self._verdict = verdict

    def run(
        self, brief_text: str, workspace: Path, resume_session_id: str | None = None
    ) -> SessionResult:
        if self._verdict is not None:
            d = Path(workspace) / ".autoresearch"
            d.mkdir(exist_ok=True)
            (d / "syscall.json").write_text(json.dumps({"type": "verdict", **self._verdict}))
        return SessionResult(
            stop_reason="completed",
            is_error=False,
            cost_usd=0.0,
            num_turns=1,
            session_id="s",
            final_text="(verdict via tool)",
            transcript_path="",
        )


def test_agent_findings_flow_to_inline_review_payload(tmp_path: Path) -> None:
    role_result = run_role(reviewer_spec(), _Harness(_AGENT_FINDINGS), "brief", tmp_path)
    review = review_result_from_role(role_result)
    assert review is not None
    assert len(review.findings) == 1
    assert review.findings[0].blocking is True

    rendered = format_review(review, _DIFF)
    assert rendered is not None
    body, inline = rendered
    # the blocking finding anchors inline on the diff line the kernel posts
    assert any(c.get("path") == "models/encoder.py" for c in inline)
    assert "Verdict" in body


def test_failed_role_yields_no_review() -> None:
    failed = RoleResult(
        ok=False,
        session=SessionResult("error", True, 0.0, 0, "", "", "", error_detail="boom"),
        error="boom",
    )
    assert review_result_from_role(failed) is None


def test_no_verdict_yields_no_review(tmp_path: Path) -> None:
    # the judge never concluded -> run_role fails -> policy returns None
    # (post a skip stub)
    role_result = run_role(reviewer_spec(), _Harness(None), "brief", tmp_path)
    assert review_result_from_role(role_result) is None
