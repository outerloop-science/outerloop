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
