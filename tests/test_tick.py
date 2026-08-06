"""The tick's fail-safe sweep, scenario by scenario (fake Slurm, fake wakes)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from autoresearch.compute import CommandResult, SlurmCompute
from autoresearch.runstate import (
    ENDED,
    STUCK,
    WAITING,
    RunRecord,
    acquire_lease,
    load_record,
    read_lease,
    save_record,
)
from autoresearch.tick import (
    PAUSE_SENTINEL,
    RecordingDispatcher,
    tick,
)

NOW = 1_000_000.0
GRACE = 900.0
TTL = 4500.0


@dataclass
class FakeSlurm:
    """status() by job id; '!' prefix means the query itself fails."""

    states: dict[str, str] = field(default_factory=dict)
    cancelled: list[str] = field(default_factory=list)

    def _runner(self, argv, timeout_s):
        if argv[0] == "sacct":
            job_id = argv[argv.index("-j") + 1]
            state = self.states.get(job_id, "")
            if state == "!":
                return CommandResult(1, "", "slurmdbd down")
            return CommandResult(0, state + "\n" if state else "", "")
        if argv[0] == "scancel":
            self.cancelled.append(argv[1])
            return CommandResult(0, "", "")
        raise AssertionError(f"unexpected command {argv}")

    def compute(self) -> SlurmCompute:
        return SlurmCompute(runner=self._runner)


def waiting_run(root: Path, run_id: str = "r1", **overrides) -> RunRecord:
    base = dict(
        run_id=run_id,
        target="org/repo",
        task_title="t",
        state=WAITING,
        experiment_job_id="100",
        deadline=NOW + 10_000,
        # default: the sweep already saw the experiment terminal, grace passed
        terminal_seen=NOW - GRACE - 1,
    )
    record = RunRecord(**{**base, **overrides})
    save_record(root, record, now=NOW - GRACE - 1)
    return record


def run_tick(root: Path, slurm: FakeSlurm, dispatcher=None, now: float = NOW):
    dispatcher = dispatcher if dispatcher is not None else RecordingDispatcher()
    report = tick(root, slurm.compute(), dispatcher, now=now)
    return report, dispatcher


def test_pause_sentinel_noops_but_heartbeats(tmp_path: Path) -> None:
    (tmp_path / PAUSE_SENTINEL).touch()
    waiting_run(tmp_path)
    report, dispatcher = run_tick(tmp_path, FakeSlurm(states={"100": "COMPLETED"}))
    assert report.paused
    assert dispatcher.dispatched == []
    assert (tmp_path / "heartbeat.json").exists()


def test_terminal_experiment_past_grace_gets_backup_wake(tmp_path: Path) -> None:
    waiting_run(tmp_path)
    report, dispatcher = run_tick(tmp_path, FakeSlurm(states={"100": "FAILED"}))
    assert report.woken == (("r1", "FAILED"),)
    assert dispatcher.dispatched == [("r1", "experiment FAILED")]
    # sync dispatch → lease released afterward
    assert read_lease(tmp_path, "r1") is None
    assert load_record(tmp_path, "r1").wake_attempts == 1


def test_terminal_within_grace_leaves_it_to_the_afterany_job(tmp_path: Path) -> None:
    waiting_run(tmp_path, terminal_seen=NOW - 10)  # first seen moments ago
    report, dispatcher = run_tick(tmp_path, FakeSlurm(states={"100": "COMPLETED"}))
    assert report.woken == ()
    assert dispatcher.dispatched == []


def test_first_terminal_sighting_starts_the_grace_clock_not_a_wake(tmp_path: Path) -> None:
    """Grace runs from when the sweep FIRST saw the experiment terminal — a
    24h experiment must not be woken by the backup the instant it completes
    (the afterany job owns the fresh case)."""
    waiting_run(tmp_path, terminal_seen=0.0)
    _report, dispatcher = run_tick(tmp_path, FakeSlurm(states={"100": "COMPLETED"}))
    assert dispatcher.dispatched == []
    assert load_record(tmp_path, "r1").terminal_seen == NOW
    # attempts untouched by the sighting
    assert load_record(tmp_path, "r1").wake_attempts == 0
    # next tick, grace elapsed → wake
    report2, _dispatcher2 = run_tick(
        tmp_path, FakeSlurm(states={"100": "COMPLETED"}), now=NOW + GRACE + 1
    )
    assert report2.woken == (("r1", "COMPLETED"),)


def test_dry_run_reports_without_any_writes(tmp_path: Path) -> None:
    """python -m autoresearch.tick runs exactly this until phase 5: a healthy
    completed run must survive any number of dry ticks unchanged."""
    from autoresearch.tick import RecordingDispatcher as RD
    from autoresearch.tick import tick as tick_fn

    waiting_run(tmp_path)
    slurm = FakeSlurm(states={"100": "COMPLETED"})
    dispatcher = RD()
    for i in range(5):
        report = tick_fn(tmp_path, slurm.compute(), dispatcher, now=NOW + i * 60, dry_run=True)
        assert report.woken == (("r1", "COMPLETED"),)
    after = load_record(tmp_path, "r1")
    assert after.wake_attempts == 0
    assert after.state == WAITING
    assert dispatcher.dispatched == []
    assert read_lease(tmp_path, "r1") is None


def test_live_lease_blocks_the_sweep(tmp_path: Path) -> None:
    waiting_run(tmp_path)
    acquire_lease(tmp_path, "r1", "wake-job:55", "55", now=NOW - 60)
    report, dispatcher = run_tick(tmp_path, FakeSlurm(states={"100": "COMPLETED", "55": "RUNNING"}))
    assert dispatcher.dispatched == []
    assert report.reaped_leases == ()


def test_dead_holder_lease_is_reaped_then_next_tick_wakes(tmp_path: Path) -> None:
    waiting_run(tmp_path)
    acquire_lease(tmp_path, "r1", "wake-job:55", "55", now=NOW - 60)
    slurm = FakeSlurm(states={"100": "COMPLETED", "55": "FAILED"})
    report, dispatcher = run_tick(tmp_path, slurm)
    assert report.reaped_leases == ("r1",)
    # the same sweep pass continues after reaping — wake delivered
    assert dispatcher.dispatched == [("r1", "experiment COMPLETED")]


def test_query_failure_defers_never_concludes(tmp_path: Path) -> None:
    waiting_run(tmp_path, deadline=NOW - 1)  # even past deadline!
    report, dispatcher = run_tick(tmp_path, FakeSlurm(states={"100": "!"}))
    assert report.deferred == ("r1",)
    assert dispatcher.dispatched == []
    assert load_record(tmp_path, "r1").state == WAITING


def test_gone_before_deadline_waits_for_sacct_lag(tmp_path: Path) -> None:
    waiting_run(tmp_path)  # deadline far in the future
    _report, dispatcher = run_tick(tmp_path, FakeSlurm(states={}))  # sacct empty
    assert dispatcher.dispatched == []


def test_gone_past_deadline_wakes_with_vanished(tmp_path: Path) -> None:
    waiting_run(tmp_path, deadline=NOW - 1)
    report, _dispatcher = run_tick(tmp_path, FakeSlurm(states={}))
    assert report.woken == (("r1", "vanished"),)


def test_pending_past_deadline_cancels_then_wakes(tmp_path: Path) -> None:
    waiting_run(tmp_path, deadline=NOW - 1)
    slurm = FakeSlurm(states={"100": "PENDING"})
    report, _dispatcher = run_tick(tmp_path, slurm)
    assert slurm.cancelled == ["100"]
    assert report.woken == (("r1", "unschedulable"),)


def test_pending_before_deadline_is_left_alone(tmp_path: Path) -> None:
    waiting_run(tmp_path)
    slurm = FakeSlurm(states={"100": "PENDING"})
    _report, dispatcher = run_tick(tmp_path, slurm)
    assert slurm.cancelled == []
    assert dispatcher.dispatched == []


def test_running_experiment_is_left_alone(tmp_path: Path) -> None:
    waiting_run(tmp_path)
    _report, dispatcher = run_tick(tmp_path, FakeSlurm(states={"100": "RUNNING"}))
    assert dispatcher.dispatched == []


def test_attempts_exhausted_becomes_stuck(tmp_path: Path) -> None:
    waiting_run(tmp_path, wake_attempts=3)
    report, dispatcher = run_tick(tmp_path, FakeSlurm(states={"100": "COMPLETED"}))
    assert report.stuck == ("r1",)
    assert dispatcher.dispatched == []
    ended = load_record(tmp_path, "r1")
    assert ended.state == ENDED
    assert ended.ending == STUCK


def test_live_lease_defers_even_the_stuck_verdict(tmp_path: Path) -> None:
    """Attempt 3's wake session may be the one that succeeds — a run with a
    LIVE lease must not be truncated to stuck underneath it."""
    waiting_run(tmp_path, wake_attempts=3)
    acquire_lease(tmp_path, "r1", "wake-job:55", "55", now=NOW - 60)
    report, _dispatcher = run_tick(
        tmp_path, FakeSlurm(states={"100": "COMPLETED", "55": "RUNNING"})
    )
    assert report.stuck == ()
    assert load_record(tmp_path, "r1").state == WAITING


def test_tick_held_lease_reaped_by_ttl_alone(tmp_path: Path) -> None:
    """A crashed tick's lease has no holder job id — only the TTL can free
    it. This is the sole escape path; a regression here strands runs."""
    waiting_run(tmp_path)
    acquire_lease(tmp_path, "r1", "tick:dead-host:1", "", now=NOW - TTL - 1)
    report, dispatcher = run_tick(tmp_path, FakeSlurm(states={"100": "COMPLETED"}))
    assert report.reaped_leases == ("r1",)
    assert dispatcher.dispatched == [("r1", "experiment COMPLETED")]


def test_corrupt_empty_lease_is_reaped_via_mtime(tmp_path: Path) -> None:
    """A crash between lease create and write must not strand the run."""
    import os as _os

    waiting_run(tmp_path)
    lease_path = tmp_path / "runs" / "r1" / "lease.json"
    lease_path.touch()  # empty: unreadable as JSON
    old = NOW - TTL - 100
    _os.utime(lease_path, (old, old))
    report, dispatcher = run_tick(tmp_path, FakeSlurm(states={"100": "COMPLETED"}))
    assert report.reaped_leases == ("r1",)
    assert dispatcher.dispatched == [("r1", "experiment COMPLETED")]


def test_legacy_zero_deadline_still_wakes_gone_runs(tmp_path: Path) -> None:
    """save_record forbids new waiting runs without a deadline, but a legacy
    record must not be immortal: GONE wakes anyway (safe), PENDING does not
    get cancelled (destructive)."""
    import json as _json

    directory = tmp_path / "runs" / "legacy"
    directory.mkdir(parents=True)
    record = dict(
        run_id="legacy",
        target="o/r",
        task_title="t",
        state=WAITING,
        agent_id="a",
        experiment_job_id="100",
        wake_job_id="",
        resume_session_id="",
        wake_attempts=0,
        deadline=0.0,
        terminal_seen=0.0,
        ending="",
        ending_note="",
        created=NOW - 5000,
        updated=NOW - 5000,
    )
    (directory / "state.json").write_text(_json.dumps(record))
    report, _ = run_tick(tmp_path, FakeSlurm(states={}))  # GONE
    assert report.woken == (("legacy", "vanished"),)

    (directory / "state.json").write_text(_json.dumps(record))
    slurm = FakeSlurm(states={"100": "PENDING"})
    _report2, _ = run_tick(tmp_path, slurm)
    assert slurm.cancelled == []  # healthy pending job never cancelled


def test_dispatch_exception_releases_lease_and_counts_attempt(tmp_path: Path) -> None:
    waiting_run(tmp_path)

    class ExplodingDispatcher:
        def dispatch(self, record, reason):
            raise RuntimeError("boom")

    report, _ = run_tick(tmp_path, FakeSlurm(states={"100": "COMPLETED"}), ExplodingDispatcher())
    assert report.woken == ()
    assert read_lease(tmp_path, "r1") is None  # released, not stranded
    assert load_record(tmp_path, "r1").wake_attempts == 1  # counts toward stuck


def test_async_dispatch_hands_lease_to_wake_job(tmp_path: Path) -> None:
    waiting_run(tmp_path)
    dispatcher = RecordingDispatcher(holder_job_id="777")
    report, _ = run_tick(tmp_path, FakeSlurm(states={"100": "COMPLETED"}), dispatcher)
    assert report.woken == (("r1", "COMPLETED"),)
    lease = read_lease(tmp_path, "r1")
    assert lease is not None
    assert lease.holder_job_id == "777"


def test_double_tick_no_double_wake_with_async_dispatch(tmp_path: Path) -> None:
    """The lease is exactly what makes the backup layer idempotent."""
    waiting_run(tmp_path)
    slurm = FakeSlurm(states={"100": "COMPLETED", "777": "RUNNING"})
    dispatcher = RecordingDispatcher(holder_job_id="777")
    run_tick(tmp_path, slurm, dispatcher)
    run_tick(tmp_path, slurm, dispatcher, now=NOW + 60)
    assert len(dispatcher.dispatched) == 1


def test_ended_and_non_waiting_runs_are_ignored(tmp_path: Path) -> None:
    waiting_run(tmp_path, run_id="active", state="implementing")
    save_record(
        tmp_path,
        RunRecord(run_id="done", target="o/r", task_title="t", state=ENDED, ending="merged"),
        now=NOW - 5000,
    )
    report, dispatcher = run_tick(tmp_path, FakeSlurm())
    assert report.swept == 0
    assert dispatcher.dispatched == []


def test_heartbeat_written_every_tick(tmp_path: Path) -> None:
    run_tick(tmp_path, FakeSlurm())
    import json

    beat = json.loads((tmp_path / "heartbeat.json").read_text())
    assert beat["ts"] == NOW
    assert "host" in beat


def test_one_bad_record_does_not_blind_the_sweep(tmp_path: Path) -> None:
    """Per-record isolation: a record that makes processing raise must not
    stop the remaining runs from being swept."""
    import json as _json

    # legacy zero-deadline record whose experiment is TERMINAL: the sighting
    # save would raise without the repair; either way the sweep must go on
    directory = tmp_path / "runs" / "a-bad"
    directory.mkdir(parents=True)
    record = dict(
        run_id="a-bad",
        target="o/r",
        task_title="t",
        state=WAITING,
        agent_id="a",
        experiment_job_id="200",
        wake_job_id="",
        resume_session_id="",
        wake_attempts=0,
        deadline=0.0,
        terminal_seen=0.0,
        ending="",
        ending_note="",
        created=NOW - 5000,
        updated=NOW - 5000,
    )
    (directory / "state.json").write_text(_json.dumps(record))
    waiting_run(tmp_path, run_id="z-good")
    slurm = FakeSlurm(states={"200": "COMPLETED", "100": "COMPLETED"})
    report, _dispatcher = run_tick(tmp_path, slurm)
    assert ("z-good", "COMPLETED") in report.woken  # the good run was served


def test_legacy_terminal_record_gets_deadline_repaired_on_sighting(tmp_path: Path) -> None:
    import json as _json

    directory = tmp_path / "runs" / "legacy2"
    directory.mkdir(parents=True)
    record = dict(
        run_id="legacy2",
        target="o/r",
        task_title="t",
        state=WAITING,
        agent_id="a",
        experiment_job_id="300",
        wake_job_id="",
        resume_session_id="",
        wake_attempts=0,
        deadline=0.0,
        terminal_seen=0.0,
        ending="",
        ending_note="",
        created=NOW - 5000,
        updated=NOW - 5000,
    )
    (directory / "state.json").write_text(_json.dumps(record))
    run_tick(tmp_path, FakeSlurm(states={"300": "FAILED"}))
    repaired = load_record(tmp_path, "legacy2")
    assert repaired.terminal_seen == NOW
    assert repaired.deadline > 0


def test_cli_grace_flag_reaches_the_sweep(tmp_path: Path, monkeypatch) -> None:
    """--grace-s must actually change sweep behavior (was parsed-but-ignored)."""
    import sys

    import autoresearch.tick as tick_mod

    waiting_run(tmp_path, terminal_seen=NOW - 5)  # 5s since sighting
    captured: dict = {}

    real_tick = tick_mod.tick

    def spy(root, compute, dispatcher, now, grace_s=0, lease_ttl_s=0, dry_run=False):
        captured["grace_s"] = grace_s
        return real_tick(root, compute, dispatcher, now, grace_s, lease_ttl_s, dry_run)

    monkeypatch.setattr(tick_mod, "tick", spy)
    monkeypatch.setattr(tick_mod, "SlurmCompute", lambda: FakeSlurm(states={}).compute())
    monkeypatch.setattr(sys, "argv", ["tick", "--root", str(tmp_path), "--grace-s", "1"])
    assert tick_mod.main() == 0
    assert captured["grace_s"] == 1.0
