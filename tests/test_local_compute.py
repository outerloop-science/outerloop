"""LocalCompute: the same job specs the cluster runs, as synchronous
subprocesses in the current allocation — and the one measurer on top of it."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from autoresearch.compute import GONE, JobSpec, LocalCompute
from autoresearch.dispatch import snapshot_tree
from autoresearch.github import Workspace
from autoresearch.measure import DispatchedMeasurer, MeasurementPending, plan_measures
from autoresearch.orchestrator import EvalError


def _spec(command: str = "", script: str = "", minutes: int = 1) -> JobSpec:
    return JobSpec(
        job_name="t",
        account="",
        partition="",
        time_minutes=minutes,
        command=command,
        script=script,
    )


def test_submit_runs_synchronously_and_status_is_terminal(tmp_path: Path) -> None:
    lc = LocalCompute()
    marker = tmp_path / "ran"
    job = lc.submit(_spec(command=f"touch {marker}"))
    assert marker.exists()  # done by the time submit returned
    assert job.isdigit()  # callers validate ids with isdigit
    assert lc.status(job) == "COMPLETED"
    lc.cancel(job)  # idempotent no-op on a finished job


def test_failed_command_reports_failed(tmp_path: Path) -> None:
    lc = LocalCompute()
    assert lc.status(lc.submit(_spec(command="exit 3"))) == "FAILED"
    assert lc.status("999") == GONE  # unknown id: no record
    assert lc.job_id_for_name("anything") == ""  # nothing is ever live
    assert lc.active_job_names() == []


def _seed_repo(tmp_path: Path) -> tuple[Workspace, str, str]:
    ws_root = tmp_path / "repo"
    ws_root.mkdir()

    def g(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(ws_root), *args], capture_output=True, text=True, check=True
        ).stdout

    g("init", "-q", "-b", "main")
    (ws_root / "solve.py").write_text("print('base')\n")
    g("add", "-A")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    base = g("rev-parse", "HEAD").strip()
    ws = Workspace(root=ws_root)
    (ws_root / "solve.py").write_text("print('cand')\n")
    snap = snapshot_tree(ws, base)
    return ws, base, snap.commit


def test_measurer_on_local_compute_never_parks(tmp_path: Path) -> None:
    # the ONE measurer, local backend: the identical eval-job script runs as a
    # subprocess (fresh checkout of each sealed sha, bare mode), every job is
    # done when checked, and results flow straight through — no park.
    ws, base, cand = _seed_repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    m = DispatchedMeasurer(
        compute=LocalCompute(),
        run_dir=run_dir,
        repo_root=ws.root,
        image="",  # bare mode: no apptainer in tests
        account="",
        partition="",
        eval_minutes=1,
        run_tag="t",
    )
    # the command reads the TREE it runs in: each measure must see a fresh
    # checkout of ITS OWN sha (base prints 'base', candidate prints 'cand'),
    # never the live workspace — the isolation the old inline path needed a
    # separate baseline worktree and drift fingerprints to approximate
    plan = plan_measures(
        command='grep -q cand solve.py && echo {\\"score\\": 2.0} || echo {\\"score\\": 1.0}',
        metric="score",
        base_sha=base,
        candidate_sha=cand,
    )
    results = m.results(plan)  # would raise MeasurementPending on a cluster
    assert results == {"baseline": 1.0, "candidate": 2.0}
    # slot-cached: a fresh measurer over the same run dir reads, never re-runs
    again = DispatchedMeasurer(
        compute=LocalCompute(),
        run_dir=run_dir,
        repo_root=ws.root,
        image="",
        account="",
        partition="",
        eval_minutes=1,
        run_tag="t",
    )
    assert again.results(plan) == results  # read from the eval dirs, not re-run


def test_local_eval_failure_is_an_eval_error_not_a_park(tmp_path: Path) -> None:
    ws, base, cand = _seed_repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    m = DispatchedMeasurer(
        compute=LocalCompute(),
        run_dir=run_dir,
        repo_root=ws.root,
        image="",
        account="",
        partition="",
        eval_minutes=1,
        run_tag="t",
    )
    plan = plan_measures(command="exit 7", metric="score", base_sha=base, candidate_sha=cand)
    with pytest.raises(EvalError, match="failed"):
        m.results(plan)


def test_seed_env_reaches_the_bare_eval(tmp_path: Path) -> None:
    # the paired seed is injected into the scrubbed bare-mode env — and the
    # submitting process's own env must NOT leak through env -i
    ws, base, cand = _seed_repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    m = DispatchedMeasurer(
        compute=LocalCompute(),
        run_dir=run_dir,
        repo_root=ws.root,
        image="",
        account="",
        partition="",
        eval_minutes=1,
        run_tag="t",
    )
    plan = plan_measures(
        command='echo {\\"s\\": ${PILOT_SEED}${LEAKED_SECRET:+9}}',
        metric="s",
        base_sha=base,
        candidate_sha=cand,
        seed_env="PILOT_SEED",
        seed=4,
    )
    import os

    os.environ["LEAKED_SECRET"] = "x"
    try:
        assert m.results(plan)["candidate"] == 4.0  # seed in, submitter env out
    finally:
        del os.environ["LEAKED_SECRET"]


def test_pending_carries_no_local_semantics() -> None:
    # MeasurementPending is a cluster concept; assert its afterany shape stays
    # intact for the callers that park on it
    assert MeasurementPending(("1", "2")).afterany() == "afterany:1:2"
    assert MeasurementPending(()).afterany() == ""


def test_invalid_spec_is_refused_like_slurm() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        LocalCompute().submit(_spec(command="true", script="/x.sh"))
    with pytest.raises(ValueError, match="exactly one"):
        LocalCompute().submit(_spec())


def test_job_terminal_without_a_result_fails_instead_of_parking(tmp_path: Path) -> None:
    # a local timeout kills the script before it writes exit-code: the job is
    # terminal with no result, and parking would wait on a job that will never
    # deliver — the measurer must fail it like a vanished job
    ws, base, cand = _seed_repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    class _DeadCompute:
        def submit(self, spec) -> str:
            return "123"  # "ran", but wrote nothing (killed at the walltime)

        def status(self, job_id: str) -> str:
            return "TIMEOUT"

        def pending_reason(self, job_id: str) -> str:
            return ""

        def job_partition(self, job_id: str) -> str:
            return ""

        def active_job_names(self) -> list:
            return []

        def job_id_for_name(self, name: str) -> str:
            return ""

        def cancel(self, job_id: str) -> None:
            pass

    m = DispatchedMeasurer(
        compute=_DeadCompute(),
        run_dir=run_dir,
        repo_root=ws.root,
        image="",
        account="",
        partition="",
        eval_minutes=1,
        run_tag="t",
    )
    plan = plan_measures(command="true", metric="s", base_sha=base, candidate_sha=cand)
    with pytest.raises(EvalError, match=r"hit its walltime|without a result"):
        m.results(plan)


def test_walltime_kill_takes_the_whole_process_group(tmp_path: Path) -> None:
    # Slurm kills the job's process group at walltime; a local job script
    # waiting on a child must not leave that child running past it
    import os
    import time

    pidfile = tmp_path / "child.pid"
    lc = LocalCompute(minute_s=1)  # 1-minute walltime == 1 second, for the test
    job = lc.submit(_spec(command=f"sleep 300 & echo $! > {pidfile}; wait", minutes=1))
    assert lc.status(job) == "TIMEOUT"
    child = int(pidfile.read_text().strip())
    time.sleep(0.1)  # let the SIGKILL land
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)  # the child died with the group


def test_job_env_is_an_allowlist_not_the_submitter_env(tmp_path: Path) -> None:
    # the submitter holds live keys, and an inherited APPTAINERENV_* would
    # cross --cleanenv into the container — the job starts from a minimal env
    import os

    out = tmp_path / "env.txt"
    os.environ["APPTAINERENV_SECRET"] = "leak"
    try:
        LocalCompute().submit(_spec(command=f'echo "x${{APPTAINERENV_SECRET}}x" > {out}'))
    finally:
        del os.environ["APPTAINERENV_SECRET"]
    assert out.read_text().strip() == "xx"


def test_terminal_states_survive_across_instances(tmp_path, monkeypatch) -> None:
    """The tick and the attempts it spawns each hold their own LocalCompute;
    a launch submitted by one must not read as GONE to another (terra #223:
    the sweep would sit on the 12h park deadline instead of waking at the
    next cadence). With a state root, terminal states persist; without one,
    memory-only behavior is unchanged."""
    from autoresearch.compute import GONE, JobSpec, LocalCompute

    monkeypatch.setenv("AUTORESEARCH_ROOT", str(tmp_path))
    submitter = LocalCompute()
    job_id = submitter.submit(
        JobSpec(job_name="probe", account="", partition="", time_minutes=1, command="true")
    )
    poller = LocalCompute()  # a different process in real life
    assert poller.status(job_id) == "COMPLETED"
    monkeypatch.delenv("AUTORESEARCH_ROOT")
    assert poller.status(job_id) == GONE  # no root -> memory-only, as before


def test_gpu_contracts_pass_the_lane_check_under_local_mode(monkeypatch) -> None:
    """Local compute has no lanes: a contract with GPU benchmarks must not be
    rejected for a missing AUTORESEARCH_GPU_PARTITION (terra #223)."""
    from types import SimpleNamespace

    from autoresearch.tick import FollowupSpec, _gpu_lane_error

    spec = FollowupSpec(
        account="", partition="", run_root=Path("/tmp/x"), image="", home=Path("/tmp/x")
    )
    contract = SimpleNamespace(benchmarks=[SimpleNamespace(name="speedrun", gpus=1)])
    assert "no GPU lane" in _gpu_lane_error(contract, "speedrun", spec)
    monkeypatch.setenv("AUTORESEARCH_COMPUTE", "local")
    assert _gpu_lane_error(contract, "speedrun", spec) == ""


def test_local_mode_places_gpu_measures_without_a_lane(monkeypatch) -> None:
    """DispatchSettings.placement must not raise for a GPU measure under
    local mode (terra #223 r7: the lane waiver admitted GPU contracts that
    then failed at placement) — local jobs run on the machine's own GPUs."""
    from autoresearch.compute import LocalCompute
    from autoresearch.measure import DispatchSettings

    settings = DispatchSettings(
        compute=LocalCompute(), image="", account="", partition="", gpu_partition=""
    )
    monkeypatch.setenv("AUTORESEARCH_COMPUTE", "local")
    assert settings.placement(1) == ("", "")
    monkeypatch.delenv("AUTORESEARCH_COMPUTE")
    import pytest

    with pytest.raises(ValueError, match="no GPU lane"):
        settings.placement(1)  # slurm mode still refuses loudly
