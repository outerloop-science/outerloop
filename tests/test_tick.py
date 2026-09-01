"""The tick's fail-safe sweep, scenario by scenario (fake Slurm, fake wakes)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

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
    DEFAULT_MIN_TICK_S,
    PAUSE_SENTINEL,
    WORK_MARKER_NAME,
    RecordingDispatcher,
    _mark_worked,
    mark_tick_complete,
    tick,
    write_heartbeat,
)

NOW = 1_000_000.0
GRACE = 900.0
TTL = 4500.0


@dataclass
class FakeSlurm:
    """status() by job id; '!' prefix means the query itself fails."""

    states: dict[str, str] = field(default_factory=dict)
    cancelled: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)  # squeue %r by job id
    cancel_sticks: bool = True  # scancel moves the job to CANCELLED, like Slurm

    def _runner(self, argv, timeout_s):
        if argv[0] == "sacct":
            job_id = argv[argv.index("-j") + 1]
            state = self.states.get(job_id, "")
            if state == "!":
                return CommandResult(1, "", "slurmdbd down")
            return CommandResult(0, state + "\n" if state else "", "")
        if argv[0] == "scancel":
            self.cancelled.append(argv[1])
            if self.cancel_sticks:
                self.states[argv[1]] = "CANCELLED"
            return CommandResult(0, "", "")
        if argv[0] == "squeue":
            if "-j" in argv:
                reason = self.reasons.get(argv[argv.index("-j") + 1], "")
                return CommandResult(0, reason + "\n" if reason else "", "")
            return CommandResult(0, "", "")  # no live jobs
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


def run_tick(
    root: Path,
    slurm: FakeSlurm,
    dispatcher=None,
    now: float = NOW,
    min_tick_s: float = DEFAULT_MIN_TICK_S,
):
    dispatcher = dispatcher if dispatcher is not None else RecordingDispatcher()
    report = tick(root, slurm.compute(), dispatcher, now=now, min_tick_s=min_tick_s)
    # mirror main(): the caller stamps the coalesce marker at completion
    mark_tick_complete(root, report, now)
    return report, dispatcher


def test_pause_sentinel_noops_but_heartbeats(tmp_path: Path) -> None:
    (tmp_path / PAUSE_SENTINEL).touch()
    waiting_run(tmp_path)
    report, dispatcher = run_tick(tmp_path, FakeSlurm(states={"100": "COMPLETED"}))
    assert report.paused
    assert dispatcher.dispatched == []
    assert (tmp_path / "heartbeat.json").exists()


def test_coalesce_skips_a_pileup_below_the_window_but_feeds_the_watchdog(tmp_path: Path) -> None:
    slurm = FakeSlurm(states={})
    # a completed tick 60s ago -> the next tick is a pile-up (< 10-min window)
    _mark_worked(tmp_path, NOW - 60)
    # seed a STALE heartbeat so the assertion below proves the COALESCED tick
    # refreshed it (not just that some earlier tick did)
    write_heartbeat(tmp_path, NOW - 9999)
    report, _ = run_tick(tmp_path, slurm)
    assert report.coalesced and not report.paused
    # the coalesced tick advanced the heartbeat NOW-9999 -> NOW: watchdog fed
    assert json.loads((tmp_path / "heartbeat.json").read_text())["ts"] == NOW
    # a completed tick 30 min ago (>= window) -> proceeds normally
    _mark_worked(tmp_path, NOW - 1800)
    report, _ = run_tick(tmp_path, slurm)
    assert not report.coalesced
    # a marker dated in the FUTURE (clock skew) -> never coalesce, always proceed
    _mark_worked(tmp_path, NOW + 5000)
    report, _ = run_tick(tmp_path, slurm)
    assert not report.coalesced


def test_a_crashed_tick_does_not_suppress_the_recovery_tick(tmp_path: Path) -> None:
    # the coalesce guard keys on WORK COMPLETION, not tick start: a tick that
    # crashed mid-work leaves a fresh heartbeat but NO work marker, so the next
    # tick must still run (recovery), not coalesce on the crashed tick's start.
    write_heartbeat(tmp_path, NOW - 5)  # crashed tick started 5s ago (very recent)
    # (no work marker — the crashed tick never reached its end)
    report, _ = run_tick(tmp_path, FakeSlurm(states={}))
    assert not report.coalesced  # recovery proceeds despite the fresh heartbeat
    assert (tmp_path / WORK_MARKER_NAME).exists()  # and this tick stamped the marker


def test_coalesce_is_disablable_and_pause_takes_precedence(tmp_path: Path) -> None:
    slurm = FakeSlurm(states={})
    # min_tick_s=0 disables coalescing even right after a completed tick
    _mark_worked(tmp_path, NOW - 1)
    report = tick(tmp_path, slurm.compute(), RecordingDispatcher(), now=NOW, min_tick_s=0)
    assert not report.coalesced
    # PAUSE wins over coalesce (paused reported, not coalesced)
    (tmp_path / PAUSE_SENTINEL).touch()
    _mark_worked(tmp_path, NOW - 1)
    report = tick(tmp_path, slurm.compute(), RecordingDispatcher(), now=NOW)
    assert report.paused and not report.coalesced


def test_mark_tick_complete_stamps_only_a_worked_tick_at_completion_time(tmp_path: Path) -> None:
    from autoresearch.tick import TickReport, _last_worked_ts

    # a worked tick stamps the marker at the COMPLETION time the caller passes
    # (not the start-of-tick `now`) — so a long tick leaves a fresh marker
    mark_tick_complete(tmp_path, TickReport(), NOW + 999)
    assert _last_worked_ts(tmp_path) == NOW + 999
    # a paused or coalesced tick did no work -> marker left untouched
    mark_tick_complete(tmp_path, TickReport(paused=True), NOW + 5000)
    mark_tick_complete(tmp_path, TickReport(coalesced=True), NOW + 6000)
    assert _last_worked_ts(tmp_path) == NOW + 999


def test_last_worked_ts_survives_a_corrupt_marker(tmp_path: Path) -> None:
    from autoresearch.tick import _last_worked_ts

    marker = tmp_path / WORK_MARKER_NAME
    for bad in (
        '{"ts": 1e400}',  # parses to inf
        '{"ts": ' + "9" * 400 + "}",  # huge int -> float() OverflowError
        '{"ts": true}',  # bool (int subclass) must not count
        '{"ts": "x"}',  # wrong type
        "not json",  # unparseable
        "[]",  # not a dict
        "[" * 6000 + "]" * 6000,  # deeply nested -> RecursionError
    ):
        marker.write_text(bad)
        assert _last_worked_ts(tmp_path) is None  # None, never a crash
    _mark_worked(tmp_path, NOW)  # a normal marker still reads back
    assert _last_worked_ts(tmp_path) == NOW


def test_min_tick_s_from_env_parses_clamps_and_rejects(monkeypatch) -> None:
    from autoresearch.tick import _min_tick_s_from_env

    # default cadence (30 min) -> safe ceiling = half-cadence = 15 min = 900s
    monkeypatch.delenv("AUTORESEARCH_CADENCE_MIN", raising=False)
    monkeypatch.delenv("AUTORESEARCH_MIN_TICK_MINUTES", raising=False)
    assert _min_tick_s_from_env() == DEFAULT_MIN_TICK_S  # unset -> default (10 min < ceiling)
    monkeypatch.setenv("AUTORESEARCH_MIN_TICK_MINUTES", "5")
    assert _min_tick_s_from_env() == 300.0
    monkeypatch.setenv("AUTORESEARCH_MIN_TICK_MINUTES", "0")
    assert _min_tick_s_from_env() == 0.0  # disables
    for bad in ("inf", "nan", "-inf", "abc"):  # non-finite / non-numeric -> default
        monkeypatch.setenv("AUTORESEARCH_MIN_TICK_MINUTES", bad)
        assert _min_tick_s_from_env() == DEFAULT_MIN_TICK_S
    # a window at/above half the cadence is clamped so normal ticks aren't coalesced
    monkeypatch.setenv("AUTORESEARCH_MIN_TICK_MINUTES", "9999")
    assert _min_tick_s_from_env() == 900.0  # clamped to the 15-min ceiling
    monkeypatch.setenv("AUTORESEARCH_MIN_TICK_MINUTES", "25")  # > 15 (half of 30-min cadence)
    assert _min_tick_s_from_env() == 900.0
    monkeypatch.setenv("AUTORESEARCH_CADENCE_MIN", "10")  # ceiling tracks the cadence
    assert _min_tick_s_from_env() == 300.0  # clamped to half of 10 min = 5 min


def test_default_coalesce_window_scales_with_cadence(monkeypatch) -> None:
    from autoresearch.tick import _default_min_tick_s, _min_tick_s_from_env

    monkeypatch.delenv("AUTORESEARCH_MIN_TICK_MINUTES", raising=False)
    # no cadence set -> assume 30-min cadence -> min(10, 15) = 10 min
    monkeypatch.delenv("AUTORESEARCH_CADENCE_MIN", raising=False)
    assert _default_min_tick_s() == DEFAULT_MIN_TICK_S
    # a SHORT cadence scales the window down (half-cadence), so the default can't
    # swallow every on-cadence tick
    monkeypatch.setenv("AUTORESEARCH_CADENCE_MIN", "6")
    assert _default_min_tick_s() == 180.0  # 6-min cadence -> 3-min window
    # a long cadence stays capped at the ceiling
    monkeypatch.setenv("AUTORESEARCH_CADENCE_MIN", "120")
    assert _default_min_tick_s() == DEFAULT_MIN_TICK_S
    # garbage cadence -> 30-min fallback -> 10 min
    monkeypatch.setenv("AUTORESEARCH_CADENCE_MIN", "abc")
    assert _default_min_tick_s() == DEFAULT_MIN_TICK_S
    # and the unset MIN_TICK path uses this cadence-aware default
    monkeypatch.setenv("AUTORESEARCH_CADENCE_MIN", "6")
    assert _min_tick_s_from_env() == 180.0


def test_terminal_experiment_past_grace_gets_backup_wake(tmp_path: Path) -> None:
    waiting_run(tmp_path)
    report, dispatcher = run_tick(tmp_path, FakeSlurm(states={"100": "FAILED"}))
    assert report.woken == (("r1", "FAILED"),)
    assert dispatcher.dispatched == [("r1", "experiment FAILED")]
    # sync dispatch → lease released afterward
    assert read_lease(tmp_path, "r1") is None
    assert load_record(tmp_path, "r1").wake_attempts == 1


def test_blind_park_with_no_job_id_wakes_on_its_deadline(tmp_path: Path) -> None:
    # a park the measurer could not attach a job id to (Slurm was blind) still
    # hibernated with a deadline — the deadline floor is its ONLY wake.
    waiting_run(tmp_path, experiment_job_id="", deadline=NOW - 1)
    _, dispatcher = run_tick(tmp_path, FakeSlurm(states={}))
    assert dispatcher.dispatched == [("r1", "blind park past deadline")]


def test_blind_park_before_its_deadline_is_left_alone(tmp_path: Path) -> None:
    waiting_run(tmp_path, experiment_job_id="", deadline=NOW + 10_000)
    _, dispatcher = run_tick(tmp_path, FakeSlurm(states={}))
    assert dispatcher.dispatched == []


def test_checkpoint_sleep_park_deadline_is_near_term() -> None:
    """A jobless checkpoint sleep must not inherit the 12h QUEUE slack (it has
    nothing in any queue) — its deadline reaches only the next sweep pass.
    Observed live (yolo heldout_probe, 2026-08-27): a nap became a 12h coma,
    and the existing deadline-floor branch then wakes it promptly."""
    from autoresearch.attempt import CHECKPOINT_SLEEP_SLACK_MIN, PARK_QUEUE_SLACK_MIN

    assert CHECKPOINT_SLEEP_SLACK_MIN * 60 < 3600  # near-term, sub-hour
    assert PARK_QUEUE_SLACK_MIN == 12 * 60  # queue slack untouched


def test_jobless_park_past_deadline_wakes_via_floor(tmp_path: Path) -> None:
    # the sweep side of the checkpoint-sleep story: once its (now near-term)
    # deadline passes, the existing blind-park floor delivers the wake — no
    # special predicate that could over-match blind re-parks or long sleeps
    waiting_run(
        tmp_path,
        experiment_job_id="",
        deadline=NOW - 1,
        stage={
            "afterany": "",
            "base_branch": "main",
            "base_sha": "b" * 40,
            "candidate_ref": "refs/autoresearch/r1",
            "candidate_sha": "c" * 40,
            "phase": "author-sleep",
            "syscall_launches": [],
        },
    )
    _, dispatcher = run_tick(tmp_path, FakeSlurm(states={}))
    assert dispatcher.dispatched == [("r1", "blind park past deadline")]


def test_multi_job_park_wakes_when_all_afterany_jobs_finish(tmp_path: Path) -> None:
    """A multi-job park (candidate + siblings, several author launches)
    records no single experiment_job_id — the sweep polls every id in the
    stage's afterany string instead of riding the deadline floor for hours
    (observed live 2026-08-25)."""
    waiting_run(
        tmp_path,
        experiment_job_id="",
        stage={"afterany": "afterany:200:201", "phase": "candidate"},
    )
    report, _ = run_tick(tmp_path, FakeSlurm(states={"200": "COMPLETED", "201": "COMPLETED"}))
    assert report.woken == (("r1", "COMPLETED"),)


def test_multi_job_park_waits_while_any_afterany_job_runs(tmp_path: Path) -> None:
    waiting_run(
        tmp_path,
        experiment_job_id="",
        terminal_seen=0.0,
        stage={"afterany": "afterany:200:201", "phase": "candidate"},
    )
    _, dispatcher = run_tick(tmp_path, FakeSlurm(states={"200": "COMPLETED", "201": "RUNNING"}))
    assert dispatcher.dispatched == []
    # not all done: the grace clock must NOT start on a partial finish
    assert load_record(tmp_path, "r1").terminal_seen == 0.0


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
        # min_tick_s=0: this test exercises dry-run idempotency across repeated
        # sweeps, not the coalesce guard (which would skip these 60s-apart ticks)
        report = tick_fn(
            tmp_path, slurm.compute(), dispatcher, now=NOW + i * 60, dry_run=True, min_tick_s=0
        )
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
    # min_tick_s=0: this test exercises the sweep twice, not the coalesce guard
    report, _ = run_tick(tmp_path, FakeSlurm(states={}), min_tick_s=0)  # GONE
    assert report.woken == (("legacy", "vanished"),)

    (directory / "state.json").write_text(_json.dumps(record))
    slurm = FakeSlurm(states={"100": "PENDING"})
    _report2, _ = run_tick(tmp_path, slurm, min_tick_s=0)
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
    # min_tick_s=0: both sweeps must run — the lease (not coalescing) is what
    # makes the second wake idempotent, which is exactly what this asserts
    run_tick(tmp_path, slurm, dispatcher, min_tick_s=0)
    run_tick(tmp_path, slurm, dispatcher, now=NOW + 60, min_tick_s=0)
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

    def spy(root, compute, dispatcher, now, grace_s=0, lease_ttl_s=0, dry_run=False, **kw):
        captured["grace_s"] = grace_s
        return real_tick(root, compute, dispatcher, now, grace_s, lease_ttl_s, dry_run)

    monkeypatch.setattr(tick_mod, "tick", spy)
    monkeypatch.setattr(tick_mod, "SlurmCompute", lambda: FakeSlurm(states={}).compute())
    monkeypatch.setattr(sys, "argv", ["tick", "--root", str(tmp_path), "--grace-s", "1"])
    assert tick_mod.main() == 0
    assert captured["grace_s"] == 1.0


def test_service_in_review_submits_followup_once(tmp_path: Path) -> None:
    """Comment present → one job submitted, recorded; second tick skips while
    that job is queued/running."""
    from autoresearch.compute import CommandResult
    from autoresearch.runstate import IN_REVIEW, RunRecord, save_record
    from autoresearch.tick import FollowupSpec, service_in_review

    record = RunRecord(
        run_id="r-rev",
        target="org/pilot",
        task_title="improve tsp",
        benchmark="tsp",
        state=IN_REVIEW,
        pr_url="https://github.com/org/pilot/pull/6",
    )
    save_record(tmp_path, record, now=NOW)

    class G:
        def get_pull_request(self, repo, number):
            return {"state": "open", "merged": False}

        def list_comments(self, repo, number, max_pages=20):
            return [
                {
                    "id": 9,
                    "body": "explain",
                    "user": {"login": "renmengye"},
                    "author_association": "MEMBER",
                }
            ]

        def list_pr_reviews(self, repo, number, max_pages=10):
            return []

        def list_pr_review_comments(self, repo, number, max_pages=10):
            return []

    submits = []

    def runner(argv, timeout_s):
        if argv[0] == "sbatch":
            submits.append(list(argv))
            return CommandResult(0, "4242\n", "")
        if argv[0] == "sacct":
            return CommandResult(0, "RUNNING\n", "")
        raise AssertionError(argv)

    compute = SlurmCompute(runner=runner)
    spec = FollowupSpec(
        account="acct",
        partition="cpu_short",
        run_root=tmp_path,
        image="/img/a.sif",
        home=Path("/home/x/autoresearch"),
    )
    _ended, submitted = service_in_review(tmp_path, G(), compute, spec, NOW)
    assert submitted == [("r-rev", "4242")]
    assert any("autoresearch.followup" in a for a in submits[0])
    from autoresearch.runstate import load_record as lr

    assert lr(tmp_path, "r-rev").followup_job_id == "4242"
    # second pass: job RUNNING → no duplicate
    _ended2, submitted2 = service_in_review(tmp_path, G(), compute, spec, NOW + 60)
    assert submitted2 == []


def test_service_in_review_ends_merged_runs(tmp_path: Path) -> None:
    from autoresearch.runstate import IN_REVIEW, RunRecord, load_record, save_record
    from autoresearch.tick import FollowupSpec, service_in_review

    record = RunRecord(
        run_id="r-m",
        target="org/pilot",
        task_title="improve tsp",
        benchmark="tsp",
        state=IN_REVIEW,
        pr_url="https://github.com/org/pilot/pull/7",
    )
    save_record(tmp_path, record, now=NOW)

    class G:
        def get_pull_request(self, repo, number):
            return {"state": "closed", "merged": True}

    spec = FollowupSpec(
        account="a", partition="p", run_root=tmp_path, image="/i.sif", home=Path("/h")
    )

    def unused_runner(argv, timeout_s):
        if argv[0] == "squeue":  # the flight reaper's liveness query
            return CommandResult(0, "", "")
        raise AssertionError("no slurm calls expected")

    ended, _submitted = service_in_review(
        tmp_path, G(), SlurmCompute(runner=unused_runner), spec, NOW
    )
    assert ended == [("r-m", "merged")]
    assert load_record(tmp_path, "r-m").ending == "merged"


def _self_contract():
    from autoresearch.contract import load_contract

    return load_contract(
        """
benchmarks:
  - {name: tsp, command: c, metric: m, direction: min}
  - {name: denoise, command: c, metric: m, direction: min}
  - {name: reach, command: c, metric: m, direction: max}
budgets: {gpu_hours_per_run: 1, runs_per_week: 3}
scope: {allowed: [src/]}
roadmap: docs/roadmap.md
""",
        "org/pilot",
    )


def _run(run_id, benchmark, created, state=ENDED, ending="negative-result", target="org/pilot"):
    return RunRecord(
        run_id=run_id,
        target=target,
        task_title="t",
        benchmark=benchmark,
        state=state,
        ending=ending if state == ENDED else "",
        created=created,
        updated=created,
    )


def test_self_initiated_selection_rules() -> None:
    from autoresearch.tick import pick_self_initiated

    contract = _self_contract()
    pick = lambda records, **kw: pick_self_initiated(records, contract, "org/pilot", now=NOW, **kw)

    # empty history -> alphabetically-first untouched benchmark
    assert pick([]) == "denoise"
    # an ACTIVE run serializes: nothing new
    assert pick([_run("a", "tsp", NOW - 100, state="implementing", ending="")]) is None
    # cooldown: recently-attempted benchmarks skipped, oldest eligible picked
    records = [
        _run("a", "denoise", NOW - 60),
        _run("b", "reach", NOW - 8 * 3600),
    ]
    assert pick(records) == "tsp"
    # weekly budget: 3 runs in the window -> None
    records = [_run(f"r{i}", "tsp", NOW - i * 3600) for i in range(3)]
    assert pick(records) is None
    # a pending attempt that died pre-record still counts toward cooldown
    assert pick([], dead_attempts={"denoise": NOW - 60}) == "reach"


def test_self_initiated_scoped_to_target() -> None:
    from autoresearch.tick import pick_self_initiated

    contract = _self_contract()
    # active run + full budget on ANOTHER target: neither blocks org/pilot
    other = [_run(f"o{i}", "tsp", NOW - i * 60, target="org/other") for i in range(3)]
    other.append(_run("oa", "tsp", NOW - 30, state="implementing", ending="", target="org/other"))
    assert pick_self_initiated(other, contract, "org/pilot", now=NOW) == "denoise"
    # while org/other itself is both serialized and over budget
    assert pick_self_initiated(other, contract, "org/other", now=NOW) is None


def test_self_initiated_stranded_implementing_unblocks() -> None:
    from autoresearch.tick import STRANDED_IMPLEMENTING_S, pick_self_initiated

    contract = _self_contract()
    stale = STRANDED_IMPLEMENTING_S + 3600
    dead = _run("d", "tsp", NOW - stale, state="implementing", ending="")
    # a crashed climb job's record stops counting as active after the window,
    # but its benchmark keeps its cooldown slot: another one is picked
    assert pick_self_initiated([dead], contract, "org/pilot", now=NOW) == "denoise"
    # a FRESH implementing run still serializes
    fresh = _run("f", "tsp", NOW - 60, state="implementing", ending="")
    assert pick_self_initiated([fresh], contract, "org/pilot", now=NOW) is None


def test_self_initiated_pending_marker_blocks_duplicates(tmp_path: Path) -> None:
    from autoresearch.tick import (
        FollowupSpec,
        read_pending,
        service_self_initiated,
    )

    contract = _self_contract()
    panel_key = tmp_path / "verifier_key"
    panel_key.write_text("k")  # preflight: no usable key, no launch
    panel_key.chmod(0o600)
    spec = FollowupSpec(
        target="org/pilot",
        account="acct",
        partition="part",
        run_root=tmp_path,
        image="img.sif",
        home=tmp_path,
        bot_login="bot",
        panel_key_file=str(panel_key),
    )
    submitted: list[str] = []

    def runner(argv, timeout_s):
        if argv[0] == "sbatch":
            submitted.append(" ".join(argv))
            return CommandResult(0, "123\n", "")
        return CommandResult(0, "RUNNING\n", "")  # sacct liveness probe

    compute = SlurmCompute(runner=runner)
    out = service_self_initiated(tmp_path, compute, spec, contract, NOW)
    assert out == ("denoise", "123")
    marker = read_pending(tmp_path, "org/pilot", "agent-01")
    assert marker is not None and marker["benchmark"] == "denoise"
    # next tick, record not yet written, job alive -> NO duplicate submit
    assert service_self_initiated(tmp_path, compute, spec, contract, NOW + 1800) is None
    assert len(submitted) == 1
    # once the climb writes its run record, the marker clears and the
    # active-run serialization (not the marker) is what blocks
    save_record(
        tmp_path,
        _run("r-live", "denoise", NOW + 1900, state="implementing", ending=""),
        now=NOW + 1900,
    )
    assert service_self_initiated(tmp_path, compute, spec, contract, NOW + 3600) is None
    assert read_pending(tmp_path, "org/pilot", "agent-01") is None
    assert len(submitted) == 1


def test_disk_preflight_gates_launch_lanes(tmp_path: Path) -> None:
    """A failed preflight turns every LAUNCH lane off for the tick (no
    follow-up submissions, no intake, no self-initiated) but the tick still
    heartbeats and reports why."""
    import json as _json

    from autoresearch.tick import FollowupSpec, tick

    class G:
        def get_file_content(self, repo, path, ref):
            raise AssertionError("self-initiated must not even fetch the contract")

        def list_issues(self, *a, **k):
            raise AssertionError("intake must not scan issues")

    spec = FollowupSpec(
        target="org/pilot",
        account="a",
        partition="p",
        run_root=tmp_path,
        image="img.sif",
        home=tmp_path,
    )

    def unused_runner(argv, timeout_s):
        if argv[0] == "squeue":  # the flight reaper's liveness query
            return CommandResult(0, "", "")
        raise AssertionError("no slurm calls expected")

    report = tick(
        tmp_path,
        SlurmCompute(runner=unused_runner),
        RecordingDispatcher(),
        now=NOW,
        github=G(),
        followup_spec=spec,
        min_free_bytes=2**62,  # no filesystem passes: forces the block
    )
    assert report.disk and any("BLOCKED" in w for w in report.disk)
    assert report.launch_blocked is True
    assert report.intake == ("", "") and report.self_initiated == ("", "")
    heartbeat = _json.loads((tmp_path / "heartbeat.json").read_text())
    assert heartbeat["disk"]["launch_ok"] is False


def test_disk_preflight_passes_normally(tmp_path: Path) -> None:
    """Healthy path must actually run the lanes: the report fields are only
    populated by the github branch, so the test provides one."""
    from autoresearch.tick import FollowupSpec, tick

    fetched = []

    class G:
        def get_file_content(self, repo, path, ref):
            fetched.append(repo)
            return None  # no contract: self-initiated stops AFTER the gate

    spec = FollowupSpec(
        target="org/pilot",
        account="a",
        partition="p",
        run_root=tmp_path,
        image="img.sif",
        home=tmp_path,
    )
    report = tick(
        tmp_path,
        FakeSlurm().compute(),
        RecordingDispatcher(),
        now=NOW,
        github=G(),
        followup_spec=spec,
        min_free_bytes=1,
    )
    # both intake and self-initiated fetched the contract: the lanes RAN
    assert fetched and set(fetched) == {"org/pilot"}
    assert report.disk == () and report.launch_blocked is False


def _implementing_run(root: Path, run_id: str, job_id: str = "", age_s: float = 0.0) -> None:
    from autoresearch.runstate import IMPLEMENTING

    save_record(
        root,
        RunRecord(
            run_id=run_id,
            target="org/pilot",
            task_title="t",
            benchmark="tsp",
            state=IMPLEMENTING,
            run_job_id=job_id,
        ),
        now=NOW - age_s,
    )


def test_killed_climb_is_ended_after_first_seen_grace(tmp_path: Path) -> None:
    """Walltime/preemption/scancel leaves no exception to contain. Tick 1
    only STAMPS first-observed-terminal (during KillWait the job reports
    terminal while the SIGTERM containment may still be writing); the
    ending lands a full grace later, with a report."""
    _implementing_run(tmp_path, "r-killed", job_id="77", age_s=3600)
    report1, _ = run_tick(tmp_path, FakeSlurm(states={"77": "TIMEOUT"}))
    assert report1.implementing_ended == ()  # stamped, not ended
    # the stamp is a SIDECAR, never a record write: the record is untouched
    stamped = load_record(tmp_path, "r-killed")
    assert stamped.state == "implementing" and stamped.terminal_seen == 0.0
    from autoresearch.tick import _kill_stamp

    assert _kill_stamp(tmp_path, "r-killed").exists()

    report2, _ = run_tick(tmp_path, FakeSlurm(states={"77": "TIMEOUT"}), now=NOW + GRACE + 1)
    assert report2.implementing_ended == ("r-killed",)
    record = load_record(tmp_path, "r-killed")
    assert record.state == ENDED and record.ending == "aborted"
    assert "ended TIMEOUT without a verdict" in record.ending_note
    from autoresearch.runstate import run_dir as _run_dir

    assert "aborted" in (_run_dir(tmp_path, "r-killed") / "report.md").read_text()


def test_climb_that_lands_its_own_ending_wins_the_race(tmp_path: Path) -> None:
    """Between first-seen and grace expiry the climb's honest ending (or a
    move to waiting) must never be clobbered by the sweep."""
    _implementing_run(tmp_path, "r-race", job_id="77", age_s=3600)
    run_tick(tmp_path, FakeSlurm(states={"77": "CANCELLED"}))  # stamps
    honest = replace(
        load_record(tmp_path, "r-race"),
        state=ENDED,
        ending="negative-result",
        ending_note="the climb's own containment got there first",
    )
    save_record(tmp_path, honest, now=NOW + 30)
    report, _ = run_tick(tmp_path, FakeSlurm(states={"77": "CANCELLED"}), now=NOW + GRACE + 1)
    assert report.implementing_ended == ()
    assert load_record(tmp_path, "r-race").ending == "negative-result"


def test_live_attempt_job_is_left_alone(tmp_path: Path) -> None:
    _implementing_run(tmp_path, "r-live", job_id="77", age_s=GRACE + 60)
    report, _ = run_tick(tmp_path, FakeSlurm(states={"77": "RUNNING"}))
    assert report.implementing_ended == ()
    record = load_record(tmp_path, "r-live")
    assert record.state == "implementing" and record.terminal_seen == 0.0


def test_slurm_outage_never_reads_as_dead_climb(tmp_path: Path) -> None:
    _implementing_run(tmp_path, "r-out", job_id="77", age_s=GRACE + 60)
    report, _ = run_tick(tmp_path, FakeSlurm(states={"77": "!"}))
    assert report.implementing_ended == ()
    from autoresearch.tick import _kill_stamp

    assert not _kill_stamp(tmp_path, "r-out").exists()


def test_sweep_never_clobbers_a_report_the_climb_wrote(tmp_path: Path) -> None:
    """A climb killed AFTER writing its report keeps that report; the sweep
    only fills the gap when none exists."""
    from autoresearch.runstate import run_dir as _run_dir

    _implementing_run(tmp_path, "r-rep", job_id="77", age_s=3600)
    (_run_dir(tmp_path, "r-rep") / "report.md").write_text("# the climb's own words\n")
    run_tick(tmp_path, FakeSlurm(states={"77": "FAILED"}))  # stamp
    report, _ = run_tick(tmp_path, FakeSlurm(states={"77": "FAILED"}), now=NOW + GRACE + 1)
    assert report.implementing_ended == ("r-rep",)
    assert (_run_dir(tmp_path, "r-rep") / "report.md").read_text() == "# the climb's own words\n"


def test_legacy_record_without_job_id_ends_only_past_deadline(tmp_path: Path) -> None:
    """No Slurm evidence -> only the 24h run deadline authors an ending; the
    stranded window frees the picker lane but never writes verdicts."""
    from autoresearch.tick import STRANDED_IMPLEMENTING_S

    _implementing_run(tmp_path, "r-old", job_id="", age_s=25 * 3600)
    _implementing_run(tmp_path, "r-stranded", job_id="", age_s=STRANDED_IMPLEMENTING_S + 60)
    report, _ = run_tick(tmp_path, FakeSlurm(states={}))
    assert report.implementing_ended == ("r-old",)
    assert load_record(tmp_path, "r-stranded").state == "implementing"
    assert "past its run deadline" in load_record(tmp_path, "r-old").ending_note


def test_self_initiated_carries_contract_limits_into_the_job(tmp_path: Path) -> None:
    """The submitted climb job wears the contract's (clamped) limits: Slurm
    walltime from attempt_job_minutes, and the climb argv carries the session
    knobs plus its own walltime for the self-deadline."""
    from autoresearch.contract import load_contract
    from autoresearch.tick import FollowupSpec, service_self_initiated

    contract = load_contract(
        """
benchmarks:
  - {name: tsp, command: c, metric: m, direction: min}
budgets:
  gpu_hours_per_run: 1
  runs_per_week: 3
  session_max_turns: 30
  session_minutes: 25
  attempt_job_minutes: 100000
scope: {allowed: [src/]}
roadmap: docs/roadmap.md
""",
        "org/pilot",
    )
    panel_key = tmp_path / "verifier_key"
    panel_key.write_text("k")  # preflight: no usable key, no launch
    panel_key.chmod(0o600)
    spec = FollowupSpec(
        target="org/pilot",
        account="acct",
        partition="part",
        run_root=tmp_path,
        image="img.sif",
        home=tmp_path,
        panel_key_file=str(panel_key),
    )
    submitted: list[list[str]] = []

    def runner(argv, timeout_s):
        submitted.append(list(argv))
        return CommandResult(0, "123\n", "")

    out = service_self_initiated(tmp_path, SlurmCompute(runner=runner), spec, contract, NOW)
    assert out == ("tsp", "123")
    sbatch = submitted[0]
    # contract budget (100000 clamped to the 120 ceiling) + the panel
    # allowance the TICK adds (3 reads x 2 lenses x 30-min judge budget
    # + the 25-min session for the revision wake = 205): the contract can
    # never raise orchestrator spend, so the panel brings its own time
    assert "--time=325" in sbatch
    wrap = sbatch[-1]
    assert "--max-turns 30" in wrap
    assert "--session-minutes 25" in wrap
    assert "--job-minutes 325" in wrap  # the ACTUAL walltime, for the alarm
    assert "--panel verify,review" in wrap  # the pre-PR panel is ON by default
    # config-driven author: the tick threads neither the author key nor the
    # backend — climb resolves them from AUTORESEARCH_AUTHOR_* env by backend.
    # (FollowupSpec has no author key_file field at all — the tick can't thread it.)
    assert "--key-file" not in wrap and "--author-backend" not in wrap


def test_followup_jobs_carry_the_session_turn_budget(tmp_path: Path) -> None:
    """The 40-turn CLI default silently starved a live steward follow-up
    ($6 of session, zero output): the tick now passes --max-turns
    explicitly, clamped by the contract's session_max_turns."""
    from autoresearch.followup import REPLY_MARKER  # noqa: F401 (import sanity)
    from autoresearch.tick import FollowupSpec, service_in_review

    save_record(
        tmp_path,
        RunRecord(
            run_id="rev-t",
            target="org/pilot",
            task_title="t",
            state="in-review",
            pr_url="https://github.com/org/pilot/pull/9",
        ),
        now=NOW - 5000,
    )

    class G:
        def get_pull_request(self, repo, number):
            return {"state": "open", "merged": False}

        def list_comments(self, repo, number, max_pages: int = 20):
            return [
                {
                    "id": 101,
                    "body": "please fix",
                    "user": {"login": "renmengye"},
                    "author_association": "OWNER",
                }
            ]

        def list_pr_reviews(self, repo, number, max_pages: int = 10):
            return []

        def list_pr_review_comments(self, repo, number, max_pages: int = 10):
            return []

    submitted: list[list[str]] = []

    def runner(argv, timeout_s):
        submitted.append(list(argv))
        return CommandResult(0, "55\n", "")

    spec = FollowupSpec(
        target="org/pilot",
        account="a",
        partition="p",
        run_root=tmp_path,
        image="img.sif",
        home=tmp_path,
    )
    _, followups = service_in_review(tmp_path, G(), SlurmCompute(runner=runner), spec, NOW)
    assert followups
    wrap = submitted[0][-1]
    assert "--max-turns 120" in wrap  # spec default = harness ceiling
    # a spec shrunk by the tick's clamp is what lands in argv
    submitted.clear()
    save_record(
        tmp_path,
        RunRecord(
            run_id="rev-t",
            target="org/pilot",
            task_title="t",
            state="in-review",
            pr_url="https://github.com/org/pilot/pull/9",
        ),
        now=NOW - 5000,
    )
    _, followups = service_in_review(
        tmp_path, G(), SlurmCompute(runner=runner), replace(spec, max_turns=25), NOW
    )
    assert followups and "--max-turns 25" in submitted[0][-1]


def _git_home(tmp_path: Path) -> Path:
    home = tmp_path / "checkout"
    home.mkdir()
    (home / "marker.txt").write_text("v1\n")
    subprocess.run(["git", "-C", str(home), "init", "-q", "-b", "main"], check=True)
    subprocess.run(
        ["git", "-C", str(home), "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(home), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "s"],
        check=True,
    )
    return home


def test_flight_snapshot_survives_a_deploy_reset(tmp_path: Path) -> None:
    """A submitted job runs the tree that SUBMITTED it: the shared checkout
    is reset --hard at every deploy, and the flight must not move with it."""
    from autoresearch.tick import _flight_command, flight_checkout

    home = _git_home(tmp_path)
    cmd = _flight_command(home, "climb-tsp", NOW, ["echo", "hi"])
    assert "flights/climb-tsp-" in cmd and "echo hi" in cmd
    flight = next((home.parent / "flights").iterdir())
    assert (flight / "marker.txt").read_text() == "v1\n"
    # deploy rewrites the shared checkout; the flight keeps its tree
    (home / "marker.txt").write_text("v2\n")
    subprocess.run(
        [
            "git",
            "-C",
            str(home),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-aqm",
            "d",
        ],
        check=True,
    )
    assert (flight / "marker.txt").read_text() == "v1\n"
    # a same-tick name collision gets its own tree, not a silent fallback
    second = flight_checkout(home, "climb-tsp", NOW)
    assert second != flight and second.name.endswith("-2") and second.exists()
    # and failure falls back to the shared checkout, never grounding the job
    bare = tmp_path / "notarepo"
    bare.mkdir()
    assert flight_checkout(bare, "x", NOW) == bare


def test_reap_flights_removes_only_expired(tmp_path: Path) -> None:
    from autoresearch.tick import FLIGHT_TTL_S, flight_checkout, reap_flights

    home = _git_home(tmp_path)
    import os as _os

    old_flight = flight_checkout(home, "old", NOW)
    old_collided = flight_checkout(home, "old", NOW)  # -2 suffix
    fresh = flight_checkout(home, "fresh", NOW)
    queued = flight_checkout(home, "climb-nav", NOW)
    stale = NOW - FLIGHT_TTL_S - 10
    for d in (old_flight, old_collided, queued):
        _os.utime(d, (stale, stale))  # age is mtime, not name parsing
    (home.parent / "flights" / "not-a-flight").mkdir()  # ignored: fresh mtime
    # a PENDING/RUNNING job's flight is immune regardless of age: GPU
    # queues can pend past any TTL, and age alone must never strand a job
    assert reap_flights(home, NOW, live_job_names=["climb-nav"]) == 2
    assert not old_flight.exists() and not old_collided.exists()
    assert fresh.exists() and queued.exists()


def test_reap_clears_non_worktree_debris(tmp_path: Path) -> None:
    """A half-created flight (not a registered worktree) must still go
    away instead of warning forever."""
    import os as _os

    from autoresearch.tick import FLIGHT_TTL_S, reap_flights

    home = _git_home(tmp_path)
    debris = home.parent / "flights" / "climb-x-123"
    debris.mkdir(parents=True)
    stale = NOW - FLIGHT_TTL_S - 10
    _os.utime(debris, (stale, stale))
    assert reap_flights(home, NOW) == 1
    assert not debris.exists()


def test_flight_name_exhaustion_still_gets_a_unique_tree(tmp_path: Path) -> None:
    from autoresearch.tick import flight_checkout

    home = _git_home(tmp_path)
    made = [flight_checkout(home, "same", NOW) for _ in range(7)]
    assert len({str(m) for m in made}) == 7  # no silent fallback to home
    assert all(m != home for m in made)


def test_contract_alarm_opens_once_and_closes_on_recovery(tmp_path: Path) -> None:
    """Three consecutive failures open ONE issue on the target; recovery
    comments and closes it. State loss must not spawn duplicates."""
    from autoresearch.tick import CONTRACT_ALARM_MARKER, contract_alarm

    class G:
        def __init__(self):
            self.issues: list[dict] = []
            self.comments: list[tuple[int, str]] = []
            self.closed: list[int] = []
            self.next = 7

        def list_open_issues(self, repo, max_pages: int = 3):
            return [i for i in self.issues if i["number"] not in self.closed]

        def create_issue(self, repo, title, body):
            self.issues.append(
                {
                    "number": self.next,
                    "title": title,
                    "body": body,
                    "user": {"login": "agentic-learning-bot"},
                }
            )
            return self.next

        def comment(self, repo, number, body):
            self.comments.append((number, body))

        def close_issue(self, repo, number):
            self.closed.append(number)

    g = G()
    contract_alarm(tmp_path, g, "org/pilot", "ScopeError: overlaps forbidden", NOW)
    contract_alarm(tmp_path, g, "org/pilot", "ScopeError: overlaps forbidden", NOW)
    assert g.issues == []  # below the threshold: log-only
    contract_alarm(tmp_path, g, "org/pilot", "ScopeError: overlaps forbidden", NOW)
    (issue,) = g.issues
    assert CONTRACT_ALARM_MARKER in issue["body"] and "overlaps forbidden" in issue["body"]
    contract_alarm(tmp_path, g, "org/pilot", "ScopeError: overlaps forbidden", NOW)
    assert len(g.issues) == 1  # never a duplicate while open
    # state loss: the open-issue search still prevents a duplicate
    (tmp_path / "contract-alarm.json").unlink()
    for _ in range(3):
        contract_alarm(tmp_path, g, "org/pilot", "ScopeError: overlaps forbidden", NOW)
    assert len(g.issues) == 1
    # recovery: comment + close + state cleared
    contract_alarm(tmp_path, g, "org/pilot", None, NOW)
    assert g.closed == [7]
    assert any("lanes resume" in body for _, body in g.comments)
    assert not (tmp_path / "contract-alarm.json").exists()
    # healthy steady state: no writes at all
    contract_alarm(tmp_path, g, "org/pilot", None, NOW)
    assert g.closed == [7] and len(g.comments) == 1


def test_contract_alarm_close_failure_keeps_state_for_retry(tmp_path: Path) -> None:
    """A failed close must not orphan the open alarm: state survives so
    the next healthy tick retries — and a lost issue number is recovered
    by the marker search."""
    from autoresearch.tick import contract_alarm

    class G:
        def __init__(self):
            self.issues: list[dict] = []
            self.closed: list[int] = []
            self.refuse_close = True
            self.next = 9

        def list_open_issues(self, repo, max_pages: int = 3):
            return [i for i in self.issues if i["number"] not in self.closed]

        def create_issue(self, repo, title, body):
            self.issues.append(
                {
                    "number": self.next,
                    "title": title,
                    "body": body,
                    "user": {"login": "agentic-learning-bot"},
                }
            )
            return self.next

        def comment(self, repo, number, body):
            pass

        def close_issue(self, repo, number):
            if self.refuse_close:
                raise RuntimeError("403")
            self.closed.append(number)

    g = G()
    for _ in range(3):
        contract_alarm(tmp_path, g, "org/pilot", "boom", NOW)
    assert len(g.issues) == 1
    contract_alarm(tmp_path, g, "org/pilot", None, NOW)  # close refused
    assert g.closed == [] and (tmp_path / "contract-alarm.json").exists()
    g.refuse_close = False
    contract_alarm(tmp_path, g, "org/pilot", None, NOW)  # retried and closed
    assert g.closed == [9] and not (tmp_path / "contract-alarm.json").exists()

    # lost issue number (e.g. dry-run created nothing recordable): the
    # recovery path still finds an open alarm by marker and closes it
    g2 = G()
    g2.refuse_close = False
    for _ in range(3):
        contract_alarm(tmp_path, g2, "org/pilot", "boom", NOW)
    (tmp_path / "contract-alarm.json").write_text('{"count": 3}')  # number lost
    contract_alarm(tmp_path, g2, "org/pilot", None, NOW)
    assert g2.closed == [9]

    # a stranger's issue carrying the (public) marker is never adopted:
    # the bot must not close third-party issues or let them suppress the
    # real alarm
    g3 = G()
    g3.refuse_close = False
    g3.issues.append(
        {
            "number": 1,
            "title": "spoof",
            "body": "<!-- autoresearch:contract-alarm -->\nmine now",
            "user": {"login": "stranger"},
        }
    )
    for _ in range(3):
        contract_alarm(tmp_path, g3, "org/pilot", "boom", NOW)
    assert [i["number"] for i in g3.issues] == [1, 9]  # real alarm created
    contract_alarm(tmp_path, g3, "org/pilot", None, NOW)
    assert g3.closed == [9]  # the stranger's issue is untouched


def test_contract_alarm_redacts_and_fences_the_error(tmp_path: Path) -> None:
    """Transport errors can echo request material; loader errors echo
    contract content. Neither may leak a token or escape the fence."""
    from autoresearch.tick import contract_alarm

    class Auth:
        def token(self):
            return "tok-fixture-98765"

    class G:
        def __init__(self):
            self.auth = Auth()
            self.issues: list[dict] = []
            self.next = 3

        def list_open_issues(self, repo, max_pages: int = 3):
            return self.issues

        def create_issue(self, repo, title, body):
            self.issues.append(
                {
                    "number": self.next,
                    "title": title,
                    "body": body,
                    "user": {"login": "agentic-learning-bot"},
                }
            )
            return self.next

    g = G()
    nasty = "401 fetching contract; request carried tok-fixture-98765\n```\nescape attempt"
    for _ in range(3):
        contract_alarm(tmp_path, g, "org/pilot", nasty, NOW)
    (issue,) = g.issues
    assert "tok-fixture-98765" not in issue["body"]
    # the fence around the error is longer than any backtick run inside
    import re as _re

    runs = sorted(_re.findall(r"`+", issue["body"]), key=len, reverse=True)
    assert len(runs[0]) >= 4  # widened past the embedded ```


def test_shape_followup_spec_clamps_strictly_downward() -> None:
    """The clamp itself, against untrusted contract values (round-1
    finding: the argv test alone left the tick's clamp unverified)."""
    from autoresearch.contract import load_contract
    from autoresearch.limits import effective_limits
    from autoresearch.tick import FollowupSpec, shape_followup_spec

    base = """
benchmarks:
  - {name: tsp, command: c, metric: m, direction: min}
budgets: {gpu_hours_per_run: 0, runs_per_week: 20%s}
scope: {allowed: [src/]}
roadmap: docs/roadmap.md
"""
    spec = FollowupSpec(
        target="org/pilot",
        account="a",
        partition="p",
        run_root=Path("/tmp"),
        image="i",
        home=Path("/tmp"),
    )
    # contract asking for MORE turns than the ceiling gets the ceiling
    greedy = load_contract(base % ", session_max_turns: 100000", "org/pilot")
    shaped = shape_followup_spec(spec, effective_limits(greedy.budgets), greedy)
    assert shaped.max_turns == 120 and shaped.time_minutes == 90
    # a contract SILENT on turns never reduces deliberate operator config
    silent = load_contract(base % "", "org/pilot")
    raised = replace(spec, max_turns=200)
    assert shape_followup_spec(raised, effective_limits(silent.budgets), silent).max_turns == 200
    # contract shrinking turns shrinks follow-ups too, walltime untouched
    frugal = load_contract(base % ", session_max_turns: 15", "org/pilot")
    shaped = shape_followup_spec(spec, effective_limits(frugal.budgets), frugal)
    assert shaped.max_turns == 15 and shaped.time_minutes == 90
    # explicit followup walltime shrinks walltime
    tight = load_contract(base % ", followup_job_minutes: 30", "org/pilot")
    shaped = shape_followup_spec(spec, effective_limits(tight.budgets), tight)
    assert shaped.time_minutes == 30
    # no contract at all: pure defaults, nothing raised
    shaped = shape_followup_spec(spec, effective_limits(None), None)
    assert shaped.max_turns == 120 and shaped.time_minutes == 90


def test_contract_followup_walltime_never_raises_operator_config(tmp_path: Path) -> None:
    """Strictly-downward holds against the OPERATOR's spec too: a contract
    asking for more follow-up walltime than the spec grants gets the spec."""
    from autoresearch.contract import load_contract
    from autoresearch.limits import effective_limits

    contract = load_contract(
        """
benchmarks:
  - {name: tsp, command: c, metric: m, direction: min}
budgets: {gpu_hours_per_run: 1, runs_per_week: 3, followup_job_minutes: 60}
scope: {allowed: [src/]}
roadmap: docs/roadmap.md
""",
        "org/pilot",
    )
    limits = effective_limits(contract.budgets)
    operator_minutes = 30
    assert min(operator_minutes, limits.followup_job_minutes) == 30


def test_outage_latch_pauses_spawning_lanes_but_not_endings(tmp_path: Path) -> None:
    """A stamped outage sits every session-spawning lane out for the
    cooldown — steward claims, self-initiated climbs, follow-up
    submissions — while PR-state transitions (endings) keep running."""
    from autoresearch.contract import load_contract
    from autoresearch.limits import effective_limits
    from autoresearch.runstate import stamp_outage
    from autoresearch.tick import (
        FollowupSpec,
        service_in_review,
        service_self_initiated,
        service_steward,
    )

    contract = load_contract(
        """
benchmarks:
  - {name: tsp, command: c, metric: m, direction: min}
budgets: {gpu_hours_per_run: 0, runs_per_week: 20}
scope: {allowed: [src/pilot/solvers/]}
steward: {allowed: [src/pilot/instances.py]}
roadmap: docs/roadmap.md
""",
        "org/pilot",
    )
    limits = effective_limits(contract.budgets)
    submitted: list[list[str]] = []

    def runner(argv, timeout_s):
        submitted.append(list(argv))
        return CommandResult(0, "77\n", "")

    compute = SlurmCompute(runner=runner)
    spec = FollowupSpec(
        target="org/pilot",
        account="a",
        partition="p",
        run_root=tmp_path,
        image="img.sif",
        home=tmp_path,
        steward_key_file="/k",
        panel="",  # the outage latch must be what returns None, not the preflight
    )

    class G:
        def list_open_issues(self, repo, max_pages: int = 3):
            return [
                {
                    "number": 21,
                    "title": "re-base the tsp pool",
                    "body": "",
                    "user": {"login": "renmengye"},
                    "author_association": "OWNER",
                    "labels": [{"name": "autoresearch:steward"}],
                }
            ]

        def list_comments(self, repo, number, max_pages: int = 20):
            return []

        def comment(self, repo, number, body):
            pass

        def get_pull_request(self, repo, number):
            return {"state": "closed", "merged": True}

    # per-role latches: each role's stamp pauses only its own lanes (the
    # cross-role isolation itself is pinned in test_steward via
    # outage_active role separation)
    stamp_outage(tmp_path, "credit balance is too low", now=NOW - 60, role="solver")
    stamp_outage(tmp_path, "credit balance is too low", now=NOW - 60, role="steward")
    assert service_self_initiated(tmp_path, compute, spec, contract, NOW, limits) is None
    assert service_steward(tmp_path, G(), compute, spec, NOW, contract, limits) is None
    # an in-review record: the merged ending still lands, no job submitted
    save_record(
        tmp_path,
        RunRecord(
            run_id="rev-1",
            target="org/pilot",
            task_title="t",
            state="in-review",
            pr_url="https://github.com/org/pilot/pull/9",
        ),
        now=NOW - 5000,
    )
    ended, followups = service_in_review(tmp_path, G(), compute, spec, NOW)
    assert ended == [("rev-1", "merged")]
    assert submitted == [] and followups == []
    # cooldown over: the lanes wake back up (steward claims and submits)
    late = NOW + 45 * 60 + 1
    out = service_steward(tmp_path, G(), compute, spec, late, contract, limits)
    assert out is not None and submitted


def test_steward_lane_gates_on_key_and_contract_scope(tmp_path: Path) -> None:
    """Off without the steward's own key; off without a contract steward
    section; claims + submits when both exist."""
    from autoresearch.contract import load_contract
    from autoresearch.limits import effective_limits
    from autoresearch.tick import FollowupSpec, service_steward

    base_contract = """
benchmarks:
  - {name: tsp, command: c, metric: m, direction: min}
budgets: {gpu_hours_per_run: 0, runs_per_week: 20}
scope: {allowed: [src/pilot/solvers/]}
%s
roadmap: docs/roadmap.md
"""
    with_steward = load_contract(
        base_contract % "steward: {allowed: [src/pilot/instances.py]}", "org/pilot"
    )
    without_steward = load_contract(base_contract % "", "org/pilot")
    limits = effective_limits(with_steward.budgets)

    class G:
        def __init__(self):
            self.comments_posted = []

        def list_open_issues(self, repo, max_pages: int = 3):
            return [
                {
                    "number": 21,
                    "title": "re-base the tsp pool",
                    "body": "",
                    "user": {"login": "renmengye"},
                    "author_association": "OWNER",
                    "labels": [{"name": "autoresearch:steward"}],
                }
            ]

        def list_comments(self, repo, number, max_pages: int = 20):
            return []

        def comment(self, repo, number, body):
            self.comments_posted.append((number, body))

    submitted: list[list[str]] = []

    def runner(argv, timeout_s):
        submitted.append(list(argv))
        return CommandResult(0, "321\n", "")

    def spec(key: str) -> FollowupSpec:
        return FollowupSpec(
            target="org/pilot",
            account="a",
            partition="p",
            run_root=tmp_path,
            image="img.sif",
            home=tmp_path,
            steward_key_file=key,
        )

    compute = SlurmCompute(runner=runner)
    # no key -> lane off, not even an issue scan
    assert service_steward(tmp_path, G(), compute, spec(""), NOW, with_steward, limits) is None
    # no steward section -> lane off
    assert service_steward(tmp_path, G(), compute, spec("/k"), NOW, without_steward, limits) is None
    assert submitted == []
    # an ACTIVE run for the target serializes the lane
    save_record(
        tmp_path,
        RunRecord(
            run_id="busy",
            target="org/pilot",
            task_title="t",
            state="implementing",
        ),
        now=NOW - 5000,
    )
    assert service_steward(tmp_path, G(), compute, spec("/k"), NOW, with_steward, limits) is None
    save_record(
        tmp_path,
        RunRecord(
            run_id="busy",
            target="org/pilot",
            task_title="t",
            state="ended",
            ending="aborted",
        ),
        now=NOW - 5000,
    )
    # both present -> claim BEFORE submit, job carries the steward module + key
    github = G()
    out = service_steward(tmp_path, github, compute, spec("/k"), NOW, with_steward, limits)
    assert out == ("steward-issue-21", "321")
    # the queue window is bridged: pending marker written, second pass no-ops
    from autoresearch.tick import read_pending

    marker = read_pending(tmp_path, "org/pilot")  # steward lane: legacy marker
    assert marker is not None and marker["benchmark"] == "steward:tsp"
    assert (
        service_steward(tmp_path, G(), compute, spec("/k"), NOW + 60, with_steward, limits) is None
    )
    assert github.comments_posted and "Claimed by the steward" in github.comments_posted[0][1]
    wrap = submitted[0][-1]
    assert "autoresearch.steward" in wrap
    assert "--key-file /k" in wrap
    assert "--job-minutes 120" in wrap


def test_followup_key_routing_by_role(tmp_path: Path) -> None:
    """The STEWARD follow-up carries the steward's key explicitly; the author
    (solver) follow-up threads NO key — it resolves its key per the run's backend
    from env (config-driven). Without a steward key the steward record is skipped
    while solver servicing continues."""
    from autoresearch.runstate import IN_REVIEW
    from autoresearch.tick import FollowupSpec, service_in_review

    for run_id, agent in (("tsp-r1", "agent-01"), ("steward-tsp-r1", "steward-01")):
        save_record(
            tmp_path,
            RunRecord(
                run_id=run_id,
                target="org/pilot",
                task_title="t",
                benchmark="tsp",
                state=IN_REVIEW,
                agent_id=agent,
                pr_url=f"https://github.com/org/pilot/pull/{1 if agent == 'agent-01' else 2}",
                resume_session_id="s",
            ),
            now=NOW - 100,
        )

    class G:
        def get_pull_request(self, repo, number):
            return {"state": "open", "merged": False}

        def list_comments(self, repo, number, max_pages=20):
            return [
                {
                    "id": 900,
                    "body": "please respond",
                    "user": {"login": "renmengye"},
                    "author_association": "OWNER",
                }
            ]

        def list_pr_reviews(self, repo, number, max_pages=10):
            return []

        def list_pr_review_comments(self, repo, number, max_pages=10):
            return []

    submitted: list[str] = []

    def runner(argv, timeout_s):
        submitted.append(" ".join(argv))
        return CommandResult(0, "77\n", "")

    spec = FollowupSpec(
        target="org/pilot",
        account="a",
        partition="p",
        run_root=tmp_path,
        image="img.sif",
        home=tmp_path,
        steward_key_file="/steward-key",
    )
    _, subs = service_in_review(tmp_path, G(), SlurmCompute(runner=runner), spec, NOW)
    assert len(subs) == 2
    solver_cmd = next(c for c in submitted if "followup-tsp-r1" in c)
    steward_cmd = next(c for c in submitted if "steward-tsp-r1" in c)
    assert "--key-file" not in solver_cmd  # author resolves its key from env
    assert "--key-file /steward-key" in steward_cmd
    # no steward key -> steward record skipped, solver still serviced
    submitted.clear()
    spec_nokey = FollowupSpec(
        target="org/pilot",
        account="a",
        partition="p",
        run_root=tmp_path,
        image="img.sif",
        home=tmp_path,
    )
    for run_id in ("tsp-r1", "steward-tsp-r1"):
        rec = load_record(tmp_path, run_id)
        save_record(tmp_path, replace(rec, followup_job_id="", wake_attempts=0), now=NOW)
    _, subs2 = service_in_review(tmp_path, G(), SlurmCompute(runner=runner), spec_nokey, NOW)
    assert len(subs2) == 1 and subs2[0][0] == "tsp-r1"


def test_panel_spec_disables_and_reconfigures_the_climb_argv(tmp_path: Path) -> None:
    """An empty panel spec drops the flags; a custom panel and key file ride
    into the climb argv verbatim."""
    from autoresearch.tick import FollowupSpec, _climb_panel_argv

    def make(panel: str = "verify,review", panel_key_file: str = "") -> FollowupSpec:
        return FollowupSpec(
            target="org/pilot",
            account="a",
            partition="p",
            run_root=tmp_path,
            image="i.sif",
            home=tmp_path,
            panel=panel,
            panel_key_file=panel_key_file,
        )

    assert _climb_panel_argv(make(panel="")) == []
    assert _climb_panel_argv(make()) == ["--panel", "verify,review"]
    assert _climb_panel_argv(make(panel="verify", panel_key_file="/keys/verifier")) == [
        "--panel",
        "verify",
        "--panel-key-file",
        "/keys/verifier",
    ]


def test_panel_env_knobs_flow_into_the_spec(monkeypatch: Any, tmp_path: Path) -> None:
    """AUTORESEARCH_PANEL/AUTORESEARCH_PANEL_KEY_FILE reach the FollowupSpec
    through the chain environment, and empty AUTORESEARCH_PANEL turns the
    panel off (not back to the default)."""
    from autoresearch.tick import _followup_spec_from_env

    image = tmp_path / "agent.sif"
    image.write_text("")
    pat = tmp_path / "pat"
    pat.write_text("t")
    env = {
        "AUTORESEARCH_PAT_FILE": str(pat),
        "AUTORESEARCH_ACCOUNT": "a",
        "AUTORESEARCH_PARTITION": "p",
        "AUTORESEARCH_IMAGE": str(image),
        "AUTORESEARCH_HOME": str(tmp_path),
        "AUTORESEARCH_PANEL": "verify",
        "AUTORESEARCH_PANEL_KEY_FILE": "/keys/verifier",
    }
    import autoresearch.tick as tick_mod

    monkeypatch.setattr(tick_mod.os, "environ", env)
    _github, spec = _followup_spec_from_env(tmp_path)
    assert spec is not None
    assert spec.panel == "verify" and spec.panel_key_file == "/keys/verifier"
    env["AUTORESEARCH_PANEL"] = ""
    _github, off = _followup_spec_from_env(tmp_path)
    assert off is not None and off.panel == ""
    # work jobs can ride a longer partition than the tick chain, with the
    # walltime cap raised in lockstep (clamped to the code-side ceiling)
    env["AUTORESEARCH_JOB_PARTITION"] = "cpu48"
    env["AUTORESEARCH_MAX_JOB_MINUTES"] = "480"
    _github, longer = _followup_spec_from_env(tmp_path)
    assert longer is not None
    assert longer.job_partition == "cpu48" and longer.max_job_minutes == 480
    env["AUTORESEARCH_MAX_JOB_MINUTES"] = "9000"  # above the ceiling
    _github, capped = _followup_spec_from_env(tmp_path)
    assert capped is not None and capped.max_job_minutes == 600


def test_author_config_preflight_blocks_before_side_effects(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A codex misconfig (backend=codex, no non-claude model) is caught on the
    tick host BEFORE self-initiated submits or intake claims — the same
    strand-safety the panel preflight has, now for the config-driven author."""
    from autoresearch.tick import FollowupSpec, _author_config_error, service_self_initiated

    # a VALID panel key, so the panel preflight passes and the AUTHOR gate is what
    # blocks below (else "submits nothing" could be the panel preflight, not us)
    panel_key = tmp_path / "verifier_key"
    panel_key.write_text("k")
    panel_key.chmod(0o600)

    def make(**kw: Any) -> FollowupSpec:
        return FollowupSpec(
            target="org/pilot",
            account="a",
            partition="p",
            run_root=tmp_path,
            image="img.sif",
            home=tmp_path,
            panel_key_file=str(panel_key),
            **kw,
        )

    monkeypatch.delenv("AUTORESEARCH_AUTHOR_MODEL", raising=False)
    # claude (default) is always fine; codex with the claude default model is not
    monkeypatch.setenv("AUTORESEARCH_AUTHOR_BACKEND", "claude")
    assert _author_config_error(make()) == ""
    monkeypatch.setenv("AUTORESEARCH_AUTHOR_BACKEND", "codex")
    assert _author_config_error(make()) != ""  # codex + default claude model
    # a codex fleet with no valid model submits NOTHING (no wasted job)
    contract = _self_contract()
    submitted: list[str] = []

    def runner(argv, timeout_s):
        submitted.append(" ".join(argv))
        return CommandResult(0, "1\n", "")

    out = service_self_initiated(tmp_path, SlurmCompute(runner=runner), make(), contract, NOW)
    assert out is None and submitted == []
    # once the model is set, codex is accepted
    monkeypatch.setenv("AUTORESEARCH_AUTHOR_MODEL", "gpt-5.6-terra")
    assert _author_config_error(make()) == ""


def test_panel_key_preflight_blocks_claim_and_launch(tmp_path: Path, monkeypatch: Any) -> None:
    """Panel on + a key the climb would reject (missing, group-readable,
    empty): the intake lane claims nothing and the self-initiated lane
    submits nothing — the strand is caught before any side effect. The
    preflight reads through FileTokenProvider so its acceptance rules ARE
    the climb's; a 0600 non-empty key or a disabled panel passes, and a
    ~ path expands (operator env values arrive verbatim)."""
    from autoresearch.tick import (
        FollowupSpec,
        _panel_preflight_error,
        service_intake,
        service_self_initiated,
    )

    def make(**kw: Any) -> FollowupSpec:
        return FollowupSpec(
            target="org/pilot",
            account="a",
            partition="p",
            run_root=tmp_path,
            image="i.sif",
            home=tmp_path,
            **kw,
        )

    loose = tmp_path / "loose"
    loose.write_text("k")
    loose.chmod(0o644)  # pinned: write_text's mode depends on the umask
    empty = tmp_path / "empty"
    empty.write_text("")
    empty.chmod(0o600)
    good = tmp_path / "good"
    good.write_text("k")
    good.chmod(0o600)
    assert "chmod 600" in _panel_preflight_error(make(panel_key_file=str(loose)))
    assert "empty" in _panel_preflight_error(make(panel_key_file=str(empty)))
    assert _panel_preflight_error(make(panel_key_file=str(tmp_path / "nope"))) != ""
    assert _panel_preflight_error(make(panel_key_file=str(good))) == ""
    assert _panel_preflight_error(make(panel="")) == ""
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _panel_preflight_error(make(panel_key_file="~/good")) == ""
    # the climb's OTHER startup rejections are preflighted too: a typo'd lens
    # spec (climb dies at argparse) and a relative key path (the climb runs
    # from a flight dir, not the tick's cwd)
    both_wrong = make(panel="verfy:hermes", panel_key_file=str(good))
    err = _panel_preflight_error(both_wrong)
    assert "unknown kind" in err  # kind is checked before backend
    assert "claude backend" not in err
    # a hermes lens preflights the shelled-judge rules (image first)
    assert "requires a real container image" in _panel_preflight_error(
        make(panel="verify:hermes", panel_key_file=str(good))
    )
    # a codex lens preflights the image requirement too (climb parity)
    judge = tmp_path / "panel_codex_key"
    judge.write_text("sk-judge")
    judge.chmod(0o600)
    monkeypatch.setenv("AUTORESEARCH_PANEL_CODEX_KEY_FILE", str(judge))
    no_image = FollowupSpec(
        target="org/pilot",
        account="a",
        partition="p",
        run_root=tmp_path,
        image="",
        home=tmp_path,
        panel="review:codex:gpt-5.6-terra",
        panel_key_file=str(good),
    )
    assert "requires a real container image" in _panel_preflight_error(no_image)
    assert "relative" in _panel_preflight_error(make(panel_key_file="good"))
    # role separation: the panel key must not BE the (resolved) author key. The
    # author key is now config-driven — resolved per the fleet backend from env —
    # so the collision is set via AUTORESEARCH_HARNESS_KEY_FILE, not spec.key_file.
    monkeypatch.setenv("AUTORESEARCH_HARNESS_KEY_FILE", str(good))
    assert "author key" in _panel_preflight_error(make(panel_key_file=str(good)))
    # ... and a RELATIVE author key path fails too (the climb resolves from a
    # flight dir): a relative env value survives ~-expansion as non-absolute
    monkeypatch.setenv("AUTORESEARCH_HARNESS_KEY_FILE", "keys/author")
    rel_err = _panel_preflight_error(make(panel_key_file=str(good)))
    assert "author key path" in rel_err and "relative" in rel_err
    monkeypatch.delenv("AUTORESEARCH_HARNESS_KEY_FILE")

    # both lanes consult it BEFORE side effects: nothing claimed or submitted
    bad = make(panel_key_file=str(tmp_path / "nope"))
    submitted: list[str] = []

    def runner(argv, timeout_s):
        submitted.append(" ".join(argv))
        return CommandResult(0, "123\n", "")

    contract = _self_contract()
    compute = SlurmCompute(runner=runner)
    assert service_self_initiated(tmp_path, compute, bad, contract, NOW) is None

    class IntakeGitHub:
        def __init__(self) -> None:
            self.comments: list[str] = []

        def list_open_issues(self, repo):
            return [
                {
                    "number": 5,
                    "title": "improve tsp",
                    "body": "",
                    "user": {"login": "mengye"},
                    "author_association": "OWNER",
                    "labels": [],
                }
            ]

        def list_comments(self, repo, number):
            return []

        def comment(self, repo, number, body):
            self.comments.append(body)

    gh = IntakeGitHub()
    assert service_intake(tmp_path, gh, compute, bad, NOW, contract=contract) is None
    assert submitted == [] and gh.comments == []
    # positive control: the same issue IS claimed once the key is usable,
    # so the bad-path assertions above cannot pass vacuously
    ok = make(panel_key_file=str(good))
    assert service_intake(tmp_path, gh, compute, ok, NOW, contract=contract) is not None
    assert len(gh.comments) == 1 and len(submitted) == 1

    # a failed submit RELEASES the claim (pick_issue skips claimed issues,
    # so without the release the issue would be stranded forever)
    from autoresearch.intake import RELEASE_MARKER

    def failing_runner(argv, timeout_s):
        return CommandResult(1, "", "sbatch: error")

    gh2 = IntakeGitHub()
    out = service_intake(
        tmp_path, gh2, SlurmCompute(runner=failing_runner), ok, NOW, contract=contract
    )
    assert out is None
    assert any(RELEASE_MARKER in c for c in gh2.comments)

    # the walltime allowance exists exactly when the panel does — and the
    # panel-augmented total clamps at the 6h partition cap (sbatch would
    # REJECT a longer request outright, grounding every climb)
    from autoresearch.limits import effective_limits
    from autoresearch.tick import MAX_ATTEMPT_JOB_MINUTES, _panel_job_minutes

    limits = effective_limits()  # defaults: 120-min job, 90-min session
    assert _panel_job_minutes(make(panel=""), limits) == 0
    wanted = limits.attempt_job_minutes + _panel_job_minutes(make(), limits)
    # the default path NEEDS the clamp (spec.max_job_minutes defaults to
    # MAX_ATTEMPT_JOB_MINUTES = cpu_short's 6h MaxTime)
    assert wanted > MAX_ATTEMPT_JOB_MINUTES
    submitted2: list[str] = []

    def runner2(argv, timeout_s):
        submitted2.append(" ".join(argv))
        return CommandResult(0, "77\n", "")

    # a low cap must reach BOTH consumers at every site: the Slurm request
    # AND the --job-minutes the self-deadline arms against (a deadline armed
    # past the real walltime is a kill before a clean ending)
    low = make(panel_key_file=str(good), max_job_minutes=60)
    submitted_low: list[str] = []

    def runner_low(argv, timeout_s):
        submitted_low.append(" ".join(argv))
        return CommandResult(0, "88\n", "")

    from autoresearch.tick import clear_pending

    clear_pending(tmp_path, "org/pilot")
    clear_pending(tmp_path, "org/pilot", "agent-01")
    out_low = service_self_initiated(
        tmp_path, SlurmCompute(runner=runner_low), low, contract, NOW + 4000
    )
    assert out_low is not None
    assert "--time=60" in submitted_low[0] and "--job-minutes 60" in submitted_low[0]
    # the session shrinks to fit the capped job (same rule as the contract
    # clamp): 60-min job - 20-min overhead, never the full 90-min default
    assert "--session-minutes 40" in submitted_low[0]
    clear_pending(tmp_path, "org/pilot")
    clear_pending(tmp_path, "org/pilot", "agent-01")

    ok2 = make(panel_key_file=str(good))
    from autoresearch.contract import load_contract

    default_contract = load_contract(
        """
benchmarks:
  - {name: tsp, command: c, metric: m, direction: min}
budgets: {gpu_hours_per_run: 1, runs_per_week: 3}
scope: {allowed: [src/]}
roadmap: docs/roadmap.md
""",
        "org/pilot",
    )
    out2 = service_self_initiated(
        tmp_path, SlurmCompute(runner=runner2), ok2, default_contract, NOW + 9000
    )
    assert out2 is not None
    assert f"--time={MAX_ATTEMPT_JOB_MINUTES}" in submitted2[0]
    assert f"--job-minutes {MAX_ATTEMPT_JOB_MINUTES}" in submitted2[0]


def test_job_wake_dispatcher_submits_a_resume_job_after_the_eval_jobs(tmp_path, monkeypatch):
    # the production WakeDispatcher: a wake becomes a Slurm job that runs the
    # wake CLI (`climb --resume <run_id>`), depending on the eval jobs so it
    # fires when they finish.
    from autoresearch.runstate import RunRecord
    from autoresearch.tick import FollowupSpec, JobWakeDispatcher

    submits = []

    def runner(argv, timeout_s):
        if argv[0] == "sbatch":
            submits.append(list(argv))
            return CommandResult(0, "9001\n", "")
        raise AssertionError(argv)

    compute = SlurmCompute(runner=runner)
    spec = FollowupSpec(
        account="acct",
        partition="cpu_short",
        run_root=tmp_path,
        image="/img/a.sif",
        home=tmp_path,
        pat_file="/pat",
        panel="verify,review",
    )
    # isolate the dispatcher's job spec from flight_checkout's git dependency
    monkeypatch.setattr(
        "autoresearch.tick._flight_command", lambda home, name, now, argv: " ".join(argv)
    )
    record = RunRecord(
        run_id="tsp-1",
        target="org/pilot",
        task_title="improve tsp",
        benchmark="tsp",
        state="waiting",
        stage={"afterany": "afterany:501:502"},
    )
    job_id = JobWakeDispatcher(compute, spec, now=NOW).dispatch(record, "eval done")
    assert job_id == "9001"  # async: the wake job now owns the lease
    argv = submits[0]
    joined = " ".join(argv)
    assert "autoresearch.attempt" in joined and "--resume tsp-1" in joined
    assert "--dependency=afterany:501:502" in argv  # runs after the eval jobs
    assert "--panel verify,review" in joined  # the wake runs the verification panel
    assert "--account=acct" in argv and "--partition=cpu_short" in argv


def test_wake_dispatcher_on_switch_lands_dark_by_default(tmp_path, monkeypatch):
    # dispatched climbing must NOT deliver wakes unless the operator flips the
    # explicit on-switch AND the chain env is complete.
    from autoresearch.tick import (
        DISPATCH_WAKE_SENTINEL,
        FollowupSpec,
        JobWakeDispatcher,
        LoggingDispatcher,
        _wake_dispatcher_from_env,
    )

    compute = SlurmCompute(runner=lambda argv, t: CommandResult(0, "", ""))
    spec = FollowupSpec(
        account="a", partition="p", run_root=tmp_path, image="/i.sif", home=tmp_path
    )

    # default: no env, no sentinel -> dry sweep, logging dispatcher
    monkeypatch.delenv("AUTORESEARCH_DISPATCH_WAKE", raising=False)
    dispatcher, live = _wake_dispatcher_from_env(compute, spec, NOW, tmp_path)
    assert isinstance(dispatcher, LoggingDispatcher) and live is False

    # env on-switch + complete env -> live sweep, real dispatcher
    monkeypatch.setenv("AUTORESEARCH_DISPATCH_WAKE", "1")
    dispatcher, live = _wake_dispatcher_from_env(compute, spec, NOW, tmp_path)
    assert isinstance(dispatcher, JobWakeDispatcher) and live is True

    # env on-switch but incomplete env -> fail SAFE to dry, not a wake that can't run
    dispatcher, live = _wake_dispatcher_from_env(compute, None, NOW, tmp_path)
    assert isinstance(dispatcher, LoggingDispatcher) and live is False

    # sentinel file arms it too (mirrors PAUSE) — no env var needed
    monkeypatch.delenv("AUTORESEARCH_DISPATCH_WAKE", raising=False)
    (tmp_path / DISPATCH_WAKE_SENTINEL).touch()
    dispatcher, live = _wake_dispatcher_from_env(compute, spec, NOW, tmp_path)
    assert isinstance(dispatcher, JobWakeDispatcher) and live is True
    # ...still fail-safe to dry on an incomplete env
    dispatcher, live = _wake_dispatcher_from_env(compute, None, NOW, tmp_path)
    assert isinstance(dispatcher, LoggingDispatcher) and live is False
    # disarming removes the sentinel -> back to dry (reversible, like PAUSE)
    (tmp_path / DISPATCH_WAKE_SENTINEL).unlink()
    dispatcher, live = _wake_dispatcher_from_env(compute, spec, NOW, tmp_path)
    assert isinstance(dispatcher, LoggingDispatcher) and live is False


def test_job_wake_dispatcher_walltime_includes_the_panel(tmp_path, monkeypatch):
    # the wake now runs the verification panel, so its Slurm walltime must be
    # more than the bare read+PR base when a panel is configured.
    from autoresearch.runstate import RunRecord
    from autoresearch.tick import FollowupSpec, JobWakeDispatcher, _wake_panel_minutes

    times = []

    def runner(argv, timeout_s):
        if argv[0] == "sbatch":
            for a in argv:
                if a.startswith("--time="):
                    times.append(int(a.split("=")[1]))
            return CommandResult(0, "9001\n", "")
        raise AssertionError(argv)

    monkeypatch.setattr(
        "autoresearch.tick._flight_command", lambda home, name, now, argv: " ".join(argv)
    )
    record = RunRecord(
        run_id="tsp-1", target="o/r", task_title="t", benchmark="tsp", state="waiting", stage={}
    )
    base = dict(account="a", partition="p", run_root=tmp_path, image="/i.sif", home=tmp_path)
    with_panel = FollowupSpec(**base, panel="verify,review")
    no_panel = FollowupSpec(**base, panel="")
    JobWakeDispatcher(SlurmCompute(runner=runner), with_panel, now=NOW).dispatch(record, "x")
    JobWakeDispatcher(SlurmCompute(runner=runner), no_panel, now=NOW).dispatch(record, "x")
    assert _wake_panel_minutes(with_panel) > 0 and _wake_panel_minutes(no_panel) == 0
    assert times[0] == 20 + _wake_panel_minutes(with_panel)  # panel allowance added
    assert times[1] == 20  # no panel -> just the base


def test_author_sleep_wake_gets_a_full_session_walltime(tmp_path, monkeypatch):
    # an author-sleep wake resumes a FULL author session, so its Slurm job must
    # fit the session (+ overhead) and pass --session-minutes, not the short
    # candidate-wake budget (terra #135 r3).
    from autoresearch.limits import ATTEMPT_OVERHEAD_MINUTES
    from autoresearch.roles import author_spec
    from autoresearch.runstate import RunRecord
    from autoresearch.tick import FollowupSpec, JobWakeDispatcher

    times: list[int] = []
    joined_argv: list[str] = []

    def runner(argv, timeout_s):
        if argv[0] == "sbatch":
            joined_argv.append(" ".join(argv))
            for a in argv:
                if a.startswith("--time="):
                    times.append(int(a.split("=")[1]))
            return CommandResult(0, "9001\n", "")
        raise AssertionError(argv)

    monkeypatch.setattr(
        "autoresearch.tick._flight_command", lambda home, name, now, argv: " ".join(argv)
    )
    spec = FollowupSpec(
        account="a", partition="p", run_root=tmp_path, image="/i.sif", home=tmp_path, panel=""
    )
    sleep_rec = RunRecord(
        run_id="tsp-1",
        target="o/r",
        task_title="t",
        benchmark="tsp",
        state="waiting",
        stage={"phase": "author-sleep", "afterany": "afterany:501"},
    )
    JobWakeDispatcher(SlurmCompute(runner=runner), spec, now=NOW).dispatch(sleep_rec, "x")
    session_minutes = author_spec().budget.walltime_s // 60
    assert times[0] == session_minutes + ATTEMPT_OVERHEAD_MINUTES  # fits the session
    assert times[0] > 20  # far more than a candidate wake's base
    assert f"--session-minutes {session_minutes}" in joined_argv[0]  # self-deadline set


class LedgerGitHub:
    """Duck-typed fake for the research-log service (cast at call sites)."""

    def __init__(self, issues=(), put_ok=True):
        self.issues = list(issues)
        self.put_ok = put_ok
        self.files = []
        self.comments = []
        self.created = []

    def ensure_branch(self, repo, branch):
        return True

    def put_file(self, repo, path, content, branch, message):
        if not self.put_ok:
            return ""
        created = path not in {p for p, _, _ in self.files}
        self.files.append((path, content, branch))
        return "created" if created else "updated"

    def list_open_issues(self, repo, max_pages=3):
        return self.issues

    def create_issue(self, repo, title, body):
        self.created.append(title)
        return 91

    def comment(self, repo, number, body):
        self.comments.append((number, body))


def _ended_run(root: Path, run_id: str, saved_at: float = NOW, **over) -> None:
    from autoresearch.runstate import run_dir as _rd

    base = dict(
        run_id=run_id,
        target="org/yolo",
        task_title="t",
        state=ENDED,
        ending="negative-result",
        benchmark="heldout_probe",
    )
    # save_record stamps `updated` with the save time
    save_record(root, RunRecord(**{**base, **over}), saved_at)
    (_rd(root, run_id) / "report.md").write_text("# report\ncontent")


def _spec(target="org/yolo"):
    from autoresearch.tick import FollowupSpec

    return FollowupSpec(
        account="a",
        partition="p",
        run_root=Path("/tmp"),
        image="i.sif",
        home=Path("/tmp"),
        target=target,
    )


def test_research_log_first_pass_adopts_history_silently(tmp_path: Path) -> None:
    from autoresearch.tick import _ledger_marker, service_research_log

    _ended_run(tmp_path, "old-1", saved_at=NOW - 100)
    # a run that went terminal DURING the first pass is new work, not history
    _ended_run(tmp_path, "fresh-1", saved_at=NOW + 1)
    gh = LedgerGitHub()
    assert service_research_log(tmp_path, gh, _spec(), NOW) == 1
    assert len(gh.files) == 1 and gh.files[0][0].endswith("-fresh-1.md")
    assert _ledger_marker(tmp_path, "old-1").exists()  # adopted silently


def test_research_log_publishes_once_and_routes_to_the_order_issue(tmp_path: Path) -> None:
    from autoresearch.tick import _ledger_since, service_research_log

    _ledger_since(tmp_path, "org/yolo").write_text("1")  # past first pass
    _ended_run(tmp_path, "r-1")
    gh = LedgerGitHub(issues=[{"number": 15, "title": "heldout_probe: order", "body": ""}])
    assert service_research_log(tmp_path, gh, _spec(), NOW) == 1
    ((path, content, branch),) = gh.files
    assert branch == "research-log" and path.endswith("-r-1.md") and "content" in content
    ((num, line),) = gh.comments
    assert num == 15 and "negative-result" in line and "research-log" in line
    # idempotent: second pass publishes nothing
    assert service_research_log(tmp_path, gh, _spec(), NOW) == 0


def test_research_log_archive_failure_defers_pointer_and_retries(tmp_path: Path) -> None:
    from autoresearch.tick import _ledger_marker, _ledger_since, service_research_log

    _ledger_since(tmp_path, "org/yolo").write_text("1")
    _ended_run(tmp_path, "r-2")
    gh = LedgerGitHub(put_ok=False)
    assert service_research_log(tmp_path, gh, _spec(), NOW) == 0
    assert gh.comments == []  # no dead link
    assert not _ledger_marker(tmp_path, "r-2").exists()  # retried next tick
    gh.put_ok = True
    assert service_research_log(tmp_path, gh, _spec(), NOW) == 1


def test_research_log_rolling_issue_created_once_via_cache(tmp_path: Path) -> None:
    from autoresearch.tick import _ledger_since, service_research_log

    _ledger_since(tmp_path, "org/yolo").write_text("1")
    _ended_run(tmp_path, "r-3", benchmark="tsp")
    _ended_run(tmp_path, "r-4", benchmark="tsp")
    gh = LedgerGitHub()  # no order issues -> rolling issue
    assert service_research_log(tmp_path, gh, _spec(), NOW) == 2
    assert gh.created == ["Research log"]  # created once, cached, reused
    assert [n for n, _ in gh.comments] == [91, 91]


def test_research_log_claimed_issue_gets_no_duplicate_pointer(tmp_path: Path) -> None:
    from autoresearch.tick import _ledger_since, service_research_log

    _ledger_since(tmp_path, "org/yolo").write_text("1")
    _ended_run(tmp_path, "r-5", issue_number=15)
    gh = LedgerGitHub(issues=[{"number": 15, "title": "heldout_probe: order", "body": ""}])
    assert service_research_log(tmp_path, gh, _spec(), NOW) == 1
    assert len(gh.files) == 1 and gh.comments == []  # archived, not re-posted


def test_research_log_pointer_failure_retries_pointer_only(tmp_path: Path) -> None:
    """terra #170 r3: a comment failure after a successful archive must NOT
    be treated as posted — the staged marker ("archived") makes the next
    pass retry the POINTER without re-putting the archive, then mark done."""
    from autoresearch.tick import _ledger_marker, _ledger_since, service_research_log

    _ledger_since(tmp_path, "org/yolo").write_text("1")
    _ended_run(tmp_path, "r-6")

    gh = LedgerGitHub()
    boom = {"on": True}
    real_comment = gh.comment

    def flaky_comment(repo, number, body):
        if boom["on"]:
            raise RuntimeError("comment API down")
        real_comment(repo, number, body)

    gh.comment = flaky_comment  # type: ignore[method-assign]
    assert service_research_log(tmp_path, gh, _spec(), NOW) == 0
    assert _ledger_marker(tmp_path, "r-6").read_text().splitlines()[0] == "pointer-pending"
    assert len(gh.files) == 1 and gh.comments == []
    boom["on"] = False
    assert service_research_log(tmp_path, gh, _spec(), NOW) == 1
    assert _ledger_marker(tmp_path, "r-6").read_text().splitlines()[0] == "done"
    assert len(gh.files) == 1  # archive NOT re-put
    assert len(gh.comments) == 1  # the pointer arrived exactly once


def test_research_log_lost_marker_duplicates_at_most_once(tmp_path: Path) -> None:
    """If the marker FILE is lost after full success, the retry re-posts one
    pointer — the bounded duplicate is the accepted price of never silently
    losing a pointer (the r2/r3 trade, documented in the publisher)."""
    from autoresearch.tick import _ledger_marker, _ledger_since, service_research_log

    _ledger_since(tmp_path, "org/yolo").write_text("1")
    _ended_run(tmp_path, "r-7")
    gh = LedgerGitHub()
    assert service_research_log(tmp_path, gh, _spec(), NOW) == 1
    _ledger_marker(tmp_path, "r-7").unlink()
    assert service_research_log(tmp_path, gh, _spec(), NOW) == 1
    assert _ledger_marker(tmp_path, "r-7").read_text().splitlines()[0] == "done"  # settled
    assert len(gh.comments) == 2  # one bounded duplicate, then stable
    assert service_research_log(tmp_path, gh, _spec(), NOW) == 0


def test_research_log_unwritable_marker_stalls_without_posting(tmp_path: Path) -> None:
    """terra #170 r4: a persistently unwritable marker must stall the
    publish (retry next tick), never stream duplicate pointers — the marker
    write is the license to post."""
    import os

    from autoresearch.runstate import run_dir as _rd
    from autoresearch.tick import _ledger_since, service_research_log

    _ledger_since(tmp_path, "org/yolo").write_text("1")
    _ended_run(tmp_path, "r-8")
    gh = LedgerGitHub()
    rd = _rd(tmp_path, "r-8")
    os.chmod(rd, 0o555)  # marker dir read-only
    try:
        assert service_research_log(tmp_path, gh, _spec(), NOW) == 0
        assert gh.comments == []  # no pointer without a successful probe
        assert service_research_log(tmp_path, gh, _spec(), NOW) == 0
        assert gh.comments == []  # still stalled, still zero — bounded
    finally:
        os.chmod(rd, 0o755)
    assert service_research_log(tmp_path, gh, _spec(), NOW) == 1
    assert len(gh.comments) == 1  # posts exactly once after recovery


def test_research_log_stale_cached_issue_self_heals(tmp_path: Path) -> None:
    """terra #170 r5: a cached rolling-issue number that no longer accepts
    comments (locked/deleted) must not stall delivery forever — the failed
    comment drops the cache, and the next pass re-creates."""
    from autoresearch.tick import (
        _ledger_issue_cache,
        _ledger_since,
        service_research_log,
    )

    _ledger_since(tmp_path, "org/yolo").write_text("1")
    _ended_run(tmp_path, "r-9", benchmark="tsp")
    _ledger_issue_cache(tmp_path, "org/yolo").write_text("404")  # stale

    gh = LedgerGitHub()
    real_comment = gh.comment

    def locked_404(repo, number, body):
        if number == 404:
            raise RuntimeError("issue locked")
        real_comment(repo, number, body)

    gh.comment = locked_404  # type: ignore[method-assign]
    assert service_research_log(tmp_path, gh, _spec(), NOW) == 0
    assert not _ledger_issue_cache(tmp_path, "org/yolo").exists()  # dropped
    assert service_research_log(tmp_path, gh, _spec(), NOW) == 1  # re-created
    assert gh.created == ["Research log"] and len(gh.comments) == 1


def test_research_log_lost_cache_rediscovers_instead_of_duplicating(tmp_path: Path) -> None:
    """A failed/lost cache write must not spawn a second rolling issue: the
    marker scan is the source of truth, the cache only a fast path."""
    from autoresearch.tick import (
        RESEARCH_LOG_MARKER,
        _ledger_issue_cache,
        _ledger_since,
        service_research_log,
    )

    _ledger_since(tmp_path, "org/yolo").write_text("1")
    _ended_run(tmp_path, "r-10", benchmark="tsp")
    gh = LedgerGitHub(issues=[{"number": 77, "title": "Research log", "body": RESEARCH_LOG_MARKER}])
    assert service_research_log(tmp_path, gh, _spec(), NOW) == 1
    assert gh.created == []  # rediscovered via the marker, no duplicate
    assert gh.comments == [(77, gh.comments[0][1])]
    assert _ledger_issue_cache(tmp_path, "org/yolo").read_text() == "77"  # re-cached


def test_cooldown_dial_zero_redispatches_immediately(tmp_path: Path) -> None:
    """The RSI-era dial: attempt_cooldown_minutes: 0 re-dispatches
    back-to-back (runs_per_week is the spend guard); unset keeps the 6h
    default for standard research repos."""
    from autoresearch.contract import load_contract
    from autoresearch.tick import pick_self_initiated

    base = """
benchmarks:
  - {name: reach, command: c, metric: m, direction: max}
budgets: {gpu_hours_per_run: 1, runs_per_week: 500%s}
scope: {allowed: [src/]}
roadmap: docs/roadmap.md
"""
    just_ended = RunRecord(
        run_id="r1",
        target="o/r",
        task_title="t",
        state=ENDED,
        benchmark="reach",
        created=NOW - 60,
    )
    hot = load_contract(base % ", attempt_cooldown_minutes: 0", "o/r")
    assert pick_self_initiated([just_ended], hot, "o/r", NOW) == "reach"
    standard = load_contract(base % "", "o/r")
    assert pick_self_initiated([just_ended], standard, "o/r", NOW) is None  # 6h holds


def test_zero_cooldown_keeps_the_dead_launch_backoff(tmp_path: Path) -> None:
    """terra #172: a launch that died before writing a record is invisible
    to runs_per_week, so even a zero-cooldown contract backs it off by the
    dead-launch floor instead of resubmitting every tick."""
    from autoresearch.contract import load_contract
    from autoresearch.tick import DEAD_LAUNCH_BACKOFF_S, pick_self_initiated

    hot = load_contract(
        """
benchmarks:
  - {name: reach, command: c, metric: m, direction: max}
budgets: {gpu_hours_per_run: 1, runs_per_week: 500, attempt_cooldown_minutes: 0}
scope: {allowed: [src/]}
roadmap: docs/roadmap.md
""",
        "o/r",
    )
    # a pre-record death 60s ago: blocked despite cooldown 0
    assert pick_self_initiated([], hot, "o/r", NOW, {"reach": NOW - 60}) is None
    # past the floor: dispatches again
    assert (
        pick_self_initiated([], hot, "o/r", NOW, {"reach": NOW - DEAD_LAUNCH_BACKOFF_S - 1})
        == "reach"
    )


def test_dead_launch_tombstones_are_per_benchmark(tmp_path: Path) -> None:
    """terra #172 r2/r3: crash memory is a per-benchmark tombstone that a
    SECOND benchmark's launch cannot erase, persists across ticks inside its
    window, and prunes itself after."""
    from autoresearch.contract import load_contract
    from autoresearch.tick import (
        DEAD_LAUNCH_BACKOFF_S,
        pick_self_initiated,
        read_tombstones,
        write_tombstone,
    )

    hot = load_contract(
        """
benchmarks:
  - {name: reach, command: c, metric: m, direction: max}
  - {name: denoise, command: c, metric: m, direction: max}
budgets: {gpu_hours_per_run: 1, runs_per_week: 500, attempt_cooldown_minutes: 0}
scope: {allowed: [src/]}
roadmap: docs/roadmap.md
""",
        "o/r",
    )
    write_tombstone(tmp_path, "o/r", "reach", NOW - 60)
    dead = read_tombstones(tmp_path, "o/r", hot, NOW)
    # reach is floored; denoise still dispatches (width of the SELECTION,
    # not an overwrite of reach's memory)
    assert pick_self_initiated([], hot, "o/r", NOW, dead) == "denoise"
    # denoise also crashes: BOTH tombstones coexist, nothing dispatches
    write_tombstone(tmp_path, "o/r", "denoise", NOW - 30)
    dead = read_tombstones(tmp_path, "o/r", hot, NOW)
    assert pick_self_initiated([], hot, "o/r", NOW, dead) is None
    # a second tick inside the window: same refusal (persistence)
    dead = read_tombstones(tmp_path, "o/r", hot, NOW + 60)
    assert pick_self_initiated([], hot, "o/r", NOW + 60, dead) is None
    # past the window: pruned on read, dispatch resumes
    later = NOW + DEAD_LAUNCH_BACKOFF_S + 61
    assert read_tombstones(tmp_path, "o/r", hot, later) == {}
    assert pick_self_initiated([], hot, "o/r", later, {}) == "denoise"


def test_width_dial_runs_two_slots_and_caps(tmp_path: Path) -> None:
    """WIDTH-V1: max_active_attempts: 2 dispatches two concurrent attempts
    with distinct agent identities (branch/ledger uniqueness), refuses a
    third while both slots are occupied, and frees slots as runs land+end."""
    from autoresearch.contract import load_contract
    from autoresearch.tick import FollowupSpec, service_self_initiated

    contract = load_contract(
        """
benchmarks:
  - {name: tsp, command: c, metric: m, direction: min}
budgets:
  gpu_hours_per_run: 1
  runs_per_week: 500
  attempt_cooldown_minutes: 0
  max_active_attempts: 2
scope: {allowed: [src/]}
roadmap: docs/roadmap.md
""",
        "org/pilot",
    )
    panel_key = tmp_path / "vkey"
    panel_key.write_text("k")
    panel_key.chmod(0o600)
    spec = FollowupSpec(
        target="org/pilot",
        account="acct",
        partition="part",
        run_root=tmp_path,
        image="img.sif",
        home=tmp_path,
        bot_login="bot",
        panel_key_file=str(panel_key),
    )
    submitted: list[str] = []
    job_ids = iter(["101", "102", "103"])

    def runner(argv, timeout_s):
        if argv[0] == "sbatch":
            submitted.append(" ".join(argv))
            return CommandResult(0, next(job_ids) + "\n", "")
        return CommandResult(0, "RUNNING\n", "")  # both holders alive

    compute = SlurmCompute(runner=runner)
    assert service_self_initiated(tmp_path, compute, spec, contract, NOW) == ("tsp", "101")
    assert "--agent-id agent-01" in submitted[0]
    # slot 2: dispatches concurrently with a DISTINCT identity
    assert service_self_initiated(tmp_path, compute, spec, contract, NOW + 60) == ("tsp", "102")
    assert "--agent-id agent-02" in submitted[1]
    # both slots occupied: the third asks is refused
    assert service_self_initiated(tmp_path, compute, spec, contract, NOW + 120) is None
    assert len(submitted) == 2
    # slot 1 lands and ends -> a slot frees, dispatch resumes as agent-01
    save_record(
        tmp_path,
        RunRecord(
            run_id="w1",
            target="org/pilot",
            task_title="t",
            state=ENDED,
            ending="negative-result",
            benchmark="tsp",
            agent_id="agent-01",
            created=NOW + 61,
        ),
        NOW + 200,
    )
    assert service_self_initiated(tmp_path, compute, spec, contract, NOW + 300) == ("tsp", "103")
    assert "--agent-id agent-01" in submitted[2]
    # the landed check is SLOT-SCOPED: agent-01's record must not have
    # cleared agent-02's still-live marker (terra #173 r1)
    from autoresearch.tick import read_pending

    assert read_pending(tmp_path, "org/pilot", "agent-02") is not None


def test_list_pendings_is_delimited_by_target(tmp_path: Path) -> None:
    """org/pilot's marker scan must not absorb org/pilotx's markers ("/"
    encodes as "__" so a bare glob is prefix-ambiguous), nor any suffixed
    file that isn't a width-slot name (terra #173 r1), nor the LEGACY
    marker of a repo literally named pilot__agent-01 — "_" is legal in
    repo names, which is why the slot separator is "@" (terra #173 r2)."""
    from autoresearch.tick import list_pendings, read_pending, write_pending

    write_pending(tmp_path, "org/pilot", "tsp", "1", NOW, agent="agent-01")
    write_pending(tmp_path, "org/pilotx", "tsp", "2", NOW)
    write_pending(tmp_path, "org/pilotx", "tsp", "3", NOW, agent="agent-01")
    write_pending(tmp_path, "org/pilot", "tsp", "4", NOW, agent="not-a-slot")
    write_pending(tmp_path, "org/pilot__agent-01", "tsp", "5", NOW)
    got = [(agent, p["job_id"]) for agent, p in list_pendings(tmp_path, "org/pilot")]
    assert got == [("agent-01", "1")]
    marker = read_pending(tmp_path, "org/pilot__agent-01")
    assert marker is not None and marker["job_id"] == "5"


def test_width_queued_slots_count_toward_weekly_budget(tmp_path: Path) -> None:
    """With one run left in runs_per_week, a width-2 target must not submit
    two attempts in the pre-record queue window (terra #173 r2)."""
    from autoresearch.contract import load_contract
    from autoresearch.tick import FollowupSpec, service_self_initiated

    contract = load_contract(
        """
benchmarks:
  - {name: tsp, command: c, metric: m, direction: min}
budgets:
  gpu_hours_per_run: 1
  runs_per_week: 1
  attempt_cooldown_minutes: 0
  max_active_attempts: 2
scope: {allowed: [src/]}
roadmap: docs/roadmap.md
""",
        "org/pilot",
    )
    panel_key = tmp_path / "vkey"
    panel_key.write_text("k")
    panel_key.chmod(0o600)
    spec = FollowupSpec(
        target="org/pilot",
        account="acct",
        partition="part",
        run_root=tmp_path,
        image="img.sif",
        home=tmp_path,
        bot_login="bot",
        panel_key_file=str(panel_key),
    )
    job_ids = iter(["101", "102"])

    def runner(argv, timeout_s):
        if argv[0] == "sbatch":
            return CommandResult(0, next(job_ids) + "\n", "")
        return CommandResult(0, "RUNNING\n", "")

    compute = SlurmCompute(runner=runner)
    assert service_self_initiated(tmp_path, compute, spec, contract, NOW) == ("tsp", "101")
    # slot 2 is free, but the queued slot already spent the last weekly run
    assert service_self_initiated(tmp_path, compute, spec, contract, NOW + 60) is None


def test_width_never_launches_beside_a_steward_run(tmp_path: Path) -> None:
    """Width applies AMONG self-initiated slots; an active stewardship keeps
    its pre-width one-run-per-target exclusivity (terra #173 r1)."""
    from autoresearch.contract import load_contract
    from autoresearch.tick import FollowupSpec, service_self_initiated

    contract = load_contract(
        """
benchmarks:
  - {name: tsp, command: c, metric: m, direction: min}
budgets:
  gpu_hours_per_run: 1
  runs_per_week: 500
  max_active_attempts: 2
scope: {allowed: [src/]}
roadmap: docs/roadmap.md
""",
        "org/pilot",
    )
    spec = FollowupSpec(
        target="org/pilot",
        account="acct",
        partition="part",
        run_root=tmp_path,
        image="img.sif",
        home=tmp_path,
        bot_login="bot",
    )
    save_record(
        tmp_path,
        RunRecord(
            run_id="stew",
            target="org/pilot",
            task_title="t",
            state="implementing",
            agent_id="steward-01",
        ),
        now=NOW - 100,
    )
    compute = SlurmCompute(runner=lambda argv, timeout_s: CommandResult(0, "RUNNING\n", ""))
    assert service_self_initiated(tmp_path, compute, spec, contract, NOW) is None


def test_steward_waits_for_a_queued_width_slot(tmp_path: Path) -> None:
    """A slotted pending marker (width launch queued, record not written yet)
    blocks the steward lane the same way an active run does (terra #173 r1
    parity: the lane used to read only the legacy un-suffixed marker)."""
    from autoresearch.contract import load_contract
    from autoresearch.tick import FollowupSpec, effective_limits, service_steward, write_pending

    contract = load_contract(
        """
benchmarks:
  - {name: tsp, command: c, metric: m, direction: min}
budgets: {gpu_hours_per_run: 0, runs_per_week: 20}
scope: {allowed: [src/pilot/solvers/]}
steward: {allowed: [src/pilot/instances.py]}
roadmap: docs/roadmap.md
""",
        "org/pilot",
    )
    limits = effective_limits(contract.budgets)

    class G:
        def __init__(self):
            self.comments_posted = []

        def list_open_issues(self, repo, max_pages: int = 3):
            return [
                {
                    "number": 21,
                    "title": "re-base the tsp pool",
                    "body": "",
                    "user": {"login": "renmengye"},
                    "author_association": "OWNER",
                    "labels": [{"name": "autoresearch:steward"}],
                }
            ]

        def list_comments(self, repo, number, max_pages: int = 20):
            return []

        def comment(self, repo, number, body):
            self.comments_posted.append((number, body))

    spec = FollowupSpec(
        target="org/pilot",
        account="a",
        partition="p",
        run_root=tmp_path,
        image="img.sif",
        home=tmp_path,
        steward_key_file="/k",
    )
    write_pending(tmp_path, "org/pilot", "tsp", "777", NOW, agent="agent-02")
    compute = SlurmCompute(runner=lambda argv, timeout_s: CommandResult(0, "RUNNING\n", ""))
    github = G()
    assert service_steward(tmp_path, github, compute, spec, NOW + 60, contract, limits) is None
    assert github.comments_posted == []


def test_gpu_benchmark_refused_without_a_gpu_lane(tmp_path: Path) -> None:
    """A contract asking for GPUs on a deployment with no GPU lane must not
    launch: its evals would queue into jobs that can never run. With a lane,
    the launch proceeds and the climb job carries the lane coordinates."""
    from autoresearch.contract import load_contract
    from autoresearch.tick import FollowupSpec, service_self_initiated

    contract = load_contract(
        """
benchmarks:
  - {name: speedrun, command: c, metric: m, direction: min, gpus: 1, eval_minutes: 240}
budgets:
  gpu_hours_per_run: 60
  runs_per_week: 40
  attempt_cooldown_minutes: 0
scope: {allowed: [train.py]}
roadmap: docs/roadmap.md
""",
        "org/speedrun",
    )
    panel_key = tmp_path / "vkey"
    panel_key.write_text("k")
    panel_key.chmod(0o600)

    def spec(gpu_partition: str) -> FollowupSpec:
        return FollowupSpec(
            target="org/speedrun",
            account="acct",
            partition="cpu",
            run_root=tmp_path,
            image="img.sif",
            home=tmp_path,
            bot_login="bot",
            panel_key_file=str(panel_key),
            gpu_partition=gpu_partition,
        )

    submitted: list[str] = []

    def runner(argv, timeout_s):
        if argv[0] == "sbatch":
            submitted.append(" ".join(argv))
            return CommandResult(0, "501\n", "")
        return CommandResult(0, "RUNNING\n", "")

    compute = SlurmCompute(runner=runner)
    assert service_self_initiated(tmp_path, compute, spec(""), contract, NOW) is None
    assert submitted == []
    assert service_self_initiated(tmp_path, compute, spec("h200"), contract, NOW) == (
        "speedrun",
        "501",
    )


def test_gpu_benchmark_is_not_stewarded(tmp_path: Path) -> None:
    """A stewardship validates its rewrite in-job, inside the CPU work job:
    a GPU benchmark cannot be stewarded yet, lane or no lane (terra #174 r2)."""
    from autoresearch.contract import load_contract
    from autoresearch.tick import FollowupSpec, effective_limits, service_steward

    contract = load_contract(
        """
benchmarks:
  - {name: speedrun, command: c, metric: m, direction: min, gpus: 1, eval_minutes: 240}
budgets: {gpu_hours_per_run: 60, runs_per_week: 40}
scope: {allowed: [train.py]}
steward: {allowed: [eval/]}
roadmap: docs/roadmap.md
""",
        "org/speedrun",
    )
    limits = effective_limits(contract.budgets)

    class G:
        def __init__(self):
            self.comments_posted = []

        def list_open_issues(self, repo, max_pages: int = 3):
            return [
                {
                    "number": 7,
                    "title": "re-cut the val shard",
                    "body": "",
                    "user": {"login": "renmengye"},
                    "author_association": "OWNER",
                    "labels": [{"name": "autoresearch:steward"}],
                }
            ]

        def list_comments(self, repo, number, max_pages: int = 20):
            return []

        def comment(self, repo, number, body):
            self.comments_posted.append((number, body))

    spec = FollowupSpec(
        target="org/speedrun",
        account="a",
        partition="p",
        run_root=tmp_path,
        image="img.sif",
        home=tmp_path,
        steward_key_file="/k",
        gpu_partition="h200",
    )
    submitted: list[list[str]] = []

    def runner(argv, timeout_s):
        submitted.append(list(argv))
        return CommandResult(0, "9\n", "")

    compute = SlurmCompute(runner=runner)
    github = G()
    assert service_steward(tmp_path, github, compute, spec, NOW, contract, limits) is None
    assert submitted == [] and github.comments_posted == []


def test_gpu_sibling_needs_the_lane_even_for_a_cpu_climb(tmp_path: Path) -> None:
    """The suite gate measures siblings: a CPU benchmark's climb on a
    contract with a GPU sibling still needs the GPU lane (terra #174 r3)."""
    from autoresearch.contract import load_contract
    from autoresearch.tick import FollowupSpec, _gpu_lane_error

    contract = load_contract(
        """
benchmarks:
  - {name: tsp, command: c, metric: m, direction: min}
  - {name: speedrun, command: s, metric: steps, direction: min, gpus: 1, eval_minutes: 240}
budgets: {gpu_hours_per_run: 60, runs_per_week: 40}
scope: {allowed: [src/]}
roadmap: docs/roadmap.md
""",
        "org/mixed",
    )

    def spec(gpu_partition: str) -> FollowupSpec:
        return FollowupSpec(
            target="org/mixed",
            account="acct",
            partition="cpu",
            run_root=tmp_path,
            image="img.sif",
            home=tmp_path,
            gpu_partition=gpu_partition,
        )

    assert "speedrun" in _gpu_lane_error(contract, "tsp", spec(""))
    assert _gpu_lane_error(contract, "tsp", spec("h200")) == ""


def _wake_spec(tmp_path: Path):
    from autoresearch.tick import FollowupSpec

    return FollowupSpec(
        account="acct", partition="cpu_short", run_root=tmp_path, image="/img/a.sif", home=tmp_path
    )


def _sbatch_capture():
    submits: list[list[str]] = []

    def runner(argv, timeout_s):
        if argv[0] == "sbatch":
            submits.append(list(argv))
            return CommandResult(0, f"{9000 + len(submits)}\n", "")
        raise AssertionError(argv)

    return submits, SlurmCompute(runner=runner)


def test_arm_wake_submits_the_dependent_wake_and_hands_it_the_lease(
    tmp_path: Path, monkeypatch
) -> None:
    """A park submits its own wake at once, depending on the jobs it waits
    on — the same job and lease the sweep would use, only ~30 minutes
    (cadence + grace + cadence) earlier; arming is not a redelivery, so the
    stuck counter does not move."""
    from autoresearch.tick import JobWakeDispatcher, arm_wake

    monkeypatch.setattr(
        "autoresearch.tick._flight_command", lambda home, name, now, argv: " ".join(argv)
    )
    record = waiting_run(tmp_path, experiment_job_id="", stage={"afterany": "afterany:501:502"})
    submits, compute = _sbatch_capture()
    dispatcher = JobWakeDispatcher(compute, _wake_spec(tmp_path), NOW)
    job = arm_wake(tmp_path, record, dispatcher, NOW, holder_job_id="")
    assert job == "9001"
    assert "--dependency=afterany:501:502" in submits[0]
    lease = read_lease(tmp_path, "r1")
    assert lease is not None and lease.holder == "wake-job:9001" and lease.holder_job_id == "9001"
    assert load_record(tmp_path, "r1").wake_attempts == 0


def test_arm_wake_hands_over_its_own_lease_and_leaves_another_holder_alone(
    tmp_path: Path, monkeypatch
) -> None:
    from autoresearch.tick import JobWakeDispatcher, arm_wake

    monkeypatch.setattr(
        "autoresearch.tick._flight_command", lambda home, name, now, argv: " ".join(argv)
    )
    submits, compute = _sbatch_capture()
    dispatcher = JobWakeDispatcher(compute, _wake_spec(tmp_path), NOW)
    # a wake job (55) re-parks: its own lease passes to the wake it arms
    record = waiting_run(tmp_path, experiment_job_id="", stage={"afterany": "afterany:601"})
    acquire_lease(tmp_path, "r1", "wake-job:55", "55", now=NOW - 60)
    assert arm_wake(tmp_path, record, dispatcher, NOW, holder_job_id="55") == "9001"
    lease = read_lease(tmp_path, "r1")
    assert lease is not None and lease.holder_job_id == "9001"
    # a lease held by someone else (a tick mid-delivery) is respected: nothing armed
    other = waiting_run(
        tmp_path, run_id="r2", experiment_job_id="", stage={"afterany": "afterany:7"}
    )
    acquire_lease(tmp_path, "r2", "tick:host:1", "", now=NOW - 5)
    assert arm_wake(tmp_path, other, dispatcher, NOW, holder_job_id="55") == ""
    assert len(submits) == 1
    assert load_record(tmp_path, "r2").wake_attempts == 0


def test_sweep_leaves_an_armed_pending_wake_alone_however_old(tmp_path: Path) -> None:
    """A wake armed at park time waits in the queue as long as the evals run;
    an alive holder is never reaped by age."""
    waiting_run(tmp_path)
    acquire_lease(tmp_path, "r1", "wake-job:55", "55", now=NOW - 3 * 3600)
    slurm = FakeSlurm(states={"100": "RUNNING", "55": "PENDING"}, reasons={"55": "Dependency"})
    report, dispatcher = run_tick(tmp_path, slurm)
    assert dispatcher.dispatched == [] and report.reaped_leases == ()


def test_sweep_reaps_an_armed_wake_slurm_will_never_start(tmp_path: Path) -> None:
    """Torch does not kill jobs whose dependency can never be satisfied: such a
    wake would hold the lease forever. The sweep cancels it, reaps the lease,
    and redelivers — in the same tick."""
    waiting_run(tmp_path)
    acquire_lease(tmp_path, "r1", "wake-job:55", "55", now=NOW - 60)
    slurm = FakeSlurm(
        states={"100": "COMPLETED", "55": "PENDING"}, reasons={"55": "DependencyNeverSatisfied"}
    )
    report, dispatcher = run_tick(tmp_path, slurm)
    assert slurm.cancelled == ["55"]
    assert report.reaped_leases == ("r1",)
    assert dispatcher.dispatched == [("r1", "experiment COMPLETED")]


def test_sweep_gives_a_dependency_pending_wake_the_grace_window(tmp_path: Path) -> None:
    """Dependencies all terminal but the wake still pending on them: the grace
    window runs from when the sweep first saw that, then it is redelivered."""
    from autoresearch.tick import tick as tick_fn

    waiting_run(tmp_path, terminal_seen=0.0)
    acquire_lease(tmp_path, "r1", "wake-job:55", "55", now=NOW - 60)
    slurm = FakeSlurm(states={"100": "COMPLETED", "55": "PENDING"}, reasons={"55": "Dependency"})
    from autoresearch.tick import RecordingDispatcher as RD

    d = RD()
    tick_fn(tmp_path, slurm.compute(), d, now=NOW, min_tick_s=0)
    assert d.dispatched == [] and slurm.cancelled == []
    assert load_record(tmp_path, "r1").terminal_seen == NOW
    tick_fn(tmp_path, slurm.compute(), d, now=NOW + GRACE + 1, min_tick_s=0)
    assert slurm.cancelled == ["55"] and d.dispatched == [("r1", "experiment COMPLETED")]


def test_wake_spec_round_trips_and_is_absent_when_wakes_are_dry(tmp_path: Path) -> None:
    from autoresearch.tick import load_wake_spec, remove_wake_spec, write_wake_spec

    assert load_wake_spec(tmp_path) is None
    spec = _wake_spec(tmp_path)
    write_wake_spec(tmp_path, spec)
    assert load_wake_spec(tmp_path) == spec
    remove_wake_spec(tmp_path)
    assert load_wake_spec(tmp_path) is None
    (tmp_path / "wake-spec.json").write_text("not json")
    assert load_wake_spec(tmp_path) is None


def test_arm_wake_skips_a_park_with_nothing_to_depend_on(tmp_path: Path, monkeypatch) -> None:
    """A checkpoint sleep or blind park has no jobs: an armed wake would run
    at once instead of at the deadline floor, so nothing is armed."""
    from autoresearch.tick import JobWakeDispatcher, arm_wake

    monkeypatch.setattr(
        "autoresearch.tick._flight_command", lambda home, name, now, argv: " ".join(argv)
    )
    record = waiting_run(tmp_path, experiment_job_id="", stage={"afterany": ""})
    submits, compute = _sbatch_capture()
    dispatcher = JobWakeDispatcher(compute, _wake_spec(tmp_path), NOW)
    assert arm_wake(tmp_path, record, dispatcher, NOW, holder_job_id="") == ""
    assert submits == [] and read_lease(tmp_path, "r1") is None


def test_sweep_keeps_a_wake_it_could_not_cancel(tmp_path: Path) -> None:
    """Redelivery waits for a cancellation Slurm confirms: a wake still pending
    after scancel would otherwise run beside its replacement."""
    waiting_run(tmp_path)
    acquire_lease(tmp_path, "r1", "wake-job:55", "55", now=NOW - 60)
    slurm = FakeSlurm(
        states={"100": "COMPLETED", "55": "PENDING"},
        reasons={"55": "DependencyNeverSatisfied"},
        cancel_sticks=False,
    )
    report, dispatcher = run_tick(tmp_path, slurm)
    assert slurm.cancelled == ["55"]
    assert report.reaped_leases == () and dispatcher.dispatched == []


def test_pending_reason_query_failure_is_an_error_not_a_reason() -> None:
    import pytest

    from autoresearch.compute import SlurmQueryError

    def runner(argv, timeout_s):
        return CommandResult(1, "", "slurmctld down")

    with pytest.raises(SlurmQueryError):
        SlurmCompute(runner=runner).pending_reason("55")


def test_dispatch_wake_switch_reads_env_or_sentinel(tmp_path: Path, monkeypatch) -> None:
    from autoresearch.tick import dispatch_wake_armed

    monkeypatch.delenv("AUTORESEARCH_DISPATCH_WAKE", raising=False)
    assert not dispatch_wake_armed(tmp_path)
    (tmp_path / "DISPATCH_WAKE").touch()
    assert dispatch_wake_armed(tmp_path)
    (tmp_path / "DISPATCH_WAKE").unlink()
    monkeypatch.setenv("AUTORESEARCH_DISPATCH_WAKE", "1")
    assert dispatch_wake_armed(tmp_path)


def test_service_in_review_wakes_a_conflicted_pr_without_comments(tmp_path: Path) -> None:
    """A DIRTY PR is its own wake condition: no comments, job still submitted;
    a record already woken for this head is skipped."""
    from autoresearch.compute import CommandResult
    from autoresearch.runstate import IN_REVIEW, RunRecord, save_record
    from autoresearch.tick import FollowupSpec, service_in_review

    save_record(
        tmp_path,
        RunRecord(
            run_id="r-dirty",
            target="org/pilot",
            task_title="improve tsp",
            benchmark="tsp",
            state=IN_REVIEW,
            pr_url="https://github.com/org/pilot/pull/7",
        ),
        now=NOW,
    )
    save_record(
        tmp_path,
        RunRecord(
            run_id="r-dirty-woken",
            target="org/pilot",
            task_title="improve tsp",
            benchmark="tsp",
            state=IN_REVIEW,
            pr_url="https://github.com/org/pilot/pull/8",
            dirty_wake_head="h" * 40,
        ),
        now=NOW,
    )

    class G:
        def get_pull_request(self, repo, number):
            return {
                "state": "open",
                "merged": False,
                "mergeable": False,
                "mergeable_state": "dirty",
                "head": {"sha": "h" * 40},
                "base": {"ref": "main"},
            }

        def list_comments(self, repo, number, max_pages=20):
            return []

        def list_pr_reviews(self, repo, number, max_pages=10):
            return []

        def list_pr_review_comments(self, repo, number, max_pages=10):
            return []

    submits = []

    def runner(argv, timeout_s):
        if argv[0] == "sbatch":
            submits.append(list(argv))
            return CommandResult(0, "4243\n", "")
        if argv[0] == "sacct":
            return CommandResult(0, "RUNNING\n", "")
        raise AssertionError(argv)

    compute = SlurmCompute(runner=runner)
    spec = FollowupSpec(
        account="acct",
        partition="cpu_short",
        run_root=tmp_path,
        image="/img/a.sif",
        home=Path("/home/x/autoresearch"),
    )
    _ended, submitted = service_in_review(tmp_path, G(), compute, spec, NOW)
    assert submitted == [("r-dirty", "4243")]  # the already-woken head is skipped


def test_service_syncs_fetches_for_live_sessions(tmp_path: Path, monkeypatch) -> None:
    """A live implementing run's sync request gets a pinned-URL fetch and a
    done stamp; ended runs and unrequested workspaces are untouched."""
    import subprocess

    from autoresearch.runstate import RunRecord, run_dir, save_record
    from autoresearch.syscall import SYSCALL_DIR, sync_requested
    from autoresearch.tick import service_syncs

    def _g(cwd, *args):
        return subprocess.run(
            ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
        ).stdout

    seed = tmp_path / "seed"
    (seed / "docs").mkdir(parents=True)
    (seed / "docs" / "a.md").write_text("v1\n")
    _g(tmp_path, "init", "-q", "-b", "main", str(seed))
    _g(seed, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _g(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed")
    bare = tmp_path / "origin.git"
    _g(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))

    record = RunRecord(
        run_id="r-live",
        target="org/pilot",
        task_title="t",
        state="implementing",
        benchmark="tsp",
    )
    save_record(tmp_path, record, 1.0)
    ws = run_dir(tmp_path, "r-live") / "ws"
    ws.parent.mkdir(parents=True, exist_ok=True)
    _g(tmp_path, "clone", "-q", str(bare), str(ws))
    (ws / SYSCALL_DIR).mkdir()
    (ws / SYSCALL_DIR / "sync-request").touch()

    # main moves after the clone
    mover = tmp_path / "mover"
    _g(tmp_path, "clone", "-q", str(bare), str(mover))
    (mover / "docs" / "b.md").write_text("new\n")
    _g(mover, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _g(mover, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "moves")
    _g(mover, "push", "-q", "origin", "main")

    monkeypatch.setattr("autoresearch.attempt._target_clone_url", lambda t: str(bare))

    class Spec:
        pat_file = ""

    service_syncs(tmp_path, Spec(), 2.0)
    assert not sync_requested(ws)  # done stamped
    assert _g(ws, "show", "origin/main:docs/b.md").strip() == "new"
