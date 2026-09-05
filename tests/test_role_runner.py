"""Role-runner: the unified harness builder, the verdict-tool read, and the
role specs. The harness is faked; no session is actually run."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from outerloop.harness import SessionResult
from outerloop.review import FINDINGS_SCHEMA
from outerloop.role_runner import run_role
from outerloop.roles import author_spec, reviewer_spec

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


def test_reviewer_spec_is_an_executing_judge_with_findings_schema() -> None:
    # a judge runs like every other role (shell for the syscall tool; the
    # deployment's boundary contains it) and edits nothing
    spec = reviewer_spec()
    assert spec.execution.can_execute is True
    assert "Bash" in spec.tools
    assert spec.scope is None
    assert spec.output_schema is FINDINGS_SCHEMA


def test_author_spec_is_an_executing_editor_with_no_schema() -> None:
    spec = author_spec()
    assert spec.execution.can_execute is True
    assert {"Write", "Edit", "Bash"} <= set(spec.tools)
    assert spec.output_schema is None  # the artifact is the diff + report
    assert spec.scope == ()  # filled from the contract by the kernel
    assert author_spec(scope=("src/x/",)).scope == ("src/x/",)


def test_session_error_propagates(tmp_path: Path) -> None:
    harness = _SeqHarness([_session(is_error=True, error_detail="boom")])
    result = run_role(reviewer_spec(), harness, "brief", tmp_path)
    assert result.ok is False
    assert result.error == "boom"


def test_editing_role_needs_no_schema() -> None:
    result = run_role(author_spec(), _SeqHarness([_session("done")]), "brief", _WORKSPACE)
    assert result.ok is True
    assert result.data is None


def test_followup_spec_carries_the_resuming_roles_key() -> None:
    from outerloop.roles import followup_spec

    spec = followup_spec()
    assert spec.name == "followup" and spec.key == "author"
    assert spec.execution.can_execute is True
    assert {"Write", "Edit", "Bash"} <= set(spec.tools)
    assert spec.output_schema is None  # the reply is prose; changes are re-measured
    assert followup_spec(resuming="steward").key == "steward"


def test_steward_spec_is_an_executing_editor_in_its_own_territory() -> None:
    from outerloop.roles import steward_spec

    spec = steward_spec()
    assert spec.name == "steward" and spec.key == "steward"
    assert spec.execution.can_execute is True
    assert {"Write", "Edit", "Bash"} <= set(spec.tools)
    assert spec.output_schema is None  # the artifact is the env-work diff + report
    assert spec.scope == ()  # filled from contract.steward.allowed by the kernel


# --- verdict-tool mode (docs/design/role-cli.md Phase 2) ---


@dataclass
class _VerdictHarness:
    """A harness whose session runs the verdict tool: on run it writes a
    committed verdict (as the judge would via `conclude` on a shell in the
    jail)."""

    verdict: dict | None
    installed: bool = False

    def run(self, brief_text, workspace, resume_session_id=None) -> SessionResult:
        # the tool was installed before run (we assert the dir exists); simulate
        # the judge committing a verdict syscall (type-tagged, as the tool does)
        self.installed = (Path(workspace) / ".outerloop" / "syscall").exists()
        if self.verdict is not None:
            d = Path(workspace) / ".outerloop"
            d.mkdir(exist_ok=True)
            (d / "syscall.json").write_text(json.dumps({"type": "verdict", **self.verdict}))
        return _session("(verdict via tool)")


def _verdict_spec():
    return reviewer_spec()


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


# --- build_harness: the ONE construction for every role and backend ---


def test_build_harness_claude_judge_gets_tools_and_bare() -> None:
    from outerloop.harness import ClaudeCodeHarness
    from outerloop.role_runner import build_harness

    h = build_harness("k", reviewer_spec())
    assert isinstance(h, ClaudeCodeHarness)
    # native tools from the spec: the read set plus the shell that runs the
    # syscall tool (MCP names like pr-context-read are wired separately)
    assert set(h.allowed_tools) == {"Read", "Grep", "Glob", "Bash", "WebSearch", "WebFetch"}
    assert h.bare is True  # untrusted checkout: never load its instructions
    assert h.max_turns == 40 and h.timeout_s == 1800  # budget from the spec


def test_build_harness_codex_is_uniform_for_all_roles() -> None:
    # one execution surface: codex's own sandbox stays off; the deployment's
    # container or ephemeral runner is the boundary, for judges and editors alike
    from outerloop.harness import CodexHarness
    from outerloop.role_runner import build_harness

    judge = build_harness("k", reviewer_spec(), backend="codex")
    editor = build_harness("k", author_spec(), backend="codex", container_image="img.sif")
    assert isinstance(judge, CodexHarness) and isinstance(editor, CodexHarness)
    assert judge.sandbox == editor.sandbox == "danger-full-access"
    assert judge.container_image == "" and editor.container_image == "img.sif"
    # the web: the exec-compatible config override, never `--search` (which
    # `codex exec` rejects — it killed every fleet attempt on 2026-08-29)
    for h in (judge, editor):
        assert "tools.web_search=true" in h.extra_args and "--search" not in h.extra_args


def test_build_harness_hermes_toolsets_follow_the_spec(tmp_path: Path) -> None:
    from outerloop.harness import HermesHarness
    from outerloop.role_runner import build_harness

    h = build_harness(
        "k", reviewer_spec(), backend="hermes", hermes_repo=tmp_path, hermes_provider="openai"
    )
    assert isinstance(h, HermesHarness)
    assert h.repo_dir == tmp_path
    assert h.provider == "openai-api" and h.key_env == "OPENAI_API_KEY"
    # an executing role has the shell; everything else stays off for parity
    assert set(h.enabled_toolsets) == {"file", "terminal", "web", "search"}
    assert "terminal" not in h.disabled_toolsets and "web" not in h.disabled_toolsets
    assert "browser" in h.disabled_toolsets


def test_build_harness_hermes_terminal_keys_on_the_bash_tool(tmp_path: Path) -> None:
    # the shell is granted from the SAME signal claude uses (the Bash tool),
    # not can_execute — so a role without Bash gets no terminal even if it
    # executes (terra #140 r5).
    from dataclasses import replace

    from outerloop.harness import HermesHarness
    from outerloop.role_runner import build_harness

    spec = replace(reviewer_spec(), tools=("Read", "Grep", "Glob"))  # no Bash
    h = build_harness("k", spec, backend="hermes", hermes_repo=tmp_path)
    assert isinstance(h, HermesHarness)
    assert h.enabled_toolsets == ("file",)  # no terminal without Bash
    assert "terminal" in h.disabled_toolsets


def test_build_harness_hermes_requires_repo_and_known_provider(tmp_path: Path) -> None:
    import pytest

    from outerloop.role_runner import build_harness

    with pytest.raises(ValueError, match="hermes_repo"):
        build_harness("k", reviewer_spec(), backend="hermes")
    with pytest.raises(ValueError, match="unknown hermes provider"):
        build_harness(
            "k", reviewer_spec(), backend="hermes", hermes_repo=tmp_path, hermes_provider="wat"
        )


def test_build_harness_rejects_unknown_backend() -> None:
    import pytest

    from outerloop.role_runner import build_harness

    with pytest.raises(ValueError, match="unknown backend"):
        build_harness("k", reviewer_spec(), backend="gemini-cli")
