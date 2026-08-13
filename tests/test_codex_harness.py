"""CodexHarness command construction and output parsing.

The Codex CLI is not run here (no binary in CI); these tests pin the argv shape
and the defensive JSONL parsing. The exact flag spellings and event schema are
verified on the cluster — see CodexHarness.
"""

from __future__ import annotations

import json
from pathlib import Path

from autoresearch.harness import _codex_command, _parse_codex_result


def test_command_reads_prompt_from_stdin_not_argv() -> None:
    cmd = _codex_command("codex", "m", "read-only", Path("/w"), Path("/w-last.txt"), None, ())
    assert cmd[:2] == ["codex", "exec"]
    # the brief travels on stdin, never as an argument
    assert all(part != "brief_text" for part in cmd)
    assert "--output-last-message" in cmd and "/w-last.txt" in cmd
    assert "--sandbox" in cmd and "read-only" in cmd
    assert "--cd" in cmd and "/w" in cmd


def test_command_resume_uses_resume_subcommand() -> None:
    cmd = _codex_command("codex", "m", "read-only", Path("/w"), Path("/l"), "sess-123", ())
    assert cmd[:4] == ["codex", "exec", "resume", "sess-123"]


def test_parse_success_pulls_session_and_final_text() -> None:
    stdout = "\n".join(
        [
            json.dumps({"type": "session.created", "session_id": "sess-9"}),
            json.dumps({"type": "item.completed"}),
        ]
    )
    result = _parse_codex_result(stdout, "final answer\n", 0, "t.jsonl")
    assert result.is_error is False
    assert result.session_id == "sess-9"
    assert result.final_text == "final answer"
    assert result.transcript_path == "t.jsonl"


def test_parse_flags_error_on_nonzero_returncode() -> None:
    assert _parse_codex_result("", "", 1).is_error is True


def test_parse_flags_error_event_with_message() -> None:
    stdout = json.dumps({"type": "error", "message": "model unavailable"})
    result = _parse_codex_result(stdout, "", 0)
    assert result.is_error is True
    assert "model unavailable" in result.error_detail


def test_parse_tolerates_non_json_lines() -> None:
    stdout = "starting up...\n" + json.dumps({"session_id": "s"}) + "\nbye"
    result = _parse_codex_result(stdout, "ok", 0)
    assert result.session_id == "s"
    assert result.is_error is False
