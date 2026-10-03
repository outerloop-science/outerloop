"""Contract policies change context and credit without changing scope or budgets."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast

import pytest

from fakes import FakeHarness
from outerloop.attempt import _fetch_research_reports, _sibling_entries
from outerloop.brief import BriefInputs, SessionBrief, Task, build_brief, render
from outerloop.contract import Channels, load_contract
from outerloop.github import GitError, GitHubClient, Workspace
from outerloop.panel import resolve_lenses
from outerloop.progress import parse_leader, parse_pending, render_markdown
from outerloop.runstate import RunRecord, load_record, save_record
from outerloop.syscall import SyscallError, read_request, tool_update_note, write_policy
from outerloop.syscall_cli import ToolError, build_parser, cmd_message, cmd_sleep, cmd_submit
from test_orchestrator import CONFIG, CONTRACT, _write_syscall, ok_session, run_climb


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize(
    ("channel", "fragment"),
    [
        ("siblings", "syscall siblings"),
        ("messages", "--to thread|self|agent-NN"),
        ("shared_reports", "unique report lesson"),
        ("branches", "sibling branches locally"),
    ],
)
def test_brief_channels(channel, fragment, enabled):
    brief = build_brief(
        BriefInputs(
            task=Task("h", "tsp", "e", "d"),
            contract_text="contract",
            ruler="ruler",
            lessons="unique report lesson",
            recent_reports=("unique report lesson",),
            report_archive=True,
            syscalls=True,
            launch_budget=2,
            channels={channel: enabled},
        ),
        "today",
    )
    text = render(brief)
    assert (fragment in text) == enabled
    if channel == "siblings" and not enabled:
        assert "Check it before choosing" not in text
        assert "every agent's" not in text
    if channel == "shared_reports" and not enabled:
        assert "research-log" not in text and "syscall reports" not in text
    assert SessionBrief.from_json(brief.to_json()) == brief


@pytest.mark.parametrize("gpu", [False, True])
@pytest.mark.parametrize("direction,baseline,candidate", [("min", 100, 90), ("max", 100, 110)])
def test_self_report_skips_measurement_panel_and_compute(
    tmp_path, direction, baseline, candidate, gpu
):
    contract = CONTRACT.replace(
        "direction: min", f"direction: {direction}\n    verification: self_report\n    min_delta: 5"
    )
    if gpu:
        contract = contract.replace(
            "verification: self_report",
            "verification: self_report\n    gpus: 1\n    eval_minutes: 60",
        )
    harness = FakeHarness(
        ok_session(),
        script=lambda _p, ws: _write_syscall(
            ws,
            {
                "submit": True,
                "claimed_value": candidate,
                "claimed_baseline": baseline,
                "report": "My hypothesis and claim",
            },
        ),
    )
    meters = []
    result, _, evaluator = run_climb(
        tmp_path,
        [],
        contract=contract,
        harness=harness,
        launcher=lambda *_: pytest.fail("unexpected launch"),
        panel_runner=lambda *_: pytest.fail("unexpected panel"),
        on_meter=lambda *values: meters.append(values),
    )
    assert result.outcome == "improved"
    assert result.provenance == "self_reported"
    assert result.candidate == candidate and result.baseline == baseline
    assert result.candidate_sha and result.measured_paths
    assert evaluator.calls == [] and result.panel_rounds == 0
    assert meters[-1] == (0, 1, 0.0)
    assert "self-reported" in result.report(CONFIG)
    from outerloop.orchestrator import pr_body

    body = pr_body(result, CONFIG, ())
    assert "self_reported" in body and "## Measured" not in body


@pytest.mark.parametrize("candidate", [99, 101])
def test_self_report_floor_refuses_claim(tmp_path, candidate):
    _write_syscall(tmp_path, {"submit": True, "claimed_value": candidate, "claimed_baseline": 100})
    result, _, evaluator = run_climb(
        tmp_path,
        [],
        contract=CONTRACT.replace(
            "direction: min", "direction: min\n    verification: self_report\n    min_delta: 2"
        ),
        launcher=lambda *_: pytest.fail("launch"),
    )
    assert result.outcome == "no-improvement"
    assert result.provenance == "self_reported" and not evaluator.calls


@pytest.mark.parametrize(
    "path", [".outerloop.yaml", ".github/test.yml", "docs/roadmap.md", "src/pilot/eval.py"]
)
def test_trust_scope_stays_on(tmp_path, path):
    _write_syscall(tmp_path, {"submit": True, "claimed_value": 1, "claimed_baseline": 100})
    result, _, evaluator = run_climb(
        tmp_path,
        [],
        contract=CONTRACT.replace(
            "direction: min", "direction: min\n    verification: self_report"
        ),
        harness=FakeHarness(ok_session(), supports_resume=False),
        changed=[path],
        launcher=lambda *_: pytest.fail("launch"),
    )
    assert result.tree_rejected and result.outcome != "improved" and not evaluator.calls


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "1"])
def test_claims_must_be_finite_numbers(tmp_path, value):
    _write_syscall(tmp_path, {"submit": True, "claimed_value": value})
    with pytest.raises(SyscallError, match="finite number"):
        read_request(tmp_path)


def test_claim_cli_roundtrip_and_required_values(tmp_path):
    write_policy(tmp_path, "self_report", Channels().model_dump())
    cli = build_parser()
    with pytest.raises(ToolError, match="requires"):
        cmd_submit(tmp_path, cli.parse_args(["submit"]))
    assert "self-reported" in cmd_submit(
        tmp_path, cli.parse_args(["submit", "--claimed-value", "9", "--claimed-baseline", "10"])
    )
    assert "self-reported" in cmd_sleep(tmp_path, cli.parse_args(["sleep"]))
    request = read_request(tmp_path)
    assert request and request.claimed_value == 9 and request.claimed_baseline == 10


@pytest.mark.parametrize("enabled", [True, False])
def test_message_cli_policy_keeps_operator_and_self(tmp_path, enabled):
    write_policy(tmp_path, "gate", {"messages": enabled})
    cli = build_parser()
    for destination in ("self", "thread"):
        assert "staged" in cmd_message(
            tmp_path, cli.parse_args(["message", "--to", destination, "hi"])
        )
    args = cli.parse_args(["message", "--to", "agent-02", "hi"])
    if enabled:
        assert "staged" in cmd_message(tmp_path, args)
    else:
        with pytest.raises(ToolError, match=r"channels.messages"):
            cmd_message(tmp_path, args)
    assert ("agent-NN" in tool_update_note(".outerloop", messages=enabled)) == enabled


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.parametrize("enabled", [True, False])
def test_branch_fetch_and_isolated_research_views(tmp_path, enabled):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "main")
    git(origin, "config", "user.name", "Tester")
    git(origin, "config", "user.email", "tester@example.org")
    (origin / "base").write_text("base")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "base")
    for branch in ("agents/agent-01", "agents/agent-02", "research-log"):
        git(origin, "checkout", "-b", branch, "main")
        (origin / "private-data").write_text(branch)
        (origin / "reports").mkdir(exist_ok=True)
        (origin / "reports" / "2026-01-01-agent-02.md").write_text("shared report")
        (origin / "climb").mkdir(exist_ok=True)
        (origin / "climb" / "status.json").write_text(
            json.dumps({"runs": [{"agent": "agent-02", "direction": "other direction"}]})
        )
        git(origin, "add", ".")
        git(origin, "commit", "-m", branch)
    ws = Workspace.clone(origin.as_uri(), tmp_path / "ws", single_branch="main")
    ws.configure_channels({"branches": enabled, "shared_reports": enabled}, "agent-01")
    ws.fetch_origin()
    refs = ws.git("for-each-ref", "--format=%(refname)", "refs/remotes")
    assert ("origin/agents/agent-02" in refs) == enabled
    assert "origin/agents/agent-01" in refs
    assert ("origin/research-log" in refs) == enabled
    assert _sibling_entries(ws, "agent-01")[0]["direction"] == "other direction"
    if not enabled:
        research_sha = git(origin, "rev-parse", "research-log")
        with pytest.raises(GitError):
            ws.git("cat-file", "-e", research_sha)
    # Reports may remain enabled independently of siblings; only their text is delivered.
    ws.configure_channels({"siblings": False}, "agent-01")
    assert _fetch_research_reports(ws, 5)[0][1] == "shared report"


def test_pinned_judges_do_not_inherit_author():
    pin = "verify:claude:fixed-judge,review:claude:fixed-judge"
    assert resolve_lenses(pin, "claude", "author-a") == resolve_lenses(pin, "codex", "author-b")


def test_legacy_policy_fixture(tmp_path):
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "pre_contract_switches.json").read_text()
    )
    directory = tmp_path / "runs" / "legacy"
    directory.mkdir(parents=True)
    (directory / "state.json").write_text(json.dumps(fixture["run"]))
    record = load_record(tmp_path, "legacy")
    assert record.verification == "gate" and all(record.channels.values())
    save_record(tmp_path, record, 1)
    assert Channels.model_validate(load_record(tmp_path, "legacy").channels) == Channels()
    pending = parse_pending(json.dumps(fixture["pending"]))
    assert pending and pending.provenance == "measured"
    leader = parse_leader(json.dumps(fixture["leader"]))
    assert leader["tsp"].provenance == "measured"
    assert SessionBrief.from_json(json.dumps(fixture["brief"])).verification == "gate"
    assert load_contract(CONTRACT, "owner/repo").channels == Channels()
    trusted = replace(leader["tsp"], provenance="self_reported")
    assert "self-reported" in render_markdown({"tsp": trusted}, "owner/repo")
    assert parse_leader(json.dumps({"tsp": asdict(trusted)}))["tsp"] == trusted


@pytest.mark.parametrize("enabled", [True, False])
def test_kernel_message_policy_cannot_be_bypassed(tmp_path, enabled):
    from outerloop.inbox import pending
    from test_messages import outgoing, runs, send

    sender, recipient = runs(tmp_path)
    sender = replace(sender, channels={"messages": enabled})
    save_record(tmp_path, sender, 2)
    send(tmp_path, sender, outgoing("agent-02", "shared idea"), outgoing("self", "own note"))
    their = pending(tmp_path / "runs" / recipient.run_id, 0)
    own = pending(tmp_path / "runs" / sender.run_id, 0)
    assert bool(their) == enabled
    assert any(m.payload.get("text") == "own note" for m in own)
    if not enabled:
        assert any("channels.messages" in m.payload.get("text", "") for m in own)


@pytest.mark.parametrize("enabled", [True, False])
def test_queue_sibling_switch(tmp_path, enabled):
    from outerloop.watcher import SessionWatcher, WatcherContext
    from test_watcher import _Compute, _fleet, _row

    root, ws = _fleet(tmp_path)
    record = replace(load_record(root, "r1"), channels={"siblings": enabled})
    save_record(root, record, 2)
    view = SessionWatcher(
        WatcherContext(
            workspace=ws,
            run_root=root,
            run_id="r1",
            target="o/r",
            agent_id="agent-01",
            compute=_Compute([_row("555", "r2-launch-lr")]),
        )
    ).queue_view()
    assert ("try lr 3e-4" in json.dumps(view)) == enabled


def test_trust_missing_claim_never_falls_back_to_gate(tmp_path):
    _write_syscall(tmp_path, {"submit": True})
    result, _, evaluator = run_climb(
        tmp_path,
        [],
        contract=CONTRACT.replace(
            "direction: min", "direction: min\n    verification: self_report"
        ),
        harness=FakeHarness(ok_session(), supports_resume=False),
        on_replies=lambda _: None,
    )
    assert result.outcome == "no-improvement" and not evaluator.calls


def test_trust_submit_without_compute_backend(tmp_path):
    _write_syscall(tmp_path, {"submit": True, "claimed_value": 9, "claimed_baseline": 10})
    result, _, evaluator = run_climb(
        tmp_path,
        [],
        contract=CONTRACT.replace(
            "direction: min", "direction: min\n    verification: self_report"
        ),
        on_replies=lambda _: None,
    )
    assert result.outcome == "improved" and not evaluator.calls


def test_live_trust_publish_records_claim_without_measurement(tmp_path, monkeypatch):
    from outerloop.attempt import live_attempt
    from test_attempt import FakeGitHub, NoAuth, _queued_local, _seed_target

    contract = CONTRACT.replace("direction: min", "direction: min\n    verification: self_report")
    contract += (
        "\nchannels: {siblings: false, messages: false, shared_reports: false, branches: false}\n"
    )
    _seed_target(tmp_path, monkeypatch, contract)

    def author(_prompt, ws):
        (ws / "src/pilot/solvers/tsp.py").write_text("def solve(): return 9\n")
        _write_syscall(ws, {"submit": True, "claimed_value": 9, "claimed_baseline": 10})

    github = FakeGitHub()
    with _queued_local([]):  # Any measurement would fail: there are no queued values.
        outcome = live_attempt(
            config=CONFIG,
            run_root=tmp_path / "state",
            run_id="trust",
            harness=FakeHarness(ok_session(), script=author),
            github=cast(GitHubClient, github),
            bot_auth=NoAuth(),
            now=1000,
            created="2026-01-01T00:00:00Z",
        )
    assert outcome.outcome == "improved"
    record = load_record(tmp_path / "state", "trust")
    assert record.verification == "self_report"
    assert not any(record.channels.values())
    assert record.claimed_value == 9 and record.claimed_baseline == 10
    assert record.pr_url and not record.auto_blessed_head
    assert not github.armed
    assert "self-reported" in github.prs[0]["body"]
    assert "self-reported" in github.prs[0]["title"]
    pending = next(
        json.loads(v)
        for k, v in github.ledger_files.items()
        if k.startswith("results/submissions/")
    )
    assert pending["provenance"] == "self_reported" and pending["candidate"] == 9
    assert "self-reported" in Path(outcome.report_path).read_text()


def test_operator_status_labels_self_report(tmp_path):
    from outerloop.status import collect_status, render_text

    record = RunRecord("trust", "owner/repo", "t", "parked", verification="self_report")
    save_record(tmp_path, record, 1)
    status = collect_status(tmp_path)
    assert status["runs"][0]["verification"] == "self_report"
    assert "self-reported" in render_text(status)


def test_policy_write_does_not_follow_author_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    channel = tmp_path / ".outerloop"
    channel.mkdir()
    (channel / "policy.json").symlink_to(outside)
    write_policy(tmp_path, "self_report", {"messages": False})
    assert outside.read_text() == "untouched"
    assert not (channel / "policy.json").is_symlink()


@pytest.mark.parametrize("enabled", [True, False])
def test_wake_protocol_honors_message_channel(enabled):
    from outerloop.orchestrator import _render_author_inbox

    text = _render_author_inbox(
        [],
        budgets="budget",
        redact_secrets=(),
        messages_enabled=enabled,
        verification="self_report",
    )
    assert ("Post with `message`" in text) == enabled
    assert "self-reported" in text and "--claimed-value" in text


def test_disabled_messages_hide_pending_and_chain_history(tmp_path):
    from outerloop.inbox import Message, append, write_messages
    from outerloop.orchestrator import _render_author_inbox

    run = tmp_path / "run"
    ws = tmp_path / "ws"
    messages = [
        Message(
            0,
            "agent-message",
            "agent",
            "",
            1,
            "other",
            {"text": "hidden sibling idea"},
            origin="other",
            to="run",
        ),
        Message(
            0,
            "agent-message",
            "agent",
            "",
            2,
            "self",
            {"text": "own reminder"},
            origin="run",
            to="run",
            in_reply_to="other/other",
        ),
        Message(
            0,
            "comment",
            "human",
            "",
            3,
            "operator",
            {"body": "operator instruction"},
            origin="operator",
        ),
        Message(0, "comment", "author", "", 4, "thread", {"body": "thread reply"}, origin="run"),
    ]
    for message in messages:
        append(run, message)
    from outerloop.inbox import pending

    stored = pending(run, 0)
    text = _render_author_inbox(
        stored[1:],
        budgets="",
        redact_secrets=(),
        inbox_dir=run,
        messages_enabled=False,
    )
    write_messages(ws, run, messages_enabled=False)
    exported = (ws / ".outerloop/messages.json").read_text()
    for output in (text, exported):
        assert "hidden sibling idea" not in output
        assert "own reminder" in output
        assert "operator instruction" in output
        assert "thread reply" in output
    text = _render_author_inbox(
        stored,
        budgets="",
        redact_secrets=(),
        inbox_dir=run,
        messages_enabled=False,
    )
    assert "hidden sibling idea" not in text


def test_claimed_leader_is_not_early_failure_baseline(tmp_path, monkeypatch):
    from outerloop.attempt import live_attempt
    from outerloop.harness import SessionResult
    from outerloop.progress import LeaderEntry
    from test_attempt import FakeGitHub, NoAuth, _queued_local, _seed_target

    _seed_target(tmp_path, monkeypatch, CONTRACT)
    github = FakeGitHub()
    github.ledger_files["results/leader.json"] = json.dumps(
        {
            "tsp": asdict(
                LeaderEntry(
                    "tsp",
                    "mean_tour_length",
                    "min",
                    987654,
                    876543,
                    "claim",
                    "today",
                    provenance="self_reported",
                )
            )
        }
    )
    with _queued_local([]):
        outcome = live_attempt(
            config=CONFIG,
            run_root=tmp_path / "state",
            run_id="failed",
            harness=FakeHarness(
                SessionResult("error", True, 0, 0, "", "", "", "author failed"),
                supports_resume=False,
            ),
            github=cast(GitHubClient, github),
            bot_auth=NoAuth(),
            now=1000,
            created="today",
        )
    assert outcome.outcome == "session-error"
    report = Path(outcome.report_path).read_text()
    assert "876543" not in report and "987654" not in report


@pytest.mark.parametrize("restricted", [False, True])
def test_bootstrap_default_has_no_fetch_and_restricted_has_no_hidden_objects(tmp_path, restricted):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "main")
    git(origin, "config", "user.name", "Tester")
    git(origin, "config", "user.email", "tester@example.org")
    contract = CONTRACT
    if restricted:
        contract += "\nchannels: {branches: false, siblings: false, shared_reports: false}\n"
    (origin / ".outerloop.yaml").write_text(contract)
    git(origin, "add", ".")
    git(origin, "commit", "-m", "base")
    for branch in ("agents/agent-02", "research-log"):
        git(origin, "checkout", "-b", branch, "main")
        (origin / "hidden").write_text(branch)
        git(origin, "add", ".")
        git(origin, "commit", "-m", branch)
    hidden_sha = git(origin, "rev-parse", "HEAD")
    git(origin, "checkout", "main")

    class CountingWorkspace(Workspace):
        fetches = 0

        def fetch_origin(self):
            self.fetches += 1
            if not restricted:
                raise GitError("fetch unavailable")
            super().fetch_origin()

    ws = CountingWorkspace.clone_for_channels(
        origin.as_uri(),
        tmp_path / "ws",
        "owner/repo",
        "main",
        "agent-01",
    )
    assert isinstance(ws, CountingWorkspace)
    assert ws.fetches == int(restricted)
    refs = ws.git("for-each-ref", "--format=%(refname)", "refs/remotes")
    assert ("origin/agents/agent-02" in refs) != restricted
    assert ("origin/research-log" in refs) != restricted
    if restricted:
        with pytest.raises(GitError):
            ws.git("cat-file", "-e", hidden_sha)


def test_default_serialization_and_rendering_match_head(tmp_path):
    from dataclasses import make_dataclass

    from outerloop.climbboard import render_html
    from outerloop.progress import PendingSubmission, record_pending, write_progress

    fixtures = Path(__file__).parent / "fixtures"
    old = json.loads((fixtures / "pre_contract_switches.json").read_text())
    expected = json.loads((fixtures / "default_contract_head.json").read_text())
    pending = PendingSubmission(**old["pending"])
    wire = record_pending(pending)[pending.path]
    assert wire == expected["pending"]
    # HEAD's strict reader constructs a dataclass directly from all JSON keys.
    # An added provenance key raises TypeError here, just as in that reader.
    HeadPending = make_dataclass("HeadPending", expected["pending_fields"])
    assert HeadPending(**json.loads(wire)).candidate == pending.candidate
    leaders = parse_leader(json.dumps(old["leader"]))
    write_progress(tmp_path, leaders, "owner/repo")
    assert (tmp_path / "results/leader.json").read_text() == expected["leader"]
    assert (tmp_path / "BENCHMARKS.md").read_text() == expected["markdown"]
    brief = SessionBrief.from_json(json.dumps(old["brief"]))
    assert brief.to_json() == expected["brief_json"]
    assert render(brief) == expected["brief_text"]
    assert render_html("owner/repo", {}, {}) == expected["html"]
    record = RunRecord(**old["run"], channels=Channels().model_dump())
    save_record(tmp_path, record, 1)
    assert (tmp_path / "runs/legacy/state.json").read_text() == expected["run"]
    write_policy(tmp_path, "gate", Channels().model_dump())
    assert not (tmp_path / ".outerloop/policy.json").exists()


@pytest.mark.parametrize("disabled", ["sender", "recipient"])
def test_disabled_channel_refuses_previously_staged_delivery(tmp_path, monkeypatch, disabled):
    import outerloop.attempt as attempt
    from outerloop.inbox import pending
    from test_messages import outgoing, runs, send

    sender, recipient = runs(tmp_path)
    original = attempt.append

    def interrupted(directory, message):
        if message.kind == "agent-message":
            raise OSError("delivery interrupted")
        return original(directory, message)

    monkeypatch.setattr(attempt, "append", interrupted)
    send(tmp_path, sender, outgoing("agent-02", "staged sibling idea"))
    assert load_record(tmp_path, sender.run_id).stage["message_delivery"]
    record = load_record(tmp_path, sender.run_id if disabled == "sender" else recipient.run_id)
    save_record(tmp_path, replace(record, channels={"messages": False}), 2)
    monkeypatch.setattr(attempt, "append", original)
    send(tmp_path, load_record(tmp_path, sender.run_id))
    assert not pending(tmp_path / "runs" / recipient.run_id, 0)
    own = pending(tmp_path / "runs" / sender.run_id, 0)
    assert any("channels.messages" in str(m.payload) for m in own)


def test_claim_never_gets_merge_blessing_even_with_prior_panel_metadata():
    from types import SimpleNamespace

    from outerloop.attempt import _bless_decision

    result = SimpleNamespace(
        provenance="self_reported",
        panel_rounds=1,
        panel_blocking_open=False,
        panel_degraded=False,
    )
    head, reason = _bless_decision(
        cast(Workspace, None),
        result,
        SimpleNamespace(merge="auto"),
        "main",
        "base",
    )
    assert head == "" and reason == "self-reported claim"


@pytest.mark.parametrize("direction,claim,candidate", [("min", 1, 90), ("max", 1000, 110)])
def test_measured_merge_replaces_claim_without_inheriting_claimed_baseline(
    direction, claim, candidate
):
    from outerloop.progress import confirm
    from test_progress import ancestor, submission

    claimed = submission(
        direction=direction, baseline=500, candidate=claim, provenance="self_reported"
    )
    entries = confirm({}, claimed, "1", is_ancestor=ancestor)
    measured = submission(direction=direction, baseline=100, candidate=candidate, run_id="measured")
    promoted = confirm(entries, measured, "2", is_ancestor=ancestor)["bench"]
    assert promoted.provenance == "measured"
    assert (promoted.baseline, promoted.best, promoted.best_run) == (100, candidate, "measured")
    assert confirm({"bench": promoted}, claimed, "3", is_ancestor=ancestor) == {"bench": promoted}


def test_author_leg_applies_current_policy_before_retrying_messages(tmp_path, monkeypatch):
    import outerloop.attempt as attempt
    from outerloop.inbox import pending
    from test_messages import outgoing, runs, send

    sender, recipient = runs(tmp_path)
    sender = replace(sender, benchmark="tsp")
    save_record(tmp_path, sender, 1)
    original = attempt.append

    def interrupted(directory, message):
        if message.kind == "agent-message":
            raise OSError("delivery interrupted")
        return original(directory, message)

    monkeypatch.setattr(attempt, "append", interrupted)
    send(tmp_path, sender, outgoing("agent-02", "staged sibling idea"))
    monkeypatch.setattr(attempt, "append", original)
    sender = load_record(tmp_path, sender.run_id)
    contract = CONTRACT.replace("direction: min", "direction: min\n    verification: self_report")
    contract += "\nchannels: {messages: false}\n"
    wsroot = tmp_path / "ws"
    wsroot.mkdir()
    git(wsroot, "init", "-b", "main")

    class StopLeg(Exception):
        pass

    def stop(_):
        assert load_record(tmp_path, sender.run_id).verification == "self_report"
        raise StopLeg

    monkeypatch.setattr(attempt, "shipped_channel", stop)
    with pytest.raises(StopLeg):
        attempt.run_author_leg(
            CONFIG,
            contract,
            wsroot,
            FakeHarness(ok_session()),
            None,
            "base",
            lambda: "sha",
            pinned_tip="base",
            run_root=tmp_path,
            record=sender,
            ws=Workspace(wsroot),
            dispatch=None,
            github=cast(GitHubClient, object()),
            secrets=(),
        )
    assert not pending(tmp_path / "runs" / recipient.run_id, 0)
    assert any(
        "channels.messages" in str(m.payload) for m in pending(tmp_path / "runs" / sender.run_id, 0)
    )


@pytest.mark.parametrize("disabled", ["branches", "siblings", "shared_reports"])
def test_wake_reads_current_contract_before_fetch(tmp_path, monkeypatch, disabled):
    import outerloop.attempt as attempt
    from test_attempt import (
        CONTRACT_SYSCALLS,
        FakeGitHub,
        NoAuth,
        _fake_dispatch,
        _push_contract,
        _push_line,
        _write_parked_author_sleep,
    )

    root, run_id, wsroot, _ = _write_parked_author_sleep(tmp_path, monkeypatch)
    bare = tmp_path / f"origin-{run_id}.git"
    current = CONTRACT_SYSCALLS.replace(
        "direction: min", "direction: min\n    verification: self_report"
    )
    current += f"\nchannels: {{{disabled}: false}}\n"
    _push_contract(tmp_path, bare, current, "policy")
    hidden_branch = "agents/agent-02" if disabled == "branches" else "research-log"
    _push_line(tmp_path, bare, {"hidden": "new private data"}, name=hidden_branch)
    hidden = git(bare, "rev-parse", hidden_branch)
    original = Workspace.fetch_origin
    fetch_policies = []

    class StopWake(Exception):
        pass

    def fetch(ws):
        saved = load_record(root, run_id)
        fetch_policies.append((saved.verification, saved.channels, ws.channels))
        original(ws)
        assert hidden_branch not in ws.git("for-each-ref", "--format=%(refname)", "refs/remotes")
        with pytest.raises(GitError):
            ws.git("cat-file", "-e", hidden)

    monkeypatch.setattr(Workspace, "fetch_origin", fetch)

    def stop(**kwargs):
        # verification stays what the run started with; channels follow at once
        assert kwargs["bench"].verification == "gate"
        assert kwargs["contract"].channels.model_dump()[disabled] is False
        raise StopWake

    monkeypatch.setattr(attempt, "_wake_author_sleep", stop)
    with pytest.raises(StopWake):
        attempt.resume_run(
            root,
            run_id,
            dispatch=_fake_dispatch(),
            github=cast(GitHubClient, FakeGitHub()),
            bot_auth=NoAuth(),
            now=2_000_000,
        )
    # Fetch failures are best-effort, so check policy and effects outside its callback.
    assert fetch_policies == [("gate", {disabled: False}, {disabled: False})]
    with pytest.raises(GitError):
        Workspace(wsroot).git("cat-file", "-e", hidden)
