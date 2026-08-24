"""Role-runner: structured-output validation, the repair loop, and the role
specs. The harness is faked; no session is actually run."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from autoresearch.harness import SessionResult
from autoresearch.review import FINDINGS_SCHEMA
from autoresearch.role_runner import run_role
from autoresearch.roles import author_spec, reviewer_spec

_WORKSPACE = Path("/tmp/does-not-matter")
_FINDINGS = json.dumps({"findings": [], "notes": "looks fine"})


def _session(
    final_text: str = "", *, is_error: bool = False, error_detail: str = ""
) -> SessionResult:
    return SessionResult(
        stop_reason="error" if is_error else "completed",
        is_error=is_error,
        cost_usd=0.0,
        num_turns=1,
        session_id="sess-1",
        final_text=final_text,
        transcript_path="",
        error_detail=error_detail,
    )


@dataclass
class _SeqHarness:
    """Returns queued results in order; records each call's resume id."""

    results: list[SessionResult]
    calls: list[str | None] = field(default_factory=list)

    def run(
        self, brief_text: str, workspace: Path, resume_session_id: str | None = None
    ) -> SessionResult:
        self.calls.append(resume_session_id)
        return self.results.pop(0)


def test_reviewer_spec_is_read_only_and_uses_findings_schema() -> None:
    spec = reviewer_spec()
    assert spec.execution.can_execute is False
    assert spec.scope is None
    assert spec.output_schema is FINDINGS_SCHEMA


def test_author_spec_is_an_executing_editor_with_no_schema() -> None:
    spec = author_spec()
    assert spec.execution.can_execute is True
    assert {"Write", "Edit", "Bash"} <= set(spec.tools)
    assert spec.output_schema is None  # the artifact is the diff + report
    assert spec.scope == ()  # filled from the contract by the kernel
    assert author_spec(scope=("src/x/",)).scope == ("src/x/",)


def test_happy_path_returns_validated_data() -> None:
    harness = _SeqHarness([_session(_FINDINGS)])
    result = run_role(reviewer_spec(), harness, "brief", _WORKSPACE)
    assert result.ok is True
    assert result.data == {"findings": [], "notes": "looks fine"}


def test_tolerates_code_fenced_json() -> None:
    fenced = f"Here are my findings:\n```json\n{_FINDINGS}\n```\n"
    result = run_role(reviewer_spec(), _SeqHarness([_session(fenced)]), "brief", _WORKSPACE)
    assert result.ok is True
    assert result.data is not None


def test_repairs_once_then_succeeds() -> None:
    harness = _SeqHarness([_session("not json at all"), _session(_FINDINGS)])
    result = run_role(reviewer_spec(), harness, "brief", _WORKSPACE)
    assert result.ok is True
    # the repair call resumed the first session
    assert harness.calls == [None, "sess-1"]


def test_exhausts_repairs_and_fails() -> None:
    harness = _SeqHarness([_session("nope"), _session("still nope")])
    result = run_role(reviewer_spec(), harness, "brief", _WORKSPACE, max_repairs=1)
    assert result.ok is False
    assert "invalid structured output" in result.error


def test_missing_required_key_is_invalid() -> None:
    partial = json.dumps({"findings": []})  # no "notes"
    result = run_role(
        reviewer_spec(), _SeqHarness([_session(partial)]), "brief", _WORKSPACE, max_repairs=0
    )
    assert result.ok is False
    assert "notes" in result.error


def test_session_error_propagates() -> None:
    harness = _SeqHarness([_session(is_error=True, error_detail="boom")])
    result = run_role(reviewer_spec(), harness, "brief", _WORKSPACE)
    assert result.ok is False
    assert result.error == "boom"


def test_editing_role_needs_no_schema() -> None:
    result = run_role(author_spec(), _SeqHarness([_session("done")]), "brief", _WORKSPACE)
    assert result.ok is True
    assert result.data is None


def test_followup_spec_carries_the_resuming_roles_key() -> None:
    from autoresearch.roles import followup_spec

    spec = followup_spec()
    assert spec.name == "followup" and spec.key == "author"
    assert spec.execution.can_execute is True
    assert {"Write", "Edit", "Bash"} <= set(spec.tools)
    assert spec.output_schema is None  # the reply is prose; changes are re-measured
    assert followup_spec(resuming="steward").key == "steward"


def test_steward_spec_is_an_executing_editor_in_its_own_territory() -> None:
    from autoresearch.roles import steward_spec

    spec = steward_spec()
    assert spec.name == "steward" and spec.key == "steward"
    assert spec.execution.can_execute is True
    assert {"Write", "Edit", "Bash"} <= set(spec.tools)
    assert spec.output_schema is None  # the artifact is the env-work diff + report
    assert spec.scope == ()  # filled from contract.steward.allowed by the kernel


# --- verdict-tool mode (docs/design/role-cli.md Phase 2) ---


@dataclass
class _VerdictHarness:
    """A harness that hosts the verdict tool: on run it writes a committed
    verdict (as the judge would via the tool), and declares the capability."""

    verdict: dict | None
    supports_verdict_tool: bool = True
    installed: bool = False

    def run(self, brief_text, workspace, resume_session_id=None) -> SessionResult:
        # the tool was installed before run (we assert the dir exists); simulate
        # the judge committing a verdict syscall (type-tagged, as the tool does)
        self.installed = (Path(workspace) / ".autoresearch" / "syscall").exists()
        if self.verdict is not None:
            d = Path(workspace) / ".autoresearch"
            d.mkdir(exist_ok=True)
            (d / "syscall.json").write_text(json.dumps({"type": "verdict", **self.verdict}))
        return _session("(verdict via tool)")


def _verdict_spec():
    from dataclasses import replace

    return replace(reviewer_spec(), verdict_tool=True)


def test_verdict_tool_mode_reads_the_committed_verdict(tmp_path: Path) -> None:
    v = {
        "findings": [
            {
                "file": "x.py",
                "line": 3,
                "confidence": "high",
                "summary": "s",
                "detail": "d",
                "blocking": True,
                "kind": "change",
            }
        ],
        "notes": "one defect",
    }
    harness = _VerdictHarness(verdict=v)
    result = run_role(_verdict_spec(), harness, "review it", tmp_path)
    assert harness.installed  # tool installed BEFORE the session
    assert result.ok and result.data is not None
    assert result.data["findings"][0]["blocking"] is True
    assert result.data["notes"] == "one defect"


def test_verdict_tool_mode_no_verdict_is_a_failure(tmp_path: Path) -> None:
    # the judge never concluded -> no clean read (silence is never endorsement)
    result = run_role(_verdict_spec(), _VerdictHarness(verdict=None), "x", tmp_path)
    assert not result.ok and "no verdict" in result.error


def test_verdict_tool_mode_malformed_verdict_is_a_failure(tmp_path: Path) -> None:
    bad = {
        "findings": [
            {
                "file": "x",
                "line": 1,
                "confidence": "SURE",
                "summary": "s",
                "detail": "d",
                "blocking": True,
                "kind": "note",
            }
        ],
        "notes": "",
    }
    result = run_role(_verdict_spec(), _VerdictHarness(verdict=bad), "x", tmp_path)
    assert not result.ok and "invalid verdict" in result.error


def test_verdict_spec_falls_back_to_parsing_when_backend_lacks_support(tmp_path: Path) -> None:
    # a verdict_tool spec on a backend WITHOUT the capability parses the final
    # message (the message-based fallback) — no tool installed.
    harness = _SeqHarness(results=[_session(_FINDINGS)])  # supports_verdict_tool absent -> False
    result = run_role(_verdict_spec(), harness, "x", tmp_path)
    assert result.ok and result.data == {"findings": [], "notes": "looks fine"}
    assert not (tmp_path / ".autoresearch").exists()  # tool NOT installed
