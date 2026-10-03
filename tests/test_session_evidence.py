"""Synthetic evidence only: no real session content or credentials."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from outerloop.harness import SessionResult, _parse_codex_result, _parse_hermes_result
from outerloop.runstate import RunRecord
from outerloop.session_evidence import (
    active,
    capture_session,
    native_path,
    number,
    price,
    read_bounded,
    restore_session,
    session_totals,
    usage,
)


@pytest.mark.parametrize(
    "backend,data,expected",
    [
        (
            "claude",
            {
                "input_tokens": 50,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 20,
                "output_tokens": 12,
            },
            {
                "input_tokens": 170,
                "cached_input_tokens": 100,
                "cache_creation_input_tokens": 20,
                "output_tokens": 12,
            },
        ),
        (
            "codex",
            {"input_tokens": 170, "cached_input_tokens": 100, "output_tokens": 12},
            {"input_tokens": 170, "cached_input_tokens": 100, "output_tokens": 12},
        ),
        (
            "hermes",
            {
                "prompt_tokens": 170,
                "prompt_tokens_details": {"cached_tokens": 100},
                "completion_tokens": 12,
            },
            {"input_tokens": 170, "cached_input_tokens": 100, "output_tokens": 12},
        ),
    ],
)
def test_usage(backend, data, expected):
    assert usage(data, backend) == expected
    assert usage(None, backend) == {}
    assert usage({"input_tokens": -1, "output_tokens": "bad"}, backend) == {}


def test_backend_parsers():
    events = [
        {"type": "thread.started", "thread_id": "session-1"},
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 12},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 30, "cached_input_tokens": 10, "output_tokens": 8},
        },
    ]
    result = _parse_codex_result("\n".join(map(json.dumps, events)), "done", 0)
    assert result.tokens == {"input_tokens": 130, "cached_input_tokens": 60, "output_tokens": 20}
    assert result.cost_usd is None
    assert result.num_turns == 2
    sample = {
        "messages": [{"role": "assistant", "content": "Synthetic reply"}],
        "usage": {"input_tokens": 170, "cached_input_tokens": 100, "output_tokens": 12},
    }
    result = _parse_hermes_result("", sample, 0)
    assert result.tokens == sample["usage"]
    assert result.cost_usd is None


def test_prices_unknown_zero_and_cached(monkeypatch):
    tokens = {"input_tokens": 1_000_000, "cached_input_tokens": 200_000, "output_tokens": 100_000}
    monkeypatch.delenv("OUTERLOOP_TOKEN_PRICES", raising=False)
    assert price("model", tokens) is None
    monkeypatch.setenv(
        "OUTERLOOP_TOKEN_PRICES",
        json.dumps({"model": {"input_tokens": 2, "cached_input_tokens": 0.5, "output_tokens": 10}}),
    )
    assert price("model", tokens) == pytest.approx(2.7)
    monkeypatch.setenv(
        "OUTERLOOP_TOKEN_PRICES",
        json.dumps({"model": {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}}),
    )
    assert price("model", tokens) == 0.0
    assert price("model", {}) is None
    for bad in ("[]", "null", "{", '{"model": {"input_tokens": -1}}'):
        monkeypatch.setenv("OUTERLOOP_TOKEN_PRICES", bad)
        assert price("model", tokens) is None


def fake_run(backend, ws, native, prompt="brief", resume=None):
    class Adapter:
        api_key = "synthetic-secret"
        model = "test-model"

        @capture_session(backend)
        def run(self, brief, workspace, resume_session_id=None):
            state = active.get()
            assert state is not None
            if backend == "hermes":
                state["native_id"] = "session-1"
                state["prompt"] = "Prior context\n" + brief if resume_session_id else brief
            capture = workspace.parent / "capture.json"
            capture.write_text("{}")
            return SessionResult("completed", False, 0.42, 2, "session-1", "done", str(capture))

    return Adapter().run(prompt, ws, resume)


@pytest.mark.parametrize(
    "backend,relative",
    [
        ("claude", ".claude/projects/project/session-1.jsonl"),
        ("codex", ".codex/sessions/2026/10/02/rollout-date-session-1.jsonl"),
        ("hermes", "evidence-session-1.json"),
    ],
)
def test_evidence_redaction_hash_cap_resume_and_retention(tmp_path, monkeypatch, backend, relative):
    ws = tmp_path / "ws"
    ws.mkdir()
    home = tmp_path / "ws-home"
    native = home / relative
    native.parent.mkdir(parents=True)
    native.write_text("prefix synthetic-secret tail " + "x" * 200)
    monkeypatch.setenv("OUTERLOOP_NATIVE_LOG_MAX_BYTES", "32")
    first = fake_run(backend, ws, native, "brief synthetic-secret")
    second = fake_run(backend, ws, native, "wake synthetic-secret", "session-1")
    assert first.session_record_path != second.session_record_path
    record = json.loads(Path(second.session_record_path).read_text())
    assert record["session_id"] == "session-1"
    assert record["resume_session_id"] == "session-1"
    assert record["artifacts"]["native"]["truncated"]
    assert record["started_at"] <= record["ended_at"]
    for artifact in record["artifacts"].values():
        path = Path(artifact["path"])
        payload = path.read_bytes()
        assert b"synthetic-secret" not in payload
        assert hashlib.sha256(payload).hexdigest() == artifact["sha256"]
        if path != tmp_path / "capture.json":
            assert path.stat().st_mode & 0o777 == 0o600
    assert Path(second.prompt_path).read_text().endswith("wake [redacted]")
    assert Path(record["artifacts"]["native"]["path"]).stat().st_size <= 32
    # Exercise real housekeeping: only ws and ws-home are shed.
    import outerloop.housekeeping as housekeeping
    from outerloop.housekeeping import shed_workspace

    monkeypatch.setattr(housekeeping, "run_dir", lambda *args: tmp_path)
    monkeypatch.setattr(housekeeping, "mark_workspace_shed", lambda *args: None)
    assert shed_workspace(tmp_path, RunRecord("synthetic", "owner/repo", "test", "ended"), 100)
    assert not home.exists()
    assert Path(second.session_record_path).exists()
    assert all(Path(a["path"]).exists() for a in record["artifacts"].values())


def test_missing_native_and_capture_failure(tmp_path, monkeypatch):
    result = fake_run("codex", tmp_path / "ws", None)
    data = json.loads(Path(result.session_record_path).read_text())
    assert data["artifacts"]["native"]["status"] == "missing"
    import outerloop.session_evidence as evidence

    monkeypatch.setattr(evidence, "save", lambda *a: (_ for _ in ()).throw(OSError("full")))
    result = fake_run("codex", tmp_path / "ws", None)
    assert not result.is_error


def test_native_rejects_symlinks_and_wrong_id(tmp_path):
    path = tmp_path / "real"
    path.write_text("private")
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        read_bounded(link, 10)
    directory = tmp_path / "dir"
    directory.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        read_bounded(directory / "real", 10)
    assert native_path(tmp_path, "codex", "../real") is None
    assert native_path(tmp_path, "claude", "different-session") is None


def test_legacy_and_unknown_totals(tmp_path):
    legacy = json.loads((Path(__file__).parent / "fixtures/session_cost_legacy.json").read_text())
    restored = restore_session(legacy, "old-session")
    assert restored.cost_usd == 0.42
    assert restored.num_turns == 3
    assert restored.tokens == {}
    assert restored.session_record_path == ""
    assert restored.final_text == "Synthetic legacy report"
    assert restore_session({"session_cost_usd": None}, "new").cost_usd is None
    assert restore_session({"session_cost_usd": 0}, "new").cost_usd == 0
    assert number(legacy.get("session_cost_usd")) == 0.42
    assert usage(legacy.get("session_tokens"), "codex") == {}
    assert session_totals(tmp_path)["session_cost_usd"] is None
    (tmp_path / "one.session.json").write_text('{"cost_usd":0}')
    assert session_totals(tmp_path)["session_cost_usd"] == 0
    (tmp_path / "two.session.json").write_text('{"cost_usd":null}')
    (tmp_path / "three.session.json").write_text('{"cost_usd":0.42}')
    totals = session_totals(tmp_path)
    assert totals["session_cost_usd"] is None
    assert totals["known_session_cost_usd"] == 0.42
    assert totals["unpriced_sessions"] == 1


def test_hermes_wrapper_keeps_reported_usage_and_full_messages(tmp_path, monkeypatch, capsys):
    import sys
    from types import ModuleType

    from outerloop import hermes_capture

    module = ModuleType("run_agent")

    class Agent:
        model = "test-model"
        session_id = "native-id"
        session_prompt_tokens = 170
        session_completion_tokens = 12
        session_cache_read_tokens = 100
        session_cache_write_tokens = 0
        _last_turn_usage = True

        def run_conversation(self, query):
            if query == "Synthetic prompt":
                Agent().run_conversation("Synthetic child prompt")
            return {
                "messages": [
                    {"role": "user", "content": query},
                    {"role": "tool", "content": "Synthetic tool result"},
                    {"role": "assistant", "content": "Synthetic reply"},
                ],
                "completed": True,
            }

    module.__dict__["AIAgent"] = Agent
    module.__dict__["main"] = lambda: Agent().run_conversation("Synthetic prompt")
    cli = ModuleType("agent.legacy_cli")

    def cli_main(run):
        result = run()
        assert result["messages"][-1]["content"] == "Synthetic reply"
        return 0

    cli.__dict__["main"] = cli_main
    monkeypatch.setitem(sys.modules, "run_agent", module)
    monkeypatch.setitem(sys.modules, "agent", ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.legacy_cli", cli)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OUTERLOOP_CAPTURE_ID", "session-1-invocation-1")
    monkeypatch.setattr(sys, "argv", ["capture.py", str(tmp_path)])
    with pytest.raises(SystemExit) as end:
        hermes_capture.main()
    assert end.value.code == 0
    data = json.loads((tmp_path / "evidence-session-1-invocation-1.json").read_text())
    assert data["native_session_id"] == "native-id"
    assert data["messages"][0]["content"] == "Synthetic prompt"
    assert data["messages"][1]["role"] == "tool"
    result = _parse_hermes_result(capsys.readouterr().out, data, 0)
    assert result.tokens["input_tokens"] == 170
    assert result.tokens["cached_input_tokens"] == 100
    assert result.tokens["output_tokens"] == 12


def test_redaction_at_cap_boundary_and_issued_tokens(tmp_path, monkeypatch):
    import outerloop.appauth as appauth

    native = tmp_path / "ws-home/.claude/projects/project/session-1.jsonl"
    native.parent.mkdir(parents=True)
    native.write_text("prefix synthetic-secret and synthetic-issued-token")
    monkeypatch.setattr(appauth, "issued_tokens", lambda: ("synthetic-issued-token",))
    monkeypatch.setenv("OUTERLOOP_NATIVE_LOG_MAX_BYTES", "10")
    result = fake_run("claude", tmp_path / "ws", native)
    artifact = json.loads(Path(result.session_record_path).read_text())["artifacts"]["native"]
    assert Path(artifact["path"]).read_text() == "prefix [re"
    assert artifact["truncated"]
    monkeypatch.setenv("OUTERLOOP_NATIVE_LOG_MAX_BYTES", "1000")
    result = fake_run("claude", tmp_path / "ws", native)
    artifact = json.loads(Path(result.session_record_path).read_text())["artifacts"]["native"]
    assert Path(artifact["path"]).read_text() == "prefix [redacted] and [redacted]"
    assert not artifact["truncated"]


def test_native_lookup_ignores_other_sessions(tmp_path):
    correct = tmp_path / ".codex/sessions/2026/10/01/rollout-time-session-1.jsonl"
    correct.parent.mkdir(parents=True)
    correct.write_text("{}")
    unrelated = correct.with_name("rollout-newer-other-session.jsonl")
    unrelated.write_text("{}")
    assert native_path(tmp_path, "codex", "session-1") == correct
    duplicate = correct.with_name("rollout-other-session-1.jsonl")
    duplicate.write_text("{}")
    assert native_path(tmp_path, "codex", "session-1") is None


def test_secret_prefix_after_shrinking_replacements(tmp_path, monkeypatch):
    secret = "synthetic-secret"
    cap = 40
    native = tmp_path / "ws-home/evidence-session-1.json"
    native.parent.mkdir()
    # The fourth secret crosses the read boundary (cap + lookahead), while
    # redacting the first three pulls its prefix inside the output cap.
    native.write_text(secret * 4 + " trailing content")
    monkeypatch.setenv("OUTERLOOP_NATIVE_LOG_MAX_BYTES", str(cap))
    result = fake_run("hermes", tmp_path / "ws", native)
    artifact = json.loads(Path(result.session_record_path).read_text())["artifacts"]["native"]
    retained = Path(artifact["path"]).read_text()
    assert retained == "[redacted]" * 3
    assert all(secret[:size] not in retained for size in range(1, len(secret) + 1))
    assert artifact["truncated"]


@pytest.mark.parametrize("limit", ["depth", "entries", "time"])
def test_native_discovery_budget_records_unavailable(tmp_path, monkeypatch, limit):
    import outerloop.session_evidence as evidence

    root = tmp_path / "ws-home/.codex/sessions"
    root.mkdir(parents=True)
    for index in range(5):
        (root / f"unrelated-{index}").mkdir()
    if limit == "depth":
        monkeypatch.setattr(evidence, "NATIVE_MAX_DEPTH", 0)
    elif limit == "entries":
        monkeypatch.setattr(evidence, "NATIVE_MAX_ENTRIES", 2)
    else:
        ticks = iter([0.0, 1.0])
        monkeypatch.setattr(evidence.time, "monotonic", lambda: next(ticks, 1.0))
    result = fake_run("codex", tmp_path / "ws", None)
    assert not result.is_error
    artifact = json.loads(Path(result.session_record_path).read_text())["artifacts"]["native"]
    assert artifact["status"] == "unavailable"
    assert artifact["reason"] == "discovery-limit"
    assert artifact["path"] == ""


@pytest.mark.parametrize("missing", [None, {}, {"input_tokens": 10}, {"output_tokens": 2}])
def test_codex_incomplete_turn_usage_is_not_priceable(tmp_path, monkeypatch, missing):
    monkeypatch.setenv(
        "OUTERLOOP_TOKEN_PRICES",
        json.dumps({"test-model": {"input_tokens": 2, "output_tokens": 10}}),
    )

    class Adapter:
        api_key = "synthetic-secret"
        model = "test-model"

        @capture_session("codex")
        def run(self, brief, workspace, resume_session_id=None):
            events = [
                {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 5}},
                {"type": "turn.completed", "usage": missing},
            ]
            return _parse_codex_result("\n".join(map(json.dumps, events)), "done", 0)

    result = Adapter().run("brief", tmp_path / "ws")
    assert result.num_turns == 2
    assert result.tokens == {}
    assert result.cost_usd is None
    record = json.loads(Path(result.session_record_path).read_text())
    assert record["cost_usd"] is None
