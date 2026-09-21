"""Branch publish/terminal handoff, without a candidate checkout ledger."""

import json
from dataclasses import asdict, replace
from typing import cast

import pytest

from ledger_fake import LedgerGitHub
from outerloop.attempt import _clear_stage, _ledger_comparison, close_if_done
from outerloop.contract import load_contract
from outerloop.github import GitHubClient, GitHubError
from outerloop.ledger_branch import LedgerWriteError
from outerloop.ledger_events import (
    LEDGER_RETRY,
    display_leader,
    observe_target,
    queue_pending,
)
from outerloop.progress import LEADER_FILE, LeaderEntry, PendingSubmission, record_pending
from outerloop.runstate import PARKED, RunRecord, load_record, save_record

CONTRACT = """
benchmarks:
  - name: bench
    command: python eval.py
    metric: score
    direction: max
    display_digits: 3
    min_delta: 0.1
budgets: {gpu_hours_per_run: 1, runs_per_week: 10}
scope: {allowed: [solver.py]}
roadmap: docs/roadmap.md
"""


def pending(**kwargs):
    return replace(
        PendingSubmission(
            "bench",
            "score",
            "max",
            1.0,
            2.34567,
            "run-1",
            987654321,
            "ruler",
            "signature",
            "sealed",
            1,
            "published",
            "2026-09-21",
        ),
        **kwargs,
    )


def record(root):
    r = RunRecord(
        "run-1",
        "org/repo",
        "test",
        PARKED,
        pr_url="https://github.com/org/repo/pull/1",
        stage={"ledger_digits": {"bench": 3}, "launches_used": 7},
    )
    save_record(root, r, 1)
    return r


def client():
    fake = LedgerGitHub()
    fake.trees = {"sealed": "tree-1", "merge": "tree-1"}
    fake.pull_requests[1] = {
        "state": "closed",
        "merged": True,
        "merge_commit_sha": "merge",
        "head": {"sha": "published"},
    }
    fake.ancestry = ["merge"]
    fake.ledger_files.update(record_pending(pending()))
    return fake, cast(GitHubClient, fake)


@pytest.mark.parametrize("merged", [True, False])
def test_terminal_persists_ledger_before_cleanup(tmp_path, monkeypatch, merged):
    fake, github = client()
    r = record(tmp_path)
    fake.pull_requests[1]["merged"] = merged
    called = []

    def finish(root, rec, ending, note, now, gh):
        assert fake.ledger_files[pending().path] == "null\n"
        called.append(ending)

    monkeypatch.setattr("outerloop.attempt.finish_run", finish)
    assert close_if_done(tmp_path, r, github, 2) == ("merged" if merged else "rejected")
    assert called == ["merged" if merged else "rejected"]
    leader = json.loads(fake.ledger_files[LEADER_FILE])
    if merged:
        assert leader["bench"]["main_commit"] == "merge"
        assert leader["bench"]["measured_sha"] == "sealed"
        assert leader["bench"]["run_seed"] == 987654321
        assert "| 2.35 |" in fake.ledger_files["BENCHMARKS.md"]
    else:
        assert leader == {}


def test_write_failure_preserves_stage_and_tick_retries(tmp_path, monkeypatch, caplog):
    from fakes import RecordingDispatcher
    from outerloop.tick import sweep
    from test_tick import FakeSlurm

    fake, github = client()
    fake.ledger_files.clear()
    r = record(tmp_path)
    fake.pull_requests[1] = {"state": "open"}
    original = fake.put_files

    def fail(*args, **kwargs):
        raise LedgerWriteError("secret-token")

    monkeypatch.setattr(fake, "put_files", fail)
    r = queue_pending(tmp_path, r, github, pending(), load_contract(CONTRACT, r.target), 2)
    assert LEDGER_RETRY in r.stage
    assert "secret-token" not in caplog.text
    r = _clear_stage(r, tmp_path)
    assert r.stage["launches_used"] == 7 and LEDGER_RETRY in r.stage
    save_record(tmp_path, r, 2)
    monkeypatch.setattr(fake, "put_files", original)
    sweep(tmp_path, FakeSlurm().compute(), RecordingDispatcher(), 3, github=github)
    assert LEDGER_RETRY not in load_record(tmp_path, r.run_id).stage
    assert json.loads(fake.ledger_files[pending().path])["run_seed"] == 987654321
    assert json.loads(fake.ledger_files[LEADER_FILE]) == {}


@pytest.mark.parametrize("edited_head", [False, True])
def test_unmeasured_merge_remains_pending_with_note(tmp_path, edited_head):
    fake, github = client()
    fake.trees["merge"] = "human-edited"
    if edited_head:
        fake.pull_requests[1]["head"]["sha"] = "human-head"
    assert close_if_done(tmp_path, record(tmp_path), github, 2) == ""
    assert load_record(tmp_path, "run-1").stage["ledger_note"] == "unmeasured merge tree"
    assert json.loads(fake.ledger_files[pending().path])["status"] == "PENDING"
    assert not fake.ledger_writes


def test_sweep_keeps_observer_note_in_later_bookkeeping(tmp_path, monkeypatch):
    from fakes import RecordingDispatcher
    from outerloop.tick import sweep
    from test_tick import FakeSlurm

    fake, github = client()
    fake.trees["merge"] = "human-edited"
    record(tmp_path)
    seen = []

    def later(root, compute, dispatcher, now, grace, ttl, dry_run, rec, *args):
        seen.append(rec.stage.get("ledger_note"))

    monkeypatch.setattr("outerloop.tick._sweep_one", later)
    sweep(tmp_path, FakeSlurm().compute(), RecordingDispatcher(), 2, github=github)
    assert seen == ["unmeasured merge tree"]


def test_reset_first_then_solvers_in_ancestry_order(monkeypatch):
    fake, github = client()
    fake.ledger_files.clear()
    events = [
        pending(run_id="new", pr_number=3, published_head="p3", measured_sha="s3", candidate=7),
        pending(run_id="old", pr_number=1, published_head="p1", measured_sha="s1", candidate=99),
        pending(
            run_id="reset",
            pr_number=2,
            published_head="p2",
            measured_sha="s2",
            candidate=5,
            kind="RESET",
        ),
    ]
    for event in events:
        n = event.pr_number
        fake.ledger_files.update(record_pending(event))
        fake.pull_requests[n] = {
            "merged": True,
            "merge_commit_sha": f"m{n}",
            "head": {"sha": f"p{n}"},
        }
        fake.trees[f"s{n}"] = fake.trees[f"m{n}"] = f"t{n}"
    fake.ancestry = ["m1", "m2", "m3"]
    from outerloop.progress import confirm

    calls = []

    def tracked(leader, event, sha, **kwargs):
        calls.append(sha)
        return confirm(leader, event, sha, **kwargs)

    monkeypatch.setattr("outerloop.ledger_events.confirm", tracked)
    observe_target(github, "org/repo", {})
    assert calls == ["m2", "m1", "m3"]
    row = json.loads(fake.ledger_files[LEADER_FILE])["bench"]
    assert row["baseline"] == 5 and row["best"] == 7 and row["main_commit"] == "m3"


def test_ancestry_failure_does_not_guess_or_cleanup(tmp_path, monkeypatch):
    fake, github = client()
    fake.ledger_files[LEADER_FILE] = json.dumps(
        {
            "bench": asdict(
                LeaderEntry("bench", "score", "max", 1, 2, "r", "d", reset_commit="reset")
            )
        }
    )

    def unavailable(*args):
        raise GitHubError(503, "compare", "unavailable")

    monkeypatch.setattr(fake, "head_contains", unavailable)
    r = record(tmp_path)
    with pytest.raises(LedgerWriteError, match="observation deferred") as error:
        close_if_done(tmp_path, r, github, 2)
    assert isinstance(error.value.__cause__, GitHubError)
    assert load_record(tmp_path, r.run_id).state == PARKED
    assert not fake.ledger_writes


def test_noise_floor_reads_branch_and_tolerates_failure(caplog):
    fake, github = client()
    fake.ledger_files[LEADER_FILE] = json.dumps(
        {"bench": asdict(LeaderEntry("bench", "score", "max", 1, 2, "r", "d"))}
    )
    contract = load_contract(CONTRACT, "org/repo")
    prior, note = _ledger_comparison(github, contract.benchmarks[0], 2.05, "org/repo")
    assert prior.best == 2 and "noise floor" in note
    fake.ledger_fail = True
    assert display_leader(github, "org/repo") == {}
    assert "using no prior measurement" in caplog.text


@pytest.mark.parametrize("leased", [False, True])
def test_sweep_waits_for_deferred_reset_before_observing_solvers(tmp_path, monkeypatch, leased):
    from fakes import RecordingDispatcher
    from outerloop.runstate import acquire_lease
    from outerloop.tick import sweep
    from test_tick import FakeSlurm

    fake, github = client()
    reset = pending(run_id="reset", kind="RESET", pr_number=2)
    reset_record = replace(record(tmp_path), run_id="reset")
    fake.ledger_fail = True
    reset_record = queue_pending(
        tmp_path, reset_record, github, reset, load_contract(CONTRACT, "org/repo"), 2
    )
    if leased:
        assert acquire_lease(tmp_path, "reset", "worker", "", 2)
        fake.ledger_fail = False
    monkeypatch.setattr(
        "outerloop.attempt.close_if_done",
        lambda *args: pytest.fail("observed solver before reset was persisted"),
    )
    sweep(tmp_path, FakeSlurm().compute(), RecordingDispatcher(), 3, github=github)
    assert LEDGER_RETRY in load_record(tmp_path, reset_record.run_id).stage
    assert not fake.ledger_writes


@pytest.mark.parametrize("candidate,expected", [(1.9, 2), (2.05, 2), (2.2, 2.2)])
def test_confirmation_preserves_monotonic_noise_floor(candidate, expected):
    from outerloop.progress import confirm

    old = LeaderEntry("bench", "score", "max", 1, 2, "old", "d")
    result = confirm(
        {"bench": old},
        pending(candidate=candidate, min_delta=0.1),
        "merge",
        is_ancestor=lambda a, b: True,
    )
    assert result["bench"].best == expected
    assert result["bench"].baseline == 1
