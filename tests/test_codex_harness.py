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
from autoresearch.harness import (
    CodexHarness,
    SessionResult,
    _codex_command,
    _parse_codex_result,
    _rmtree_at,
)


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


def test_run_clears_codex_scratch_but_keeps_durable_state(monkeypatch: Any, tmp_path: Path) -> None:
    """Codex leaks temp dirs into .codex/.tmp across a run's wakes; run() clears
    that scratch each time, while its durable state (sessions, sqlite) stays."""
    from autoresearch import harness as harness_mod

    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "ws-home"
    tmp = home / ".codex" / ".tmp" / "leaked-dir"
    tmp.mkdir(parents=True)
    (tmp / "junk").write_text("x")
    tmp_alt = home / ".codex" / "tmp" / "leaked-dir"  # the alternate scratch name
    tmp_alt.mkdir(parents=True)
    (tmp_alt / "junk").write_text("x")
    sessions = home / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "s.json").write_text("keep")

    class FakePopen:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def communicate(self, *a: Any, **k: Any) -> Any:
            return ("", "")

        returncode = 1

    monkeypatch.setattr(harness_mod.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(CodexHarness, "_login", lambda self, hm: None)
    CodexHarness(api_key="k").run("brief", workspace)
    assert not (home / ".codex" / ".tmp").exists()  # scratch cleared
    assert not (home / ".codex" / "tmp").exists()  # alternate scratch cleared too
    assert (sessions / "s.json").read_text() == "keep"  # durable state kept


def test_run_does_not_follow_a_symlinked_codex_out_of_home(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A prior session owns its home; if it replaced .codex with a symlink to
    an external directory, the scratch cleanup must not follow the link and
    delete the target's .tmp."""
    from autoresearch import harness as harness_mod

    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "ws-home"
    home.mkdir()
    outside = tmp_path / "outside"
    keep = outside / ".tmp" / "keep"
    keep.mkdir(parents=True)
    (keep / "f").write_text("x")
    (home / ".codex").symlink_to(outside)

    class FakePopen:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def communicate(self, *a: Any, **k: Any) -> Any:
            return ("", "")

        returncode = 1

    monkeypatch.setattr(harness_mod.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(CodexHarness, "_login", lambda self, hm: None)
    CodexHarness(api_key="k").run("brief", workspace)
    assert (keep / "f").read_text() == "x"  # symlink target untouched


def test_run_survives_a_scratch_cleanup_that_raises(monkeypatch: Any, tmp_path: Path) -> None:
    """Scratch cleanup is best-effort: even if rmtree raises (a RecursionError
    on an adversarially deep leaked tree), run() returns a SessionResult rather
    than aborting before login."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "ws-home"
    (home / ".codex" / ".tmp").mkdir(parents=True)

    def boom(*a: Any, **k: Any) -> None:
        raise RecursionError("too deep")

    class FakePopen:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def communicate(self, *a: Any, **k: Any) -> Any:
            return ("", "")

        returncode = 1

    monkeypatch.setattr(harness_mod, "_rmtree_at", boom)
    monkeypatch.setattr(harness_mod.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(CodexHarness, "_login", lambda self, hm: None)
    result = CodexHarness(api_key="k").run("brief", workspace)
    assert isinstance(result, SessionResult)  # cleanup did not propagate


def test_rmtree_at_deletes_a_tree_but_never_follows_a_symlink(tmp_path: Path) -> None:
    """_rmtree_at removes a real directory subtree, and when the named entry is
    a symlink it deletes nothing (the target is left intact)."""
    import os

    parent = tmp_path / "parent"
    (parent / "scratch" / "deep").mkdir(parents=True)
    (parent / "scratch" / "deep" / "f").write_text("x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("y")
    (parent / "link").symlink_to(outside)

    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        _rmtree_at(fd, "scratch")  # real subtree: gone
        _rmtree_at(fd, "link")  # symlink: not followed
    finally:
        os.close(fd)

    assert not (parent / "scratch").exists()
    assert (parent / "link").is_symlink()  # the link itself is left in place
    assert (outside / "keep").read_text() == "y"  # target untouched


def test_run_does_not_follow_a_symlinked_scratch_dir(monkeypatch: Any, tmp_path: Path) -> None:
    """.codex is a real directory but .codex/.tmp is a symlink to an external
    dir; cleanup must not follow it and delete the target."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "ws-home"
    (home / ".codex").mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "keep").mkdir(parents=True)
    (outside / "keep" / "f").write_text("x")
    (home / ".codex" / ".tmp").symlink_to(outside)

    class FakePopen:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def communicate(self, *a: Any, **k: Any) -> Any:
            return ("", "")

        returncode = 1

    monkeypatch.setattr(harness_mod.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(CodexHarness, "_login", lambda self, hm: None)
    CodexHarness(api_key="k").run("brief", workspace)
    assert (outside / "keep" / "f").read_text() == "x"  # symlink target untouched


def test_run_does_not_follow_a_symlinked_run_home(monkeypatch: Any, tmp_path: Path) -> None:
    """A prior session (uncontained: a plain host process) replaces ws-home with
    a symlink to an external dir; cleanup resolves the run home with O_NOFOLLOW
    anchored on the run directory, so it does not follow the link and delete the
    target's .codex/.tmp."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    workspace = run_dir / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    (outside / ".codex" / ".tmp" / "keep").mkdir(parents=True)
    (outside / ".codex" / ".tmp" / "keep" / "f").write_text("x")
    (run_dir / "ws-home").symlink_to(outside)  # session_home is a symlink

    class FakePopen:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def communicate(self, *a: Any, **k: Any) -> Any:
            return ("", "")

        returncode = 1

    monkeypatch.setattr(harness_mod.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(CodexHarness, "_login", lambda self, hm: None)
    CodexHarness(api_key="k").run("brief", workspace)
    assert (outside / ".codex" / ".tmp" / "keep" / "f").read_text() == "x"  # untouched
