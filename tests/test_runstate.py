"""Run-state and lease semantics — the durable half of the agent."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from outerloop.runstate import (
    ENDED,
    MAX_CLOCK_SKEW_S,
    OUTAGE_COOLDOWN_S,
    PARKED,
    STUCK,
    THROTTLE_COOLDOWN_S,
    RunRecord,
    acquire_lease,
    lease_is_stale,
    list_runs,
    load_record,
    outage_active,
    read_lease,
    release_lease,
    run_dir,
    save_record,
    stamp_outage,
    update_lease_holder,
)


def make_record(**overrides) -> RunRecord:
    base = dict(run_id="r1", target="org/repo", task_title="t", state=PARKED, deadline=999.0)
    return RunRecord(**{**base, **overrides})


def test_save_load_roundtrip(tmp_path: Path) -> None:
    save_record(tmp_path, make_record(experiment_job_id="9"), now=100.0)
    loaded = load_record(tmp_path, "r1")
    assert loaded.experiment_job_id == "9"
    assert loaded.created == 100.0
    assert loaded.updated == 100.0


def test_save_stamps_updated_but_keeps_created(tmp_path: Path) -> None:
    save_record(tmp_path, make_record(), now=100.0)
    save_record(tmp_path, load_record(tmp_path, "r1"), now=200.0)
    loaded = load_record(tmp_path, "r1")
    assert loaded.created == 100.0
    assert loaded.updated == 200.0


def test_save_is_atomic_no_tmp_left_behind(tmp_path: Path) -> None:
    save_record(tmp_path, make_record(), now=1.0)
    names = {p.name for p in run_dir(tmp_path, "r1").iterdir()}
    assert names == {"state.json", ".record-lock"}


def test_invalid_state_and_ending_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown state"):
        save_record(tmp_path, make_record(state="dancing"), now=1.0)
    with pytest.raises(ValueError, match="valid ending"):
        save_record(tmp_path, make_record(state=ENDED, ending="tired"), now=1.0)
    save_record(tmp_path, make_record(state=ENDED, ending=STUCK), now=1.0)  # ok


def test_list_runs_skips_corrupt_records(tmp_path: Path, caplog) -> None:
    save_record(tmp_path, make_record(run_id="good"), now=1.0)
    bad = run_dir(tmp_path, "bad")
    bad.mkdir(parents=True)
    (bad / "state.json").write_text("{not json")
    records = list_runs(tmp_path)
    assert [r.run_id for r in records] == ["good"]
    assert "unreadable" in caplog.text


def test_list_runs_skips_non_run_dirs(tmp_path: Path, caplog) -> None:
    save_record(tmp_path, make_record(run_id="good"), now=1.0)
    # A dir under runs/ with no state.json is not a run (e.g. the baselines
    # eval cache); it must be skipped silently, not warned about every sweep.
    cache = run_dir(tmp_path, "baselines")
    cache.mkdir(parents=True)
    (cache / "speedrun@abc.json").write_text("{}")
    records = list_runs(tmp_path)
    assert [r.run_id for r in records] == ["good"]
    assert "unreadable" not in caplog.text


def test_lease_exactly_one_winner(tmp_path: Path) -> None:
    assert acquire_lease(tmp_path, "r1", "tick:a", "", now=1.0)
    assert not acquire_lease(tmp_path, "r1", "tick:b", "", now=2.0)
    lease = read_lease(tmp_path, "r1")
    assert lease is not None and lease.holder == "tick:a"


def test_lease_release_then_reacquire(tmp_path: Path) -> None:
    acquire_lease(tmp_path, "r1", "a", "", now=1.0)
    release_lease(tmp_path, "r1")
    release_lease(tmp_path, "r1")  # idempotent
    assert acquire_lease(tmp_path, "r1", "b", "", now=2.0)


def test_lease_handoff_updates_holder(tmp_path: Path) -> None:
    acquire_lease(tmp_path, "r1", "tick:x", "", now=1.0)
    update_lease_holder(tmp_path, "r1", "wake-job:99", "99", now=2.0)
    lease = read_lease(tmp_path, "r1")
    assert lease is not None
    assert lease.holder_job_id == "99"
    assert not acquire_lease(tmp_path, "r1", "other", "", now=3.0)  # still held


def test_lease_staleness_rules(tmp_path: Path) -> None:
    acquire_lease(tmp_path, "r1", "h", "77", now=1000.0)
    lease = read_lease(tmp_path, "r1")
    assert lease is not None
    # dead holder → stale regardless of age
    assert lease_is_stale(lease, now=1001.0, ttl_s=3600, holder_alive=False)
    # live holder, young → not stale
    assert not lease_is_stale(lease, now=1001.0, ttl_s=3600, holder_alive=True)
    # Slurm unknown → only the TTL can prove staleness
    assert not lease_is_stale(lease, now=1001.0, ttl_s=3600, holder_alive=None)
    assert lease_is_stale(lease, now=1000.0 + 3601, ttl_s=3600, holder_alive=None)
    # live holder but ancient → stale (TTL wins: sessions are bounded)
    # a holder Slurm reports alive is never stale by age (walltime bounds it)
    assert not lease_is_stale(lease, now=1000.0 + 3601, ttl_s=3600, holder_alive=True)


def test_reap_lease_exactly_one_reaper_wins(tmp_path: Path) -> None:
    from outerloop.runstate import reap_lease

    acquire_lease(tmp_path, "r1", "dead", "", now=1.0)
    stale = read_lease(tmp_path, "r1")
    assert stale is not None
    assert reap_lease(tmp_path, "r1", reaper="a", expected=stale)
    assert not reap_lease(tmp_path, "r1", reaper="b", expected=stale)  # gone
    assert acquire_lease(tmp_path, "r1", "next", "", now=2.0)


def test_reap_lease_refuses_a_fresh_lease_it_did_not_observe(tmp_path: Path) -> None:
    """The CAS: reaper B saw the stale lease, but reaper A already reaped it
    and a fresh lease was written — B must restore, not steal."""
    from outerloop.runstate import reap_lease

    acquire_lease(tmp_path, "r1", "dead", "", now=1.0)
    stale = read_lease(tmp_path, "r1")
    assert stale is not None
    # A's reap + a new wake's fresh lease happen "before" B acts:
    release_lease(tmp_path, "r1")
    acquire_lease(tmp_path, "r1", "wake-job:777", "777", now=50.0)
    assert not reap_lease(tmp_path, "r1", reaper="b", expected=stale)
    fresh = read_lease(tmp_path, "r1")
    assert fresh is not None and fresh.holder == "wake-job:777"  # restored


def test_non_object_json_record_is_skipped_not_fatal(tmp_path: Path) -> None:
    """Valid JSON that is not an object (null, list) must be 'corrupt',
    never an exception that blinds the whole sweep."""

    save_record(tmp_path, make_record(run_id="good"), now=1.0)
    bad = run_dir(tmp_path, "nulled")
    bad.mkdir(parents=True)
    (bad / "state.json").write_text("null")
    assert [r.run_id for r in list_runs(tmp_path)] == ["good"]

    lease_dir = run_dir(tmp_path, "good")
    (lease_dir / "lease.json").write_text("[1, 2]")
    lease = read_lease(tmp_path, "good")
    assert lease is not None and lease.holder == "unreadable"  # mtime fallback


def test_load_record_ignores_unknown_keys(tmp_path: Path) -> None:
    """After a bad-merge revert, old code must still read new-code records."""
    import json

    save_record(tmp_path, make_record(), now=1.0)
    path = run_dir(tmp_path, "r1") / "state.json"
    data = json.loads(path.read_text())
    data["field_from_the_future"] = 42
    path.write_text(json.dumps(data))
    assert load_record(tmp_path, "r1").run_id == "r1"
    assert list_runs(tmp_path)  # not treated as corrupt


def test_unreadable_lease_synthesizes_mtime_timestamp(tmp_path: Path) -> None:
    import os

    directory = run_dir(tmp_path, "r1")
    directory.mkdir(parents=True)
    lease_path = directory / "lease.json"
    lease_path.touch()
    os.utime(lease_path, (500.0, 500.0))
    lease = read_lease(tmp_path, "r1")
    assert lease is not None
    assert lease.holder == "unreadable"
    assert lease.acquired == 500.0


def test_outage_latch_pauses_then_expires(tmp_path) -> None:
    assert outage_active(tmp_path, now=1000.0) == ""  # no stamp: inactive
    stamp_outage(tmp_path, "credit balance is too low", now=1000.0)
    assert "credit balance" in outage_active(tmp_path, now=1000.0 + OUTAGE_COOLDOWN_S - 1)
    assert outage_active(tmp_path, now=1000.0 + OUTAGE_COOLDOWN_S) == ""  # expired
    assert outage_active(tmp_path, now=500.0) == ""  # clock moved backwards: expired


def test_throttling_stamps_a_short_pause(tmp_path) -> None:
    """A 429/529 is transient: the stamp carries its own short cooldown,
    so one throttled session never idles the lanes for most of an hour."""
    stamp_outage(tmp_path, "rate_limit_error: Number of requests exceeded", now=1000.0)
    assert "rate_limit" in outage_active(tmp_path, now=1000.0 + THROTTLE_COOLDOWN_S - 1)
    assert outage_active(tmp_path, now=1000.0 + THROTTLE_COOLDOWN_S) == ""


def test_corrupt_outage_stamp_reads_inactive(tmp_path) -> None:
    """A bad latch must never brick the loop. The path must be the one the
    reader actually consults (review finding: a stale filename made this
    vacuous) — prove it by planting a VALID stamp at the same path first."""
    latch = tmp_path / "outage-solver.json"
    stamp_outage(tmp_path, "credit balance", now=1000.0)
    assert latch.exists() and outage_active(tmp_path, now=1000.0) != ""
    latch.write_text("not json")
    assert outage_active(tmp_path, now=1000.0) == ""
    latch.write_text('{"detail": "x"}')  # no time field
    assert outage_active(tmp_path, now=1000.0) == ""


def test_future_stamp_within_skew_is_active(tmp_path) -> None:
    """Stamps are written on compute nodes and read by the tick on another
    host: small NTP skew must not void the pause, while a far-future
    timestamp (corrupt) reads as inactive."""
    stamp_outage(tmp_path, "credit balance", now=1000.0)
    assert outage_active(tmp_path, now=1000.0 - 60) != ""  # reader behind writer
    assert outage_active(tmp_path, now=1000.0 - MAX_CLOCK_SKEW_S - 1) == ""


@pytest.mark.parametrize(
    "old,new",
    [
        ("implementing", "running"),
        ("waiting", "parked"),
        ("in-review", "parked"),
        ("concluding", "parked"),
    ],
)
def test_old_states_migrate(tmp_path, caplog, old, new):
    import json

    from outerloop.runstate import run_dir

    directory = run_dir(tmp_path, "legacy")
    directory.mkdir(parents=True)
    (directory / "state.json").write_text(
        json.dumps(
            {
                "run_id": "legacy",
                "target": "org/repo",
                "task_title": "old",
                "state": old,
                "followup_stage": {"candidate_sha": "old"},
                "followup_job_id": "123",
                "last_comment_id": 900,
            }
        )
    )
    record = load_record(tmp_path, "legacy")
    assert record.state == new
    assert ("migrating concluding to parked" in caplog.text) == (old == "concluding")
    from outerloop.inbox import github_positions

    assert github_positions(directory) == {}
    from outerloop.runstate import acquire_lease, migrate_inbox

    assert acquire_lease(tmp_path, "legacy", "test", "", 1)
    migrate_inbox(tmp_path, "legacy", 1)
    assert github_positions(directory)["comment"] == 900
    save_record(tmp_path, record, 1)
    raw = json.loads((directory / "state.json").read_text())
    assert not {"followup_stage", "followup_job_id", "last_comment_id"} & raw.keys()


@pytest.mark.parametrize("existing", [False, True])
def test_legacy_positions_migrate_once_in_leased_sweep(tmp_path, monkeypatch, existing):
    import outerloop.inbox as inbox
    from fakes import RecordingDispatcher
    from outerloop.compute import LocalCompute
    from outerloop.runstate import migrate_inbox, read_lease
    from outerloop.tick import sweep

    record = RunRecord("legacy", "org/repo", "task", PARKED)
    save_record(tmp_path, record, 1)
    directory = tmp_path / "runs" / record.run_id
    path = directory / "state.json"
    raw = json.loads(path.read_text())
    raw.update(last_comment_id=900, last_review_id=20, last_review_comment_id=3)
    path.write_text(json.dumps(raw))
    if existing:
        inbox.advance_github_positions(directory, {"comment": 5})
    before = {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    load_record(tmp_path, record.run_id)
    assert {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()} == before
    with pytest.raises(RuntimeError, match="requires the run lease"):
        migrate_inbox(tmp_path, record.run_id, 2)
    writes = []
    original = inbox.advance_github_positions

    def advance(folder, positions):
        assert read_lease(tmp_path, record.run_id) is not None
        writes.append(positions)
        original(folder, positions)

    monkeypatch.setattr(inbox, "advance_github_positions", advance)
    sweep(tmp_path, LocalCompute(), RecordingDispatcher(), 2)
    assert len(writes) == (0 if existing else 1)
    assert inbox.github_positions(directory) == (
        {"comment": 5} if existing else {"comment": 900, "review": 20, "review_comment": 3}
    )
    assert "last_comment_id" not in json.loads(path.read_text())
    stamp = (directory / "inbox/positions.json").stat().st_mtime_ns
    sweep(tmp_path, LocalCompute(), RecordingDispatcher(), 3)
    assert len(writes) == (0 if existing else 1)
    assert (directory / "inbox/positions.json").stat().st_mtime_ns == stamp


@pytest.mark.parametrize("writer_state", [PARKED, ENDED])
def test_wake_writers_leave_ended_record_byte_identical(tmp_path, writer_state):
    from dataclasses import replace

    record = RunRecord("run", "org/repo", "task", ENDED, ending="merged", ending_note="PR merged")
    save_record(tmp_path, record, 1)
    path = run_dir(tmp_path, "run") / "state.json"
    before = path.read_bytes()
    save_record(
        tmp_path, replace(record, state=writer_state, inbox_seq=5, stage={"sleeps_used": 9}), 2
    )
    assert path.read_bytes() == before


def test_migration_drops_the_follow_up_job_fields(tmp_path):
    """An idle old record that names a follow-up job or a dispatched
    re-measure is migrated once under the lease and stops counting as legacy."""
    import json

    from outerloop.runstate import acquire_lease, migrate_inbox

    record = RunRecord("legacy2", "org/repo", "t", "parked", pr_url="https://x/pull/3")
    save_record(tmp_path, record, 1)
    path = run_dir(tmp_path, "legacy2") / "state.json"
    raw = json.loads(path.read_text())
    raw["followup_job_id"] = "12345"
    raw["followup_stage"] = {"candidate_sha": "abc"}
    raw["state"] = "in-review"
    path.write_text(json.dumps(raw))
    assert acquire_lease(tmp_path, "legacy2", holder="t", holder_job_id="", now=1)
    migrate_inbox(tmp_path, "legacy2", 2)
    after = json.loads(path.read_text())
    assert "followup_job_id" not in after and "followup_stage" not in after
    assert load_record(tmp_path, "legacy2").state == "parked"


def test_legacy_bless_reason_defaults_empty(tmp_path):
    record = RunRecord("legacy-bless", "org/repo", "task", "parked", auto_blessed_head="head")
    save_record(tmp_path, record, 1)
    path = run_dir(tmp_path, record.run_id) / "state.json"
    raw = json.loads(path.read_text())
    del raw["auto_bless_reason"]
    path.write_text(json.dumps(raw))
    latest = load_record(tmp_path, record.run_id)
    assert latest.auto_blessed_head == "head"
    assert latest.auto_bless_reason == ""
