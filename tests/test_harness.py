"""Harness adapter tests run against a fake `claude` executable — a script
that records its environment and argv, then prints canned JSON. No network,
no real key."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from autoresearch.harness import (
    SESSION_ENV_ALLOWLIST,
    ClaudeCodeHarness,
    FakeHarness,
    SessionResult,
    budget_exhausted,
    outage,
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


def test_malformed_fields_are_salvaged_not_discarded(tmp_path: Path) -> None:
    """A quirky cost value must not cost us the session id (resume depends
    on it) — field-level salvage, and never an exception."""
    bad = dict(CANNED, total_cost_usd="not-a-number")
    binary = fake_claude(tmp_path, json.dumps(bad))
    ws = tmp_path / "ws"
    ws.mkdir()
    result = ClaudeCodeHarness(api_key="k", binary=binary).run("task", ws)
    assert result.cost_usd == 0.0
    assert result.session_id == "sess-1"
    assert result.num_turns == 3
    assert not result.is_error


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


def test_exhausted_turns_carry_an_honest_detail(tmp_path: Path) -> None:
    """A session that dies at max turns reports WHY on the result — the
    subtype and the backend's message — and classifies as budget
    exhaustion, because stop_reason alone reads as noise ("tool_use")."""
    payload = dict(CANNED)
    payload.update(
        is_error=True,
        stop_reason="tool_use",
        subtype="error_max_turns",
        errors=["Reached maximum number of turns (120)"],
    )
    binary = fake_claude(tmp_path, json.dumps(payload))
    ws = tmp_path / "ws"
    ws.mkdir()
    result = ClaudeCodeHarness(api_key="k", binary=binary).run("task", ws)
    assert result.is_error
    assert result.error_detail == "error_max_turns: Reached maximum number of turns (120)"
    assert budget_exhausted(result)


def test_timeout_is_budget_exhaustion_with_walltime_detail(tmp_path: Path) -> None:
    binary = fake_claude(tmp_path, json.dumps(CANNED), sleep=30)
    ws = tmp_path / "ws"
    ws.mkdir()
    result = ClaudeCodeHarness(api_key="k", binary=binary, timeout_s=1).run("task", ws)
    assert budget_exhausted(result)
    assert "walltime" in result.error_detail


def test_outage_classification_matches_api_refusals_only() -> None:
    """Dead credits, spend caps, auth, throttling — and nothing else. An
    agent REPORT that mentions billing must never read as an outage
    (outage() only looks at error surfaces of is_error results)."""

    def result(is_error: bool, detail: str = "", text: str = "") -> SessionResult:
        return SessionResult(
            stop_reason="end_turn",
            is_error=is_error,
            cost_usd=0.0,
            num_turns=1,
            session_id="",
            final_text=text,
            transcript_path="",
            error_detail=detail,
        )

    assert outage(result(True, detail="error_during_execution: credit balance is too low"))
    assert outage(result(True, text="API Error (400): You have reached your usage limit."))
    assert outage(result(True, detail="authentication_error: invalid x-api-key"))
    assert outage(result(True, detail="rate_limit_error: Number of requests..."))
    # hermes / OpenRouter 401 shapes (OpenAI-compatible, not Anthropic-shaped)
    assert outage(result(True, detail="HTTP 401: Missing Authentication header"))
    assert outage(
        result(True, detail='{"error":{"message":"No auth credentials found","code":401}}')
    )
    assert not outage(result(True, detail="error_max_turns: Reached maximum number of turns"))
    assert not outage(  # agent prose never consulted when backend detail exists
        result(
            True,
            detail="error_max_turns: Reached maximum number of turns",
            text="Report: we should raise the usage limit and cut billing costs.",
        )
    )
    assert not outage(result(False, text="Report: cut billing costs by caching the pool."))
    assert not outage(result(True, detail="TypeError: 'NoneType' object is not iterable"))
    # empty detail: prose is consulted only in the legacy "API Error" shape
    assert outage(result(True, text="API Error (400): credit balance is too low"))
    assert not outage(result(True, text="Report: raise the usage limit, cut billing costs."))


def test_error_detail_carries_the_real_cause_when_subtype_is_success(tmp_path: Path) -> None:
    """The CLI can flag is_error while stamping a content-free subtype
    ("success"), leaving the real cause only in `result`. The parse must lift
    that machine "API Error ..." text into error_detail so the outage latch
    sees it (classifier AND throttle-duration read the detail), not "success".
    Observed on Torch: a session hit the workspace usage cap this exact way."""
    payload = json.dumps(
        {
            "is_error": True,
            "subtype": "success",
            "stop_reason": "stop_sequence",
            "num_turns": 14,
            "total_cost_usd": 0.95,
            "result": "API Error: 400 You have reached your specified workspace API usage limits.",
        }
    )
    binary = fake_claude(tmp_path, payload)
    ws = tmp_path / "ws"
    ws.mkdir()
    res = ClaudeCodeHarness(api_key="k", binary=binary).run("task", ws)
    assert res.is_error
    # the detail is the real error, not the useless "success" subtype
    assert res.error_detail.startswith("API Error: 400")
    assert "usage limits" in res.error_detail
    # so the outage latch fires off the detail (no final_text fallback needed)
    assert outage(res)


def test_parse_keeps_a_real_subtype_when_the_result_quotes_an_api_error(tmp_path: Path) -> None:
    """The result-lift is ONLY for the content-free "success" subtype. A
    caps-hit ending (error_max_turns) whose last message happens to quote an
    API error must KEEP its subtype, so budget_exhausted() still fires and the
    ending is not reclassified as an outage. Drives the real parse, not a hand-
    built SessionResult (the outage() unit test cannot exercise the lift)."""
    payload = json.dumps(
        {
            "is_error": True,
            "subtype": "error_max_turns",
            "stop_reason": "error_max_turns",
            "num_turns": 50,
            "result": "API Error: 429 rate limit — retried, then hit the turn cap.",
        }
    )
    binary = fake_claude(tmp_path, payload)
    ws = tmp_path / "ws"
    ws.mkdir()
    res = ClaudeCodeHarness(api_key="k", binary=binary).run("task", ws)
    assert res.is_error
    assert res.error_detail.startswith("error_max_turns")  # subtype NOT clobbered
    assert budget_exhausted(res)  # still a budget ending
    assert not outage(res)  # and NOT misclassified as an outage


def test_parse_keeps_backend_error_messages_over_a_success_subtype(tmp_path: Path) -> None:
    """A "success" subtype can still carry real `errors` — the lift must NOT
    clobber them with the result text. The backend message is the authoritative
    cause and classification must come from it, not the quoted result."""
    payload = json.dumps(
        {
            "is_error": True,
            "subtype": "success",
            "errors": ["overloaded_error: the model is temporarily overloaded"],
            "result": "API Error: 400 something the agent echoed",
        }
    )
    binary = fake_claude(tmp_path, payload)
    ws = tmp_path / "ws"
    ws.mkdir()
    res = ClaudeCodeHarness(api_key="k", binary=binary).run("task", ws)
    assert res.is_error
    # the real backend cause is kept, not replaced by the result text
    assert "overloaded_error" in res.error_detail
    assert not res.error_detail.startswith("API Error")
    assert outage(res)  # classified from the real cause (overloaded_error)


def test_clean_sessions_and_real_failures_are_not_budget_endings(tmp_path: Path) -> None:
    binary = fake_claude(tmp_path, json.dumps(CANNED))
    ws = tmp_path / "ws"
    ws.mkdir()
    ok = ClaudeCodeHarness(api_key="k", binary=binary).run("task", ws)
    assert ok.error_detail == "" and not budget_exhausted(ok)
    dead = ClaudeCodeHarness(api_key="k", binary=str(tmp_path / "nope")).run("task", tmp_path)
    assert not budget_exhausted(dead)


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


def test_transcript_write_refuses_symlinks(tmp_path: Path) -> None:
    """A dangling symlink planted at the transcript name must not redirect
    the write to another same-user file."""
    import os as _os

    binary = fake_claude(tmp_path, json.dumps(CANNED))
    ws = tmp_path / "ws"
    ws.mkdir()
    target = tmp_path / "victim"
    _os.symlink(target, tmp_path / "ws-session.json")
    result = ClaudeCodeHarness(api_key="k", binary=binary).run("task", ws)
    assert not target.exists()  # the symlink was not followed
    assert result.transcript_path.endswith("ws-session-2.json")


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


def test_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    """Bash-tool descendants of a timed-out session must not survive holding
    the API key and writing into the clone."""
    import os as _os

    script = tmp_path / "claude"
    script.write_text(
        f"#!/bin/sh\ncat > /dev/null\nsleep 300 &\necho $! > {tmp_path}/child_pid\nsleep 300\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    ws = tmp_path / "ws"
    ws.mkdir()
    result = ClaudeCodeHarness(api_key="k", binary=str(script), timeout_s=1).run("task", ws)
    assert result.stop_reason == "timeout"
    child = int((tmp_path / "child_pid").read_text().strip())
    # the grandchild lingers as a zombie until init reaps it — poll briefly
    import time

    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            _os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        raise AssertionError(f"child {child} survived the group kill")


def test_transcript_and_home_are_owner_only(tmp_path: Path) -> None:
    """Transcripts carry private research text on a shared filesystem."""
    binary = fake_claude(tmp_path, json.dumps(CANNED))
    ws = tmp_path / "ws"
    ws.mkdir()
    result = ClaudeCodeHarness(api_key="k", binary=binary).run("task", ws)
    assert stat.S_IMODE(Path(result.transcript_path).stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "ws-home").stat().st_mode) == 0o700


@pytest.mark.skipif(__import__("os").geteuid() == 0, reason="root ignores mode bits")
def test_unwritable_parent_degrades_not_raises(tmp_path: Path) -> None:
    """The adapter never raises — even when the shared filesystem misbehaves."""
    binary = fake_claude(tmp_path, json.dumps(CANNED))
    parent = tmp_path / "ro"
    parent.mkdir()
    ws = parent / "ws"
    ws.mkdir()
    parent.chmod(0o500)
    try:
        result = ClaudeCodeHarness(api_key="k", binary=binary).run("task", ws)
    finally:
        parent.chmod(0o700)
    assert result.is_error
    # home creation is the first write; a read-only parent stops it
    assert result.stop_reason == "workspace-error"


def test_stray_trailing_json_does_not_hijack_the_result(tmp_path: Path) -> None:
    """An object printed after the CLI's result must not substitute its
    fields into billing and resume."""
    noisy = (
        json.dumps(CANNED)
        + "\n"
        + json.dumps({"session_id": "evil", "total_cost_usd": 9.99, "is_error": True})
    )
    binary = fake_claude(tmp_path, noisy)
    ws = tmp_path / "ws"
    ws.mkdir()
    result = ClaudeCodeHarness(api_key="k", binary=binary).run("task", ws)
    assert not result.is_error
    assert result.session_id == "sess-1"
    assert result.cost_usd == 0.42


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
    fake.run("wake", tmp_path, resume_session_id="sess-9")
    assert fake.calls == [
        ("brief text", str(tmp_path), None),
        ("wake", str(tmp_path), "sess-9"),
    ]


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


def test_container_image_wraps_in_apptainer(tmp_path: Path) -> None:
    """Containment: --containall/--cleanenv, only workspace + run-home bound,
    binary bind-mounted, key via APPTAINERENV_ (env, never argv)."""
    binary = fake_claude(tmp_path, json.dumps(CANNED))
    # the fake stands in for apptainer itself; it records argv + env
    ws = tmp_path / "ws"
    ws.mkdir()
    harness = ClaudeCodeHarness(
        api_key="sk-c",
        binary="/real/claude",
        container_image="/img/agent.sif",
        apptainer_binary=binary,
    )
    result = harness.run("task", ws)
    assert not result.is_error
    argv = (tmp_path / "seen_argv").read_text()
    assert argv.startswith("exec --containall --cleanenv")
    assert f"--bind {ws}:{ws}" in argv
    # --home, NOT --env HOME (apptainer silently refuses HOME via --env) and
    # NOT a plain --bind (per-run state must be $HOME inside the container)
    assert f"--home {tmp_path / 'ws-home'}:{tmp_path / 'ws-home'}" in argv
    assert "--env" not in argv
    assert "--bind /real/claude:/opt/agent/claude:ro" in argv
    assert "/img/agent.sif /opt/agent/claude -p" in argv
    assert "sk-c" not in argv  # the key travels via env, never argv
    seen_env = (tmp_path / "seen_env").read_text()
    assert "APPTAINERENV_ANTHROPIC_API_KEY=sk-c" in seen_env


def test_container_requires_absolute_binary(tmp_path: Path) -> None:
    """A relative bind source fails at mount time deep inside apptainer —
    catch the misconfiguration before any money is spent."""
    ws = tmp_path / "ws"
    ws.mkdir()
    harness = ClaudeCodeHarness(api_key="k", binary="claude", container_image="/img/agent.sif")
    result = harness.run("task", ws)
    assert result.is_error
    assert result.stop_reason == "config-error"


def test_no_container_means_no_apptainer(tmp_path: Path) -> None:
    binary = fake_claude(tmp_path, json.dumps(CANNED))
    ws = tmp_path / "ws"
    ws.mkdir()
    ClaudeCodeHarness(api_key="k", binary=binary).run("task", ws)
    argv = (tmp_path / "seen_argv").read_text()
    assert "apptainer" not in argv and "--containall" not in argv
    assert "APPTAINERENV_" not in (tmp_path / "seen_env").read_text()
