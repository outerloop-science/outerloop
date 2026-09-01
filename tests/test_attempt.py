"""live_attempt end to end: real local git repos, fake harness/evaluator/API."""

from __future__ import annotations

import contextlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest

from autoresearch import attempt as climb_mod
from autoresearch.attempt import _park_run, live_attempt, resume_run
from autoresearch.dispatch import Snapshot
from autoresearch.harness import SessionResult
from autoresearch.orchestrator import RunConfig, RunParked
from autoresearch.runstate import RunRecord, load_record, save_record

CONTRACT = """\
benchmarks:
  - name: tsp
    command: uv run python -m pilot.eval --benchmark tsp --json
    metric: mean_tour_length
    direction: min
budgets: {gpu_hours_per_run: 1, runs_per_week: 10}
scope: {allowed: [src/pilot/solvers/]}
roadmap: docs/roadmap.md
"""

# Same contract with an eval hint past the in-job runway, so `should_dispatch`
# selects the dispatched backend.
CONTRACT_DISPATCH = CONTRACT.replace(
    "    direction: min\n", "    direction: min\n    eval_minutes: 30\n"
)

# Same contract with research lines on (docs/design/research-lines.md).
CONTRACT_LINES = CONTRACT.replace("    direction: min\n", "    direction: min\n    lines: true\n")


def _session(sid: str = "s1") -> SessionResult:
    return SessionResult(
        stop_reason="end_turn",
        is_error=False,
        cost_usd=1.0,
        num_turns=5,
        session_id=sid,
        final_text="report",
        transcript_path="",
    )


def test_park_run_writes_a_waiting_record_with_the_reentry_stage(tmp_path) -> None:
    record = RunRecord(
        run_id="tsp-1", target="org/pilot", task_title="t", state="implementing", benchmark="tsp"
    )
    snap = Snapshot(commit="c" * 40, tree="d" * 40, ref="refs/dispatch/tok")
    parked = RunParked(
        phase="candidate",
        afterany="afterany:101:102",
        base_sha="b" * 40,
        seed=7,
        suite_seed=9,
        candidate_sha="c" * 40,
        session=_session("s1"),
    )
    _park_run(tmp_path, record, parked, snap.ref, eval_minutes=90, now=1000.0)

    r = load_record(tmp_path, "tsp-1")
    assert r.state == "waiting"
    # a MULTI-job park records no single experiment job — the sweep must not
    # wake when job 101 finishes while 102 runs; it rides the deadline floor.
    assert r.experiment_job_id == ""
    # deadline is walltime-aware (eval walltime + queue slack), not a flat 24h,
    # so the sweep never cancels a still-queued job of a legitimately slow eval
    assert r.deadline == 1000.0 + (90 + 12 * 60) * 60
    assert r.terminal_seen == 0.0  # the NEW experiment has not been seen terminal
    assert r.resume_session_id == "s1"  # the candidate park resumes the session
    assert r.stage["phase"] == "candidate"
    assert r.stage["base_sha"] == "b" * 40 and r.stage["candidate_sha"] == "c" * 40
    assert r.stage["candidate_ref"] == "refs/dispatch/tok"  # for drop at the terminal
    assert r.stage["seed"] == 7 and r.stage["suite_seed"] == 9
    assert r.stage["afterany"] == "afterany:101:102"
    # the session's write-up + spend ride the stage so a candidate wake can
    # build the PR body and report the real cost without re-running the session
    assert r.stage["report"] == "report"
    assert r.stage["session_cost_usd"] == 1.0 and r.stage["session_turns"] == 5


def test_park_arms_its_own_wake_when_the_tick_published_the_recipe(tmp_path, monkeypatch) -> None:
    """With dispatched wakes on (the tick publishes wake-spec.json), a park
    submits its wake immediately and the wake job holds the lease; without
    the recipe nothing is armed and the sweep delivers as before."""
    from autoresearch.runstate import read_lease
    from autoresearch.tick import FollowupSpec, write_wake_spec

    monkeypatch.setattr(
        "autoresearch.tick._flight_command", lambda home, name, now, argv: " ".join(argv)
    )

    def park(run_id: str) -> None:
        record = RunRecord(
            run_id=run_id, target="org/pilot", task_title="t", state="implementing", benchmark="tsp"
        )
        parked = RunParked(
            phase="candidate",
            afterany="afterany:501:502",
            base_sha="b" * 40,
            seed=7,
            suite_seed=9,
            candidate_sha="c" * 40,
            session=_session("s9"),
        )
        _park_run(
            tmp_path, record, parked, "refs/dispatch/tok", None, 1000.0, dispatch=_fake_dispatch()
        )

    monkeypatch.delenv("AUTORESEARCH_DISPATCH_WAKE", raising=False)
    park("tsp-quiet")
    assert read_lease(tmp_path, "tsp-quiet") is None
    assert load_record(tmp_path, "tsp-quiet").wake_attempts == 0

    spec = FollowupSpec(
        account="a", partition="cpu", run_root=tmp_path, image="/img.sif", home=tmp_path
    )
    write_wake_spec(tmp_path, spec)
    park("tsp-disarmed")  # a recipe left behind after a disarm is not used
    assert read_lease(tmp_path, "tsp-disarmed") is None
    (tmp_path / "DISPATCH_WAKE").touch()
    park("tsp-armed")
    lease = read_lease(tmp_path, "tsp-armed")
    assert lease is not None and lease.holder == "wake-job:1000"
    r = load_record(tmp_path, "tsp-armed")
    assert r.state == "waiting" and r.wake_attempts == 0  # arming is not a redelivery


def test_release_own_lease_keeps_a_lease_handed_to_the_armed_wake(tmp_path, monkeypatch) -> None:
    from autoresearch.attempt import _lease_held_by_another_job, _release_own_lease
    from autoresearch.runstate import acquire_lease, read_lease, release_lease

    acquire_lease(tmp_path, "r", "wake-job:9001", "9001", now=1.0)
    monkeypatch.setenv("SLURM_JOB_ID", "55")  # the wake job that armed 9001, exiting
    assert _lease_held_by_another_job(tmp_path, "r") == "9001"
    _release_own_lease(tmp_path, "r")
    assert read_lease(tmp_path, "r") is not None
    monkeypatch.delenv("SLURM_JOB_ID")  # a manual resume never owns a job-held lease
    assert _lease_held_by_another_job(tmp_path, "r") == "9001"
    _release_own_lease(tmp_path, "r")
    assert read_lease(tmp_path, "r") is not None
    monkeypatch.setenv("SLURM_JOB_ID", "9001")
    assert _lease_held_by_another_job(tmp_path, "r") == ""
    _release_own_lease(tmp_path, "r")
    assert read_lease(tmp_path, "r") is None
    # a lease with no job id (a tick's, mid-delivery) is the caller's to release
    release_lease(tmp_path, "r")
    acquire_lease(tmp_path, "r", "park:1", "", now=1.0)
    monkeypatch.delenv("SLURM_JOB_ID")
    assert _lease_held_by_another_job(tmp_path, "r") == ""
    _release_own_lease(tmp_path, "r")
    assert read_lease(tmp_path, "r") is None


def test_launch_hours_are_reconciled_once_from_the_parks_launch_jobs(tmp_path, monkeypatch) -> None:
    """A park remembers which of its jobs were the author's launches
    (`launch_afterany`); the refund reads those jobs' elapsed time — never the
    gate's evals mixed into a candidate park's `afterany` — and happens once
    per park, however many wakes read the budget afterwards."""
    from dataclasses import dataclass

    from autoresearch.attempt import _reconcile_launch_hours, _stage_launch_job_ids
    from autoresearch.syscall import Launch

    @dataclass
    class Elapsed:
        seconds: dict

        def elapsed_seconds(self, job_id: str) -> int | None:
            return self.seconds.get(job_id)

    @dataclass
    class D:
        compute: object

    sweep = (Launch(name="sweep", command="x", minutes=240, array=2),)
    # a candidate park: gate evals 601/602 waited on too, launches 701/702
    rec = RunRecord(
        run_id="r",
        target="o/p",
        task_title="t",
        state="waiting",
        stage={
            "phase": "candidate",
            "afterany": "afterany:601:602:701:702",
            "launch_afterany": "afterany:701:702",
            "syscall_launches": [{"name": "sweep", "minutes": 240, "array": 2}],
            "gpu_hours_used": 16.0,
        },
    )
    assert _stage_launch_job_ids(rec) == ["701", "702"]
    d = D(Elapsed({"601": 12000, "602": 12000, "701": 300, "702": 300}))
    used = _reconcile_launch_hours(rec, d, 1, sweep)  # type: ignore[arg-type]
    assert abs(used - (16.0 - (8.0 - 600 / 3600))) < 1e-9
    assert rec.stage["launch_hours_refunded"] is True
    assert _reconcile_launch_hours(rec, d, 1, sweep) == used  # type: ignore[arg-type]
    # older parks: an author-sleep park's jobs were all launches; a candidate
    # park without the field has the gate mixed in, so nothing is refunded
    old_sleep = RunRecord(
        run_id="s",
        target="o/p",
        task_title="t",
        state="waiting",
        stage={"phase": "author-sleep", "afterany": "afterany:701:702"},
    )
    assert _stage_launch_job_ids(old_sleep) == ["701", "702"]
    old_cand = RunRecord(
        run_id="c",
        target="o/p",
        task_title="t",
        state="waiting",
        stage={"phase": "candidate", "afterany": "afterany:601:701"},
    )
    assert _stage_launch_job_ids(old_cand) == []
    # no GPUs: nothing is metered, nothing refunded
    assert _reconcile_launch_hours(old_sleep, d, 0, sweep) == 0.0  # type: ignore[arg-type]


def test_research_reports_fetch_and_archive(tmp_path) -> None:
    """A fresh attempt pulls the target's research-log reports: newest first
    for the brief, full texts into the channel archive; a target without the
    branch is an empty memory, never an error. Only plain file names reach
    the archive (branch content is remote-controlled)."""
    from autoresearch.attempt import _fetch_research_reports, _install_report_archive
    from autoresearch.github import Workspace

    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "README.md").write_text("x")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    _git(seed, "push", "-q", str(origin), "main")
    ws_dir = tmp_path / "ws"
    _git(tmp_path, "clone", "-q", str(origin), str(ws_dir))
    ws = Workspace(root=ws_dir)

    assert _fetch_research_reports(ws, 5) == []  # no research-log branch yet

    (seed / "reports").mkdir()
    (seed / "reports" / "2026-08-28-run-a.md").write_text("Outcome: **no-improvement**\n")
    (seed / "reports" / "2026-08-29-run-b.md").write_text("Outcome: **negative-result**\n")
    # a nested path would flatten to a basename that overwrites another
    # report (review #191 r2): only direct children are read
    (seed / "reports" / "nested").mkdir()
    (seed / "reports" / "nested" / "2026-08-29-run-b.md").write_text("shadow\n")
    _git(seed, "checkout", "-qb", "research-log")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "reports")
    _git(seed, "push", "-q", str(origin), "research-log")

    reports = _fetch_research_reports(ws, 5)
    assert [n for n, _ in reports] == ["2026-08-29-run-b.md", "2026-08-28-run-a.md"]
    assert "negative-result" in reports[0][1]  # never the nested shadow
    assert [n for n, _ in _fetch_research_reports(ws, 1)] == ["2026-08-29-run-b.md"]

    # a huge report (branch content is remote-controlled) is skipped by its
    # blob size BEFORE `git show` would load it; the next report fills the slot
    from autoresearch.attempt import MAX_ARCHIVED_REPORT_CHARS

    (seed / "reports" / "2026-08-30-run-c.md").write_text("x" * (MAX_ARCHIVED_REPORT_CHARS + 999))
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "big")
    _git(seed, "push", "-q", str(origin), "research-log")
    assert [n for n, _ in _fetch_research_reports(ws, 1)] == ["2026-08-29-run-b.md"]

    # the sibling snapshot rides the SAME fetch: status.json on the branch
    # yields the other agents' entries, self excluded, bounded fields; a
    # branch without it means no siblings known
    import json as _json

    from autoresearch.attempt import _sibling_entries

    assert _sibling_entries(ws, "agent-01") == []  # no status.json yet
    status = {
        "runs": [
            {"agent": "agent-01", "state": "waiting", "phase": "candidate", "direction": "me"},
            {
                "agent": "agent-02",
                "state": "waiting",
                "phase": "author-sleep",
                "direction": "d" * 500,
            },
        ]
    }
    (seed / "climb").mkdir()
    (seed / "climb" / "status.json").write_text(_json.dumps(status))
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "status")
    _git(seed, "push", "-q", str(origin), "research-log")
    _fetch_research_reports(ws, 5)  # refresh FETCH_HEAD
    entries = _sibling_entries(ws, "agent-01")
    assert [e["agent"] for e in entries] == ["agent-02"]  # self excluded
    assert len(entries[0]["direction"]) == 160  # bounded

    # production order: the tool installer recreates the channel dir it owns,
    # so the archive must be written AFTER it (review #191: writing before
    # deleted every archive)
    from autoresearch.syscall import ensure_excluded, install_tool

    ensure_excluded(ws_dir)
    install_tool(ws_dir)
    _install_report_archive(ws_dir, [*reports, ("../escape.md", "nope")])
    assert (ws_dir / ".autoresearch" / "syscall").exists()  # both survive
    archive = ws_dir / ".autoresearch" / "reports"
    assert sorted(f.name for f in archive.iterdir()) == [
        "2026-08-28-run-a.md",
        "2026-08-29-run-b.md",
    ]
    assert not (ws_dir / ".autoresearch" / "escape.md").exists()
    assert not (ws_dir / "escape.md").exists()


def test_author_sleep_park_carries_the_gate_verdict(tmp_path) -> None:
    """A gate negative the author answered by launching more work rides the
    park, so the wake that ends on the same tree reuses it (review #182)."""
    from autoresearch.attempt import _stage_judged
    from autoresearch.orchestrator import AttemptResult
    from autoresearch.syscall import Launch, SyscallRequest

    record = RunRecord(
        run_id="tsp-3", target="org/pilot", task_title="t", state="implementing", benchmark="tsp"
    )
    verdict = AttemptResult(
        outcome="no-improvement", baseline=13.0, candidate=13.0, note="inside the floor"
    )
    parked = RunParked(
        phase="author-sleep",
        afterany="afterany:501",
        base_sha="b" * 40,
        seed=7,
        suite_seed=9,
        candidate_sha="c" * 40,
        session=_session("s9"),
        syscall=SyscallRequest(launches=(Launch(name="probe", command="x", minutes=5),)),
        judged=("d" * 40, verdict),
        launch_afterany="afterany:501",
    )
    _park_run(tmp_path, record, parked, "refs/dispatch/tok", eval_minutes=None, now=1000.0)
    r = load_record(tmp_path, "tsp-3")
    assert r.stage["judged"] == {
        "sha": "d" * 40,
        "outcome": "no-improvement",
        "baseline": 13.0,
        "candidate": 13.0,
        "note": "inside the floor",
    }
    assert _stage_judged(r) == ("d" * 40, verdict)
    assert r.stage["launch_afterany"] == "afterany:501"
    bare = RunRecord(run_id="x", target="o/p", task_title="t", state="waiting")
    assert _stage_judged(bare) is None


def test_author_sleep_park_persists_the_request_and_floors_on_the_launch(tmp_path) -> None:
    # Phase A (research-loop-buildout.md): an author-sleep park carries the
    # launch names/artifacts, note, session id, and budget counts the wake
    # needs — and its deadline floors on the LONGEST LAUNCH's walltime, not the
    # benchmark's eval hint (an in-job-cheap benchmark can still train for
    # hours; the sweep must not cancel the author's jobs).
    from autoresearch.syscall import Launch, SyscallRequest

    record = RunRecord(
        run_id="tsp-2", target="org/pilot", task_title="t", state="implementing", benchmark="tsp"
    )
    parked = RunParked(
        phase="author-sleep",
        afterany="afterany:501",
        base_sha="b" * 40,
        seed=7,
        suite_seed=9,
        candidate_sha="c" * 40,
        session=_session("s9"),
        syscall=SyscallRequest(
            launches=(
                Launch(
                    name="train",
                    command="uv run train.py",
                    minutes=180,
                    artifacts=("out/curve.json",),
                ),
            ),
            note="compare to the lr sweep",
        ),
        launches_used=1,
        sleeps_used=1,
    )
    _park_run(tmp_path, record, parked, "refs/dispatch/tok", eval_minutes=None, now=1000.0)

    r = load_record(tmp_path, "tsp-2")
    assert r.state == "waiting"
    # eval_minutes=None (in-job benchmark) but the launch asks 180 min: the
    # floor rides the launch, so a healthy queued job never gets swept
    assert r.deadline == 1000.0 + (180 + 12 * 60) * 60
    assert r.stage["phase"] == "author-sleep"
    assert r.stage["syscall_launches"] == [
        {"name": "train", "minutes": 180, "artifacts": ["out/curve.json"]}
    ]
    assert r.stage["syscall_note"] == "compare to the lr sweep"
    assert r.stage["launches_used"] == 1 and r.stage["sleeps_used"] == 1
    assert r.resume_session_id == "s9"  # the record's own field; no stage duplicate


def test_checkpoint_sleep_park_gets_a_near_term_deadline(tmp_path) -> None:
    """A LAUNCH-LESS author sleep has nothing in any queue — its deadline
    must reach only the next sweep pass (CHECKPOINT_SLEEP_SLACK_MIN), never
    the 12h queue slack. Observed live (yolo heldout_probe, 2026-08-27): the
    queue slack turned a checkpoint nap into a 12h coma."""
    from autoresearch.attempt import CHECKPOINT_SLEEP_SLACK_MIN
    from autoresearch.syscall import SyscallRequest

    record = RunRecord(
        run_id="tsp-3", target="org/pilot", task_title="t", state="implementing", benchmark="tsp"
    )
    parked = RunParked(
        phase="author-sleep",
        afterany="",
        base_sha="b" * 40,
        seed=7,
        suite_seed=9,
        candidate_sha="c" * 40,
        session=_session("s10"),
        syscall=SyscallRequest(launches=(), note="pausing to reread results next wake"),
        launches_used=2,
        sleeps_used=3,
    )
    _park_run(tmp_path, record, parked, "refs/dispatch/tok", eval_minutes=90, now=1000.0)

    r = load_record(tmp_path, "tsp-3")
    assert r.state == "waiting"
    # the writer itself must produce the near-term deadline — not the queue
    # slack, and not the benchmark eval hint (nothing was dispatched)
    assert r.deadline == 1000.0 + CHECKPOINT_SLEEP_SLACK_MIN * 60
    assert r.stage["syscall_launches"] == []
    assert r.stage["launches_used"] == 2 and r.stage["sleeps_used"] == 3


def test_park_run_redacts_the_saved_report(tmp_path) -> None:
    # a session that echoed a secret must not leave it readable in record.json
    record = RunRecord(
        run_id="tsp-9", target="org/pilot", task_title="t", state="implementing", benchmark="tsp"
    )
    leaky = SessionResult(
        stop_reason="end_turn",
        is_error=False,
        cost_usd=1.0,
        num_turns=5,
        session_id="s1",
        final_text="used key sk-secret-123 to fetch",
        transcript_path="",
    )
    parked = RunParked(
        phase="candidate",
        afterany="afterany:1",
        base_sha="b" * 40,
        seed=1,
        suite_seed=1,
        candidate_sha="c" * 40,
        session=leaky,
    )
    _park_run(tmp_path, record, parked, "refs/dispatch/tok", 90, 1000.0, ("sk-secret-123",))
    report = str(load_record(tmp_path, "tsp-9").stage["report"])
    assert "sk-secret-123" not in report and "used key" in report


def test_park_run_single_job_records_it_for_the_sweep(tmp_path) -> None:
    # one eval job (a baseline park, or a candidate with no siblings): the sweep
    # CAN poll it directly for a terminal+grace wake.
    record = RunRecord(
        run_id="tsp-3", target="org/pilot", task_title="t", state="implementing", benchmark="tsp"
    )
    parked = RunParked(
        phase="baseline", afterany="afterany:77", base_sha="b" * 40, seed=0, suite_seed=0
    )
    _park_run(tmp_path, record, parked, "", eval_minutes=90, now=1000.0)
    assert load_record(tmp_path, "tsp-3").experiment_job_id == "77"


def test_park_resets_wake_attempts_a_productive_park_left_waiting(tmp_path) -> None:
    # the run reached IMPLEMENTING (left waiting, did work) before this park, so
    # "wakes since it last left waiting" resets — a productive park/wake cycle
    # must not creep toward the stuck cap.
    record = RunRecord(
        run_id="tsp-4",
        target="org/pilot",
        task_title="t",
        state="implementing",
        benchmark="tsp",
        wake_attempts=2,
    )
    parked = RunParked(
        phase="baseline", afterany="afterany:9", base_sha="b" * 40, seed=0, suite_seed=0
    )
    _park_run(tmp_path, record, parked, "", eval_minutes=90, now=1000.0)
    assert load_record(tmp_path, "tsp-4").wake_attempts == 0


def test_park_run_baseline_phase_has_no_candidate_or_session(tmp_path) -> None:
    record = RunRecord(
        run_id="tsp-2", target="org/pilot", task_title="t", state="implementing", benchmark="tsp"
    )
    parked = RunParked(
        phase="baseline", afterany="afterany:55", base_sha="b" * 40, seed=0, suite_seed=0
    )
    _park_run(tmp_path, record, parked, "", eval_minutes=90, now=1000.0)

    r = load_record(tmp_path, "tsp-2")
    assert r.state == "waiting" and r.resume_session_id == ""  # session not run yet
    assert r.stage["phase"] == "baseline"
    assert r.stage["candidate_sha"] == "" and r.stage["candidate_ref"] == ""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


def _seed_target(tmp_path: Path, monkeypatch, contract: str) -> Path:
    """A bare 'github' repo seeded with a pilot-shaped tree; Workspace.clone
    is monkeypatched to clone from it instead of github.com."""
    seed = tmp_path / "seed"
    (seed / "src" / "pilot" / "solvers").mkdir(parents=True)
    (seed / "docs").mkdir()
    (seed / ".autoresearch.yaml").write_text(contract)
    (seed / "docs" / "roadmap.md").write_text("# roadmap\n")
    (seed / "src" / "pilot" / "solvers" / "tsp.py").write_text("def solve(): ...\n")
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed")
    bare = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))

    from autoresearch.github import Workspace

    real_clone = Workspace.clone

    def fake_clone(url, dest, auth=None, dry_run=False):
        return real_clone(str(bare), dest, auth=None, dry_run=dry_run)

    monkeypatch.setattr(climb_mod.Workspace, "clone", staticmethod(fake_clone))
    return bare


@pytest.fixture
def target_repo(tmp_path: Path, monkeypatch) -> Path:
    return _seed_target(tmp_path, monkeypatch, CONTRACT)


@pytest.fixture
def target_repo_dispatch(tmp_path: Path, monkeypatch) -> Path:
    return _seed_target(tmp_path, monkeypatch, CONTRACT_DISPATCH)


@dataclass
class ScriptedHarness:
    """Applies edits to the workspace like a session would."""

    edits: dict[str, str]
    text: str = "Report: swapped construction heuristic; tours shortened."

    def run(self, brief_text, workspace, resume_session_id=None) -> SessionResult:
        for rel, content in self.edits.items():
            path = workspace / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return SessionResult(
            stop_reason="end_turn",
            is_error=False,
            cost_usd=0.9,
            num_turns=12,
            session_id="s1",
            final_text=self.text,
            transcript_path="",
        )


@dataclass
class QueueCompute:
    """A LocalCompute stand-in for the gate's eval jobs: `submit` writes the
    next queued value straight into the job's eval dir (every metric key the
    test contracts use, so any measure parses it) instead of running the
    script, so a test's values are consumed in submit order."""

    values: list = field(default_factory=list)
    submitted: list = field(default_factory=list)
    _seq: int = 0

    def submit(self, spec) -> str:
        import json as _json

        self._seq += 1
        ev = Path(spec.script).parent
        self.submitted.append(ev.name)
        value = self.values.pop(0)
        if isinstance(value, Exception):
            (ev / "stdout").write_text("")
            (ev / "stderr").write_text(str(value))
            (ev / "exit-code").write_text("1")
        else:
            keys = ("mean_tour_length", "solve_rate", "score")
            (ev / "stdout").write_text(_json.dumps({k: value for k in keys}))
            (ev / "exit-code").write_text("0")
        return str(9_000_000_000 + self._seq)

    def status(self, job_id: str) -> str:
        return "COMPLETED"

    def active_job_names(self) -> list:
        return []

    def job_id_for_name(self, name: str) -> str:
        return ""

    def cancel(self, job_id: str) -> None:
        pass


@dataclass
class FakeGitHub:
    prs: list[dict] = field(default_factory=list)
    armed: list[tuple[str, int]] = field(default_factory=list)
    arming_error: str = ""
    existing_pr: str = ""  # find_open_pull_for_head returns this (idempotency)

    def create_pull(self, repo, title, head, base, body, draft=False) -> str:
        self.prs.append(dict(repo=repo, title=title, head=head, base=base, body=body, draft=draft))
        return f"https://github.com/{repo}/pull/1"

    def find_open_pull_for_head(self, repo, head_branch, base):
        if not self.existing_pr:
            return None
        num = self.existing_pr.rstrip("/").rsplit("/", 1)[-1]
        return {"html_url": self.existing_pr, "number": int(num), "draft": False}

    def arm_auto_merge_when_review_required(self, repo, number) -> bool:
        if self.arming_error:
            raise RuntimeError(self.arming_error)
        self.armed.append((repo, number))
        return True


@dataclass
class CommentingGitHub(FakeGitHub):
    issue_comments: list = field(default_factory=list)

    def comment(self, repo, number, body):
        self.issue_comments.append((number, body))

    def list_comments(self, repo, number, max_pages=20):
        return []


@dataclass
class NoAuth:
    def token(self) -> str:
        return "unused"


@contextlib.contextmanager
def _queued_local(queue):
    """Route the gate's local eval jobs through QueueCompute(queue)."""
    import autoresearch.attempt as _climb_mod

    orig = _climb_mod.LocalCompute
    _climb_mod.LocalCompute = lambda: QueueCompute(values=queue)  # type: ignore[assignment,misc]
    try:
        yield
    finally:
        _climb_mod.LocalCompute = orig  # type: ignore[misc]


def run_live(
    tmp_path,
    target_repo,
    edits,
    values,
    run_id="tsp-1",
    dispatch=None,
    author_backend="claude",
    author_model="claude-opus-5",
    author_key_file="",
    eval_image="",
) -> tuple:
    github = FakeGitHub()
    queue = list(values)
    with _queued_local(queue):
        outcome = live_attempt(
            config=RunConfig(target="org/pilot", benchmark="tsp"),
            run_root=tmp_path / "state",
            run_id=run_id,
            harness=ScriptedHarness(edits=edits),
            github=github,  # type: ignore[arg-type]
            bot_auth=NoAuth(),  # type: ignore[arg-type]
            now=1_000_000.0,
            created="2026-08-06T00:00:00Z",
            secrets=("sk-live-key",),
            dispatch=dispatch,
            author_backend=author_backend,
            author_model=author_model,
            author_key_file=author_key_file,
            eval_image=eval_image,
        )
    return outcome, github


def _fake_dispatch(image="/img.sif", account="acct", partition="cpu", cancelled=None):
    """A DispatchSettings whose SlurmCompute never touches real Slurm: sbatch
    returns a fresh numeric id, squeue reports no live job (so results()
    dispatches then parks on the ids it just submitted). `cancelled`, if given,
    records the job ids passed to scancel."""
    from autoresearch.compute import CommandResult, SlurmCompute
    from autoresearch.measure import DispatchSettings

    ids = iter(range(1000, 1100))

    def runner(argv, timeout_s):
        if argv[0] == "sbatch":
            return CommandResult(0, f"{next(ids)}\n", "")
        if argv[0] == "squeue":
            return CommandResult(0, "", "")  # nothing live -> dispatch
        if argv[0] == "scancel":
            if cancelled is not None:
                cancelled.append(argv[1])
            return CommandResult(0, "", "")
        raise AssertionError(f"unexpected slurm call: {argv[0]}")

    return DispatchSettings(
        compute=SlurmCompute(runner=runner),
        image=image,
        account=account,
        partition=partition,
    )


def test_improvement_produces_branch_commit_and_pr(tmp_path, target_repo) -> None:
    outcome, github = run_live(
        tmp_path,
        target_repo,
        edits={"src/pilot/solvers/tsp.py": "def solve(): return 'better'\n"},
        values=[13.876, 13.1],
    )
    assert outcome.outcome == "improved"
    assert outcome.pr_url.endswith("/pull/1")
    # the branch landed in the bare origin: the solver edit plus the two
    # orchestrator-written progress files, nothing else
    files = set(
        _git(target_repo, "diff", "--name-only", "main", "feat/auto/agent-01/tsp-1").split()
    )
    assert files == {"src/pilot/solvers/tsp.py", "BENCHMARKS.md", "results/leader.json"}
    pr = github.prs[0]
    assert pr["head"] == "feat/auto/agent-01/tsp-1"
    assert pr["title"] == "[agent] tsp: 13.88 -> 13.1"  # 4 sig figs, not full floats
    assert "measured by the orchestrator" in pr["body"]
    # run record went in-review with the PR url
    record = load_record(tmp_path / "state", "tsp-1")
    assert record.state == "in-review"
    assert "pull/1" in record.ending_note
    # report exists and is redacted-safe
    report = Path(outcome.report_path).read_text()
    assert "improved" in report


def test_run_record_persists_the_author_pair(tmp_path, target_repo) -> None:
    # a wake/follow-up reproduces the parked run's author, so the (backend, model)
    # PAIR it was started with is stamped on the record — a codex record must not
    # be resumed with a claude model.
    run_live(
        tmp_path,
        target_repo,
        edits={"src/pilot/solvers/tsp.py": "def solve(): return 'better'\n"},
        values=[13.876, 13.1],
        author_backend="codex",
        author_model="gpt-5.6-terra",
        author_key_file="/keys/codex",
    )
    rec = load_record(tmp_path / "state", "tsp-1")
    assert rec.author_backend == "codex" and rec.author_model == "gpt-5.6-terra"
    assert rec.author_key_file == "/keys/codex"  # exact key survives for the wake


def test_resume_author_reproduces_the_run_not_the_fleet(monkeypatch) -> None:
    """A wake/follow-up derives (backend, model, key_file) from the record, never
    the fleet: a legacy record is claude (not the fleet default), a claude record
    keeps a claude model, a codex record keeps its own model, and an explicit
    recorded key path survives (else it resolves per backend)."""
    from types import SimpleNamespace

    from autoresearch.attempt import resume_author

    monkeypatch.setenv("AUTORESEARCH_HARNESS_KEY_FILE", "/h")
    monkeypatch.setenv("AUTORESEARCH_CODEX_KEY_FILE", "/c")

    legacy = SimpleNamespace(author_backend="", author_model="", author_key_file="")
    assert resume_author(legacy, fleet_model="gpt-5.6-terra") == ("claude", "claude-opus-5", "/h")
    claude_rec = SimpleNamespace(
        author_backend="claude", author_model="claude-opus-5", author_key_file=""
    )
    assert resume_author(claude_rec, fleet_model="gpt-5.6-terra") == (
        "claude",
        "claude-opus-5",
        "/h",
    )
    codex_rec = SimpleNamespace(
        author_backend="codex", author_model="gpt-5.6-terra", author_key_file=""
    )
    assert resume_author(codex_rec, fleet_model="claude-opus-5") == (
        "codex",
        "gpt-5.6-terra",
        "/c",
    )
    # an explicit recorded key path wins over the per-backend env
    pinned = SimpleNamespace(
        author_backend="codex", author_model="gpt-5.6-terra", author_key_file="/custom/key"
    )
    assert resume_author(pinned, fleet_model="x") == ("codex", "gpt-5.6-terra", "/custom/key")
    assert resume_author(None, fleet_model="x") == ("claude", "claude-opus-5", "/h")


def test_codex_author_config_error() -> None:
    """codex needs --image and a non-claude model; claude is always fine."""
    from autoresearch.attempt import codex_author_config_error

    assert codex_author_config_error("claude", "claude-opus-5", "") == ""
    assert codex_author_config_error("codex", "gpt-5.6-terra", "img.sif") == ""
    assert "requires --image" in codex_author_config_error("codex", "gpt-5.6-terra", "")
    assert "claude default" in codex_author_config_error("codex", "claude-opus-5", "img.sif")
    assert "claude default" in codex_author_config_error("codex", "", "img.sif")
    # an unknown backend (typo'd env default) is rejected, not silently accepted
    assert "unknown author backend" in codex_author_config_error("hermes", "m", "img.sif")


def test_resolve_author_key_file(monkeypatch) -> None:
    """Per-backend author keys COEXIST and are selected by backend; an explicit
    path wins, else the per-backend env var, else the packaged default."""
    import os

    from autoresearch.attempt import (
        CODEX_KEY_DEFAULT,
        HARNESS_KEY_DEFAULT,
        resolve_author_key_file,
    )

    monkeypatch.delenv("AUTORESEARCH_HARNESS_KEY_FILE", raising=False)
    monkeypatch.delenv("AUTORESEARCH_CODEX_KEY_FILE", raising=False)
    assert resolve_author_key_file("codex", "/x/key") == "/x/key"  # explicit wins
    assert resolve_author_key_file("claude") == os.path.expanduser(HARNESS_KEY_DEFAULT)
    assert resolve_author_key_file("codex") == os.path.expanduser(CODEX_KEY_DEFAULT)
    monkeypatch.setenv("AUTORESEARCH_HARNESS_KEY_FILE", "/h-key")
    monkeypatch.setenv("AUTORESEARCH_CODEX_KEY_FILE", "/c-key")
    assert resolve_author_key_file("claude") == "/h-key"
    assert resolve_author_key_file("codex") == "/c-key"


def test_snapshot_refs_are_dropped_after_a_climb(tmp_path, target_repo) -> None:
    # the candidate snapshots are retained by ref during measurement; the climb
    # must drop every one when it ends, or each parked-or-finished run leaks a
    # ref and its commit (terra's #102 round-9 concern, now enforced in code).
    outcome, _ = run_live(
        tmp_path,
        target_repo,
        edits={"src/pilot/solvers/tsp.py": "def solve(): return 'better'\n"},
        values=[13.876, 13.1],
    )
    # an improved outcome means a candidate was measured, which REQUIRES a
    # snapshot — so refs-empty here proves dropped, not never-created.
    assert outcome.outcome == "improved"
    ws = tmp_path / "state" / "runs" / "tsp-1" / "ws"
    assert _git(ws, "for-each-ref", "refs/dispatch/").strip() == ""


def test_no_improvement_ends_negative_result_and_pushes_nothing(tmp_path, target_repo) -> None:
    outcome, github = run_live(
        tmp_path,
        target_repo,
        edits={"src/pilot/solvers/tsp.py": "def solve(): return 'worse'\n"},
        values=[13.876, 14.5],
    )
    assert outcome.outcome == "no-improvement"
    assert github.prs == []
    branches = _git(target_repo, "branch", "--list")
    assert "feat/auto" not in branches
    record = load_record(tmp_path / "state", "tsp-1")
    assert record.state == "ended"
    assert record.ending == "negative-result"


def test_out_of_scope_edit_aborts_without_pr(tmp_path, target_repo) -> None:
    outcome, github = run_live(
        tmp_path,
        target_repo,
        edits={
            "src/pilot/solvers/tsp.py": "def solve(): ...\n",
            "docs/roadmap.md": "doctored\n",
        },
        values=[13.876, 1.0],
    )
    assert outcome.outcome == "scope-violation"
    assert github.prs == []
    record = load_record(tmp_path / "state", "tsp-1")
    assert record.ending == "aborted"
    assert "roadmap" in record.ending_note


def test_session_error_aborts_cleanly(tmp_path, target_repo) -> None:
    @dataclass
    class DeadHarness:
        def run(self, brief_text, workspace, resume_session_id=None) -> SessionResult:
            return SessionResult(
                stop_reason="spawn-error",
                is_error=True,
                cost_usd=0.0,
                num_turns=0,
                session_id="",
                final_text="",
                transcript_path="",
            )

    github = FakeGitHub()
    _q = list([13.876])
    with _queued_local(_q):
        outcome = live_attempt(
            config=RunConfig(target="org/pilot", benchmark="tsp"),
            run_root=tmp_path / "state",
            run_id="tsp-err",
            harness=DeadHarness(),
            github=github,  # type: ignore[arg-type]
            bot_auth=NoAuth(),  # type: ignore[arg-type]
            now=1_000_000.0,
            created="t",
        )
    assert outcome.outcome == "session-error"
    assert github.prs == []


def test_exhausted_live_attempt_ends_budget_exhausted(tmp_path, target_repo) -> None:
    """The session-budget outcome flows through the ending map on a LIVE
    climb: the record says budget-exhausted with the real cause."""

    @dataclass
    class DryHarness:
        def run(self, brief_text, workspace, resume_session_id=None) -> SessionResult:
            return SessionResult(
                stop_reason="tool_use",
                is_error=True,
                cost_usd=2.0,
                num_turns=120,
                session_id="",
                final_text="",
                transcript_path="",
                error_detail="error_max_turns: Reached maximum number of turns (120)",
            )

    _q = list([13.876])
    with _queued_local(_q):
        outcome = live_attempt(
            config=RunConfig(target="org/pilot", benchmark="tsp"),
            run_root=tmp_path / "state",
            run_id="tsp-dry",
            harness=DryHarness(),
            github=FakeGitHub(),  # type: ignore[arg-type]
            bot_auth=NoAuth(),  # type: ignore[arg-type]
            now=1_000_000.0,
            created="t",
        )
    assert outcome.outcome == "session-budget"
    record = load_record(tmp_path / "state", "tsp-dry")
    assert record.ending == "budget-exhausted"
    assert "maximum number of turns" in record.ending_note


def test_second_run_gets_its_own_branch(tmp_path, target_repo) -> None:
    """A fixed branch name would non-fast-forward on run two."""
    edits = {"src/pilot/solvers/tsp.py": "def solve(): return 1\n"}
    run_live(tmp_path, target_repo, edits=edits, values=[13.876, 13.1], run_id="tsp-a")
    edits2 = {"src/pilot/solvers/tsp.py": "def solve(): return 2\n"}
    outcome, _ = run_live(tmp_path, target_repo, edits=edits2, values=[13.1, 12.5], run_id="tsp-b")
    assert outcome.outcome == "improved"
    branches = _git(target_repo, "branch", "--list")
    assert "feat/auto/agent-01/tsp-a" in branches
    assert "feat/auto/agent-01/tsp-b" in branches


def test_branch_is_kept_and_recorded_after_pr_failure(tmp_path, target_repo) -> None:
    """create_pull failing does NOT prove no PR exists — the pushed branch is
    left alone (deleting could close a real PR) and recorded for a sweeper."""

    @dataclass
    class FailingGitHub2:
        def create_pull(self, *a, **k) -> str:
            raise RuntimeError("boom")

    _q = [13.876, 13.1]
    with _queued_local(_q):
        live_attempt(
            config=RunConfig(target="org/pilot", benchmark="tsp"),
            run_root=tmp_path / "state",
            run_id="tsp-orphan",
            harness=ScriptedHarness(edits={"src/pilot/solvers/tsp.py": "q=4\n"}),
            github=FailingGitHub2(),  # type: ignore[arg-type]
            bot_auth=NoAuth(),  # type: ignore[arg-type]
            now=1_000_000.0,
            created="t",
        )
    assert "tsp-orphan" in _git(target_repo, "branch", "--list")
    record = load_record(tmp_path / "state", "tsp-orphan")
    assert "branch left on remote: feat/auto/agent-01/tsp-orphan" in record.ending_note


def _push_contract(tmp_path, target_repo, contract_text: str, name: str) -> None:
    seed = tmp_path / f"contract-{name}"
    _git(tmp_path, "clone", "-q", str(target_repo), str(seed))
    (seed / ".autoresearch.yaml").write_text(contract_text)
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", name)
    _git(seed, "push", "-q", "origin", "main")


def test_beats_baseline_but_not_recorded_best_still_opens_a_pr(tmp_path, target_repo) -> None:
    """We credit beating YOUR baseline (docs/design/research-loop.md): a clean
    win over base_sha opens a PR even when it does not beat the ledger's best.
    The ledger's `best` is unchanged (SOTA tracked, not required), so the finish
    pushes the candidate with no leaderboard commit on top."""
    import json as _json

    # seed a leader whose best (12.0) is better than this run's candidate (13.1)
    seed = tmp_path / "leaderseed"
    _git(tmp_path, "clone", "-q", str(target_repo), str(seed))
    (seed / "results").mkdir(exist_ok=True)
    (seed / "results" / "leader.json").write_text(
        _json.dumps(
            {
                "tsp": {
                    "benchmark": "tsp",
                    "metric": "mean_tour_length",
                    "direction": "min",
                    "baseline": 13.876,
                    "best": 12.0,
                    "best_run": "r0",
                    "updated": "d",
                }
            }
        )
    )
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "leader")
    _git(seed, "push", "-q", "origin", "main")

    outcome, github = run_live(
        tmp_path,
        target_repo,
        edits={"src/pilot/solvers/tsp.py": "w=9\n"},
        values=[13.876, 13.1],  # beats own baseline, worse than recorded best 12.0
        run_id="tsp-composable",
    )
    assert outcome.outcome == "improved"  # a composable win, not a rejection
    assert github.prs and github.prs[0]["head"] == "feat/auto/agent-01/tsp-composable"


def test_seeded_climb_records_the_seed_in_the_ledger(tmp_path, target_repo) -> None:
    """The ledger row carries the seed the best was measured under: the
    number becomes re-derivable instead of pool luck."""
    import json as _json

    _push_contract(
        tmp_path,
        target_repo,
        CONTRACT.replace(
            "    direction: min\n", "    direction: min\n    seed_env: PILOT_TSP_SEED\n", 1
        ),
        "seeded",
    )
    outcome, _github = run_live(
        tmp_path,
        target_repo,
        edits={"src/pilot/solvers/tsp.py": "w=8\n"},
        values=[13.876, 13.1, 13.876, 13.1],  # climb pair + freshness pair
        run_id="tsp-seeded",
    )
    assert outcome.outcome == "improved"
    leader = _json.loads(
        _git(target_repo, "show", "feat/auto/agent-01/tsp-seeded:results/leader.json")
    )
    assert leader["tsp"]["run_seed"] > 0


def test_climb_error_still_writes_a_report(tmp_path, target_repo) -> None:
    github = FakeGitHub()
    _q = list([1.0])
    with _queued_local(_q):
        outcome = live_attempt(
            config=RunConfig(target="org/pilot", benchmark="chess"),
            run_root=tmp_path / "state",
            run_id="chess-2",
            harness=ScriptedHarness(edits={}),
            github=github,  # type: ignore[arg-type]
            bot_auth=NoAuth(),  # type: ignore[arg-type]
            now=1_000_000.0,
            created="t",
        )
    assert outcome.outcome == "attempt-error"
    assert Path(outcome.report_path).read_text().startswith("# Run report")


def test_title_pair_never_renders_identical() -> None:
    from autoresearch.attempt import _title_pair

    assert _title_pair(13.875696168157484, 10.844662077277105) == "13.88 -> 10.84"
    assert _title_pair(10.00001, 10.00002) == "10.00001 -> 10.00002"
    assert " -> " in _title_pair(1e-7, 2e-7)


def test_issue_run_references_issue_and_reports_back(tmp_path, target_repo) -> None:
    """The requested lane's visible loop: claim → Addresses #N → report."""
    github = CommentingGitHub()
    _q = list([13.876, 13.1])
    with _queued_local(_q):
        outcome = live_attempt(
            config=RunConfig(target="org/pilot", benchmark="tsp"),
            run_root=tmp_path / "state",
            run_id="tsp-iss",
            harness=ScriptedHarness(edits={"src/pilot/solvers/tsp.py": "i=1\n"}),
            github=github,  # type: ignore[arg-type]
            bot_auth=NoAuth(),  # type: ignore[arg-type]
            now=1_000_000.0,
            created="2026-08-07T00:00:00Z",
            issue_number=42,
            task_hypothesis="A maintainer asked: make tsp faster (fenced text here)",
        )
    assert outcome.outcome == "improved"
    assert "Addresses #42." in github.prs[0]["body"]
    claim, report = github.issue_comments[0], github.issue_comments[-1]
    assert claim[0] == 42 and "autoresearch:claimed" in claim[1]
    assert report[0] == 42 and "finished (improved)" in report[1]
    assert "pull/1" in report[1]
    record = load_record(tmp_path / "state", "tsp-iss")
    assert record.issue_number == 42


def test_clone_crash_ends_record_and_reports_to_issue(tmp_path, monkeypatch) -> None:
    """A crash BEFORE the contained call (clone/contract/claim) must end the
    record and surface on the issue — not strand `implementing`."""

    def exploding_clone(url, dest, auth=None, dry_run=False):
        raise OSError(122, "Disk quota exceeded")

    monkeypatch.setattr(climb_mod.Workspace, "clone", staticmethod(exploding_clone))
    github = CommentingGitHub()
    _q: list = []
    with _queued_local(_q):
        outcome = live_attempt(
            config=RunConfig(target="org/pilot", benchmark="tsp"),
            run_root=tmp_path / "state",
            run_id="tsp-clonefail",
            harness=ScriptedHarness(edits={}),
            github=github,  # type: ignore[arg-type]
            bot_auth=NoAuth(),  # type: ignore[arg-type]
            now=1_000_000.0,
            created="t",
            issue_number=7,
        )
    assert outcome.outcome == "attempt-error"
    record = load_record(tmp_path / "state", "tsp-clonefail")
    assert record.state == "ended" and record.ending == "aborted"
    assert "quota" in record.ending_note
    assert any("attempt-error" in body for _, body in github.issue_comments)
    # exception DETAIL stays local: redact() only knows the secrets tuple,
    # so raw messages (paths, embedded tokens) never reach the public issue
    assert not any("quota" in body for _, body in github.issue_comments)


def _save_failing_after_first(monkeypatch):
    """save_record succeeds once (the implementing record) then raises — the
    quota-crisis failure mode where the ENDING write is what dies."""

    real_save = climb_mod.save_record
    calls = {"n": 0}

    def failing(root, record, now):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError(122, "Disk quota exceeded")
        real_save(root, record, now)

    monkeypatch.setattr(climb_mod, "save_record", failing)


def test_ending_steps_degrade_independently(tmp_path, target_repo, monkeypatch) -> None:
    """A full disk must not block the GitHub failure report (the 2026-08-07
    stranding: the ending record raised EDQUOT inside the handler and took
    the report and issue post down with it)."""
    _save_failing_after_first(monkeypatch)
    github = CommentingGitHub()
    _q = list([1.0])
    with _queued_local(_q):
        outcome = live_attempt(
            config=RunConfig(target="org/pilot", benchmark="chess"),  # not in contract
            run_root=tmp_path / "state",
            run_id="chess-disk",
            harness=ScriptedHarness(edits={}),
            github=github,  # type: ignore[arg-type]
            bot_auth=NoAuth(),  # type: ignore[arg-type]
            now=1_000_000.0,
            created="t",
            issue_number=7,
        )
    assert outcome.outcome == "attempt-error"  # returned, never raised
    # the record could not be ended (disk dead) — but the failure is VISIBLE:
    assert any("attempt-error" in body for _, body in github.issue_comments)
    assert Path(outcome.report_path).exists()
    assert not any("not in contract" in body for _, body in github.issue_comments)


def test_final_record_failure_does_not_lose_pr_or_issue_report(
    tmp_path, target_repo, monkeypatch
) -> None:
    """An improvement whose FINAL record save dies must still open the PR and
    report back to the issue."""
    _save_failing_after_first(monkeypatch)
    github = CommentingGitHub()
    _q = list([13.876, 13.1])
    with _queued_local(_q):
        outcome = live_attempt(
            config=RunConfig(target="org/pilot", benchmark="tsp"),
            run_root=tmp_path / "state",
            run_id="tsp-finaldisk",
            harness=ScriptedHarness(edits={"src/pilot/solvers/tsp.py": "z=1\n"}),
            github=github,  # type: ignore[arg-type]
            bot_auth=NoAuth(),  # type: ignore[arg-type]
            now=1_000_000.0,
            created="2026-08-07T00:00:00Z",
            issue_number=9,
        )
    assert outcome.outcome == "improved"
    assert github.prs and outcome.pr_url.endswith("/pull/1")
    assert any("finished (improved)" in body for _, body in github.issue_comments)
    # the un-saveable record means follow-up servicing is blind to this PR —
    # the warning lands where the humans are looking
    assert any(
        num == 1 and "follow-up servicing is offline" in body for num, body in github.issue_comments
    )


def test_first_record_write_failure_is_contained(tmp_path, target_repo, monkeypatch) -> None:
    """If not even the initial record can be written, the run must not
    proceed invisibly OR crash the caller: attempt-error plus an issue post."""

    def always_failing(root, record, now):
        raise OSError(122, "Disk quota exceeded")

    monkeypatch.setattr(climb_mod, "save_record", always_failing)
    github = CommentingGitHub()
    _q: list = []
    with _queued_local(_q):
        outcome = live_attempt(
            config=RunConfig(target="org/pilot", benchmark="tsp"),
            run_root=tmp_path / "state",
            run_id="tsp-recfail",
            harness=ScriptedHarness(edits={}),
            github=github,  # type: ignore[arg-type]
            bot_auth=NoAuth(),  # type: ignore[arg-type]
            now=1_000_000.0,
            created="t",
            issue_number=7,
        )
    assert outcome.outcome == "attempt-error"
    assert outcome.report_path == ""  # never point at a report that was not written
    assert any("could not start" in body for _, body in github.issue_comments)


def test_improvement_arms_auto_merge(tmp_path, target_repo) -> None:
    """Publish hands the merge to the human approval: auto-merge is armed on
    the fresh PR, so approving is the last human action needed."""
    outcome, github = run_live(
        tmp_path,
        target_repo,
        edits={"src/pilot/solvers/tsp.py": "am=1\n"},
        values=[13.876, 13.1],
        run_id="tsp-arm",
    )
    assert outcome.outcome == "improved"
    assert github.armed == [("org/pilot", 1)]


def test_arming_failure_never_fails_the_publish(tmp_path, target_repo) -> None:
    """Repos without auto-merge enabled refuse the mutation; the PR must
    survive that (arming is convenience, not correctness)."""
    github = FakeGitHub(arming_error="auto merge is not allowed")
    _q = list([13.876, 13.1])
    with _queued_local(_q):
        outcome = live_attempt(
            config=RunConfig(target="org/pilot", benchmark="tsp"),
            run_root=tmp_path / "state",
            run_id="tsp-noarm",
            harness=ScriptedHarness(edits={"src/pilot/solvers/tsp.py": "na=2\n"}),
            github=github,  # type: ignore[arg-type]
            bot_auth=NoAuth(),  # type: ignore[arg-type]
            now=1_000_000.0,
            created="t",
        )
    assert outcome.outcome == "improved"
    assert outcome.pr_url.endswith("/pull/1")
    assert github.armed == []


def test_moved_base_publishes_the_sealed_candidate_without_merging(tmp_path, target_repo) -> None:
    # main moves during the climb: the SEALED candidate publishes as-is — no
    # orchestrator merge, no re-measure (the wake path's long-standing stance,
    # now the one publish). A stale PR is review's to handle.
    class MovingHarness(ScriptedHarness):
        def run(self, brief_text, workspace, resume_session_id=None):
            _push_upstream(target_repo, tmp_path, "docs/roadmap.md", "moved\n", "mid-climb")
            return super().run(brief_text, workspace, resume_session_id)

    _q = [13.876, 13.1]
    github = FakeGitHub()
    with _queued_local(_q):
        outcome = live_attempt(
            config=RunConfig(target="org/pilot", benchmark="tsp"),
            run_root=tmp_path / "state",
            run_id="tsp-moved",
            harness=MovingHarness(edits={"src/pilot/solvers/tsp.py": "m=1\n"}),
            github=github,  # type: ignore[arg-type]
            bot_auth=NoAuth(),  # type: ignore[arg-type]
            now=1_000_000.0,
            created="t",
        )
    assert outcome.outcome == "improved"
    branch = "feat/auto/agent-01/tsp-moved"
    # no merge commit — the branch parents on the CLONED base, not the moved tip
    assert _git(target_repo, "log", "--merges", "--oneline", branch).strip() == ""
    # the upstream commit is NOT folded into the branch
    assert _git(target_repo, "show", f"{branch}:docs/roadmap.md") != "moved\n"


def test_inline_publish_ships_the_sealed_sha_not_the_live_workspace(tmp_path, target_repo) -> None:
    # THE unification's core pin: after the seal, the workspace diverges (an
    # untracked file appears AND a tracked file is rewritten — eval cruft, a
    # stray write, anything). The pushed branch must be exactly the SEALED
    # candidate plus the ledger commit — the divergence never ships.
    class DivergingCompute(QueueCompute):
        def __init__(self, values, ws_root):
            super().__init__(values=values)
            self.ws_root = ws_root

        def submit(self, spec) -> str:
            # runs AFTER snapshot(): the gate's eval jobs are submitted on the
            # sealed sha, so this write postdates the seal
            (self.ws_root / "post-seal-cruft.tmp").write_text("junk\n")
            (self.ws_root / "src" / "pilot" / "solvers" / "tsp.py").write_text(
                "def solve(): return 'REWRITTEN AFTER SEAL'\n"
            )
            return super().submit(spec)

    ws_root = tmp_path / "state" / "runs" / "tsp-seal" / "ws"
    _q = [13.876, 13.1]
    github = FakeGitHub()
    import autoresearch.attempt as _climb_mod

    orig = _climb_mod.LocalCompute
    _climb_mod.LocalCompute = lambda: DivergingCompute(_q, ws_root)  # type: ignore[assignment,misc]
    try:
        outcome = live_attempt(
            config=RunConfig(target="org/pilot", benchmark="tsp"),
            run_root=tmp_path / "state",
            run_id="tsp-seal",
            harness=ScriptedHarness(edits={"src/pilot/solvers/tsp.py": "def solve(): return 7\n"}),
            github=github,  # type: ignore[arg-type]
            bot_auth=NoAuth(),  # type: ignore[arg-type]
            now=1_000_000.0,
            created="t",
        )
    finally:
        _climb_mod.LocalCompute = orig  # type: ignore[misc]
    assert outcome.outcome == "improved"
    branch = "feat/auto/agent-01/tsp-seal"
    files = set(_git(target_repo, "diff", "--name-only", "main", branch).split())
    assert files == {"src/pilot/solvers/tsp.py", "BENCHMARKS.md", "results/leader.json"}
    # the tracked file ships at its SEALED content, not the post-seal rewrite
    assert "return 7" in _git(target_repo, "show", f"{branch}:src/pilot/solvers/tsp.py")


def test_zero_change_improvement_is_a_negative_result_not_a_pr(tmp_path, target_repo) -> None:
    # the gate reporting improved with an EMPTY sealed diff is metric noise:
    # the run ends no-improvement (negative-result), nothing is pushed
    outcome, github = run_live(
        tmp_path,
        target_repo,
        edits={},  # the session changes nothing
        values=[13.876, 13.1],
    )
    assert outcome.outcome == "no-improvement"
    assert github.prs == []
    assert "feat/auto" not in _git(target_repo, "branch", "--list")
    record = load_record(tmp_path / "state", "tsp-1")
    assert record.ending == "negative-result"


def test_non_default_base_branch_is_built_on_and_measured(tmp_path, target_repo) -> None:
    # --base-branch=dev: the session edits, the gate measures, and the PR
    # branch parents on DEV's tree — never the clone's default checkout
    side = tmp_path / "side-dev"
    _git(tmp_path, "clone", "-q", str(target_repo), str(side))
    _git(side, "checkout", "-q", "-b", "dev")
    (side / "ONLY_ON_DEV.md").write_text("dev base\n")
    _git(side, "-c", "user.name=u", "-c", "user.email=u@u", "add", "-A")
    _git(side, "-c", "user.name=u", "-c", "user.email=u@u", "commit", "-qm", "dev base")
    _git(side, "push", "-q", "origin", "dev")
    _q = [13.876, 13.1]
    github = FakeGitHub()
    with _queued_local(_q):
        outcome = live_attempt(
            config=RunConfig(target="org/pilot", benchmark="tsp"),
            run_root=tmp_path / "state",
            run_id="tsp-dev",
            harness=ScriptedHarness(edits={"src/pilot/solvers/tsp.py": "d=1\n"}),
            github=github,  # type: ignore[arg-type]
            bot_auth=NoAuth(),  # type: ignore[arg-type]
            now=1_000_000.0,
            created="t",
            base_branch="dev",
        )
    assert outcome.outcome == "improved"
    assert github.prs[0]["base"] == "dev"
    branch = "feat/auto/agent-01/tsp-dev"
    # the pushed branch carries dev's tree (the dev-only file), i.e. it was
    # built on and sealed against the requested base — not the default branch
    files = _git(target_repo, "ls-tree", "-r", "--name-only", branch)
    assert "ONLY_ON_DEV.md" in files


def _push_upstream(target_repo, tmp_path, rel_path: str, content: str, name: str) -> None:
    """Simulate a concurrent merge: land a commit on the origin's main."""
    side = tmp_path / f"side-{name}"
    _git(tmp_path, "clone", "-q", str(target_repo), str(side))
    p = side / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    _git(side, "-c", "user.name=u", "-c", "user.email=u@u", "add", "-A")
    _git(side, "-c", "user.name=u", "-c", "user.email=u@u", "commit", "-qm", f"upstream {name}")
    _git(side, "push", "-q", "origin", "main")


def test_terminated_is_contained_like_any_crash(tmp_path, target_repo) -> None:
    """A SIGTERM surfaced as Terminated mid-session must end the run through
    the ordinary containment: record aborted, report written."""
    from autoresearch.attempt import Terminated

    @dataclass
    class KilledHarness(ScriptedHarness):
        def run(self, brief_text, workspace, resume_session_id=None):
            raise Terminated("SIGTERM from Slurm (walltime, preemption, or scancel)")

    github = CommentingGitHub()
    _q = list([13.876])
    with _queued_local(_q):
        outcome = live_attempt(
            config=RunConfig(target="org/pilot", benchmark="tsp"),
            run_root=tmp_path / "state",
            run_id="tsp-term",
            harness=KilledHarness(edits={}),
            github=github,  # type: ignore[arg-type]
            bot_auth=NoAuth(),  # type: ignore[arg-type]
            now=1_000_000.0,
            created="t",
            issue_number=7,
        )
    assert outcome.outcome == "attempt-error"
    record = load_record(tmp_path / "state", "tsp-term")
    assert record.state == "ended" and record.ending == "aborted"
    assert "SIGTERM" in record.ending_note
    assert any("attempt-error" in body for _, body in github.issue_comments)


def test_sigterm_containment_is_one_shot() -> None:
    """First SIGTERM raises Terminated; a second must NOT interrupt the
    containment the first one started."""
    import os
    import signal

    import pytest

    from autoresearch.attempt import Terminated, arm_sigterm_containment

    original = signal.getsignal(signal.SIGTERM)
    try:
        arm_sigterm_containment()
        with pytest.raises(Terminated):
            os.kill(os.getpid(), signal.SIGTERM)
        os.kill(os.getpid(), signal.SIGTERM)  # disarmed: must not raise
    finally:
        signal.signal(signal.SIGTERM, original)


def test_run_job_id_is_stamped_from_slurm_env(tmp_path, target_repo, monkeypatch) -> None:
    """The sweep's entire kill-detection keys on this field: the record must
    carry the climb's own SLURM_JOB_ID."""
    monkeypatch.setenv("SLURM_JOB_ID", "4242")
    run_live(
        tmp_path,
        target_repo,
        edits={"src/pilot/solvers/tsp.py": "jid=1\n"},
        values=[13.876, 13.1],
        run_id="tsp-jid",
    )
    assert load_record(tmp_path / "state", "tsp-jid").run_job_id == "4242"


def test_self_deadline_arms_before_the_walltime(monkeypatch) -> None:
    """The alarm fires margin seconds before the job's walltime — our only
    pre-kill warning on clusters that never signal our process. Hermetic:
    inside a real allocation SLURM_JOB_START_TIME would change the math."""
    import signal

    from autoresearch.attempt import arm_self_deadline

    monkeypatch.delenv("SLURM_JOB_START_TIME", raising=False)
    original = signal.getsignal(signal.SIGALRM)
    try:
        armed = arm_self_deadline(90, margin_s=120.0)
        assert armed == 90 * 60 - 120
        assert signal.alarm(0) > 0  # a real alarm was pending
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, original)


def test_self_deadline_anchors_on_slurm_job_start(monkeypatch) -> None:
    """With SLURM_JOB_START_TIME set, startup latency erodes the runway,
    never the margin: a job already 10 minutes in arms 10 minutes less."""
    import signal
    import time

    from autoresearch.attempt import arm_self_deadline

    monkeypatch.setenv("SLURM_JOB_START_TIME", str(int(time.time()) - 600))
    original = signal.getsignal(signal.SIGALRM)
    try:
        armed = arm_self_deadline(90, margin_s=120.0)
        assert abs(armed - (90 * 60 - 600 - 120)) <= 2
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, original)


def test_self_deadline_margin_floor_and_off_switch(monkeypatch) -> None:
    import signal

    from autoresearch.attempt import MIN_ARM_S, arm_self_deadline

    monkeypatch.delenv("SLURM_JOB_START_TIME", raising=False)
    original = signal.getsignal(signal.SIGALRM)
    try:
        assert arm_self_deadline(0) == 0  # off
        assert arm_self_deadline(1, margin_s=1.0) == 0  # walltime <= floored margin
        assert arm_self_deadline(3, margin_s=1.0) == 0  # 120s runway < 180s floor
        armed = arm_self_deadline(10, margin_s=1.0)
        assert armed == 10 * 60 - 60 and armed >= MIN_ARM_S  # margin floor 60
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, original)


def test_self_deadline_raises_terminated_into_containment(monkeypatch) -> None:
    import signal

    import pytest

    from autoresearch.attempt import Terminated, arm_self_deadline

    monkeypatch.delenv("SLURM_JOB_START_TIME", raising=False)
    original = signal.getsignal(signal.SIGALRM)
    try:
        arm_self_deadline(90)
        handler = signal.getsignal(signal.SIGALRM)
        assert callable(handler)
        with pytest.raises(Terminated, match="self-deadline"):
            handler(signal.SIGALRM, None)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, original)


def test_author_harness_is_built_from_the_spec() -> None:
    """The spec is the single source for the session budget: manifest and
    harness cannot disagree (one build_harness for every role)."""
    from autoresearch.harness import ClaudeCodeHarness
    from autoresearch.role_runner import build_harness
    from autoresearch.roles import author_spec

    spec = author_spec(max_turns=7, walltime_s=120)
    harness = build_harness("sk-key", spec, container_image="img.sif")
    assert isinstance(harness, ClaudeCodeHarness)  # default backend; narrows the type
    assert harness.max_turns == 7
    assert harness.timeout_s == 120
    assert harness.container_image == "img.sif"
    assert harness.bare is False  # an editor keeps the target repo's guidance


def test_editor_harness_codex_backend_is_contained() -> None:
    """The codex author backend is one branch of the ONE builder: apptainer is
    the boundary (--sandbox danger-full-access; no bwrap), containment passed
    through from the deployment."""
    from autoresearch.harness import CodexHarness
    from autoresearch.role_runner import build_harness
    from autoresearch.roles import author_spec

    spec = author_spec(max_turns=9, walltime_s=300)
    harness = build_harness(
        "sk-o",
        spec,
        backend="codex",
        model="gpt-5.6-terra",
        container_image="img.sif",
        codex_extra_args=("-c", "use_legacy_landlock=true"),
    )
    assert isinstance(harness, CodexHarness)
    assert harness.sandbox == "danger-full-access"  # apptainer is the boundary; no bwrap
    assert harness.container_image == "img.sif"
    assert harness.model == "gpt-5.6-terra"
    assert harness.timeout_s == 300
    # host codex config, then the web (the author spec grants WebSearch)
    assert harness.extra_args == (
        "-c",
        "use_legacy_landlock=true",
        "-c",
        "tools.web_search=true",
    )
    assert harness.supports_resume is True  # codex exec resume, validated on 0.130.0
    with pytest.raises(ValueError, match="unknown backend"):
        build_harness("sk-o", spec, backend="bogus", container_image="img.sif")


def _panel_judge(texts):
    """A scripted judge for the real run_panel path: commits its verdict
    through the syscall channel, exactly as the tool does."""
    import json as json_mod
    from dataclasses import dataclass, field

    @dataclass
    class _J:
        queue: list = field(default_factory=lambda: list(texts))
        seen_ws: list = field(default_factory=list)

        def run(self, brief_text, workspace, resume_session_id=None) -> SessionResult:
            self.seen_ws.append(Path(workspace))
            text = self.queue.pop(0)
            # commit the verdict through the syscall channel, as the tool would
            payload = None
            try:
                payload = json_mod.loads(text)
            except (json_mod.JSONDecodeError, TypeError):
                payload = None  # a judge that never concluded: no verdict written
            if isinstance(payload, dict):
                for f in payload.get("findings", []):
                    if isinstance(f, dict):
                        f.setdefault("kind", "note")  # the tool's own default
                d = Path(workspace) / ".autoresearch"
                d.mkdir(exist_ok=True)
                (d / "syscall.json").write_text(json_mod.dumps({"type": "verdict", **payload}))
            return SessionResult(
                stop_reason="end_turn",
                is_error=False,
                cost_usd=0.0,
                num_turns=1,
                session_id="judge",
                final_text=text,
                transcript_path="",
            )

    return _J()


def test_panel_clean_read_lands_a_normal_pr_with_transcript(tmp_path, target_repo) -> None:
    import json as _json

    from autoresearch.panel import PanelLens

    judge = _panel_judge([_json.dumps({"findings": [], "notes": "clean"})])
    github = FakeGitHub()
    _q = list([13.876, 13.1])
    with _queued_local(_q):
        outcome = live_attempt(
            config=RunConfig(target="org/pilot", benchmark="tsp"),
            run_root=tmp_path / "state",
            run_id="tsp-panel-ok",
            harness=ScriptedHarness(edits={"src/pilot/solvers/tsp.py": "p=1\n"}),
            github=github,  # type: ignore[arg-type]
            bot_auth=NoAuth(),  # type: ignore[arg-type]
            now=1_000_000.0,
            created="2026-08-15T00:00:00Z",
            panel_lenses=(PanelLens("review", judge),),
        )
    assert outcome.outcome == "improved"
    pr = github.prs[0]
    assert pr["draft"] is False
    assert "## Pre-PR verification" in pr["body"]
    assert "0 blocking" in pr["body"]
    # the judge read a SANITIZED candidate worktree, not the live workspace
    assert judge.seen_ws[0].name == "pr-head"
    # panel worktrees cleaned up
    leftovers = [p.name for p in (tmp_path / "state" / "runs" / "tsp-panel-ok").iterdir()]
    assert "panel" not in leftovers
    assert github.armed  # clean panel: auto-merge arming still runs


def test_panel_capped_blocking_opens_a_draft_and_never_arms(tmp_path, target_repo) -> None:
    import json as _json

    from autoresearch.panel import PanelLens

    blocking = _json.dumps(
        {
            "findings": [
                {
                    "file": "src/pilot/solvers/tsp.py",
                    "line": 1,
                    "confidence": "high",
                    "summary": "suspicious lever",
                    "detail": "looks structural",
                    "blocking": True,
                }
            ],
            "notes": "",
        }
    )
    judge = _panel_judge([blocking, blocking])
    github = FakeGitHub()
    _q = list([13.876, 13.1, 13.05])
    with _queued_local(_q):
        outcome = live_attempt(
            config=RunConfig(target="org/pilot", benchmark="tsp"),
            run_root=tmp_path / "state",
            run_id="tsp-panel-draft",
            harness=ScriptedHarness(edits={"src/pilot/solvers/tsp.py": "p=2\n"}),
            github=github,  # type: ignore[arg-type]
            bot_auth=NoAuth(),  # type: ignore[arg-type]
            now=1_000_000.0,
            created="2026-08-15T00:00:00Z",
            panel_lenses=(PanelLens("review", judge),),
        )
    assert outcome.outcome == "improved"
    pr = github.prs[0]
    assert pr["draft"] is True
    assert pr["body"].startswith("> **Draft")
    assert "suspicious lever" in pr["body"]
    assert github.armed == []  # a draft with open blocking findings never arms


def test_expensive_benchmark_runs_session_then_parks_candidate(tmp_path, target_repo_dispatch):
    # eval_minutes=30 is past the in-job runway. There is NO pre-session baseline
    # park: the session runs, then the gate dispatches baseline+candidate
    # together and the climb parks the CANDIDATE.
    outcome, github = run_live(
        tmp_path,
        target_repo_dispatch,
        edits={"src/pilot/solvers/tsp.py": "def solve(): return 'better'\n"},
        values=[],  # the inline evaluator is never called on the dispatched path
        dispatch=_fake_dispatch(),
    )
    assert outcome.outcome == "parked"
    assert github.prs == []  # not decided yet; no PR
    record = load_record(tmp_path / "state", "tsp-1")
    assert record.state == "waiting"
    # the only park is the candidate; baseline+candidate dispatched together, so
    # the afterany carries BOTH jobs and the run rides the multi-job deadline
    assert record.stage["phase"] == "candidate"
    assert record.stage["candidate_sha"]  # a real snapshot was taken
    assert record.stage["afterany"] == "afterany:1000:1001"
    assert record.experiment_job_id == ""  # multi-job park: rides the deadline floor
    # the session ran and its write-up was saved for the wake
    assert record.stage["report"]


def test_cheap_benchmark_ignores_dispatch_and_measures_inline(tmp_path, target_repo) -> None:
    # dispatch settings are present, but the benchmark has no eval hint, so
    # should_dispatch() is False and the climb measures inline as usual.
    outcome, github = run_live(
        tmp_path,
        target_repo,
        edits={"src/pilot/solvers/tsp.py": "def solve(): return 'better'\n"},
        values=[13.876, 13.1],
        dispatch=_fake_dispatch(),
    )
    assert outcome.outcome == "improved"
    assert github.prs[0]["head"] == "feat/auto/agent-01/tsp-1"


CONTRACT_SYSCALLS = CONTRACT.replace("    direction: min\n", "    direction: min\n    depth_k: 3\n")
CONTRACT_OPTOUT = CONTRACT.replace("    direction: min\n", "    direction: min\n    depth_k: 0\n")


@pytest.fixture
def target_repo_syscalls(tmp_path: Path, monkeypatch) -> Path:
    return _seed_target(tmp_path, monkeypatch, CONTRACT_SYSCALLS)


@pytest.fixture
def target_repo_optout(tmp_path: Path, monkeypatch) -> Path:
    return _seed_target(tmp_path, monkeypatch, CONTRACT_OPTOUT)


def test_syscalls_arm_by_default_with_dispatch_and_resume(tmp_path, target_repo_syscalls) -> None:
    # CONTRACT-DRIVEN enablement: no flag, no env — dispatch coords + a
    # resumable backend + a benchmark that has not opted out arm the feature.
    import json as json_mod

    outcome, _ = run_live(
        tmp_path,
        target_repo_syscalls,
        edits={
            "src/pilot/solvers/tsp.py": "def solve(): return 'probe'\n",
            ".autoresearch/syscall.json": json_mod.dumps(
                {"type": "sleep", "launches": [{"name": "probe", "command": "uv run probe.py"}]}
            ),
        },
        values=[],  # parked before any measurement
        dispatch=_fake_dispatch(),
    )
    assert outcome.outcome == "parked"
    record = load_record(tmp_path / "state", "tsp-1")
    assert record.state == "waiting" and record.stage["phase"] == "author-sleep"


def test_depth_k_zero_opts_the_benchmark_out(tmp_path, target_repo_optout) -> None:
    # `depth_k: 0` is the per-benchmark off switch: even with dispatch coords
    # and a resumable backend, the tool is not installed and a stray request
    # file is staged and judged like any other edit.
    import json as json_mod

    outcome, _ = run_live(
        tmp_path,
        target_repo_optout,
        edits={
            "src/pilot/solvers/tsp.py": "def solve(): return 'better'\n",
            ".autoresearch/syscall.json": json_mod.dumps({"type": "sleep", "launches": []}),
        },
        values=[],  # scope refuses before any measurement
        dispatch=_fake_dispatch(),
    )
    assert outcome.outcome == "scope-violation"  # staged + judged, not honored


def test_author_sleep_live_parks_and_submits_launch_jobs(
    tmp_path, target_repo_syscalls, monkeypatch
) -> None:
    # end-to-end sleep side (research-loop-buildout.md Phase A): the session
    # writes a syscall request and ends; the climb seals the tree, submits the
    # launch as a jailed job, and parks as author-sleep with the request +
    # counts aboard. Cheap benchmark (no eval hint) — launches do not require
    # a dispatched GATE, only dispatch coords.
    import json as json_mod

    outcome, github = run_live(
        tmp_path,
        target_repo_syscalls,
        edits={
            "src/pilot/solvers/tsp.py": "def solve(): return 'probe'\n",
            ".autoresearch/syscall.json": json_mod.dumps(
                {
                    "type": "sleep",
                    "launches": [
                        {
                            "name": "probe",
                            "command": "uv run probe.py",
                            "minutes": 45,
                            "artifacts": ["out/tails.json"],
                        }
                    ],
                    "note": "look at the tails",
                }
            ),
        },
        values=[],  # parked before any measurement
        dispatch=_fake_dispatch(),
    )
    assert outcome.outcome == "parked"
    assert github.prs == []
    record = load_record(tmp_path / "state", "tsp-1")
    assert record.state == "waiting"
    assert record.stage["phase"] == "author-sleep"
    assert record.stage["afterany"] == "afterany:1000"
    assert record.stage["syscall_launches"] == [
        {"name": "probe", "minutes": 45, "artifacts": ["out/tails.json"]}
    ]
    assert record.stage["syscall_note"] == "look at the tails"
    assert record.stage["launches_used"] == 1 and record.stage["sleeps_used"] == 1
    assert record.resume_session_id == "s1"  # the wake resumes the SAME session
    # the agent-facing TOOL + its budget were installed into the channel dir
    import json as _json

    ws = tmp_path / "state" / "runs" / "tsp-1" / "ws"
    assert (ws / ".autoresearch" / "syscall").exists()
    budget = _json.loads((ws / ".autoresearch" / "budget.json").read_text())
    assert budget == {"launches_remaining": 3, "sleeps_remaining": 20}
    # the job is the eval jail on the sealed tree; the author's command travels
    # via command.txt (never shell-interpolated into the script)
    ev = tmp_path / "state" / "runs" / "tsp-1" / "eval-launch-probe"
    assert (ev / "command.txt").read_text() == "uv run probe.py"
    assert "out/tails.json" in (ev / "job.sh").read_text()


def test_symlinked_channel_disables_syscalls_and_never_writes_through_it(
    tmp_path, monkeypatch
) -> None:
    # a target that commits `.autoresearch` as a SYMLINK to a host path must not
    # get the tool/exclude/budget written THROUGH it (terra #133 r1): a
    # pre-existing channel in any form disables the feature for the run.
    target = _seed_target(tmp_path, monkeypatch, CONTRACT_SYSCALLS)
    seed = tmp_path / "seed"
    escape = tmp_path / "ESCAPE"
    escape.mkdir()
    (seed / ".autoresearch").symlink_to(escape, target_is_directory=True)
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "trap")
    _git(seed, "push", "-q", str(tmp_path / "origin.git"), "main")

    outcome, _ = run_live(
        tmp_path,
        target,
        edits={"src/pilot/solvers/tsp.py": "def solve(): return 'better'\n"},
        values=[13.876, 13.1],
        dispatch=_fake_dispatch(),
    )
    assert outcome.outcome == "improved"  # ran normally, feature off
    # nothing was written through the symlink to the escape dir
    assert list(escape.iterdir()) == []


def test_tracked_request_file_disables_syscalls_for_the_run(tmp_path, monkeypatch) -> None:
    # a target repo that COMMITS a valid syscall.json must not consume cluster
    # compute: a request is only honored if the session wrote it, so a
    # pre-session (= tracked, in a fresh clone) file disables the feature for
    # the run and the climb proceeds normally (terra #132 r3).
    import json as json_mod

    target = _seed_target(tmp_path, monkeypatch, CONTRACT_SYSCALLS)
    seed = tmp_path / "seed"
    (seed / ".autoresearch").mkdir()
    (seed / ".autoresearch" / "syscall.json").write_text(
        json_mod.dumps({"launches": [{"name": "steal", "command": "mine coins"}]})
    )
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "trap")
    _git(seed, "push", "-q", str(tmp_path / "origin.git"), "main")

    sbatched: list = []
    dispatch = _fake_dispatch()
    real_submit = dispatch.compute.submit

    def recording_submit(spec):
        sbatched.append(spec)
        return real_submit(spec)

    dispatch.compute.submit = recording_submit

    outcome, _ = run_live(
        tmp_path,
        target,
        edits={"src/pilot/solvers/tsp.py": "def solve(): return 'better'\n"},
        values=[13.876, 13.1],
        dispatch=dispatch,
    )
    # no park, no launch jobs: the tracked request was never honored, and the
    # climb ran to a normal ending
    assert outcome.outcome == "improved"
    assert sbatched == []


def test_configured_image_contains_local_evals_without_cluster_coords(
    tmp_path, target_repo
) -> None:
    # --image with no account/partition: dispatch is None, but the local eval
    # jobs must still run jailed — an incomplete cluster triple must not
    # silently drop the container
    outcome, _ = run_live(
        tmp_path,
        target_repo,
        edits={"src/pilot/solvers/tsp.py": "def solve(): return 9\n"},
        values=[13.876, 13.1],
        eval_image="/img.sif",
    )
    assert outcome.outcome == "improved"
    jobs = list((tmp_path / "state" / "runs" / "tsp-1").glob("eval-*/job.sh"))
    assert jobs and all("/img.sif" in j.read_text() for j in jobs)


def test_syscalls_off_without_dispatch_treats_stray_files_as_edits(tmp_path, target_repo) -> None:
    # with the feature OFF (no dispatch coords -> no launcher), the
    # `.autoresearch/` name is NOT magic: an untracked file there is staged
    # and judged like any other agent edit (here: out of scope) (terra #132 r2).
    outcome, _ = run_live(
        tmp_path,
        target_repo,
        edits={
            "src/pilot/solvers/tsp.py": "def solve(): return 'better'\n",
            ".autoresearch/junk.txt": "leftover",
        },
        values=[],  # scope refuses before any measurement
    )
    assert outcome.outcome == "scope-violation"


def test_armed_syscalls_exclude_the_channel_from_the_candidate(tmp_path, target_repo) -> None:
    # with the feature ON (dispatch coords present), the channel dir is
    # invisible to diffs/scope/drift: a stray non-request file there neither
    # blocks nor ships.
    outcome, _ = run_live(
        tmp_path,
        target_repo,
        edits={
            "src/pilot/solvers/tsp.py": "def solve(): return 'better'\n",
            ".autoresearch/notes.txt": "scratch",
        },
        values=[13.876, 13.1],
        dispatch=_fake_dispatch(),
    )
    assert outcome.outcome == "improved"


def test_author_sleep_partial_submit_failure_cancels_earlier_jobs(
    tmp_path, target_repo_syscalls, monkeypatch
) -> None:
    # one launch submits, the next sbatch fails: the already-submitted job must
    # be reaped (no park record exists to ever wake or cancel it) and the run
    # ends as a loud error (terra #132 r1).
    import json as json_mod

    from autoresearch.compute import CommandResult, SlurmCompute
    from autoresearch.measure import DispatchSettings

    cancelled: list[str] = []
    calls = {"sbatch": 0}

    def runner(argv, timeout_s):
        if argv[0] == "sbatch":
            calls["sbatch"] += 1
            if calls["sbatch"] > 1:
                return CommandResult(1, "", "QOSMaxSubmitJobPerUserLimit")
            return CommandResult(0, "1000\n", "")
        if argv[0] == "scancel":
            cancelled.append(argv[1])
            return CommandResult(0, "", "")
        return CommandResult(0, "", "")

    dispatch = DispatchSettings(
        compute=SlurmCompute(runner=runner), image="/img.sif", account="acct", partition="cpu"
    )
    outcome, _ = run_live(
        tmp_path,
        target_repo_syscalls,
        edits={
            ".autoresearch/syscall.json": json_mod.dumps(
                {
                    "type": "sleep",
                    "launches": [
                        {"name": "a", "command": "run a"},
                        {"name": "b", "command": "run b"},
                    ],
                }
            ),
        },
        values=[],
        dispatch=dispatch,
    )
    assert outcome.outcome == "attempt-error"
    assert cancelled == ["1000"]  # the successful submit was reaped, not orphaned
    record = load_record(tmp_path / "state", "tsp-1")
    assert record.state == "ended"


def test_failed_park_write_cancels_orphaned_eval_jobs(
    tmp_path, target_repo_dispatch, monkeypatch
) -> None:
    # the candidate park dispatched baseline+candidate (two jobs); if the
    # WAITING record then fails to write, nothing will ever wake those jobs, so
    # BOTH must be cancelled rather than left orphaned in the queue.

    cancelled: list[str] = []
    dispatch = _fake_dispatch(cancelled=cancelled)

    def boom(*args, **kwargs):
        raise OSError("state dir full")

    monkeypatch.setattr(climb_mod, "_park_run", boom)
    outcome, _ = run_live(
        tmp_path,
        target_repo_dispatch,
        edits={"src/pilot/solvers/tsp.py": "def solve(): return 'better'\n"},
        values=[],
        dispatch=dispatch,
    )
    assert outcome.outcome == "attempt-error"  # the failed park ends the run
    assert cancelled == ["1000", "1001"]  # both dispatched jobs were cancelled


def test_expensive_benchmark_without_coords_falls_back_inline(
    tmp_path, target_repo_dispatch, caplog
) -> None:
    # eval_minutes=30 wants dispatch, but no cluster coordinates reached us
    # (dispatch=None) — measure inline rather than fail, and say so.
    import logging

    with caplog.at_level(logging.WARNING):
        outcome, github = run_live(
            tmp_path,
            target_repo_dispatch,
            edits={"src/pilot/solvers/tsp.py": "def solve(): return 'better'\n"},
            values=[13.876, 13.1],
            dispatch=None,
        )
    assert outcome.outcome == "improved"  # inline fallback measured it
    assert github.prs[0]["head"] == "feat/auto/agent-01/tsp-1"
    assert "wants dispatched eval" in caplog.text


# --- resume_run: waking a parked candidate (slice 1b: re-park + negative end) ---


class _FakeMeasurer:
    """A measurer for the wake path: returns canned values by measure name, or
    raises (e.g. MeasurementPending to force a re-park)."""

    def __init__(self, values=None, raise_exc=None):
        self.values = values or {}
        self.raise_exc = raise_exc

    def results(self, measures):
        if self.raise_exc is not None:
            raise self.raise_exc
        return {m.name: self.values[m.name] for m in measures}


def _write_parked_candidate(
    tmp_path,
    monkeypatch,
    *,
    values=None,
    raise_exc=None,
    run_id="tsp-1",
    base_branch="main",
    issue_number=0,
    contract=None,
    agent_id="",
):
    """A candidate-parked run on disk in the REAL park state: HEAD is still the
    pre-session commit, the session's edits are UNCOMMITTED in the working tree,
    there is untracked cruft an eval left behind, and `candidate_sha` is a
    snapshot commit (via `snapshot_tree`, off any branch) kept alive by its ref.
    Plus a WAITING record with the re-entry stage. The dispatched measurer is
    monkeypatched to a fake so no cluster is touched."""
    from autoresearch.dispatch import snapshot_tree
    from autoresearch.github import Workspace
    from autoresearch.measure import DispatchSettings

    state = tmp_path / "state"
    wsroot = state / "runs" / run_id / "ws"
    (wsroot / "src" / "pilot" / "solvers").mkdir(parents=True)
    (wsroot / ".autoresearch.yaml").write_text(contract or CONTRACT_DISPATCH)
    (wsroot / "src" / "pilot" / "solvers" / "tsp.py").write_text("def solve(): ...\n")
    _git(wsroot, "init", "-q", "-b", "main")
    _git(wsroot, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(wsroot, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    base_sha = _git(wsroot, "rev-parse", "HEAD").strip()
    # the session's edit, left UNCOMMITTED (HEAD stays at base) — the snapshot
    # captures it into candidate_sha.
    (wsroot / "src" / "pilot" / "solvers" / "tsp.py").write_text("def solve(): return 'better'\n")
    ws = Workspace(root=wsroot)
    snap = snapshot_tree(ws, base_sha)  # candidate_sha, retained under its ref
    # cruft that appears AFTER the snapshot (a dispatched eval / session
    # leftover): it is in the wake's working tree but NOT in candidate_sha, so
    # the finish's force-checkout keeps it around and the ledger-only commit
    # must NOT sweep it into the PR.
    (wsroot / "eval-cache.tmp").write_text("junk an eval left behind\n")
    candidate_sha = snap.commit
    # a bare 'origin' (unique per run) so an improved wake can push its branch
    bare = tmp_path / f"origin-{run_id}.git"
    _git(tmp_path, "clone", "-q", "--bare", str(wsroot), str(bare))
    _git(wsroot, "remote", "add", "origin", str(bare))

    if agent_id:
        # a lines run parked here: the local line ref exists from run start
        _git(wsroot, "branch", f"agents/{agent_id}", base_sha)
    record = RunRecord(
        run_id=run_id,
        target="org/pilot",
        task_title="improve tsp",
        benchmark="tsp",
        state="waiting",
        resume_session_id="s1",
        issue_number=issue_number,
        agent_id=agent_id,
        stage={
            "phase": "candidate",
            "base_sha": base_sha,
            "candidate_sha": candidate_sha,
            "candidate_ref": snap.ref,
            "seed": 7,
            "suite_seed": 9,
            "afterany": "afterany:501",
            "report": "swapped the construction heuristic",
            "base_branch": base_branch,
        },
    )
    save_record(state, record, 1_000_000.0)
    fake = _FakeMeasurer(values=values, raise_exc=raise_exc)
    monkeypatch.setattr(DispatchSettings, "measurer", lambda self, *a, **k: fake)
    # the wake pushes to the canonical target URL (never the ws git config);
    # point that at this run's local bare so the improved-wake test can push.
    monkeypatch.setattr("autoresearch.attempt._target_clone_url", lambda target: str(bare))
    return state, run_id


def test_resume_negative_ends_run_and_drops_snapshot(tmp_path, monkeypatch) -> None:
    # candidate did not beat baseline (min direction) -> honest negative: end
    # the record and release the candidate snapshot.
    state, run_id = _write_parked_candidate(
        tmp_path, monkeypatch, values={"baseline": 13.0, "candidate": 13.0}
    )
    outcome = resume_run(
        state,
        run_id,
        dispatch=_fake_dispatch(),
        github=CommentingGitHub(),  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_100.0,
    )
    assert outcome.outcome == "no-improvement"
    record = load_record(state, run_id)
    assert record.state == "ended" and record.ending == "negative-result"
    ws = state / "runs" / run_id / "ws"
    assert _git(ws, "for-each-ref", "refs/dispatch/").strip() == ""  # snapshot dropped


def test_resume_reparks_when_a_measure_is_pending(tmp_path, monkeypatch) -> None:
    # a needed measure isn't done -> re-park on the new afterany, KEEP the
    # candidate snapshot for the next wake.
    from autoresearch.measure import MeasurementPending

    state, run_id = _write_parked_candidate(
        tmp_path, monkeypatch, raise_exc=MeasurementPending(("601", "602"))
    )
    outcome = resume_run(
        state,
        run_id,
        dispatch=_fake_dispatch(),
        github=CommentingGitHub(),  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_100.0,
    )
    assert outcome.outcome == "parked"
    record = load_record(state, run_id)
    assert record.state == "waiting"
    assert record.stage["afterany"] == "afterany:601:602"
    ws = state / "runs" / run_id / "ws"
    assert _git(ws, "for-each-ref", "refs/dispatch/").strip() != ""  # snapshot kept


def test_author_sleep_live_array_launch_submits_one_job_per_index(
    tmp_path, target_repo_syscalls, monkeypatch
) -> None:
    """`array: 2` is one launch that submits two jailed jobs, `<name>.0` and
    `<name>.1`, each told its index through SWEEP_INDEX; the park waits on
    both and the stage carries the width for re-parks and the wake."""
    import json as json_mod

    outcome, _github = run_live(
        tmp_path,
        target_repo_syscalls,
        edits={
            "src/pilot/solvers/tsp.py": "def solve(): return 'probe'\n",
            ".autoresearch/syscall.json": json_mod.dumps(
                {
                    "type": "sleep",
                    "launches": [
                        {"name": "sweep", "command": "uv run probe.py", "minutes": 45, "array": 2}
                    ],
                }
            ),
        },
        values=[],
        dispatch=_fake_dispatch(),
    )
    assert outcome.outcome == "parked"
    record = load_record(tmp_path / "state", "tsp-1")
    assert record.stage["afterany"] == "afterany:1000:1001"
    assert record.stage["syscall_launches"] == [
        {"name": "sweep", "minutes": 45, "artifacts": [], "array": 2}
    ]
    assert record.stage["launches_used"] == 1
    runs = tmp_path / "state" / "runs" / "tsp-1"
    for i in (0, 1):
        ev = runs / f"eval-launch-sweep.{i}"
        assert (ev / "command.txt").read_text() == "uv run probe.py"
        scripts = " ".join(p.read_text() for p in ev.iterdir() if p.is_file())
        assert f"SWEEP_INDEX={i}" in scripts


def test_resume_repark_of_a_submitted_park_keeps_the_submit_context(tmp_path, monkeypatch):
    # terra #144 r3: a submitted candidate whose wake dispatches MORE measures
    # (the suite fans out) re-parks BEFORE any author wake — the new stage must
    # keep the submitted marker, the budget counts, and the sibling-launch
    # descriptors, or the next wake drafts instead of waking the author and
    # the launches' results are never gathered.
    from autoresearch.measure import MeasurementPending

    state, run_id = _write_parked_candidate(
        tmp_path, monkeypatch, raise_exc=MeasurementPending(("601", "602"))
    )
    rec = load_record(state, run_id)
    rec.stage["submitted"] = True
    rec.stage["syscall_launches"] = [
        {"name": "probe", "minutes": 240, "artifacts": ["out.txt"], "array": 3}
    ]
    rec.stage["launches_used"] = 1
    rec.stage["sleeps_used"] = 1
    save_record(state, rec, 1_000_050.0)
    outcome = resume_run(
        state,
        run_id,
        dispatch=_fake_dispatch(),
        github=CommentingGitHub(),  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_100.0,
    )
    assert outcome.outcome == "parked"
    record = load_record(state, run_id)
    assert record.state == "waiting"
    assert record.stage.get("submitted") is True
    assert record.stage["syscall_launches"] == [
        {"name": "probe", "minutes": 240, "artifacts": ["out.txt"], "array": 3}
    ]  # the sweep keeps its width across the re-park (terra #181)
    assert record.stage["launches_used"] == 1 and record.stage["sleeps_used"] == 1
    # the deadline floor still covers the longest sibling launch (240 min) —
    # an undershot floor would let the sweep cancel a healthy queued launch
    assert record.deadline >= 1_000_100.0 + 240 * 60


def test_resume_improved_pushes_and_opens_pr(tmp_path, monkeypatch) -> None:
    # candidate beats baseline -> branch the sealed sha, fold in the ledger,
    # push, open the PR against main; the record goes in-review.
    state, run_id = _write_parked_candidate(
        tmp_path, monkeypatch, values={"baseline": 13.0, "candidate": 12.0}
    )
    github = FakeGitHub()
    outcome = resume_run(
        state,
        run_id,
        dispatch=_fake_dispatch(),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_100.0,
    )
    assert outcome.outcome == "improved"
    assert outcome.pr_url.endswith("/pull/1")
    record = load_record(state, run_id)
    assert record.state == "in-review" and "pull/1" in record.pr_url
    # the PR opened against main from the run's branch, carrying the saved report
    pr = github.prs[0]
    assert pr["head"] == "feat/auto/agent-01/tsp-1" and pr["base"] == "main"
    assert "swapped the construction heuristic" in pr["body"]
    # the branch landed in the bare origin; the candidate snapshot was dropped
    bare = tmp_path / "origin-tsp-1.git"
    assert "feat/auto/agent-01/tsp-1" in _git(bare, "branch", "--list")
    ws = state / "runs" / run_id / "ws"
    assert _git(ws, "for-each-ref", "refs/dispatch/").strip() == ""
    # the pushed tree carries the sealed candidate edit + the two ledger files,
    # and NOT the untracked eval cruft the session left in the workspace
    files = set(_git(bare, "ls-tree", "-r", "--name-only", "feat/auto/agent-01/tsp-1").split())
    assert "src/pilot/solvers/tsp.py" in files
    assert {"BENCHMARKS.md", "results/leader.json"} <= files
    assert "eval-cache.tmp" not in files
    assert "def solve(): return 'better'" in _git(
        bare, "show", "feat/auto/agent-01/tsp-1:src/pilot/solvers/tsp.py"
    )
    # no panel configured here -> a clean improvement arms auto-merge (only
    # where branch protection requires a review), same policy as the inline path
    assert github.armed and github.armed[0][0] == "org/pilot"
    assert github.prs[0]["draft"] is False


def test_resume_blind_repark_keeps_wake_attempts_but_progress_resets(tmp_path, monkeypatch):
    # a no-progress re-park (blind: empty afterany) must KEEP wake_attempts so
    # the stuck cap still bites; a productive re-park (a NEW job set) resets it.
    import dataclasses

    from autoresearch.measure import MeasurementPending

    # blind re-park: MeasurementPending(()) -> empty afterany -> no progress
    state, run_id = _write_parked_candidate(tmp_path, monkeypatch, raise_exc=MeasurementPending(()))
    rec = dataclasses.replace(load_record(state, run_id), wake_attempts=2)
    save_record(state, rec, 1_000_050.0)
    resume_run(
        state,
        run_id,
        dispatch=_fake_dispatch(),
        github=CommentingGitHub(),  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_100.0,
    )
    assert load_record(state, run_id).wake_attempts == 2  # kept, cap still counts

    # productive re-park: a NEW afterany (old was afterany:501) -> progress
    state2, rid2 = _write_parked_candidate(
        tmp_path, monkeypatch, raise_exc=MeasurementPending(("601", "602")), run_id="tsp-2"
    )
    rec2 = dataclasses.replace(load_record(state2, rid2), wake_attempts=2)
    save_record(state2, rec2, 1_000_050.0)
    resume_run(
        state2,
        rid2,
        dispatch=_fake_dispatch(),
        github=CommentingGitHub(),  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_100.0,
    )
    assert load_record(state2, rid2).wake_attempts == 0  # reset on progress


def test_resume_reads_contract_from_base_not_the_dirty_tree(tmp_path, monkeypatch) -> None:
    # a session could rewrite .autoresearch.yaml in the working tree to widen
    # its own scope; the wake must gate on the BASE commit's contract, not the
    # dirty tree. Corrupt the working-tree contract: if the wake read it, it
    # would crash; reading base, it still succeeds.
    state, run_id = _write_parked_candidate(
        tmp_path, monkeypatch, values={"baseline": 13.0, "candidate": 12.0}
    )
    (state / "runs" / run_id / "ws" / ".autoresearch.yaml").write_text("}{ not valid yaml :\n")
    github = FakeGitHub()
    outcome = resume_run(
        state,
        run_id,
        dispatch=_fake_dispatch(),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_100.0,
    )
    assert outcome.outcome == "improved"  # base's contract was used, not the corrupt tree
    assert github.prs and github.prs[0]["base"] == "main"


def test_resume_negative_keeps_snapshot_if_the_record_save_fails(tmp_path, monkeypatch) -> None:
    # a negative wake must save the ENDED record BEFORE dropping the snapshot:
    # if the save fails, the run stays recoverable (snapshot intact), never
    # WAITING with the candidate gone.

    state, run_id = _write_parked_candidate(
        tmp_path, monkeypatch, values={"baseline": 13.0, "candidate": 13.0}
    )
    real_save = climb_mod.save_record

    def failing_save(root, record, now):
        if record.state == "ended":
            raise OSError("disk full")
        return real_save(root, record, now)

    monkeypatch.setattr(climb_mod, "save_record", failing_save)
    resume_run(
        state,
        run_id,
        dispatch=_fake_dispatch(),
        github=CommentingGitHub(),  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_100.0,
    )
    ws = state / "runs" / run_id / "ws"
    assert _git(ws, "for-each-ref", "refs/dispatch/").strip() != ""  # snapshot kept for a re-wake


def test_resume_opens_pr_against_the_runs_base_branch_from_the_stage(tmp_path, monkeypatch) -> None:
    # a run started with --base-branch=dev must, on wake, open its PR against
    # dev — the wake job carries no --base-branch, so the branch rides the stage.
    state, run_id = _write_parked_candidate(
        tmp_path, monkeypatch, values={"baseline": 13.0, "candidate": 12.0}, base_branch="dev"
    )
    github = FakeGitHub()
    outcome = resume_run(
        state,
        run_id,
        dispatch=_fake_dispatch(),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_100.0,
        base_branch="main",  # the CLI default — must be OVERRIDDEN by the stage
    )
    assert outcome.outcome == "improved"
    assert github.prs[0]["base"] == "dev"  # not the CLI default "main"


def test_resume_cli_releases_the_lease_on_exit(tmp_path, monkeypatch) -> None:
    # the wake job holds the run's lease (transferred by the sweep on dispatch);
    # the --resume CLI must release it on every exit so a re-parked run is
    # immediately eligible for the next sweep, not stuck until the TTL reap.
    from autoresearch.attempt import AttemptOutcome, main
    from autoresearch.runstate import acquire_lease, run_dir

    run_id = "tsp-wake"
    (tmp_path / "runs" / run_id).mkdir(parents=True)
    (tmp_path / "pat").write_text("ghp_x\n")
    (tmp_path / "pat").chmod(0o600)  # token() enforces 0600
    (tmp_path / "img.sif").write_text("")  # just needs to be a file
    # deliberately NO --panel and NO author key file: a panel-less wake never
    # revises, so it must not require the author key (regression: the CLI once
    # always read it and failed).
    assert acquire_lease(tmp_path, run_id, "wake-job:1", "1", 1_000.0)
    assert (run_dir(tmp_path, run_id) / "lease.json").exists()
    monkeypatch.setenv("SLURM_JOB_ID", "1")  # this process IS the wake job that holds it

    monkeypatch.setattr(climb_mod, "arm_sigterm_containment", lambda: None)
    monkeypatch.setattr(
        climb_mod,
        "resume_run",
        lambda *a, **k: AttemptOutcome(run_id=run_id, outcome="parked"),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "climb",
            "--resume",
            run_id,
            "--run-root",
            str(tmp_path),
            "--account",
            "acct",
            "--partition",
            "cpu",
            "--image",
            str(tmp_path / "img.sif"),
            "--pat-file",
            str(tmp_path / "pat"),
            "--panel",
            "",  # no panel -> no revision -> no author key needed
        ],
    )
    assert main() == 0
    assert not (run_dir(tmp_path, run_id) / "lease.json").exists()  # released


def test_resume_reports_terminal_back_to_the_requesting_issue(tmp_path, monkeypatch) -> None:
    # an issue-requested dispatched run must post its outcome back on wake, or
    # the issue stays claimed forever. Improved -> comment with the PR link +
    # "Addresses #N" in the PR body.
    state, run_id = _write_parked_candidate(
        tmp_path, monkeypatch, values={"baseline": 13.0, "candidate": 12.0}, issue_number=42
    )
    github = CommentingGitHub()
    outcome = resume_run(
        state,
        run_id,
        dispatch=_fake_dispatch(),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_100.0,
    )
    assert outcome.outcome == "improved"
    assert github.prs[0]["body"].startswith("Addresses #42.")
    assert github.issue_comments  # posted back to the issue
    num, body = github.issue_comments[-1]
    assert num == 42 and "finished (improved)" in body and outcome.pr_url in body
    # improved KEEPS the claim (the PR is the ongoing work) -> no release marker
    from autoresearch.intake import RELEASE_MARKER

    assert RELEASE_MARKER not in body


def test_resume_negative_releases_the_issue_claim(tmp_path, monkeypatch) -> None:
    # a negative run opens no PR, so nothing will ever resolve the issue -> the
    # terminal comment must carry RELEASE_MARKER, or intake keeps it claimed and
    # it can never be re-selected (a comment alone does NOT un-claim).
    from autoresearch.intake import RELEASE_MARKER

    state, run_id = _write_parked_candidate(
        tmp_path, monkeypatch, values={"baseline": 13.0, "candidate": 13.0}, issue_number=7
    )
    github = CommentingGitHub()
    outcome = resume_run(
        state,
        run_id,
        dispatch=_fake_dispatch(),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_100.0,
    )
    assert outcome.outcome == "no-improvement"
    num, body = github.issue_comments[-1]
    assert num == 7 and "finished (no-improvement)" in body
    assert RELEASE_MARKER in body  # the claim is freed for re-selection


def _panel_lens(text):
    from autoresearch.panel import PanelLens

    return (PanelLens("review", _panel_judge([text])),)


def test_resume_clean_panel_arms_and_records_transcript(tmp_path, monkeypatch) -> None:
    # a dispatched improvement now runs the SAME verification panel as inline; a
    # clean read -> non-draft PR + arm auto-merge, with the transcript in the body.
    import json as _json

    state, run_id = _write_parked_candidate(
        tmp_path, monkeypatch, values={"baseline": 13.0, "candidate": 12.0}
    )
    github = FakeGitHub()
    outcome = resume_run(
        state,
        run_id,
        dispatch=_fake_dispatch(),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_100.0,
        panel_lenses=_panel_lens(_json.dumps({"findings": [], "notes": "clean"})),
    )
    assert outcome.outcome == "improved"
    assert github.prs[0]["draft"] is False
    assert github.armed and github.armed[0][0] == "org/pilot"
    assert "Verification" in github.prs[0]["body"]  # transcript rode the PR body


def test_resume_blocking_panel_opens_a_draft_and_never_arms(tmp_path, monkeypatch) -> None:
    # a blocking panel finding -> DRAFT PR carrying the findings, no arm (slice 1:
    # a human triages; waking the agent to revise is the next slice).
    import json as _json

    blocking = _json.dumps(
        {
            "findings": [
                {
                    "file": "src/pilot/solvers/tsp.py",
                    "line": 1,
                    "confidence": "high",
                    "summary": "suspicious",
                    "detail": "looks structural",
                    "blocking": True,
                }
            ],
            "notes": "",
        }
    )
    state, run_id = _write_parked_candidate(
        tmp_path, monkeypatch, values={"baseline": 13.0, "candidate": 12.0}
    )
    github = FakeGitHub()
    outcome = resume_run(
        state,
        run_id,
        dispatch=_fake_dispatch(),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_100.0,
        panel_lenses=_panel_lens(blocking),
    )
    assert outcome.outcome == "improved"  # PR still opens...
    assert github.prs[0]["draft"] is True  # ...as a DRAFT
    assert github.armed == []  # and never armed


def test_resume_panel_does_not_see_untracked_workspace_cruft(tmp_path, monkeypatch) -> None:
    # after checkout -f candidate_sha the workspace keeps post-snapshot untracked
    # files (the fixture's eval-cache.tmp); the panel's git add -A would sweep
    # them into the judged tree unless they are cleaned first. A judge inspects
    # its pr-head checkout to prove the cruft is absent.
    import json as _json
    from dataclasses import dataclass, field

    from autoresearch.panel import PanelLens

    @dataclass
    class _SpyJudge:
        saw_cruft: list = field(default_factory=list)

        def run(self, brief_text, workspace, resume_session_id=None) -> SessionResult:
            self.saw_cruft.append((Path(workspace) / "eval-cache.tmp").exists())
            return SessionResult(
                stop_reason="end_turn",
                is_error=False,
                cost_usd=0.0,
                num_turns=1,
                session_id="judge",
                final_text=_json.dumps({"findings": [], "notes": "clean"}),
                transcript_path="",
            )

    judge = _SpyJudge()
    state, run_id = _write_parked_candidate(
        tmp_path, monkeypatch, values={"baseline": 13.0, "candidate": 12.0}
    )
    resume_run(
        state,
        run_id,
        dispatch=_fake_dispatch(),
        github=FakeGitHub(),  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_100.0,
        panel_lenses=(PanelLens("review", judge),),
    )
    assert judge.saw_cruft == [False]  # the panel judged candidate_sha, not the cruft


def test_resume_panel_error_drafts_and_keeps_candidate(tmp_path, monkeypatch) -> None:
    # a panel ERROR (not a finding) must not abort the publish and drop the
    # candidate snapshot — the improvement is real. Fail closed to a DRAFT.
    from autoresearch.panel import PanelLens

    def boom(*a, **k):
        raise RuntimeError("worktree add failed")

    monkeypatch.setattr(climb_mod, "build_panel_runner", boom)
    state, run_id = _write_parked_candidate(
        tmp_path, monkeypatch, values={"baseline": 13.0, "candidate": 12.0}
    )
    github = FakeGitHub()
    outcome = resume_run(
        state,
        run_id,
        dispatch=_fake_dispatch(),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_100.0,
        panel_lenses=(PanelLens("review", object()),),  # type: ignore[arg-type]  # runner monkeypatched to raise
    )
    assert outcome.outcome == "improved"  # NOT publish-error
    assert github.prs[0]["draft"] is True and github.armed == []  # degraded -> draft, no arm


def test_resume_blocking_panel_on_a_submitted_park_wakes_the_author(tmp_path, monkeypatch) -> None:
    # THE DEPTH-AXIS CORE (buildout Phase B): on a SUBMITTED park, a blocking
    # panel finding goes back to the AUTHOR — the same session is resumed with
    # the findings, revises, and its re-measure re-parks — instead of drafting.
    import json as _json
    from dataclasses import dataclass

    from autoresearch.orchestrator import author_spec
    from autoresearch.panel import PanelLens

    @dataclass
    class RevisingHarness:
        def run(self, brief_text, workspace, resume_session_id=None) -> SessionResult:
            # the agent revises in response to the findings
            (workspace / "src" / "pilot" / "solvers" / "tsp.py").write_text(
                "def solve(): return 'revised'\n"
            )
            return SessionResult(
                stop_reason="end_turn",
                is_error=False,
                cost_usd=0.5,
                num_turns=3,
                session_id="s1",
                final_text="addressed the finding",
                transcript_path="",
            )

    blocking = _json.dumps(
        {
            "findings": [
                {
                    "file": "src/pilot/solvers/tsp.py",
                    "line": 1,
                    "confidence": "high",
                    "summary": "suspicious",
                    "detail": "structural",
                    "blocking": True,
                }
            ],
            "notes": "",
        }
    )
    state, run_id = _write_parked_candidate(
        tmp_path, monkeypatch, values={"baseline": 13.0, "candidate": 12.0}
    )
    # mark the park SUBMITTED: the author sealed this candidate via `submit`
    rec = load_record(state, run_id)
    rec.stage["submitted"] = True
    save_record(state, rec, 1_000_050.0)
    # the real flow excluded the channel at climb start (.git/info/exclude
    # persists in the run's ws); the hand-built test ws needs it too, or the
    # wake's budget refresh pollutes changed_paths
    from autoresearch.syscall import ensure_excluded

    ensure_excluded(state / "runs" / run_id / "ws")
    # the INITIAL measure returns improved (-> panel -> author wake); the
    # re-measure of the revised candidate PARKS, as the dispatched backend does.
    from autoresearch.measure import DispatchSettings, MeasurementPending

    class _TwoPhase:
        def __init__(self):
            self.calls = 0

        def results(self, measures):
            self.calls += 1
            if self.calls == 1:
                return {"baseline": 13.0, "candidate": 12.0}
            raise MeasurementPending(("601", "602"))

    monkeypatch.setattr(DispatchSettings, "measurer", lambda self, *a, **k: _TwoPhase())
    old = load_record(state, run_id).stage["candidate_sha"]
    github = FakeGitHub()
    outcome = resume_run(
        state,
        run_id,
        dispatch=_fake_dispatch(),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_100.0,
        panel_lenses=(PanelLens("review", _panel_judge([blocking])),),
        harness=RevisingHarness(),
        spec=author_spec(),
    )
    assert outcome.outcome == "parked"  # re-parked to measure the REVISED candidate
    assert github.prs == []  # no PR — the revision must be verified first
    rec = load_record(state, run_id)
    assert rec.state == "waiting"
    # the new park is a plain finish (the author did not resubmit)
    assert not rec.stage.get("submitted")
    assert rec.stage["candidate_sha"] != old  # a NEW candidate (the revision)
    # the revised edit landed in the new candidate; the old snapshot was dropped
    ws = state / "runs" / run_id / "ws"
    kept = _git(ws, "for-each-ref", "--format=%(objectname)", "refs/dispatch/").split()
    assert str(rec.stage["candidate_sha"]) in kept and str(old) not in kept


def test_gate_negative_wake_with_an_unchanged_tree_ends_without_a_second_gate(
    tmp_path, monkeypatch
) -> None:
    """A submitted candidate fails the gate; the woken author concludes with an
    honest negative and leaves the tree as it was. The attempt ends on that
    verdict — the identical tree is NOT sealed and dispatched to the gate a
    second time (the speedrun fleet paid a second 8 GPU-hour eval pair for a
    byte-identical tree, 2026-08-28)."""
    from dataclasses import dataclass

    from autoresearch.measure import DispatchSettings
    from autoresearch.orchestrator import author_spec
    from autoresearch.syscall import ensure_excluded

    @dataclass
    class ConcedingHarness:
        def run(self, brief_text, workspace, resume_session_id=None) -> SessionResult:
            assert "did NOT clear the gate" in brief_text
            return SessionResult(
                stop_reason="end_turn",
                is_error=False,
                cost_usd=0.2,
                num_turns=2,
                session_id="s1",
                final_text="Negative result: the change did not help.",
                transcript_path="",
            )

    state, run_id = _write_parked_candidate(
        tmp_path, monkeypatch, values={"baseline": 13.0, "candidate": 13.0}
    )
    rec = load_record(state, run_id)
    rec.stage["submitted"] = True
    save_record(state, rec, 1_000_050.0)
    ensure_excluded(state / "runs" / run_id / "ws")
    # the fixture's eval cruft is for the finish path; here the tree must be
    # exactly the candidate's
    (state / "runs" / run_id / "ws" / "eval-cache.tmp").unlink()

    class _Once:
        def __init__(self):
            self.calls = 0

        def results(self, measures):
            self.calls += 1
            assert self.calls == 1, "the same tree was measured twice"
            return {"baseline": 13.0, "candidate": 13.0}

    once = _Once()
    monkeypatch.setattr(DispatchSettings, "measurer", lambda self, *a, **k: once)
    github = FakeGitHub()
    outcome = resume_run(
        state,
        run_id,
        dispatch=_fake_dispatch(),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_100.0,
        harness=ConcedingHarness(),
        spec=author_spec(),
    )
    assert outcome.outcome == "no-improvement"
    assert github.prs == []
    assert once.calls == 1
    assert load_record(state, run_id).state != "waiting"


def test_errored_gate_wake_with_a_conceding_author_ends_without_a_retry(
    tmp_path, monkeypatch
) -> None:
    """A submitted candidate's gate eval errored (a walltime kill); the woken
    author concludes with the tree untouched. The attempt ends on that error
    — nothing is dispatched again (only a resubmit is a retry)."""
    from dataclasses import dataclass

    from autoresearch.measure import DispatchSettings
    from autoresearch.orchestrator import EvalError, author_spec
    from autoresearch.syscall import ensure_excluded

    @dataclass
    class ConcedingHarness:
        def run(self, brief_text, workspace, resume_session_id=None) -> SessionResult:
            assert "did NOT clear the gate" in brief_text and "walltime" in brief_text
            return SessionResult(
                stop_reason="end_turn",
                is_error=False,
                cost_usd=0.2,
                num_turns=2,
                session_id="s1",
                final_text="Negative result: the run could not be measured in its walltime.",
                transcript_path="",
            )

    state, run_id = _write_parked_candidate(tmp_path, monkeypatch, values={"baseline": 13.0})
    rec = load_record(state, run_id)
    rec.stage["submitted"] = True
    save_record(state, rec, 1_000_050.0)
    ensure_excluded(state / "runs" / run_id / "ws")
    (state / "runs" / run_id / "ws" / "eval-cache.tmp").unlink()

    class _Once:
        def __init__(self):
            self.calls = 0

        def results(self, measures):
            self.calls += 1
            assert self.calls == 1, "the same tree was sent to the gate again"
            raise EvalError("job 102 hit its walltime (TIMEOUT) before producing a result")

    once = _Once()
    monkeypatch.setattr(DispatchSettings, "measurer", lambda self, *a, **k: once)
    github = FakeGitHub()
    outcome = resume_run(
        state,
        run_id,
        dispatch=_fake_dispatch(),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_100.0,
        harness=ConcedingHarness(),
        spec=author_spec(),
    )
    assert outcome.outcome == "eval-error"
    assert github.prs == []
    assert once.calls == 1
    assert load_record(state, run_id).state != "waiting"


def test_resume_blocking_panel_on_a_plain_finish_drafts(tmp_path, monkeypatch) -> None:
    # a candidate park WITHOUT a submit gets no author loop: blocking findings
    # DRAFT the PR for a human (the policy-driven revision loop is retired —
    # the author drives depth via `submit`).
    import json as _json

    from autoresearch.orchestrator import author_spec
    from autoresearch.panel import PanelLens

    blocking = _json.dumps(
        {
            "findings": [
                {
                    "file": "x",
                    "line": 1,
                    "confidence": "high",
                    "summary": "s",
                    "detail": "d",
                    "blocking": True,
                }
            ],
            "notes": "",
        }
    )
    state, run_id = _write_parked_candidate(
        tmp_path, monkeypatch, values={"baseline": 13.0, "candidate": 12.0}
    )
    github = FakeGitHub()
    outcome = resume_run(
        state,
        run_id,
        dispatch=_fake_dispatch(),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_100.0,
        panel_lenses=(PanelLens("review", _panel_judge([blocking])),),
        harness=object(),  # type: ignore[arg-type]
        spec=author_spec(),
    )
    assert outcome.outcome == "improved"  # publishes...
    assert github.prs[0]["draft"] is True and github.armed == []  # ...as a DRAFT, no revise


def test_resume_improved_reconciles_to_an_existing_pr(tmp_path, monkeypatch) -> None:
    # a prior wake opened the PR but died before recording it (run left WAITING).
    # the re-wake must reconcile to that PR: no duplicate PR, no re-push, record
    # goes in-review.
    state, run_id = _write_parked_candidate(
        tmp_path, monkeypatch, values={"baseline": 13.0, "candidate": 12.0}
    )
    github = FakeGitHub(existing_pr="https://github.com/org/pilot/pull/7")
    outcome = resume_run(
        state,
        run_id,
        dispatch=_fake_dispatch(),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_100.0,
    )
    assert outcome.outcome == "improved" and outcome.pr_url.endswith("/pull/7")
    assert github.prs == []  # NO duplicate PR created (the key idempotency property)
    assert github.armed == [("org/pilot", 7)]  # the ADOPTED PR is armed (prior wake may not have)
    record = load_record(state, run_id)
    assert record.state == "in-review" and "pull/7" in record.pr_url
    # the snapshot is released (the candidate is already published)
    ws = state / "runs" / run_id / "ws"
    assert _git(ws, "for-each-ref", "refs/dispatch/").strip() == ""


# --- the author-sleep wake (research-loop-buildout.md Phase A, part 2) ---


def _write_parked_author_sleep(tmp_path, monkeypatch, *, raise_exc=None, run_id="tsp-9"):
    """An author-sleep-parked run on disk in the REAL park state: the session's
    tree persisted as the author left it (uncommitted edits over base), the
    sleep snapshot sealed under its ref, launch job outputs in the run dir, and
    a WAITING record carrying the request + budget counts."""
    import json as json_mod

    from autoresearch.dispatch import snapshot_tree
    from autoresearch.github import Workspace
    from autoresearch.measure import DispatchSettings
    from autoresearch.syscall import ensure_excluded

    state = tmp_path / "state"
    wsroot = state / "runs" / run_id / "ws"
    (wsroot / "src" / "pilot" / "solvers").mkdir(parents=True)
    (wsroot / ".autoresearch.yaml").write_text(CONTRACT_SYSCALLS)
    (wsroot / "src" / "pilot" / "solvers" / "tsp.py").write_text("def solve(): ...\n")
    _git(wsroot, "init", "-q", "-b", "main")
    _git(wsroot, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(wsroot, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    base_sha = _git(wsroot, "rev-parse", "HEAD").strip()
    ensure_excluded(wsroot)  # armed runs excluded the channel at first pass
    (wsroot / "src" / "pilot" / "solvers" / "tsp.py").write_text("def solve(): return 'wip'\n")
    ws = Workspace(root=wsroot)
    snap = snapshot_tree(ws, base_sha)  # the sealed sleep tree
    # the finished launch's job outputs, as the job script leaves them
    ev = state / "runs" / run_id / "eval-launch-probe"
    (ev / "artifacts").mkdir(parents=True)
    (ev / "exit-code").write_text("0\n")
    (ev / "stdout").write_text("tail improvement: 0.7\n")
    (ev / "stderr").write_text("")
    (ev / "artifacts" / "out.json").write_text('{"metric": 0.7}')

    record = RunRecord(
        run_id=run_id,
        target="org/pilot",
        task_title="improve tsp",
        benchmark="tsp",
        state="waiting",
        resume_session_id="s1",
        stage={
            "phase": "author-sleep",
            "base_sha": base_sha,
            "candidate_sha": snap.commit,
            "candidate_ref": snap.ref,
            "seed": 7,
            "suite_seed": 9,
            "afterany": "afterany:501",
            "report": "mid-flight",
            "base_branch": "main",
            "syscall_launches": [{"name": "probe", "artifacts": ["out.json"]}],
            "syscall_note": "compare against the sweep",
            "launches_used": 1,
            "sleeps_used": 1,
        },
    )
    save_record(state, record, 1_000_000.0)
    fake = _FakeMeasurer(values={}, raise_exc=raise_exc)
    monkeypatch.setattr(DispatchSettings, "measurer", lambda self, *a, **k: fake)
    return state, run_id, wsroot, json_mod


def test_author_sleep_wake_delivers_results_and_flows_to_a_candidate_park(
    tmp_path, monkeypatch
) -> None:
    # the woken session continues, finishes, and the gate (dispatched) parks the
    # run as a CANDIDATE — the existing wake path decides it next time.
    from autoresearch.measure import MeasurementPending

    state, run_id, wsroot, _ = _write_parked_author_sleep(
        tmp_path, monkeypatch, raise_exc=MeasurementPending(("701", "702"))
    )
    from autoresearch.roles import author_spec

    class RecordingHarness(ScriptedHarness):
        def run(self, brief_text, workspace, resume_session_id=None):
            calls.append((brief_text, str(workspace), resume_session_id))
            return super().run(brief_text, workspace, resume_session_id)

    calls: list = []
    harness = RecordingHarness(
        edits={"src/pilot/solvers/tsp.py": "def solve(): return 'polished'\n"}
    )
    outcome = resume_run(
        state,
        run_id,
        dispatch=_fake_dispatch(),
        github=CommentingGitHub(),  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_100.0,
        harness=harness,
        spec=author_spec(),
    )
    assert outcome.outcome == "parked"
    record = load_record(state, run_id)
    assert record.state == "waiting"
    assert record.stage["phase"] == "candidate"  # flows into the existing wake
    # the SAME session was resumed, with the launch results as its prompt
    wake_text, _ws, resumed = calls[0]
    assert resumed == "s1"
    assert "tail improvement: 0.7" in wake_text  # the job's stdout, delivered
    assert "compare against the sweep" in wake_text  # the author's note, echoed
    assert "2 launches and 19 sleeps remaining" in wake_text  # budgets visible
    assert ".autoresearch/results/probe/out.json" in wake_text
    # the artifact really landed in the excluded channel
    assert (wsroot / ".autoresearch" / "results" / "probe" / "out.json").read_text() == (
        '{"metric": 0.7}'
    )
    # exactly ONE snapshot ref survives (the new candidate); the sleep ref is gone
    refs = [r for r in _git(wsroot, "for-each-ref", "refs/dispatch/").splitlines() if r]
    assert len(refs) == 1
    assert str(record.stage["candidate_ref"]) in refs[0]


def test_author_sleep_wake_can_sleep_again(tmp_path, monkeypatch) -> None:
    # the woken author launches more work and sleeps again: a fresh author-sleep
    # park with the counts advanced.
    from autoresearch.roles import author_spec

    state, run_id, _wsroot, json_mod = _write_parked_author_sleep(tmp_path, monkeypatch)

    class SleepyHarness(ScriptedHarness):
        def run(self, brief_text, workspace, resume_session_id=None):
            (workspace / ".autoresearch").mkdir(exist_ok=True)
            (workspace / ".autoresearch" / "syscall.json").write_text(
                json_mod.dumps(
                    {"type": "sleep", "launches": [{"name": "second", "command": "run again"}]}
                )
            )
            return super().run(brief_text, workspace, resume_session_id)

    outcome = resume_run(
        state,
        run_id,
        dispatch=_fake_dispatch(),
        github=CommentingGitHub(),  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_100.0,
        harness=SleepyHarness(edits={}),
        spec=author_spec(),
    )
    assert outcome.outcome == "parked"
    record = load_record(state, run_id)
    assert record.stage["phase"] == "author-sleep"
    assert record.stage["syscall_launches"] == [{"name": "second", "minutes": 30, "artifacts": []}]
    assert record.stage["launches_used"] == 2 and record.stage["sleeps_used"] == 2
    # the second launch's job script was written for the sealed NEW tree
    assert (state / "runs" / run_id / "eval-launch-second" / "job.sh").exists()


def test_author_sleep_wake_without_harness_ends_loudly(tmp_path, monkeypatch) -> None:
    # the wake cannot resume without the author harness: a named ending, and the
    # sleep snapshot is released (re-waking would never help).
    state, run_id, wsroot, _ = _write_parked_author_sleep(tmp_path, monkeypatch)
    outcome = resume_run(
        state,
        run_id,
        dispatch=_fake_dispatch(),
        github=CommentingGitHub(),  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_100.0,
    )
    assert outcome.outcome == "session-error"
    record = load_record(state, run_id)
    assert record.state == "ended"
    assert "author harness" in record.ending_note
    assert _git(wsroot, "for-each-ref", "refs/dispatch/").strip() == ""


def test_codex_panel_lens_requires_the_judges_own_key(monkeypatch) -> None:
    # role separation: a codex lens must name the JUDGE's key explicitly —
    # never inherit the anthropic panel key or default to the author's
    import argparse

    import pytest

    from autoresearch.attempt import _panel_lenses_from_args

    monkeypatch.delenv("AUTORESEARCH_PANEL_CODEX_KEY_FILE", raising=False)
    args = argparse.Namespace(
        panel="review:codex:gpt-5.6-terra",
        panel_key_file="/dev/null",
        claude_bin="claude",
        codex_bin="codex",
        image="/img.sif",
    )
    monkeypatch.setattr("autoresearch.attempt.role_key", lambda *a, **k: "k")
    with pytest.raises(ValueError, match="AUTORESEARCH_PANEL_CODEX_KEY_FILE"):
        _panel_lenses_from_args(args)


def test_codex_only_panel_never_reads_the_claude_key(monkeypatch, tmp_path) -> None:
    # a codex-only panel must not demand the (unused) anthropic panel key
    import argparse

    from autoresearch.attempt import _panel_lenses_from_args

    codex_key = tmp_path / "panel_codex_key"
    codex_key.write_text("sk-judge")
    codex_key.chmod(0o600)
    monkeypatch.setenv("AUTORESEARCH_PANEL_CODEX_KEY_FILE", str(codex_key))
    args = argparse.Namespace(
        panel="review:codex:gpt-5.6-terra",
        panel_key_file=str(tmp_path / "no-such-anthropic-key"),  # absent, must not matter
        claude_bin="claude",
        codex_bin="codex",
        image="/img.sif",
    )
    lenses, secrets = _panel_lenses_from_args(args)
    assert len(lenses) == 1 and lenses[0].kind == "review"
    assert secrets == ("sk-judge",)  # the codex judge key joins the redaction set


def test_codex_panel_key_must_not_be_the_author_key(monkeypatch, tmp_path) -> None:
    # role separation enforced in the BUILDER, so a manual climb (not just the
    # tick preflight) refuses a judge running on the author's token
    import argparse

    import pytest

    from autoresearch.attempt import _panel_lenses_from_args

    author_key = tmp_path / "codex_key"
    author_key.write_text("sk-author")
    author_key.chmod(0o600)
    monkeypatch.setenv("AUTORESEARCH_CODEX_KEY_FILE", str(author_key))
    monkeypatch.setenv("AUTORESEARCH_PANEL_CODEX_KEY_FILE", str(author_key))
    args = argparse.Namespace(
        panel="review:codex:gpt-5.6-terra",
        panel_key_file="/dev/null",
        claude_bin="claude",
        codex_bin="codex",
        image="/img.sif",
    )
    with pytest.raises(ValueError, match="role separation"):
        _panel_lenses_from_args(args)


def test_codex_panel_key_must_not_be_the_claude_panel_key(monkeypatch, tmp_path) -> None:
    # provider-key confusion: pointing the codex lens at the claude panel key
    # would send the anthropic credential to OpenAI login
    import argparse

    import pytest

    from autoresearch.attempt import _panel_lenses_from_args

    shared = tmp_path / "verifier_key"
    shared.write_text("sk-ant")
    shared.chmod(0o600)
    monkeypatch.setenv("AUTORESEARCH_PANEL_CODEX_KEY_FILE", str(shared))
    args = argparse.Namespace(
        panel="review:codex:gpt-5.6-terra",
        panel_key_file=str(shared),
        claude_bin="claude",
        codex_bin="codex",
        image="/img.sif",
    )
    with pytest.raises(ValueError, match="another provider"):
        _panel_lenses_from_args(args)


def test_codex_panel_lens_refuses_to_run_uncontained(monkeypatch, tmp_path) -> None:
    # --uncontained (image="") must never produce a danger-full-access codex
    # judge on the host
    import argparse

    import pytest

    from autoresearch.attempt import _panel_lenses_from_args

    judge_key = tmp_path / "panel_codex_key"
    judge_key.write_text("sk-judge")
    judge_key.chmod(0o600)
    monkeypatch.setenv("AUTORESEARCH_PANEL_CODEX_KEY_FILE", str(judge_key))
    args = argparse.Namespace(
        panel="review:codex:gpt-5.6-terra",
        panel_key_file="/dev/null",
        claude_bin="claude",
        codex_bin="codex",
        image="",  # dev --uncontained
    )
    with pytest.raises(ValueError, match="requires --image"):
        _panel_lenses_from_args(args)


def test_hermes_panel_lens_shares_the_judge_key_rules(monkeypatch, tmp_path) -> None:
    # hermes joins the shelled-judge rules via the SAME helper as codex:
    # image required, own key named, never the author's or the claude panel's
    import argparse

    import pytest

    from autoresearch.attempt import _panel_lenses_from_args

    def args(image="/img.sif"):
        return argparse.Namespace(
            panel="review:hermes:gpt-5.6-terra",
            panel_key_file="/dev/null",
            claude_bin="claude",
            codex_bin="codex",
            image=image,
        )

    monkeypatch.delenv("AUTORESEARCH_PANEL_HERMES_KEY_FILE", raising=False)
    with pytest.raises(ValueError, match="AUTORESEARCH_PANEL_HERMES_KEY_FILE"):
        _panel_lenses_from_args(args())
    judge = tmp_path / "panel_hermes_key"
    judge.write_text("sk-judge")
    judge.chmod(0o600)
    monkeypatch.setenv("AUTORESEARCH_PANEL_HERMES_KEY_FILE", str(judge))
    with pytest.raises(ValueError, match="requires --image"):
        _panel_lenses_from_args(args(image=""))
    # author-key reuse refused (the codex author key is the OpenAI author key)
    monkeypatch.setenv("AUTORESEARCH_CODEX_KEY_FILE", str(judge))
    with pytest.raises(ValueError, match="role separation"):
        _panel_lenses_from_args(args())
    monkeypatch.delenv("AUTORESEARCH_CODEX_KEY_FILE", raising=False)
    # a properly separated key builds the contained hermes judge
    repo = tmp_path / "hermes-agent"
    repo.mkdir()
    monkeypatch.setenv("REVIEW_HERMES_REPO", str(repo))
    lenses, secrets = _panel_lenses_from_args(args())
    assert len(lenses) == 1 and lenses[0].kind == "review"
    assert secrets == ("sk-judge",)
    assert getattr(lenses[0].harness, "container_image", "") == "/img.sif"


def test_auto_merge_mode_uses_the_auto_path(tmp_path) -> None:
    """The contract's autonomy dial: merge_mode=auto calls the auto-mode
    arming (arm, or direct merge when nothing is pending) instead of the
    manual review-required guard; the base-moved decline binds in BOTH."""
    from autoresearch.attempt import _arm_unless_base_moved

    class ArmingGitHub:
        def __init__(self):
            self.auto = []
            self.manual = []

        def arm_auto_merge_auto_mode(self, repo, number):
            self.auto.append((repo, number))
            return True

        def arm_auto_merge_when_review_required(self, repo, number):
            self.manual.append((repo, number))
            return True

    class StillWs:
        url = ""

        def git_network(self, *a):
            return ""

        def git(self, *a):
            return "b" * 40

        def remote_url(self):
            return "https://x"

    gh = ArmingGitHub()
    _arm_unless_base_moved(
        cast(Any, gh),
        cast(Any, StillWs()),
        "o/r",
        "7",
        "main",
        "b" * 40,
        (),
        merge_mode="auto",
        panel_ran=True,
    )
    assert gh.auto == [("o/r", 7)] and gh.manual == []
    _arm_unless_base_moved(
        cast(Any, gh), cast(Any, StillWs()), "o/r", "8", "main", "b" * 40, (), merge_mode="manual"
    )
    assert gh.manual == [("o/r", 8)]

    class MovedWs(StillWs):
        def git(self, *a):
            return "c" * 40  # base moved

    _arm_unless_base_moved(
        cast(Any, gh),
        cast(Any, MovedWs()),
        "o/r",
        "9",
        "main",
        "b" * 40,
        (),
        merge_mode="auto",
        panel_ran=True,
    )
    assert ("o/r", 9) not in gh.auto  # moved base never self-merges


def test_auto_mode_without_a_panel_arms_manual(tmp_path) -> None:
    """auto means gate+PANEL clean: a publish that ran no panel falls back
    to the manual review-required guard (terra #171)."""
    from autoresearch.attempt import _arm_unless_base_moved

    class ArmingGitHub:
        def __init__(self):
            self.auto = []
            self.manual = []

        def arm_auto_merge_auto_mode(self, repo, number):
            self.auto.append(number)
            return True

        def arm_auto_merge_when_review_required(self, repo, number):
            self.manual.append(number)
            return True

    class StillWs:
        url = ""

        def git_network(self, *a):
            return ""

        def git(self, *a):
            return "b" * 40

        def remote_url(self):
            return "https://x"

    gh = ArmingGitHub()
    _arm_unless_base_moved(
        cast(Any, gh),
        cast(Any, StillWs()),
        "o/r",
        "5",
        "main",
        "b" * 40,
        (),
        merge_mode="auto",
        panel_ran=False,
    )
    assert gh.auto == [] and gh.manual == [5]
    _arm_unless_base_moved(
        cast(Any, gh),
        cast(Any, StillWs()),
        "o/r",
        "6",
        "main",
        "b" * 40,
        (),
        merge_mode="auto",
        panel_ran=True,
    )
    assert gh.auto == [6]


def test_dispatch_settings_read_once_for_fresh_and_wake() -> None:
    """One constructor for the cluster coordinates: the wake path must carry
    the GPU lane exactly like the fresh climb (terra #174 r1 — it dropped it)."""
    import argparse

    from autoresearch.attempt import _dispatch_settings

    args = argparse.Namespace(
        image="/i.sif",
        account="acct",
        partition="cpu",
        gpu_partition="h200",
        gpu_account="gpu-acct",
    )
    d = _dispatch_settings(args)
    assert (d.account, d.partition, d.gpu_partition, d.gpu_account) == (
        "acct",
        "cpu",
        "h200",
        "gpu-acct",
    )
    assert d.placement(1) == ("gpu-acct", "h200")


def _line_ws(tmp_path: Path, bare: Path):
    """A Workspace cloned from the test origin, checked out on main."""
    from autoresearch.github import Workspace

    ws = Workspace.clone(str(bare), tmp_path / "line-ws", auth=None)
    ws.git("checkout", "-q", "-B", "main", "origin/main")
    return ws


def _push_line(tmp_path: Path, bare: Path, files: dict[str, str], name="agents/agent-07") -> None:
    """Seed the origin with a line branch carrying `files` on top of main."""
    work = tmp_path / "line-seed"
    _git(tmp_path, "clone", "-q", str(bare), str(work))
    _git(work, "checkout", "-q", "-b", name, "origin/main")
    for rel, content in files.items():
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(work, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "line work")
    _git(work, "push", "-q", "origin", name)


def _advance_main(tmp_path: Path, bare: Path, files: dict[str, str]) -> None:
    work = tmp_path / "main-seed"
    _git(tmp_path, "clone", "-q", str(bare), str(work))
    for rel, content in files.items():
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(work, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "main moves")
    _git(work, "push", "-q", "origin", "main")


def test_checkout_line_creates_the_branch_from_base(tmp_path: Path, target_repo) -> None:
    from autoresearch.attempt import _checkout_line

    ws = _line_ws(tmp_path, target_repo)
    ref = _checkout_line(ws, ws.root, "agent-07", "main")
    assert ref == "agents/agent-07"
    assert ws.git("rev-parse", "--abbrev-ref", "HEAD").strip() == ref
    assert ws.git("rev-parse", "HEAD").strip() == ws.git("rev-parse", "origin/main").strip()
    # run-start persistence: the branch exists on the remote from its first run
    assert _git(target_repo, "rev-parse", ref).strip() == ws.git("rev-parse", "HEAD").strip()


def test_checkout_line_merges_main_into_an_existing_line(tmp_path: Path, target_repo) -> None:
    from autoresearch.attempt import _checkout_line

    _push_line(tmp_path, target_repo, {"docs/line-note.md": "belief\n"})
    _advance_main(tmp_path, target_repo, {"docs/news.md": "main moved\n"})
    ws = _line_ws(tmp_path, target_repo)
    ref = _checkout_line(ws, ws.root, "agent-07", "main")
    assert (ws.root / "docs" / "line-note.md").read_text() == "belief\n"
    assert (ws.root / "docs" / "news.md").read_text() == "main moved\n"
    assert ws.git("status", "--porcelain").strip() == ""
    assert ws.git("rev-parse", "--abbrev-ref", "HEAD").strip() == ref


def test_checkout_line_leaves_a_conflict_for_the_session(tmp_path: Path, target_repo) -> None:
    from autoresearch.attempt import _checkout_line

    _push_line(tmp_path, target_repo, {"docs/roadmap.md": "# line view\n"})
    _advance_main(tmp_path, target_repo, {"docs/roadmap.md": "# main view\n"})
    ws = _line_ws(tmp_path, target_repo)
    line_tip_before = _git(target_repo, "rev-parse", "agents/agent-07").strip()
    _checkout_line(ws, ws.root, "agent-07", "main")
    unmerged = ws.git("diff", "--name-only", "--diff-filter=U")
    assert "docs/roadmap.md" in unmerged  # the session's first task
    # a conflicted merge is session work, not line state: nothing was pushed
    assert _git(target_repo, "rev-parse", "agents/agent-07").strip() == line_tip_before


def test_line_checkout_resets_instruction_files_to_base(tmp_path: Path, target_repo) -> None:
    from autoresearch.attempt import _checkout_line

    _push_line(
        tmp_path,
        target_repo,
        {
            "CLAUDE.md": "obey the line\n",
            ".mcp.json": "{}",
            ".claude/hooks/evil.sh": "#!/bin/sh\n",
            "docs/line-note.md": "belief\n",
        },
    )
    ws = _line_ws(tmp_path, target_repo)
    _checkout_line(ws, ws.root, "agent-07", "main")
    assert not (ws.root / "CLAUDE.md").exists()  # base has none
    assert not (ws.root / ".mcp.json").exists()
    assert not (ws.root / ".claude").exists()
    assert (ws.root / "docs" / "line-note.md").exists()  # real work survives
    assert ws.git("status", "--porcelain").strip() == ""  # hygiene committed
    # the hygiene state persists: the remote line moved to this tip
    assert (
        _git(target_repo, "rev-parse", "agents/agent-07").strip()
        == ws.git("rev-parse", "HEAD").strip()
    )


def test_checkout_line_rejects_a_ref_shaping_agent_id(tmp_path: Path, target_repo) -> None:
    from autoresearch.attempt import _checkout_line

    ws = _line_ws(tmp_path, target_repo)
    with pytest.raises(ValueError, match="cannot shape a line ref"):
        _checkout_line(ws, ws.root, "../evil", "main")


@pytest.fixture
def target_repo_lines(tmp_path: Path, monkeypatch) -> Path:
    return _seed_target(tmp_path, monkeypatch, CONTRACT_LINES)


def test_push_line_snapshot_publishes_the_terminal_tree(tmp_path: Path, target_repo) -> None:
    from autoresearch.attempt import _checkout_line, _push_line_snapshot

    ws = _line_ws(tmp_path, target_repo)
    _checkout_line(ws, ws.root, "agent-07", "main")
    (ws.root / "docs" / "belief.md").write_text("depth pays\n")
    _push_line_snapshot(ws, "agents/agent-07", "tsp-9", "no-improvement")
    assert _git(target_repo, "show", "agents/agent-07:docs/belief.md") == "depth pays\n"
    msg = _git(target_repo, "log", "-1", "--format=%s", "agents/agent-07").strip()
    assert "tsp-9" in msg and "no-improvement" in msg
    # the local ref advanced with the remote: a later terminal chains on it
    assert (
        ws.git("rev-parse", "refs/heads/agents/agent-07").strip()
        == _git(target_repo, "rev-parse", "agents/agent-07").strip()
    )


def test_push_line_snapshot_skips_an_unchanged_tree(tmp_path: Path, target_repo) -> None:
    from autoresearch.attempt import _checkout_line, _push_line_snapshot

    ws = _line_ws(tmp_path, target_repo)
    _checkout_line(ws, ws.root, "agent-07", "main")
    before = _git(target_repo, "rev-parse", "agents/agent-07").strip()
    _push_line_snapshot(ws, "agents/agent-07", "tsp-9", "no-improvement")
    assert _git(target_repo, "rev-parse", "agents/agent-07").strip() == before


def test_push_line_snapshot_chains_sequential_terminals(tmp_path: Path, target_repo) -> None:
    from autoresearch.attempt import _checkout_line, _push_line_snapshot

    ws = _line_ws(tmp_path, target_repo)
    _checkout_line(ws, ws.root, "agent-07", "main")
    (ws.root / "docs" / "belief.md").write_text("first\n")
    _push_line_snapshot(ws, "agents/agent-07", "tsp-9", "no-improvement")
    first = _git(target_repo, "rev-parse", "agents/agent-07").strip()
    (ws.root / "docs" / "belief.md").write_text("second\n")
    _push_line_snapshot(ws, "agents/agent-07", "tsp-9", "eval-error")
    tip = _git(target_repo, "rev-parse", "agents/agent-07").strip()
    assert _git(target_repo, "rev-parse", f"{tip}^").strip() == first  # fast-forward chain
    assert _git(target_repo, "show", "agents/agent-07:docs/belief.md") == "second\n"


def test_push_line_snapshot_is_best_effort(tmp_path: Path, target_repo) -> None:
    from autoresearch.attempt import _push_line_snapshot

    ws = _line_ws(tmp_path, target_repo)
    _push_line_snapshot(ws, "", "tsp-9", "no-improvement")  # feature off: no-op
    _push_line_snapshot(
        ws, "agents/agent-99", "tsp-9", "no-improvement"
    )  # no such ref: logged skip
    with pytest.raises(subprocess.CalledProcessError):
        _git(target_repo, "rev-parse", "agents/agent-99")


def test_live_attempt_records_every_terminal_in_the_notebook(tmp_path, target_repo_lines) -> None:
    """End to end on the lines contract: a no-improvement run still lands the
    session's final tree on the agent's branch, message naming run + outcome."""
    github = FakeGitHub()
    queue = [13.876, 14.5]  # candidate worse: negative terminal, no PR
    with _queued_local(queue):
        outcome = live_attempt(
            config=RunConfig(target="org/pilot", benchmark="tsp", agent_id="agent-07"),
            run_root=tmp_path / "state",
            run_id="tsp-lines-1",
            harness=ScriptedHarness(edits={"src/pilot/solvers/tsp.py": "def solve(): return 1\n"}),
            github=github,  # type: ignore[arg-type]
            bot_auth=NoAuth(),  # type: ignore[arg-type]
            now=1_000_000.0,
            created="2026-08-06T00:00:00Z",
        )
    assert outcome.outcome == "no-improvement"
    assert github.prs == []
    assert (
        _git(target_repo_lines, "show", "agents/agent-07:src/pilot/solvers/tsp.py")
        == "def solve(): return 1\n"
    )
    msg = _git(target_repo_lines, "log", "-1", "--format=%s", "agents/agent-07").strip()
    assert "tsp-lines-1" in msg and "no-improvement" in msg


def test_push_line_snapshot_publishes_session_commits(tmp_path: Path, target_repo) -> None:
    """A session that COMMITTED its work advanced the local ref without
    dirtying the tree — the push must still publish that commit."""
    from autoresearch.attempt import _checkout_line, _push_line_snapshot

    ws = _line_ws(tmp_path, target_repo)
    _checkout_line(ws, ws.root, "agent-07", "main")
    (ws.root / "docs" / "belief.md").write_text("committed by the session\n")
    ws.git("add", "-A")
    ws.git("-c", "user.name=s", "-c", "user.email=s@s", "commit", "-qm", "agent commit")
    _push_line_snapshot(ws, "agents/agent-07", "tsp-9", "no-improvement")
    assert (
        _git(target_repo, "show", "agents/agent-07:docs/belief.md") == "committed by the session\n"
    )
    assert _git(target_repo, "log", "-1", "--format=%s", "agents/agent-07").strip() == (
        "agent commit"
    )


def test_improved_terminal_notebook_names_the_final_outcome(tmp_path, target_repo_lines) -> None:
    """An improved run's notebook seals BEFORE the publish mutates the tree
    (memory survives) and the message names the gate outcome; the ledger
    stays on main and reaches the line at the next run-start merge."""
    github = FakeGitHub()
    queue = [13.876, 13.1]  # improvement: PR + ledger
    with _queued_local(queue):
        outcome = live_attempt(
            config=RunConfig(target="org/pilot", benchmark="tsp", agent_id="agent-07"),
            run_root=tmp_path / "state",
            run_id="tsp-lines-2",
            harness=ScriptedHarness(edits={"src/pilot/solvers/tsp.py": "def solve(): return 2\n"}),
            github=github,  # type: ignore[arg-type]
            bot_auth=NoAuth(),  # type: ignore[arg-type]
            now=1_000_000.0,
            created="2026-08-06T00:00:00Z",
        )
    assert outcome.outcome == "improved" and len(github.prs) == 1
    msg = _git(target_repo_lines, "log", "-1", "--format=%s", "agents/agent-07").strip()
    assert "tsp-lines-2" in msg and "improved" in msg
    tree = _git(target_repo_lines, "ls-tree", "-r", "--name-only", "agents/agent-07")
    assert "src/pilot/solvers/tsp.py" in tree  # the session's final tree
    assert "results/leader.json" not in tree  # the ledger lives on main


def test_line_memory_never_reaches_a_measurable_seal(tmp_path, target_repo_lines) -> None:
    """The memory boundary end to end: memory edits are neither scope
    violations nor part of the sealed candidate the PR publishes — but the
    notebook keeps them."""
    github = FakeGitHub()
    queue = [13.876, 13.1]  # improvement
    with _queued_local(queue):
        outcome = live_attempt(
            config=RunConfig(target="org/pilot", benchmark="tsp", agent_id="agent-07"),
            run_root=tmp_path / "state",
            run_id="tsp-mem-1",
            harness=ScriptedHarness(
                edits={
                    "src/pilot/solvers/tsp.py": "def solve(): return 3\n",
                    "AGENT_MEMORY.md": "- depth pays\n",
                    "agent_memory/muon.md": "peak lr notes\n",
                }
            ),
            github=github,  # type: ignore[arg-type]
            bot_auth=NoAuth(),  # type: ignore[arg-type]
            now=1_000_000.0,
            created="2026-08-06T00:00:00Z",
        )
    # memory at the repo root is OUTSIDE scope.allowed — with the boundary it
    # is not a violation, and the improvement publishes
    assert outcome.outcome == "improved" and len(github.prs) == 1
    published = _git(target_repo_lines, "ls-tree", "-r", "--name-only", str(github.prs[0]["head"]))
    assert "src/pilot/solvers/tsp.py" in published
    assert "AGENT_MEMORY.md" not in published and "agent_memory/muon.md" not in published
    notebook = _git(target_repo_lines, "ls-tree", "-r", "--name-only", "agents/agent-07")
    assert "AGENT_MEMORY.md" in notebook and "agent_memory/muon.md" in notebook


def test_memory_only_session_measures_nothing(tmp_path, target_repo_lines) -> None:
    """A session that only wrote memory claims nothing: the unmeasured lane
    ends it, and the notebook still preserves the memory."""
    github = FakeGitHub()
    with _queued_local([13.876]):
        outcome = live_attempt(
            config=RunConfig(target="org/pilot", benchmark="tsp", agent_id="agent-07"),
            run_root=tmp_path / "state",
            run_id="tsp-mem-2",
            harness=ScriptedHarness(edits={"AGENT_MEMORY.md": "- muon low peak seems real\n"}),
            github=github,  # type: ignore[arg-type]
            bot_auth=NoAuth(),  # type: ignore[arg-type]
            now=1_000_000.0,
            created="2026-08-06T00:00:00Z",
        )
    assert outcome.outcome == "no-improvement"
    assert github.prs == []
    assert (
        _git(target_repo_lines, "show", "agents/agent-07:AGENT_MEMORY.md")
        == "- muon low peak seems real\n"
    )


def test_notebook_keeps_memory_the_target_gitignores(tmp_path: Path, target_repo) -> None:
    """A target .gitignore matching the memory paths must not silently
    discard session memory from the notebook seal."""
    from autoresearch.attempt import _checkout_line, _push_line_snapshot

    ws = _line_ws(tmp_path, target_repo)
    _checkout_line(ws, ws.root, "agent-07", "main")
    (ws.root / ".gitignore").write_text("AGENT_MEMORY.md\nagent_memory/\n")
    (ws.root / "AGENT_MEMORY.md").write_text("survives the ignore\n")
    (ws.root / "agent_memory").mkdir()
    (ws.root / "agent_memory" / "muon.md").write_text("notes\n")
    _push_line_snapshot(ws, "agents/agent-07", "tsp-9", "no-improvement")
    tree = _git(target_repo, "ls-tree", "-r", "--name-only", "agents/agent-07")
    assert "AGENT_MEMORY.md" in tree and "agent_memory/muon.md" in tree


def test_fallback_run_still_excludes_memory_from_the_seal(
    tmp_path, target_repo_lines, monkeypatch
) -> None:
    """A failed line checkout falls back to the base branch, but the memory
    boundary keys on the CONTRACT: the sealed candidate must still exclude
    memory paths (else scope config is the only thing keeping them off main)."""

    def broken_checkout(*a, **k):
        raise RuntimeError("simulated checkout failure")

    monkeypatch.setattr(climb_mod, "_checkout_line", broken_checkout)
    github = FakeGitHub()
    with _queued_local([13.876, 13.1]):
        outcome = live_attempt(
            config=RunConfig(target="org/pilot", benchmark="tsp", agent_id="agent-07"),
            run_root=tmp_path / "state",
            run_id="tsp-fallback-1",
            harness=ScriptedHarness(
                edits={
                    "src/pilot/solvers/tsp.py": "def solve(): return 4\n",
                    "AGENT_MEMORY.md": "- notes\n",
                }
            ),
            github=github,  # type: ignore[arg-type]
            bot_auth=NoAuth(),  # type: ignore[arg-type]
            now=1_000_000.0,
            created="2026-08-06T00:00:00Z",
        )
    assert outcome.outcome == "improved" and len(github.prs) == 1
    published = _git(target_repo_lines, "ls-tree", "-r", "--name-only", str(github.prs[0]["head"]))
    assert "src/pilot/solvers/tsp.py" in published and "AGENT_MEMORY.md" not in published


def test_wake_terminal_pushes_the_line_notebook(tmp_path, monkeypatch) -> None:
    """A negative candidate wake on a lines run lands the session's final
    tree — memory included — on the agent's branch."""
    state, run_id = _write_parked_candidate(
        tmp_path,
        monkeypatch,
        values={"baseline": 13.0, "candidate": 13.0},
        contract=CONTRACT_DISPATCH.replace(
            "    direction: min\n", "    direction: min\n    lines: true\n"
        ),
        agent_id="agent-07",
    )
    wsroot = state / "runs" / run_id / "ws"
    (wsroot / "AGENT_MEMORY.md").write_text("- candidate was flat\n")
    outcome = resume_run(
        state,
        run_id,
        dispatch=_fake_dispatch(),
        github=CommentingGitHub(),  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_100.0,
    )
    assert outcome.outcome == "no-improvement"
    bare = tmp_path / f"origin-{run_id}.git"
    tree = _git(bare, "ls-tree", "-r", "--name-only", "agents/agent-07")
    assert "AGENT_MEMORY.md" in tree
    msg = _git(bare, "log", "-1", "--format=%s", "agents/agent-07").strip()
    assert run_id in msg and "no-improvement" in msg


def test_line_memory_reaches_the_next_session_brief(tmp_path: Path, target_repo_lines) -> None:
    """End to end: an index committed on the line is rendered into the next
    session's brief, data-fenced."""
    _push_line(tmp_path, target_repo_lines, {"AGENT_MEMORY.md": "- depth pays, width unclear\n"})

    class BriefCapture(ScriptedHarness):
        seen: ClassVar[dict] = {}

        def run(self, brief_text, workspace, resume_session_id=None):
            BriefCapture.seen["brief"] = brief_text
            return super().run(brief_text, workspace, resume_session_id)

    github = FakeGitHub()
    with _queued_local([13.876, 14.5]):
        live_attempt(
            config=RunConfig(target="org/pilot", benchmark="tsp", agent_id="agent-07"),
            run_root=tmp_path / "state",
            run_id="tsp-mem-brief",
            harness=BriefCapture(edits={}),
            github=github,  # type: ignore[arg-type]
            bot_auth=NoAuth(),  # type: ignore[arg-type]
            now=1_000_000.0,
            created="2026-08-06T00:00:00Z",
        )
    brief = str(BriefCapture.seen["brief"])
    assert "# Your memory (AGENT_MEMORY.md" in brief
    assert "- depth pays, width unclear" in brief
    assert "Maintain the memory before you finish" in brief


def test_panel_claim_carries_the_one_contribution_mandate() -> None:
    from autoresearch.attempt import _panel_claim_body

    lines = _panel_claim_body("tsp", 13.8, 13.1, "report text", lines=True)
    assert "ONE clean contribution" in lines and "BLOCKING finding" in lines
    plain = _panel_claim_body("tsp", 13.8, 13.1, "report text", lines=False)
    assert "ONE clean contribution" not in plain
    assert "measured by the orchestrator" in plain


def test_line_divergence_reaches_the_brief(tmp_path: Path, target_repo_lines) -> None:
    _push_line(tmp_path, target_repo_lines, {"docs/line-note.md": "belief\n"})

    class BriefCapture2(ScriptedHarness):
        seen: ClassVar[dict] = {}

        def run(self, brief_text, workspace, resume_session_id=None):
            BriefCapture2.seen["brief"] = brief_text
            return super().run(brief_text, workspace, resume_session_id)

    with _queued_local([13.876, 14.5]):
        live_attempt(
            config=RunConfig(target="org/pilot", benchmark="tsp", agent_id="agent-07"),
            run_root=tmp_path / "state",
            run_id="tsp-div-1",
            harness=BriefCapture2(edits={}),
            github=FakeGitHub(),  # type: ignore[arg-type]
            bot_auth=NoAuth(),  # type: ignore[arg-type]
            now=1_000_000.0,
            created="2026-08-06T00:00:00Z",
        )
    brief = str(BriefCapture2.seen["brief"])
    assert "differs from the base branch by: 1 file changed" in brief


def test_cli_rejects_ref_shaping_agent_ids(capsys, monkeypatch) -> None:
    from autoresearch.attempt import main as climb_main

    # "-leading" is not here: argparse consumes it as an option flag and
    # rejects it on its own ("expected one argument")
    for bad in ("bad id", "bad..id", "a/b", "_leading"):
        monkeypatch.setattr(
            "sys.argv", ["climb", "--target", "o/r", "--benchmark", "b", "--agent-id", bad]
        )
        with pytest.raises(SystemExit):
            climb_main()
        assert "cannot shape a git ref" in capsys.readouterr().err
