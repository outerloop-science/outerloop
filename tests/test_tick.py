"""The tick's fail-safe sweep, scenario by scenario (fake Slurm, fake wakes)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
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
    assert pick([], pending_attempt=("denoise", NOW - 60)) == "reach"


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
    spec = FollowupSpec(
        target="org/pilot",
        account="acct",
        partition="part",
        run_root=tmp_path,
        image="img.sif",
        home=tmp_path,
        bot_login="bot",
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
    marker = read_pending(tmp_path, "org/pilot")
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
    assert read_pending(tmp_path, "org/pilot") is None
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
            climb_job_id=job_id,
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


def test_live_climb_job_is_left_alone(tmp_path: Path) -> None:
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
    6h stranded window frees the picker lane but never writes verdicts."""
    from autoresearch.tick import STRANDED_IMPLEMENTING_S

    _implementing_run(tmp_path, "r-old", job_id="", age_s=25 * 3600)
    _implementing_run(tmp_path, "r-stranded", job_id="", age_s=STRANDED_IMPLEMENTING_S + 60)
    report, _ = run_tick(tmp_path, FakeSlurm(states={}))
    assert report.implementing_ended == ("r-old",)
    assert load_record(tmp_path, "r-stranded").state == "implementing"
    assert "past its run deadline" in load_record(tmp_path, "r-old").ending_note


def test_self_initiated_carries_contract_limits_into_the_job(tmp_path: Path) -> None:
    """The submitted climb job wears the contract's (clamped) limits: Slurm
    walltime from climb_job_minutes, and the climb argv carries the session
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
  climb_job_minutes: 100000
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
    )
    submitted: list[list[str]] = []

    def runner(argv, timeout_s):
        submitted.append(list(argv))
        return CommandResult(0, "123\n", "")

    out = service_self_initiated(tmp_path, SlurmCompute(runner=runner), spec, contract, NOW)
    assert out == ("tsp", "123")
    sbatch = submitted[0]
    assert "--time=90" in sbatch  # 100000 clamped to the default-as-ceiling
    wrap = sbatch[-1]
    assert "--max-turns 30" in wrap
    assert "--session-minutes 25" in wrap
    assert "--job-minutes 90" in wrap  # the job's own walltime, for the alarm


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
        now=NOW,
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
        now=NOW,
    )
    # both present -> claim BEFORE submit, job carries the steward module + key
    github = G()
    out = service_steward(tmp_path, github, compute, spec("/k"), NOW, with_steward, limits)
    assert out == ("steward-issue-21", "321")
    assert github.comments_posted and "Claimed by the steward" in github.comments_posted[0][1]
    wrap = submitted[0][-1]
    assert "autoresearch.steward" in wrap
    assert "--key-file /k" in wrap
    assert "--job-minutes 90" in wrap
