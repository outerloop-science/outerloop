"""Run-state and lease semantics — the durable half of the agent."""

from __future__ import annotations

from pathlib import Path

import pytest

from autoresearch.runstate import (
    ENDED,
    MAX_CLOCK_SKEW_S,
    OUTAGE_COOLDOWN_S,
    STUCK,
    THROTTLE_COOLDOWN_S,
    WAITING,
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
    base = dict(run_id="r1", target="org/repo", task_title="t", state=WAITING, deadline=999.0)
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
    assert names == {"state.json"}


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
    from autoresearch.runstate import reap_lease

    acquire_lease(tmp_path, "r1", "dead", "", now=1.0)
    stale = read_lease(tmp_path, "r1")
    assert stale is not None
    assert reap_lease(tmp_path, "r1", reaper="a", expected=stale)
    assert not reap_lease(tmp_path, "r1", reaper="b", expected=stale)  # gone
    assert acquire_lease(tmp_path, "r1", "next", "", now=2.0)


def test_reap_lease_refuses_a_fresh_lease_it_did_not_observe(tmp_path: Path) -> None:
    """The CAS: reaper B saw the stale lease, but reaper A already reaped it
    and a fresh lease was written — B must restore, not steal."""
    from autoresearch.runstate import reap_lease

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


def test_load_record_maps_legacy_climb_job_id(tmp_path: Path) -> None:
    # an in-flight record written by pre-rename code carries climb_job_id;
    # load must map it to run_job_id so the sweep still ends a killed job
    import json

    from autoresearch.runstate import RECORD_NAME, load_record, run_dir

    d = run_dir(tmp_path, "r1")
    d.mkdir(parents=True)
    (d / RECORD_NAME).write_text(
        json.dumps(
            {
                "run_id": "r1",
                "target": "o/r",
                "task_title": "t",
                "state": "implementing",
                "climb_job_id": "16299",  # the legacy key
            }
        )
    )
    rec = load_record(tmp_path, "r1")
    assert rec.run_job_id == "16299"
    # a new record's run_job_id always wins over a stray legacy key
    (d / RECORD_NAME).write_text(
        json.dumps(
            {
                "run_id": "r1",
                "target": "o/r",
                "task_title": "t",
                "state": "implementing",
                "climb_job_id": "old",
                "run_job_id": "new",
            }
        )
    )
    assert load_record(tmp_path, "r1").run_job_id == "new"
