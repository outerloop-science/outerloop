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

    class FakePopen:
        returncode = 0

        def __init__(self, command: list[str], **kwargs: Any) -> None:
            seen["argv"] = command
            seen["env"] = kwargs.get("env", {})

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return "final words", ""

    monkeypatch.setattr(harness_mod.subprocess, "Popen", FakePopen)
    workspace = tmp_path / "clone"
    workspace.mkdir()
    brief = "SENTINEL_BRIEF_7q2 private research text"
    HermesHarness(
        api_key="sk-or-SECRET", repo_dir=tmp_path / "hermes", key_env="OPENAI_API_KEY"
    ).run(brief, workspace)
    assert not any("SENTINEL_BRIEF_7q2" in str(part) for part in seen["argv"])
    assert not any("sk-or-SECRET" in str(part) for part in seen["argv"])
    assert seen["env"]["OPENAI_API_KEY"] == "sk-or-SECRET"
    # the brief landed in a file inside the per-run home, referenced by the query
    home = workspace.parent / f"{workspace.name}-home"
    briefs = list(home.glob("brief*.md"))
    assert briefs and "SENTINEL_BRIEF_7q2" in briefs[0].read_text()
    query_arg = next(part for part in seen["argv"] if part.startswith("--query="))
    assert str(briefs[0]) in query_arg


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


def test_resume_is_refused_not_silently_fresh(tmp_path: Path) -> None:
    harness = HermesHarness(api_key="k", repo_dir=tmp_path)
    result = harness.run("brief", tmp_path / "ws", resume_session_id="old-session")
    assert result.is_error is True
    assert result.stop_reason == "resume-unsupported"


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
    assert result.num_turns == 1
