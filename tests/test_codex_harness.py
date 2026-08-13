"""CodexHarness command construction and output parsing.

The Codex CLI is not run here (no binary in CI); these tests pin the argv shape
and the defensive JSONL parsing. The exact flag spellings and event schema are
verified on the cluster — see CodexHarness.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import autoresearch.harness as harness_mod
from autoresearch.harness import CodexHarness, _codex_command, _parse_codex_result


def test_command_has_expected_flags() -> None:
    cmd = _codex_command("codex", "m", "read-only", Path("/w"), Path("/w-last.txt"), None, ())
    assert cmd[:2] == ["codex", "exec"]
    assert "--output-last-message" in cmd and "/w-last.txt" in cmd
    assert "--sandbox" in cmd and "read-only" in cmd
    assert "--cd" in cmd and "/w" in cmd
    assert cmd[cmd.index("--model") + 1] == "m"


def test_command_omits_model_when_empty() -> None:
    cmd = _codex_command("codex", "", "read-only", Path("/w"), Path("/l"), None, ())
    assert "--model" not in cmd  # codex uses its configured default


def test_brief_goes_to_stdin_never_argv(monkeypatch: Any, tmp_path: Path) -> None:
    """The real guarantee: the brief is fed on stdin (argv is world-readable in
    /proc), so it must never appear in the spawned command."""
    seen: dict[str, Any] = {}

    class FakePopen:
        returncode = 0

        def __init__(self, command: list[str], **_: Any) -> None:
            seen["argv"] = command

        def communicate(
            self, input: str | None = None, timeout: float | None = None
        ) -> tuple[str, str]:
            seen["stdin"] = input
            return json.dumps({"session_id": "s"}), ""

    monkeypatch.setattr(harness_mod.subprocess, "Popen", FakePopen)
    # stub the login pre-step (its own subprocess) so this test isolates exec
    monkeypatch.setattr(CodexHarness, "_login", lambda self, home: None)
    brief = "SENTINEL_BRIEF_9x7 please review this pull request"
    CodexHarness(api_key="k").run(brief, tmp_path)
    assert seen["stdin"] == brief
    assert not any("SENTINEL_BRIEF_9x7" in str(part) for part in seen["argv"])


def test_command_resume_uses_resume_subcommand() -> None:
    cmd = _codex_command("codex", "m", "read-only", Path("/w"), Path("/l"), "sess-123", ())
    assert cmd[:4] == ["codex", "exec", "resume", "sess-123"]


def test_parse_success_pulls_thread_id_and_final_text() -> None:
    # schema verified against codex-cli 0.130.0: thread.started -> thread_id
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "019ff8f0-abc"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "turn.completed", "usage": {"output_tokens": 29}}),
        ]
    )
    result = _parse_codex_result(stdout, "final answer\n", 0, "t.jsonl")
    assert result.is_error is False
    assert result.session_id == "019ff8f0-abc"
    assert result.final_text == "final answer"
    assert result.transcript_path == "t.jsonl"


def test_parse_flags_error_on_nonzero_returncode() -> None:
    assert _parse_codex_result("", "", 1).is_error is True


def test_parse_flags_error_event_with_message() -> None:
    stdout = json.dumps({"type": "error", "message": "model unavailable"})
    result = _parse_codex_result(stdout, "", 0)
    assert result.is_error is True
    assert "model unavailable" in result.error_detail


def test_parse_flags_turn_failed_with_nested_message() -> None:
    stdout = json.dumps({"type": "turn.failed", "error": {"message": "401 Unauthorized"}})
    result = _parse_codex_result(stdout, "", 0)
    assert result.is_error is True
    assert "401 Unauthorized" in result.error_detail


def test_parse_skips_timestamped_log_lines() -> None:
    # codex interleaves "2026-... ERROR ..." log lines that are not JSON
    stdout = "2026-08-13T02:25:06Z ERROR codex_api: failed to connect\n" + json.dumps(
        {"type": "thread.started", "thread_id": "t1"}
    )
    result = _parse_codex_result(stdout, "ok", 0)
    assert result.session_id == "t1"
    assert result.is_error is False
