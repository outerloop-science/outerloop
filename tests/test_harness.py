"""Harness adapter tests run against a fake `claude` executable — a script
that records its environment and argv, then prints canned JSON. No network,
no real key."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from autoresearch.harness import (
    SESSION_ENV_ALLOWLIST,
    ClaudeCodeHarness,
    FakeHarness,
    SessionResult,
    redact,
    session_env,
)

CANNED = {
    "is_error": False,
    "num_turns": 3,
    "stop_reason": "end_turn",
    "session_id": "sess-1",
    "total_cost_usd": 0.42,
    "result": "Report: hypothesis held.",
}


def fake_claude(tmp_path: Path, payload: str, exit_code: int = 0, sleep: int = 0) -> str:
    """A stand-in binary that dumps env, argv, and stdin, then emits `payload`."""
    script = tmp_path / "claude"
    script.write_text(
        "#!/bin/sh\n"
        f"cat > {tmp_path}/seen_stdin\n"
        f"sleep {sleep}\n"
        f"env > {tmp_path}/seen_env\n"
        f'printf "%s " "$@" > {tmp_path}/seen_argv\n'
        f"cat <<'EOF'\n{payload}\nEOF\n"
        f"exit {exit_code}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_successful_session_parses_everything(tmp_path: Path) -> None:
    binary = fake_claude(tmp_path, json.dumps(CANNED))
    ws = tmp_path / "ws"
    ws.mkdir()
    result = ClaudeCodeHarness(api_key="sk-test-123", binary=binary).run("do the task", ws)
    assert not result.is_error
    assert result.cost_usd == 0.42
    assert result.num_turns == 3
    assert result.session_id == "sess-1"
    assert result.final_text == "Report: hypothesis held."
    assert Path(result.transcript_path).read_text().strip().startswith("{")


def test_session_env_is_scrubbed(tmp_path: Path, monkeypatch) -> None:
    """The threat model's credential-theft guard: no PAT, no stray secrets."""
    monkeypatch.setenv("AUTORESEARCH_BOT_PAT", "github_pat_SECRET")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws_SECRET")
    binary = fake_claude(tmp_path, json.dumps(CANNED))
    ws = tmp_path / "ws"
    ws.mkdir()
    ClaudeCodeHarness(api_key="sk-test-123", binary=binary).run("task", ws)
    seen = (tmp_path / "seen_env").read_text()
    assert "github_pat_SECRET" not in seen
    assert "aws_SECRET" not in seen
    assert "ANTHROPIC_API_KEY=sk-test-123" in seen
    assert "PATH=" in seen


def test_home_is_redirected_per_session(tmp_path: Path) -> None:
    """A session's HOME must not be the real home — `~`-relative key files
    (harness key, bot PAT, ssh) must resolve nowhere."""
    import os

    binary = fake_claude(tmp_path, json.dumps(CANNED))
    ws = tmp_path / "ws"
    ws.mkdir()
    ClaudeCodeHarness(api_key="k", binary=binary).run("task", ws)
    seen = dict(
        line.split("=", 1)
        for line in (tmp_path / "seen_env").read_text().splitlines()
        if "=" in line
    )
    assert seen["HOME"] == str(tmp_path / "ws-home")
    assert seen["HOME"] != os.environ.get("HOME")
    assert Path(seen["HOME"]).is_dir()


def test_session_env_allowlist_is_exhaustive(tmp_path: Path) -> None:
    env = session_env("key", "ANTHROPIC_API_KEY", home=tmp_path)
    assert set(env) <= set(SESSION_ENV_ALLOWLIST) | {"ANTHROPIC_API_KEY", "HOME"}
    assert env["HOME"] == str(tmp_path)


def test_argv_carries_the_controls(tmp_path: Path) -> None:
    binary = fake_claude(tmp_path, json.dumps(CANNED))
    ws = tmp_path / "ws"
    ws.mkdir()
    ClaudeCodeHarness(api_key="k", binary=binary, max_turns=7).run("the brief", ws)
    argv = (tmp_path / "seen_argv").read_text()
    assert "--max-turns 7" in argv
    assert "--output-format json" in argv
    assert "--permission-mode acceptEdits" in argv


def test_brief_travels_on_stdin_never_argv(tmp_path: Path) -> None:
    """argv is world-readable via /proc on shared nodes; briefs carry private
    research text."""
    binary = fake_claude(tmp_path, json.dumps(CANNED))
    ws = tmp_path / "ws"
    ws.mkdir()
    ClaudeCodeHarness(api_key="k", binary=binary).run("SECRET research brief", ws)
    assert "SECRET research brief" not in (tmp_path / "seen_argv").read_text()
    assert (tmp_path / "seen_stdin").read_text() == "SECRET research brief"


def test_transcript_lives_outside_the_workspace(tmp_path: Path) -> None:
    """The workspace is a git clone that gets committed and pushed; the
    transcript must never be part of that diff."""
    binary = fake_claude(tmp_path, json.dumps(CANNED))
    ws = tmp_path / "ws"
    ws.mkdir()
    result = ClaudeCodeHarness(api_key="k", binary=binary).run("task", ws)
    transcript = Path(result.transcript_path).resolve()
    assert ws.resolve() not in transcript.parents
    assert transcript.exists()


def test_malformed_result_fields_degrade_not_raise(tmp_path: Path) -> None:
    bad = dict(CANNED, total_cost_usd="not-a-number")
    binary = fake_claude(tmp_path, json.dumps(bad))
    ws = tmp_path / "ws"
    ws.mkdir()
    result = ClaudeCodeHarness(api_key="k", binary=binary).run("task", ws)
    assert result.is_error
    assert result.stop_reason == "unparseable-output"


def test_result_object_recovered_from_surrounding_log_lines(tmp_path: Path) -> None:
    noisy = "warning: something\n" + json.dumps(CANNED) + "\ntrailing line"
    binary = fake_claude(tmp_path, noisy)
    ws = tmp_path / "ws"
    ws.mkdir()
    result = ClaudeCodeHarness(api_key="k", binary=binary).run("task", ws)
    assert not result.is_error
    assert result.cost_usd == 0.42


def test_timeout_returns_error_result_not_exception(tmp_path: Path) -> None:
    binary = fake_claude(tmp_path, json.dumps(CANNED), sleep=30)
    ws = tmp_path / "ws"
    ws.mkdir()
    result = ClaudeCodeHarness(api_key="k", binary=binary, timeout_s=1).run("task", ws)
    assert result.is_error
    assert result.stop_reason == "timeout"


def test_missing_binary_returns_spawn_error(tmp_path: Path) -> None:
    result = ClaudeCodeHarness(api_key="k", binary=str(tmp_path / "nope")).run("task", tmp_path)
    assert result.is_error
    assert result.stop_reason == "spawn-error"


def test_garbage_output_is_an_error_with_transcript(tmp_path: Path) -> None:
    binary = fake_claude(tmp_path, "not json at all")
    ws = tmp_path / "ws"
    ws.mkdir()
    result = ClaudeCodeHarness(api_key="k", binary=binary).run("task", ws)
    assert result.is_error
    assert result.stop_reason == "unparseable-output"
    assert "not json" in Path(result.transcript_path).read_text()


def test_transcript_is_redacted(tmp_path: Path) -> None:
    """A session that echoes its own key must not leak it into storage."""
    leaked = dict(CANNED, result="the key is sk-leak-me-456 whoops")
    binary = fake_claude(tmp_path, json.dumps(leaked))
    ws = tmp_path / "ws"
    ws.mkdir()
    result = ClaudeCodeHarness(api_key="sk-leak-me-456", binary=binary).run("task", ws)
    stored = Path(result.transcript_path).read_text()
    assert "sk-leak-me-456" not in stored
    assert "[redacted]" in stored


def test_redact_handles_empty_secrets() -> None:
    """An empty secret must not turn every character boundary into noise."""
    assert redact("text", ("", "sk-key")) == "text"
    assert redact("key sk-key end", ("sk-key",)) == "key [redacted] end"


def test_fake_harness_records_calls(tmp_path: Path) -> None:
    fake = FakeHarness(
        result=SessionResult(
            stop_reason="end_turn",
            is_error=False,
            cost_usd=0.0,
            num_turns=1,
            session_id="s",
            final_text="r",
            transcript_path="",
        )
    )
    fake.run("brief text", tmp_path)
    assert fake.calls == [("brief text", str(tmp_path))]


def test_resume_passes_session_id_and_reuses_run_home(tmp_path: Path) -> None:
    """A wake must restore the run's context: --resume in argv, same HOME."""
    binary = fake_claude(tmp_path, json.dumps(CANNED))
    ws = tmp_path / "ws"
    ws.mkdir()
    harness = ClaudeCodeHarness(api_key="k", binary=binary)
    harness.run("initial brief", ws)
    first_home = dict(
        line.split("=", 1)
        for line in (tmp_path / "seen_env").read_text().splitlines()
        if "=" in line
    )["HOME"]
    harness.run("wake update", ws, resume_session_id="sess-1")
    argv = (tmp_path / "seen_argv").read_text()
    second_home = dict(
        line.split("=", 1)
        for line in (tmp_path / "seen_env").read_text().splitlines()
        if "=" in line
    )["HOME"]
    assert "--resume sess-1" in argv
    assert second_home == first_home


def test_no_resume_flag_on_fresh_sessions(tmp_path: Path) -> None:
    binary = fake_claude(tmp_path, json.dumps(CANNED))
    ws = tmp_path / "ws"
    ws.mkdir()
    ClaudeCodeHarness(api_key="k", binary=binary).run("brief", ws)
    assert "--resume" not in (tmp_path / "seen_argv").read_text()


def test_transcripts_accumulate_per_session_not_clobber(tmp_path: Path) -> None:
    """Every session of a run keeps its own transcript."""
    binary = fake_claude(tmp_path, json.dumps(CANNED))
    ws = tmp_path / "ws"
    ws.mkdir()
    harness = ClaudeCodeHarness(api_key="k", binary=binary)
    first = harness.run("brief", ws)
    second = harness.run("wake", ws, resume_session_id="sess-1")
    assert first.transcript_path != second.transcript_path
    assert Path(first.transcript_path).exists()
    assert Path(second.transcript_path).exists()
