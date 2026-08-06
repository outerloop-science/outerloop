"""Run-state and lease semantics — the durable half of the agent."""

from __future__ import annotations

from pathlib import Path

import pytest

from autoresearch.runstate import (
    ENDED,
    STUCK,
    WAITING,
    RunRecord,
    acquire_lease,
    lease_is_stale,
    list_runs,
    load_record,
    read_lease,
    release_lease,
    run_dir,
    save_record,
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
    assert lease_is_stale(lease, now=1000.0 + 3601, ttl_s=3600, holder_alive=True)


def test_reap_lease_exactly_one_reaper_wins(tmp_path: Path) -> None:
    from autoresearch.runstate import reap_lease

    acquire_lease(tmp_path, "r1", "dead", "", now=1.0)
    assert reap_lease(tmp_path, "r1", reaper="a")
    assert not reap_lease(tmp_path, "r1", reaper="b")  # already gone
    assert acquire_lease(tmp_path, "r1", "next", "", now=2.0)


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
