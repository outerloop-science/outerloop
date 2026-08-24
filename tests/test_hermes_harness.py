"""HermesHarness command construction and output parsing.

Hermes is not run here; these pin the argv shape (verified against
hermes-agent v0.20.1 source) and the defensive trajectory parsing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import autoresearch.harness as harness_mod
from autoresearch.harness import HermesHarness, _hermes_command, _parse_hermes_result


def test_command_shape_and_toolsets() -> None:
    cmd = _hermes_command(
        Path("/opt/hermes"),
        "Read the brief at /h/brief.md",
        "anthropic/claude-sonnet-4.6",
        "",
        40,
        ("file",),
        ("terminal", "web"),
        (),
    )
    assert cmd[:4] == ["uv", "run", "--project", "/opt/hermes"]
    assert "--save_sample" in cmd
    # embedded quotes so fire literal-evals a STRING, not a tuple
    assert '--enabled_toolsets="file"' in cmd
    assert '--disabled_toolsets="terminal,web"' in cmd
    assert "--max_turns=40" in cmd
    assert not any("--base_url" in part for part in cmd)  # empty -> hermes default


def test_brief_and_key_never_in_argv(monkeypatch: Any, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    workspace = tmp_path / "clone"
    workspace.mkdir()
    home = workspace.parent / f"{workspace.name}-home"

    class FakePopen:
        returncode = 0

        def __init__(self, command: list[str], **kwargs: Any) -> None:
            seen["argv"] = command
            seen["env"] = kwargs.get("env", {})
            # the brief exists DURING the run; capture it before cleanup removes it
            briefs = list(home.glob("brief*.md"))
            seen["brief_files"] = [str(b) for b in briefs]
            seen["brief_text"] = briefs[0].read_text() if briefs else ""

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return "final words", ""

    monkeypatch.setattr(harness_mod.subprocess, "Popen", FakePopen)
    brief = "SENTINEL_BRIEF_7q2 private research text"
    HermesHarness(
        api_key="sk-or-SECRET", repo_dir=tmp_path / "hermes", key_env="OPENAI_API_KEY"
    ).run(brief, workspace)
    assert not any("SENTINEL_BRIEF_7q2" in str(part) for part in seen["argv"])
    assert not any("sk-or-SECRET" in str(part) for part in seen["argv"])
    assert seen["env"]["OPENAI_API_KEY"] == "sk-or-SECRET"
    # the brief landed in a file inside the per-run home, referenced by the query
    assert seen["brief_text"] and "SENTINEL_BRIEF_7q2" in seen["brief_text"]
    query_arg = next(part for part in seen["argv"] if part.startswith("--query="))
    assert seen["brief_files"][0] in query_arg
    # and it does not outlive the run (it holds PR content)
    assert not list(home.glob("brief*.md"))


def test_parse_prefers_sample_trajectory() -> None:
    sample = {
        "messages": [
            {"role": "user", "content": "brief"},
            {"role": "assistant", "content": "thinking..."},
            {"role": "assistant", "content": '{"findings": [], "notes": "ok"}'},
        ]
    }
    result = _parse_hermes_result("noisy stdout", sample, 0, "t.log")
    assert result.is_error is False
    assert result.final_text == '{"findings": [], "notes": "ok"}'
    assert result.num_turns == 2
    assert result.session_id == ""  # no resume seam


def test_parse_falls_back_to_stdout_but_flags_zero_turns() -> None:
    # no assistant output = failure (hermes exits 0 even on total API failure)
    result = _parse_hermes_result("  HTTP 401: Missing Authentication header  ", None, 0)
    assert result.final_text == "HTTP 401: Missing Authentication header"
    assert result.is_error is True


def test_nonzero_returncode_is_error_with_detail() -> None:
    result = _parse_hermes_result("boom: missing key", None, 2)
    assert result.is_error is True
    assert "missing key" in result.error_detail


def _make_hermes_popen(home: Path, reply: str, seen: dict[str, Any]) -> Any:
    """A FakePopen that records the brief file it was handed (captured before
    cleanup) and drops a one-turn trajectory with `reply` as the assistant."""

    class FakePopen:
        returncode = 0

        def __init__(self, command: list[str], **_: Any) -> None:
            home.mkdir(parents=True, exist_ok=True)
            briefs = list(home.glob("brief*.md"))
            seen["brief_text"] = briefs[0].read_text() if briefs else ""
            (home / "sample_x.json").write_text(
                json.dumps({"conversations": [{"from": "gpt", "value": reply}]})
            )

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return "", ""

    return FakePopen


def test_hermes_supports_resume() -> None:
    assert HermesHarness(api_key="k", repo_dir=Path(".")).supports_resume is True


def test_resume_rehydrates_prior_context(monkeypatch: Any, tmp_path: Path) -> None:
    # resume with no --resume flag: hermes reads its brief from a file, so the
    # prior conversation is rehydrated INTO the resume brief — the session can
    # never start context-blind (the reason the old refusal existed).
    workspace = tmp_path / "clone"
    workspace.mkdir()
    home = workspace.parent / f"{workspace.name}-home"
    seen: dict[str, Any] = {}
    harness = HermesHarness(api_key="k", repo_dir=tmp_path / "hermes")

    monkeypatch.setattr(
        harness_mod.subprocess, "Popen", _make_hermes_popen(home, "FIRST_REPLY_z", seen)
    )
    first = harness.run("FIRST_BRIEF_q investigate", workspace)
    assert not first.is_error
    assert first.session_id  # a session id is minted for a fresh run
    assert list(home.glob("resume-*.json"))  # transcript persisted for the next resume

    monkeypatch.setattr(
        harness_mod.subprocess, "Popen", _make_hermes_popen(home, "SECOND_REPLY", seen)
    )
    second = harness.run("SECOND_BRIEF_w continue", workspace, resume_session_id=first.session_id)
    assert second.session_id == first.session_id  # the chain keeps one id
    # the brief hermes was handed on resume carries the prior turns AND the new one
    assert "FIRST_BRIEF_q" in seen["brief_text"]  # prior user turn restored
    assert "FIRST_REPLY_z" in seen["brief_text"]  # prior assistant turn restored
    assert "SECOND_BRIEF_w" in seen["brief_text"]  # the new instructions


def test_resume_without_saved_transcript_errors_not_silently_fresh(tmp_path: Path) -> None:
    # a resume whose context cannot be restored is a hard error — never a blind
    # fresh start (the deployment must preserve the per-run home, like claude's
    # $HOME / codex's --home).
    workspace = tmp_path / "clone"
    workspace.mkdir()
    result = HermesHarness(api_key="k", repo_dir=tmp_path / "hermes").run(
        "brief", workspace, resume_session_id="never-saved"
    )
    assert result.is_error is True
    assert result.stop_reason == "resume-unavailable"


def test_resume_with_empty_transcript_errors_not_blind(tmp_path: Path) -> None:
    # an empty (or all-invalid) transcript is NO usable context: a corrupted
    # session-writable file must surface as resume-unavailable, not a blind
    # resume with only the new brief (terra #139).
    from autoresearch.harness import _resume_transcript_path

    workspace = tmp_path / "clone"
    workspace.mkdir()
    home = workspace.parent / f"{workspace.name}-home"
    home.mkdir(parents=True)
    for corrupt in ('{"turns": []}', '{"turns": [1, 2, 3]}', "{}"):
        _resume_transcript_path(home, "sid").write_text(corrupt)
        result = HermesHarness(api_key="k", repo_dir=tmp_path / "hermes").run(
            "brief", workspace, resume_session_id="sid"
        )
        assert result.is_error is True, corrupt
        assert result.stop_reason == "resume-unavailable", corrupt


def test_saved_transcript_redacts_the_session_key(monkeypatch: Any, tmp_path: Path) -> None:
    # final_text comes from the sample (not the redacted stdout), so an agent
    # reply echoing the key must not land in the persisted transcript (terra #139).
    from autoresearch.harness import _resume_transcript_path

    workspace = tmp_path / "clone"
    workspace.mkdir()
    home = workspace.parent / f"{workspace.name}-home"
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        harness_mod.subprocess, "Popen", _make_hermes_popen(home, "my key is sk-SECRET-42", seen)
    )
    result = HermesHarness(api_key="sk-SECRET-42", repo_dir=tmp_path / "hermes").run("b", workspace)
    saved = _resume_transcript_path(home, result.session_id).read_text()
    assert "sk-SECRET-42" not in saved  # the key was redacted before persisting
    assert "my key is" in saved  # the rest of the reply is kept


def test_sample_files_are_cleaned_up(monkeypatch: Any, tmp_path: Path) -> None:
    workspace = tmp_path / "clone"
    workspace.mkdir()
    home = workspace.parent / f"{workspace.name}-home"

    class FakePopen:
        returncode = 0

        def __init__(self, command: list[str], **_: Any) -> None:
            home.mkdir(exist_ok=True)
            (home / "sample_ab12.json").write_text(
                json.dumps({"messages": [{"role": "assistant", "content": "done"}]})
            )

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return "", ""

    monkeypatch.setattr(harness_mod.subprocess, "Popen", FakePopen)
    result = HermesHarness(api_key="k", repo_dir=tmp_path / "hermes").run("b", workspace)
    assert result.final_text == "done"
    assert not list(home.glob("sample_*.json"))  # trajectory (may embed the brief) removed
    assert not list(home.glob("brief*.md"))  # the brief holds PR content; not left at rest


def test_parse_sharegpt_trajectory() -> None:
    # the REAL saved format (agent_runtime_helpers.convert_to_trajectory_format)
    sample = [
        {"from": "system", "value": "You are a function calling AI model..."},
        {"from": "human", "value": "the brief"},
        {"from": "gpt", "value": "ok"},
    ]
    result = _parse_hermes_result("noise", sample, 0)
    assert result.is_error is False
    assert result.final_text == "ok"


def test_parse_conversations_wrapper() -> None:
    # run_agent.py --save_sample wraps the turns under "conversations"
    # (run_agent.py:8404, v0.20.1). Missing this key was read as zero turns and
    # dropped a real verdict as a bogus error — the whole "produced no verdict" bug.
    sample = {
        "conversations": [
            {"from": "human", "value": "the brief"},
            {"from": "gpt", "value": '{"findings": [], "notes": "clean"}'},
        ],
        "model": "deepseek/deepseek-v4-pro",
        "completed": True,
    }
    result = _parse_hermes_result("noise", sample, 0)
    assert result.is_error is False
    assert result.num_turns == 1
    assert result.final_text == '{"findings": [], "notes": "clean"}'


def test_sample_read_does_not_follow_symlink(tmp_path: Path) -> None:
    # the per-run home is session-writable; a planted sample_*.json symlink must
    # not be read as a trajectory (that would exfil an arbitrary same-user file)
    from autoresearch.harness import _collect_hermes_sample

    home = tmp_path / "home"
    home.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("SENSITIVE")
    (home / "sample_evil.json").symlink_to(secret)
    sample = _collect_hermes_sample(home)
    assert sample is None  # the link was not followed/read
    assert not (home / "sample_evil.json").exists()  # link removed, not its target
    assert secret.read_text() == "SENSITIVE"  # target untouched


def test_config_write_refuses_symlink(tmp_path: Path) -> None:
    from autoresearch.harness import _write_private_fixed

    target = tmp_path / "target.txt"
    target.write_text("ORIG")
    (tmp_path / "config.yaml").symlink_to(target)
    assert _write_private_fixed(tmp_path / "config.yaml", "NEW") is False  # refused
    assert target.read_text() == "ORIG"  # redirect target untouched
