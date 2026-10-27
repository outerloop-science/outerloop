"""Harness adapter tests run against a fake `claude` executable — a script
that records its environment and argv, then prints canned JSON. No network,
no real key."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from fakes import FakeHarness
from helpers import wait_until
from outerloop.harness import (
    SESSION_ENV_ALLOWLIST,
    ClaudeCodeHarness,
    CodexHarness,
    SessionResult,
    author_effort,
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
    "usage": {"input_tokens": 100, "cache_read_input_tokens": 50, "output_tokens": 20},
    "result": "Report: hypothesis held.",
}


pytestmark = pytest.mark.usefixtures("codex_host")


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


def run_session(
    tmp_path: Path,
    payload: dict[str, Any] | str | None = None,
    brief: str = "task",
    *,
    api_key: str = "k",
    binary: str | None = None,
    sleep: int = 0,
    **options: Any,
) -> SessionResult:
    """Run one Claude session against a fake binary that records and replies."""
    text = payload if isinstance(payload, str) else json.dumps(payload or CANNED)
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ClaudeCodeHarness(
        api_key=api_key, binary=binary or fake_claude(tmp_path, text, sleep=sleep), **options
    ).run(brief, ws)


def seen(tmp_path: Path, name: str) -> str:
    return (tmp_path / name).read_text()


def seen_env(tmp_path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1) for line in seen(tmp_path, "seen_env").splitlines() if "=" in line
    )


def test_successful_session_parses_everything(tmp_path: Path) -> None:
    result = run_session(tmp_path, api_key="sk-test-123")
    assert not result.is_error
    assert result.cost_usd == 0.42
    assert result.tokens == {"input_tokens": 150, "cached_input_tokens": 50, "output_tokens": 20}
    assert result.num_turns == 3
    assert result.session_id == "sess-1"
    assert result.final_text == "Report: hypothesis held."
    assert Path(result.transcript_path).read_text().strip().startswith("{")


def test_session_env_is_scrubbed(tmp_path: Path, monkeypatch) -> None:
    """The threat model's credential-theft guard: no PAT, no stray secrets."""
    monkeypatch.setenv("OUTERLOOP_BOT_PAT", "github_pat_SECRET")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws_SECRET")
    run_session(tmp_path, api_key="sk-test-123")
    env = seen(tmp_path, "seen_env")
    assert "github_pat_SECRET" not in env
    assert "aws_SECRET" not in env
    assert "ANTHROPIC_API_KEY=sk-test-123" in env
    assert "PATH=" in env
    assert "CUDA_VISIBLE_DEVICES=" in env  # the session sees no GPU


@pytest.mark.parametrize(
    "key, oauth", [("sk-ant-oat01-secret", True), ("sk-ant-api03-secret", False)]
)
def test_key_prefix_selects_the_credential_channel(tmp_path: Path, key: str, oauth: bool) -> None:
    """A `claude setup-token` token is a subscription OAuth credential, so the
    CLI reads it from CLAUDE_CODE_OAUTH_TOKEN, never ANTHROPIC_API_KEY. The same
    prefix picks the judge isolation flag: --bare never reads an OAuth token, so
    a judge on a subscription token runs --safe-mode (which also skips CLAUDE.md,
    hooks, plugins and MCP)."""
    run_session(tmp_path, api_key=key, bare=True)
    env = seen(tmp_path, "seen_env").splitlines()
    present, absent = (
        ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
        if oauth
        else ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
    )
    assert f"{present}={key}" in env
    assert not any(line.startswith(f"{absent}=") for line in env)
    argv = seen(tmp_path, "seen_argv").split()
    flag, other = ("--safe-mode", "--bare") if oauth else ("--bare", "--safe-mode")
    assert flag in argv and other not in argv


def test_home_is_redirected_per_session(tmp_path: Path) -> None:
    """A session's HOME must not be the real home — `~`-relative key files
    (harness key, bot PAT, ssh) must resolve nowhere."""
    import os

    run_session(tmp_path)
    env = seen_env(tmp_path)
    assert env["HOME"] == str(tmp_path / "ws-home")
    assert env["HOME"] != os.environ.get("HOME")
    assert Path(env["HOME"]).is_dir()


def test_session_env_is_an_allowlist_with_no_gpu(tmp_path: Path, monkeypatch) -> None:
    """Only allowlisted variables cross into the session. GPU work goes through
    `launch`, where it is recorded and metered, so a host that exposes GPUs to
    the loop must not expose them to the session."""
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0,1")
    env = session_env("key", "ANTHROPIC_API_KEY", home=tmp_path)
    assert set(env) <= set(SESSION_ENV_ALLOWLIST) | {
        "ANTHROPIC_API_KEY",
        "HOME",
        "CUDA_VISIBLE_DEVICES",
    }
    assert env["HOME"] == str(tmp_path)
    assert env["CUDA_VISIBLE_DEVICES"] == ""


def test_argv_carries_the_controls(tmp_path: Path) -> None:
    run_session(tmp_path, brief="the brief", max_turns=7)
    argv = seen(tmp_path, "seen_argv")
    assert "--max-turns 7" in argv
    assert "--output-format json" in argv
    assert "--permission-mode acceptEdits" in argv


@pytest.mark.parametrize("effort", ["", "low"])
def test_effort_reaches_argv_only_when_set(tmp_path: Path, effort: str) -> None:
    run_session(tmp_path, effort=effort)
    argv = seen(tmp_path, "seen_argv").split()
    assert ("--effort" in argv) is bool(effort)
    if effort:
        assert argv[argv.index("--effort") + 1] == effort


@pytest.mark.parametrize(
    "env, expected",
    [
        ({}, ""),
        ({"OUTERLOOP_AUTHOR_EFFORT": " "}, ""),
        ({"OUTERLOOP_AUTHOR_EFFORT": " XHigh "}, "xhigh"),
    ],
)
def test_author_effort_reads_the_setting(env: dict[str, str], expected: str) -> None:
    assert author_effort(env) == expected


def test_author_effort_refuses_an_unknown_level() -> None:
    with pytest.raises(ValueError, match="OUTERLOOP_AUTHOR_EFFORT must be one of"):
        author_effort({"OUTERLOOP_AUTHOR_EFFORT": "minimal"})


def test_brief_travels_on_stdin_never_argv(tmp_path: Path) -> None:
    """argv is world-readable via /proc on shared nodes; briefs carry private
    research text."""
    run_session(tmp_path, brief="SECRET research brief")
    assert "SECRET research brief" not in seen(tmp_path, "seen_argv")
    assert seen(tmp_path, "seen_stdin") == "SECRET research brief"


def test_transcript_is_stored_outside_the_workspace_owner_only(tmp_path: Path) -> None:
    """The workspace is a git clone that gets committed and pushed, so the
    transcript (private research text on a shared filesystem) must live outside
    it, readable by its owner alone."""
    result = run_session(tmp_path)
    transcript = Path(result.transcript_path).resolve()
    assert (tmp_path / "ws").resolve() not in transcript.parents
    assert transcript.exists()
    assert stat.S_IMODE(transcript.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "ws-home").stat().st_mode) == 0o700


def test_malformed_fields_are_salvaged_not_discarded(tmp_path: Path) -> None:
    """A quirky cost value must not cost us the session id (resume depends
    on it) — field-level salvage, and never an exception."""
    result = run_session(tmp_path, dict(CANNED, total_cost_usd="not-a-number"))
    assert result.cost_usd is None
    assert result.session_id == "sess-1"
    assert result.num_turns == 3
    assert not result.is_error


def test_result_object_recovered_from_surrounding_log_lines(tmp_path: Path) -> None:
    noisy = "warning: something\n" + json.dumps(CANNED) + "\ntrailing line"
    result = run_session(tmp_path, noisy)
    assert not result.is_error
    assert result.cost_usd == 0.42


def test_timeout_is_budget_exhaustion_with_error_result(tmp_path: Path) -> None:
    result = run_session(tmp_path, sleep=30, timeout_s=1)
    assert result.is_error
    assert result.stop_reason == "timeout"
    assert budget_exhausted(result)
    assert "walltime" in result.error_detail


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
    result = run_session(tmp_path, payload)
    assert result.is_error
    assert result.error_detail == "error_max_turns: Reached maximum number of turns (120)"
    assert budget_exhausted(result)


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
    Observed on the cluster: a session hit the workspace usage cap this exact way."""
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
    res = run_session(tmp_path, payload)
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
    res = run_session(tmp_path, payload)
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
    res = run_session(tmp_path, payload)
    assert res.is_error
    # the real backend cause is kept, not replaced by the result text
    assert "overloaded_error" in res.error_detail
    assert not res.error_detail.startswith("API Error")
    assert outage(res)  # classified from the real cause (overloaded_error)


def test_parse_does_not_lift_a_real_subtype_so_agent_prose_cannot_latch(tmp_path: Path) -> None:
    """The lift is scoped to the observed "success"/empty contradiction. For a
    REAL subtype (error_during_execution), `result` may be the agent's own
    closing message, so a message that merely starts "API Error" must NOT be
    lifted — a false outage that pauses every lane is worse than a rare
    mis-scoped one. The subtype is kept and the latch does not fire on prose."""
    payload = json.dumps(
        {
            "is_error": True,
            "subtype": "error_during_execution",
            "result": "API Error: 429 the agent quoted while writing its report.",
        }
    )
    res = run_session(tmp_path, payload)
    assert res.error_detail.startswith("error_during_execution")  # NOT lifted
    assert not outage(res)  # agent prose does not trip the latch


def test_parse_captures_a_non_list_errors_cause_and_skips_the_lift(tmp_path: Path) -> None:
    """A real backend cause in a non-list `errors` form (a dict) is still
    captured into the detail, so the lift does not fire and does not leave a
    bare "success" — classification comes from the real cause, not the result."""
    payload = json.dumps(
        {
            "is_error": True,
            "subtype": "success",
            "errors": {"type": "overloaded_error", "message": "temporarily overloaded"},
            "result": "API Error: 400 something the agent echoed",
        }
    )
    res = run_session(tmp_path, payload)
    assert "overloaded_error" in res.error_detail  # non-list cause captured
    assert not res.error_detail.startswith("API Error")  # lift skipped
    assert outage(res)  # classified from the real cause


def test_clean_sessions_and_real_failures_are_not_budget_endings(tmp_path: Path) -> None:
    ok = run_session(tmp_path)
    assert ok.error_detail == "" and not budget_exhausted(ok)
    dead = ClaudeCodeHarness(api_key="k", binary=str(tmp_path / "nope")).run(
        "task", tmp_path / "ws"
    )
    assert dead.is_error
    assert dead.stop_reason == "spawn-error"
    assert "nope" in dead.error_detail  # #294: the report names what failed to spawn
    assert not budget_exhausted(dead)


def test_garbage_output_is_an_error_with_transcript(tmp_path: Path) -> None:
    result = run_session(tmp_path, "not json at all")
    assert result.is_error
    assert result.stop_reason == "unparseable-output"
    assert "not json" in Path(result.transcript_path).read_text()


def test_transcript_write_refuses_symlinks(tmp_path: Path) -> None:
    """A dangling symlink planted at the transcript name must not redirect
    the write to another same-user file."""
    import os as _os

    target = tmp_path / "victim"
    _os.symlink(target, tmp_path / "ws-session.json")
    result = run_session(tmp_path)
    assert not target.exists()  # the symlink was not followed
    assert result.transcript_path.endswith("ws-session-2.json")


def test_transcript_is_redacted(tmp_path: Path) -> None:
    """A session that echoes its own key must not leak it into storage."""
    leaked = dict(CANNED, result="the key is sk-leak-me-456 whoops")
    result = run_session(tmp_path, leaked, api_key="sk-leak-me-456")
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
    def gone() -> bool:
        try:
            _os.kill(child, 0)
        except ProcessLookupError:
            return True
        return False

    assert wait_until(gone, interval=0.05), f"child {child} survived the group kill"


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
    result = run_session(tmp_path, noisy)
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
    """A wake must restore the run's context: --resume in argv, same HOME. A
    fresh session carries no --resume."""
    binary = fake_claude(tmp_path, json.dumps(CANNED))
    ws = tmp_path / "ws"
    ws.mkdir()
    harness = ClaudeCodeHarness(api_key="k", binary=binary)
    harness.run("initial brief", ws)
    assert "--resume" not in seen(tmp_path, "seen_argv")
    first_home = seen_env(tmp_path)["HOME"]
    harness.run("wake update", ws, resume_session_id="sess-1")
    assert "--resume sess-1" in seen(tmp_path, "seen_argv")
    assert seen_env(tmp_path)["HOME"] == first_home


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


@pytest.mark.parametrize("configured", [False, True])
def test_container_image_wraps_in_apptainer(tmp_path: Path, monkeypatch, configured) -> None:
    """Containment: --containall/--cleanenv, only workspace + run-home bound,
    binary bind-mounted, key via APPTAINERENV_ (env, never argv)."""
    binary = fake_claude(tmp_path, json.dumps(CANNED))
    # the fake stands in for apptainer itself; it records argv + env
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("OUTERLOOP_APPTAINER_BIN", binary if configured else "/wrong/path")
    options: dict[str, Any] = {} if configured else {"apptainer_binary": binary}
    harness = ClaudeCodeHarness(
        api_key="sk-c",
        binary="/real/claude",
        container_image="/img/agent.sif",
        **options,
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
    assert "APPTAINERENV_ANTHROPIC_API_KEY=sk-c" in seen(tmp_path, "seen_env")


def _recording_apptainer(tmp_path: Path, payload: str) -> tuple[str, Path, Path]:
    """A fake apptainer that APPENDS every invocation's argv+env to logs, so a
    test can assert BOTH codex calls (login then exec) — not just the last."""
    calls, envs = tmp_path / "calls.log", tmp_path / "envs.log"
    script = tmp_path / "fake-apptainer"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {calls}\n'
        f"env | grep APPTAINERENV >> {envs}\n"
        f"cat <<'EOF'\n{payload}\nEOF\n"
        "exit 0\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script), calls, envs


@pytest.mark.parametrize("configured", [False, True])
def test_codex_author_runs_contained_in_apptainer(tmp_path: Path, monkeypatch, configured) -> None:
    """AUTHOR mode: BOTH codex login and codex exec run inside apptainer
    (--containall/--cleanenv), so neither exposes the author key to the host —
    login is contained too, not only the exec. The host codex binary is
    bind-mounted read-only to /opt/agent/codex (like claude); exec binds the
    workspace + --pwd, passes the given --sandbox, key via APPTAINERENV_
    (never argv)."""
    binary, calls, envs = _recording_apptainer(tmp_path, json.dumps(CANNED))
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("OUTERLOOP_APPTAINER_BIN", binary if configured else "/wrong/path")
    options: dict[str, Any] = {} if configured else {"apptainer_binary": binary}
    harness = CodexHarness(
        api_key="sk-o",
        binary="/real/codex",
        model="gpt-5.6-terra",
        sandbox="danger-full-access",
        container_image="/img/agent.sif",
        **options,
    )
    harness.run("task", ws)
    logged = calls.read_text()
    login = next(ln for ln in logged.splitlines() if "codex login" in ln)
    exec_ = next(ln for ln in logged.splitlines() if "codex exec" in ln)
    home = f"--home {tmp_path / 'ws-home'}:{tmp_path / 'ws-home'}"
    codex_bind = "--bind /real/codex:/opt/agent/codex:ro"  # host codex, read-only
    # the LOGIN is contained too (the finding: an uncontained login leaks the key)
    assert login.startswith("exec --containall --cleanenv")
    assert home in login and codex_bind in login
    assert "/img/agent.sif /opt/agent/codex login --with-api-key" in login
    assert f"--bind {ws}" not in login  # login needs no workspace
    # the EXEC is contained, binds+pwd the workspace, author sandbox, bound codex
    assert exec_.startswith("exec --containall --cleanenv")
    assert f"--bind {ws}:{ws}" in exec_ and f"--pwd {ws}" in exec_ and home in exec_
    assert codex_bind in exec_
    assert "/img/agent.sif /opt/agent/codex exec" in exec_
    assert "--sandbox danger-full-access" in exec_
    # the key is never in ANY argv, and rides APPTAINERENV for both calls
    assert "sk-o" not in logged
    assert envs.read_text().count("APPTAINERENV_OPENAI_API_KEY=sk-o") == 2


def test_codex_command_resume_uses_bypass_not_sandbox_cd(tmp_path: Path) -> None:
    """`codex exec resume` has neither --sandbox nor --cd (passing them is an
    argparse error, verified on 0.160.0). A fresh exec keeps --sandbox/--cd. On
    resume the recorded session's sandbox is inherited: the author adds the
    bypass flag (to also skip approvals), a read-only reader (the structured-
    output repair turn also resumes) adds NO sandbox flag and stays read-only by
    inheritance — verified on 0.130.0 that a resumed read-only session still
    refuses writes."""
    from outerloop.harness import _codex_command

    ws = tmp_path / "ws"
    last = tmp_path / "last.txt"
    fresh = _codex_command("codex", "gpt-5.6-terra", "danger-full-access", ws, last, None, ())
    assert "resume" not in fresh
    assert "--sandbox" in fresh and "danger-full-access" in fresh
    assert "--cd" in fresh and str(ws) in fresh
    assert "--dangerously-bypass-approvals-and-sandbox" not in fresh

    author = _codex_command("codex", "gpt-5.6-terra", "danger-full-access", ws, last, "sess-7", ())
    assert author[:4] == ["codex", "exec", "resume", "sess-7"]
    assert "--dangerously-bypass-approvals-and-sandbox" in author
    assert "--sandbox" not in author  # rejected by `exec resume`
    assert "--cd" not in author  # rejected by `exec resume`
    assert "--json" in author and "--output-last-message" in author

    # a read-only reader resume: NO bypass (that would unjail it), NO --sandbox
    # (rejected) — read-only is inherited from the recorded session
    reader = _codex_command("codex", "", "read-only", ws, last, "sess-7", ())
    assert reader[:4] == ["codex", "exec", "resume", "sess-7"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in reader
    assert "--sandbox" not in reader and "--cd" not in reader


def test_codex_author_resumes_contained(tmp_path: Path) -> None:
    """A contained resume runs `codex exec resume <id>` inside apptainer with the
    same bound --home (so the recorded session is visible) and the bypass flag,
    never --sandbox/--cd."""
    binary, calls, _envs = _recording_apptainer(tmp_path, json.dumps(CANNED))
    ws = tmp_path / "ws"
    ws.mkdir()
    harness = CodexHarness(
        api_key="sk-o",
        binary="/real/codex",
        model="gpt-5.6-terra",
        sandbox="danger-full-access",
        container_image="/img/agent.sif",
        apptainer_binary=binary,
    )
    assert harness.supports_resume is True
    harness.run("revise per the finding", ws, resume_session_id="01a0-thread")
    exec_ = next(ln for ln in calls.read_text().splitlines() if "codex exec resume" in ln)
    home = f"--home {tmp_path / 'ws-home'}:{tmp_path / 'ws-home'}"
    assert "/img/agent.sif /opt/agent/codex exec resume 01a0-thread" in exec_
    assert home in exec_  # same run-home -> recorded session is visible
    assert "--dangerously-bypass-approvals-and-sandbox" in exec_
    assert "--sandbox" not in exec_ and "--cd" not in exec_


def test_codex_author_resolves_a_relative_workspace(tmp_path: Path, monkeypatch) -> None:
    """apptainer bind sources must be absolute; a relative workspace is resolved
    (else the mount fails deep inside apptainer)."""
    binary = fake_claude(tmp_path, json.dumps(CANNED))
    (tmp_path / "ws").mkdir()
    monkeypatch.chdir(tmp_path)
    harness = CodexHarness(
        api_key="k",
        binary="/real/codex",  # absolute (bind source)
        sandbox="workspace-write",
        container_image="/img/agent.sif",
        apptainer_binary=binary,
    )
    harness.run("task", Path("ws"))  # relative
    argv = (tmp_path / "seen_argv").read_text()
    assert f"--bind {tmp_path / 'ws'}:{tmp_path / 'ws'}" in argv  # absolute
    assert "--bind ws:ws" not in argv


@pytest.mark.parametrize("backend", ["claude", "codex"])
def test_contained_harness_requires_an_absolute_binary(tmp_path: Path, backend: str) -> None:
    """A relative bind source fails at mount time deep inside apptainer —
    catch the misconfiguration before any money is spent, on any backend."""
    ws = tmp_path / "ws"
    ws.mkdir()
    harness: ClaudeCodeHarness | CodexHarness
    if backend == "claude":
        harness = ClaudeCodeHarness(api_key="k", binary=backend, container_image="/img/agent.sif")
    else:
        harness = CodexHarness(
            api_key="k",
            binary=backend,
            container_image="/img/agent.sif",
            apptainer_binary="apptainer",
        )
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


def test_vertex_mode_swaps_the_key_for_adc_env(tmp_path: Path, monkeypatch) -> None:
    # vertex on: the session env carries the Vertex triple + ADC path and NO
    # ANTHROPIC_API_KEY (the CLI must not silently fall back to direct billing)
    from outerloop.harness import ClaudeCodeHarness, VertexConfig

    captured = {}

    def fake_popen(command, cwd, env, **kw):
        captured["command"] = command
        captured["env"] = env
        raise OSError("stop here")  # run() contains it as an error result

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    h = ClaudeCodeHarness(
        api_key="sk-should-not-appear",
        vertex=VertexConfig(project="p-123", region="global", adc_file=str(tmp_path / "adc.json")),
    )
    result = h.run("brief", tmp_path)
    assert result.is_error  # the fake Popen stopped the run
    env = captured["env"]
    assert env["CLAUDE_CODE_USE_VERTEX"] == "1"
    assert env["ANTHROPIC_VERTEX_PROJECT_ID"] == "p-123"
    assert env["CLOUD_ML_REGION"] == "global"
    assert env["GOOGLE_APPLICATION_CREDENTIALS"] == str(tmp_path / "adc.json")
    assert "ANTHROPIC_API_KEY" not in env


@pytest.mark.parametrize("small_model", ["", "configured-small-model"])
@pytest.mark.parametrize("contained", [False, True])
def test_vertex_small_fast_model_defaults_to_session_model(
    tmp_path: Path, monkeypatch, small_model: str, contained: bool
) -> None:
    from outerloop.harness import VertexConfig

    captured = {}

    def fake_popen(command, cwd, env, **kw):
        captured["env"] = env
        raise OSError("stop here")

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    vertex = VertexConfig(project="p-123", small_model=small_model)
    # the config exports the slot only when set; an empty value would tell the
    # CLI to use no model at all, so the harness fills it from the session model
    assert vertex.env().get("ANTHROPIC_SMALL_FAST_MODEL") == (small_model or None)
    h = ClaudeCodeHarness(
        api_key="",
        binary="/abs/claude",
        model="session-model",
        container_image="/img.sif" if contained else "",
        vertex=vertex,
    )
    assert h.run("brief", tmp_path).is_error
    expected = small_model or h.model
    assert captured["env"]["ANTHROPIC_SMALL_FAST_MODEL"] == expected
    if contained:
        assert captured["env"]["APPTAINERENV_ANTHROPIC_SMALL_FAST_MODEL"] == expected


def test_vertex_contained_session_binds_the_adc_file(tmp_path: Path, monkeypatch) -> None:
    from outerloop.harness import ClaudeCodeHarness, VertexConfig

    adc = tmp_path / "adc.json"
    adc.write_text("{}")
    captured = {}

    def fake_popen(command, cwd, env, **kw):
        captured["command"] = command
        captured["env"] = env
        raise OSError("stop here")

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    h = ClaudeCodeHarness(
        api_key="",
        binary="/abs/claude",
        container_image="/img.sif",
        vertex=VertexConfig(project="p-123", adc_file=str(adc)),
    )
    h.run("brief", tmp_path)
    cmd = captured["command"]
    assert f"{adc.resolve()}:{ClaudeCodeHarness.CONTAINER_ADC}:ro" in cmd
    env = captured["env"]
    # inside the container the credential path is the BOUND path
    assert env["APPTAINERENV_GOOGLE_APPLICATION_CREDENTIALS"] == ClaudeCodeHarness.CONTAINER_ADC
    assert env["APPTAINERENV_CLAUDE_CODE_USE_VERTEX"] == "1"
    assert "APPTAINERENV_ANTHROPIC_API_KEY" not in env


@pytest.mark.parametrize("small_model", [None, " configured-small-model "])
def test_vertex_from_env_is_the_single_owner(monkeypatch, small_model) -> None:
    from outerloop.harness import vertex_from_env

    monkeypatch.delenv("OUTERLOOP_VERTEX_PROJECT", raising=False)
    assert vertex_from_env() is None
    monkeypatch.setenv("OUTERLOOP_VERTEX_PROJECT", "p-9")
    monkeypatch.setenv("OUTERLOOP_VERTEX_ADC", "~/adc.json")
    if small_model is None:
        monkeypatch.delenv("OUTERLOOP_VERTEX_SMALL_MODEL", raising=False)
    else:
        monkeypatch.setenv("OUTERLOOP_VERTEX_SMALL_MODEL", small_model)
    cfg = vertex_from_env()
    assert cfg is not None and cfg.project == "p-9" and cfg.region == "global"
    assert cfg.adc_file.endswith("/adc.json") and not cfg.adc_file.startswith("~")
    assert cfg.small_model == (small_model or "").strip()


def test_vertex_from_env_resolves_ambient_adc_under_the_real_home(tmp_path, monkeypatch) -> None:
    # sessions get a scrubbed per-run HOME, so the gcloud default must be
    # resolved to an explicit path at config time, under the REAL home
    from outerloop.harness import vertex_from_env

    default = tmp_path / ".config" / "gcloud" / "application_default_credentials.json"
    default.parent.mkdir(parents=True)
    default.write_text("{}")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OUTERLOOP_VERTEX_PROJECT", "p-9")
    monkeypatch.delenv("OUTERLOOP_VERTEX_ADC", raising=False)
    cfg = vertex_from_env()
    assert cfg is not None and cfg.adc_file == str(default)


def test_role_key_tolerates_a_missing_file_only_under_vertex(tmp_path, monkeypatch) -> None:
    from outerloop.role_runner import role_key

    missing = tmp_path / "no-such-key"
    monkeypatch.delenv("OUTERLOOP_VERTEX_PROJECT", raising=False)
    with pytest.raises(ValueError):
        role_key(missing)  # no vertex: loud, as ever
    monkeypatch.setenv("OUTERLOOP_VERTEX_PROJECT", "p-9")
    assert role_key(missing) == ""  # ADC-only claude deployment
    with pytest.raises(ValueError):
        role_key(missing, "codex")  # vertex never excuses a non-claude backend


@pytest.mark.parametrize("backend", ["claude", "codex"])
def test_default_binary_prefers_the_recorded_path(monkeypatch, tmp_path, backend) -> None:
    from outerloop.harness import default_binary

    key = f"OUTERLOOP_{backend.upper()}_BIN"
    monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))  # not the runner's own ~/.local/bin
    assert default_binary(backend) == backend
    binary = tmp_path / backend
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    assert default_binary(backend) == str(binary.resolve())
    monkeypatch.setenv(key, f"/opt/bin/{backend}")
    assert default_binary(backend) == f"/opt/bin/{backend}"
    monkeypatch.setenv(key, f"~/tools/{backend}")
    assert default_binary(backend) == str(Path.home() / "tools" / backend)


@pytest.mark.parametrize("backend", ["claude", "codex"])
def test_default_binary_falls_back_to_the_native_install_dir(monkeypatch, tmp_path, backend):
    """A Slurm job with a bare PATH still finds a CLI the native installer put
    under ~/.local/bin; a recorded path or a PATH hit wins over it."""
    from outerloop.harness import default_binary

    monkeypatch.delenv(f"OUTERLOOP_{backend.upper()}_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path))
    local = tmp_path / ".local" / "bin" / backend
    local.parent.mkdir(parents=True)
    local.write_text("#!/bin/sh\n")
    assert default_binary(backend) == backend  # not executable yet
    local.chmod(0o755)
    assert default_binary(backend) == str(local)
    assert default_binary(backend, {"PATH": ""}) == backend  # no HOME given: no guess


def test_no_lane_hardcodes_the_harness_path() -> None:
    """The three job parsers take their binary defaults from default_binary; a
    literal ~/.local/bin/<cli> in any of them would ignore what init recorded."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "outerloop"
    for module in ("attempt.py", "steward.py"):
        text = (src / module).read_text()
        assert "~/.local/bin/claude" not in text and "~/.local/bin/codex" not in text, module
