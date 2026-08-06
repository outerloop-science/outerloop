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
    )
    record = RunRecord(**{**base, **overrides})
    # created/updated stamp: long enough ago that the grace window has passed
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
    record = waiting_run(tmp_path)
    save_record(tmp_path, record, now=NOW - 10)  # updated moments ago
    report, dispatcher = run_tick(tmp_path, FakeSlurm(states={"100": "COMPLETED"}))
    assert report.woken == ()
    assert dispatcher.dispatched == []


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
    report, dispatcher = run_tick(tmp_path, FakeSlurm(states={}))  # sacct empty
    assert dispatcher.dispatched == []


def test_gone_past_deadline_wakes_with_vanished(tmp_path: Path) -> None:
    waiting_run(tmp_path, deadline=NOW - 1)
    report, dispatcher = run_tick(tmp_path, FakeSlurm(states={}))
    assert report.woken == (("r1", "vanished"),)


def test_pending_past_deadline_cancels_then_wakes(tmp_path: Path) -> None:
    waiting_run(tmp_path, deadline=NOW - 1)
    slurm = FakeSlurm(states={"100": "PENDING"})
    report, dispatcher = run_tick(tmp_path, slurm)
    assert slurm.cancelled == ["100"]
    assert report.woken == (("r1", "unschedulable"),)


def test_pending_before_deadline_is_left_alone(tmp_path: Path) -> None:
    waiting_run(tmp_path)
    slurm = FakeSlurm(states={"100": "PENDING"})
    report, dispatcher = run_tick(tmp_path, slurm)
    assert slurm.cancelled == []
    assert dispatcher.dispatched == []


def test_running_experiment_is_left_alone(tmp_path: Path) -> None:
    waiting_run(tmp_path)
    report, dispatcher = run_tick(tmp_path, FakeSlurm(states={"100": "RUNNING"}))
    assert dispatcher.dispatched == []


def test_attempts_exhausted_becomes_stuck(tmp_path: Path) -> None:
    waiting_run(tmp_path, wake_attempts=3)
    report, dispatcher = run_tick(tmp_path, FakeSlurm(states={"100": "COMPLETED"}))
    assert report.stuck == ("r1",)
    assert dispatcher.dispatched == []
    ended = load_record(tmp_path, "r1")
    assert ended.state == ENDED
    assert ended.ending == STUCK


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
