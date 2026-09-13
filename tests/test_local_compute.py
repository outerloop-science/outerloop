"""LocalCompute: the same job specs the cluster runs, as synchronous
subprocesses in the current allocation — and the one measurer on top of it."""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

import pytest

from helpers import wait_until
from outerloop.compute import GONE, JobSpec, LocalCompute, SlurmError
from outerloop.dispatch import snapshot_tree
from outerloop.github import Workspace
from outerloop.measure import DispatchedMeasurer, MeasurementPending, plan_measures
from outerloop.orchestrator import EvalError


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
        has_lanes: ClassVar[bool] = True

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

        def lane_load(self, partition: str) -> dict[str, int]:
            return {}

        def queue_snapshot(self) -> list[dict[str, str]]:
            return []

        def job_id_for_name(self, name: str) -> str:
            return ""

        def cancel(self, job_id: str) -> bool:
            return True

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

    pidfile = tmp_path / "child.pid"
    lc = LocalCompute(minute_s=1)  # 1-minute walltime == 1 second, for the test
    job = lc.submit(_spec(command=f"sleep 300 & echo $! > {pidfile}; wait", minutes=1))
    assert lc.status(job) == "TIMEOUT"
    child = int(pidfile.read_text().strip())

    def gone() -> bool:  # died with the group, once the SIGKILL lands
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return True
        return False

    assert wait_until(gone), f"child {child} survived the group kill"


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
    from outerloop.compute import GONE, JobSpec, LocalCompute

    monkeypatch.setenv("OUTERLOOP_ROOT", str(tmp_path))
    submitter = LocalCompute()
    job_id = submitter.submit(
        JobSpec(job_name="probe", account="", partition="", time_minutes=1, command="true")
    )
    poller = LocalCompute()  # a different process in real life
    assert poller.status(job_id) == "COMPLETED"
    monkeypatch.delenv("OUTERLOOP_ROOT")
    assert poller.status(job_id) == GONE  # no root -> memory-only, as before


def test_gpu_contracts_pass_the_lane_check_on_a_backend_without_lanes() -> None:
    """A backend without lanes (local compute) runs GPU jobs on the default
    placement, so a contract with GPU benchmarks needs no OUTERLOOP_GPU_PARTITION
    there; a backend with lanes still refuses loudly (terra #223)."""
    from types import SimpleNamespace

    from outerloop.tick import ServiceSpec, _gpu_lane_error

    contract = SimpleNamespace(benchmarks=[SimpleNamespace(name="speedrun", gpus=1)])

    def spec(has_lanes: bool) -> ServiceSpec:
        return ServiceSpec(
            account="",
            partition="",
            run_root=Path("/tmp/x"),
            image="",
            home=Path("/tmp/x"),
            has_lanes=has_lanes,
        )

    assert "no GPU lane" in _gpu_lane_error(contract, "speedrun", spec(True))
    assert _gpu_lane_error(contract, "speedrun", spec(False)) == ""


def test_a_backend_without_lanes_places_gpu_launches_and_measures() -> None:
    """Both placements ask the backend: LocalCompute has no lanes, so a GPU job
    places on the default; SlurmCompute without a GPU lane refuses loudly
    (terra #223 r7; the measure placement once lacked the rule and a GPU
    benchmark on a local box aborted at its first baseline)."""
    import pytest

    from outerloop.compute import CommandResult, LocalCompute, SlurmCompute
    from outerloop.measure import DispatchSettings, Measure

    gpu = Measure(name="baseline", tree_sha="a" * 40, command="x", metric="loss", gpus=1)
    local = DispatchSettings(compute=LocalCompute(), image="", account="", partition="")
    assert local.placement(1) == ("", "")
    measurer = local.measurer(
        Path("/tmp/x"), repo_root=Path("/tmp/x"), eval_minutes=15, run_tag="r"
    )
    assert measurer._placement(gpu) == ("", "")
    slurm = DispatchSettings(
        compute=SlurmCompute(runner=lambda argv, timeout_s: CommandResult(0, "", "")),
        image="",
        account="",
        partition="",
    )
    with pytest.raises(ValueError, match="no GPU lane"):
        slurm.placement(1)
    with pytest.raises(ValueError, match="no GPU lane"):
        slurm.measurer(
            Path("/tmp/x"), repo_root=Path("/tmp/x"), eval_minutes=15, run_tag="r"
        )._placement(gpu)


def test_local_job_output_is_kept_beside_its_state(tmp_path: Path, monkeypatch) -> None:
    """#295: a failed local job used to leave only COMPLETED/FAILED; its combined
    stdout/stderr now lands in local_jobs/<id>.out."""
    from outerloop.compute import JobSpec, LocalCompute

    monkeypatch.setenv("OUTERLOOP_ROOT", str(tmp_path))
    compute = LocalCompute()
    job_id = compute.submit(
        JobSpec(
            job_name="j", account="", partition="", time_minutes=1, command="echo boom >&2; exit 3"
        )
    )
    assert compute.status(job_id) == "FAILED"
    out = tmp_path / "local_jobs" / f"{job_id}.out"
    assert out.read_text().strip() == "boom"
    assert out.stat().st_mode & 0o777 == 0o600  # a job may print a credential


def test_array_runs_every_task(tmp_path: Path) -> None:
    """A local job array runs all its tasks, each with its
    SLURM_ARRAY_TASK_ID; every task keeps its state under `<id>_<k>` and the
    array's state is theirs combined."""
    lc = LocalCompute()
    log = tmp_path / "tasks"
    script = tmp_path / "job.sh"
    script.write_text(
        f'#!/bin/sh\necho "$SLURM_ARRAY_TASK_ID" >> {log}\n[ "$SLURM_ARRAY_TASK_ID" != 1 ]\n'
    )
    spec = JobSpec(
        job_name="t", account="", partition="", time_minutes=1, script=str(script), array="0-2%1"
    )
    job = lc.submit(spec)
    assert sorted(log.read_text().split()) == ["0", "1", "2"]
    assert lc.status(job) == "FAILED"  # task 1 failed
    assert lc.status(f"{job}_0") == "COMPLETED" and lc.status(f"{job}_1") == "FAILED"


@pytest.fixture
def gpu_root(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTERLOOP_ROOT", str(tmp_path))
    monkeypatch.setenv("OUTERLOOP_LOCAL_GPUS", "2")
    return tmp_path


def test_shared_gpu_pool_and_running_queue(gpu_root):
    first, second = LocalCompute(), LocalCompute()
    release = gpu_root / "release"

    def job(k):
        return replace(
            _spec(
                command=(
                    f'echo "$CUDA_VISIBLE_DEVICES" > {gpu_root / str(k)}; '
                    f"while [ ! -f {release} ]; do sleep 0.05; done"
                )
            ),
            gpus=1,
            job_name=f"gpu-{k}",
        )

    with ThreadPoolExecutor(2) as executor:
        jobs = [executor.submit(lc.submit, job(k)) for k, lc in enumerate((first, second))]
        try:
            assert wait_until(lambda: all((gpu_root / str(k)).exists() for k in range(2)))
            assert {(gpu_root / str(k)).read_text().strip() for k in range(2)} == {"0", "1"}
            rows = LocalCompute().queue_snapshot()
            assert len(rows) == 2
            assert all(
                set(row)
                == {
                    "id",
                    "name",
                    "state",
                    "elapsed",
                    "partition",
                    "submitted",
                    "reason",
                    "gres",
                    "limit",
                }
                for row in rows
            )
            assert {r["state"] for r in rows} == {"RUNNING"}
            assert {r["gres"] for r in rows} == {"gpu:0", "gpu:1"}
            assert set(first.active_job_names()) == {"gpu-0", "gpu-1"}
            assert all(not future.done() for future in jobs)
        finally:
            release.touch()
        ids = [future.result() for future in jobs]
    assert len(set(ids)) == 2
    assert {r["id"] for r in rows} == set(ids)
    assert all(LocalCompute().status(job_id) == "COMPLETED" for job_id in ids)
    assert LocalCompute().queue_snapshot() == []
    assert _read_pool(gpu_root) == {"holders": {}, "queue": []}


def test_multi_gpu_and_oversize(gpu_root, monkeypatch):
    monkeypatch.setenv("OUTERLOOP_LOCAL_GPUS", "4")
    output = gpu_root / "visible"
    lc = LocalCompute()
    lc.submit(replace(_spec(command=f'echo "$CUDA_VISIBLE_DEVICES" > {output}'), gpus=2))
    assert output.read_text().strip() == "0,1"
    with pytest.raises(SlurmError, match="requests 5 GPUs; only 4 available"):
        lc.submit(replace(_spec(command="true"), gpus=5))


@pytest.mark.parametrize("gpus", [0, 1])
@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize("array, size, concurrency", [("0-3", 4, 2), ("0-1%1", 2, 1)])
def test_array_concurrency_and_states(
    gpu_root, monkeypatch, gpus, persistent, array, size, concurrency
):
    if not persistent:
        monkeypatch.delenv("OUTERLOOP_ROOT")
    monkeypatch.setattr(os, "cpu_count", lambda: 2)
    if not gpus:
        monkeypatch.setenv("OUTERLOOP_LOCAL_GPUS", "0")
    stamp = shlex.quote("import time; print(time.monotonic())")
    python = shlex.quote(sys.executable)
    script = gpu_root / "array.sh"
    script.write_text(
        f"{python} -c {stamp} > {gpu_root}/start_$SLURM_ARRAY_TASK_ID\n"
        f'echo "$CUDA_VISIBLE_DEVICES" > {gpu_root}/gpu_$SLURM_ARRAY_TASK_ID\n'
        "sleep 0.3\n"
        f"{python} -c {stamp} > {gpu_root}/end_$SLURM_ARRAY_TASK_ID\n"
        '[ "$SLURM_ARRAY_TASK_ID" != 1 ]\n'
    )
    lc = LocalCompute()
    job_id = lc.submit(replace(_spec(script=str(script)), gpus=gpus, array=array))
    events = []
    for i in range(size):
        start = float((gpu_root / f"start_{i}").read_text())
        end = float((gpu_root / f"end_{i}").read_text())
        events.extend([(start, 1), (end, -1)])
        assert (gpu_root / f"gpu_{i}").read_text().strip() in ({"0", "1"} if gpus else {""})
        assert lc.status(f"{job_id}_{i}") == ("FAILED" if i == 1 else "COMPLETED")
        if persistent:
            assert (gpu_root / f"local_jobs/{job_id}_{i}.out").exists()
    alive = peak = 0
    for _, delta in sorted(events):
        alive += delta
        peak = max(peak, alive)
    assert peak == concurrency
    assert lc.status(job_id) == "FAILED"


def test_stale_holder_is_reclaimed(gpu_root):
    dead = subprocess.Popen(["sh", "-c", "true"])
    dead.wait()
    state_dir = gpu_root / "local_jobs"
    state_dir.mkdir()
    (state_dir / "gpus.json").write_text(
        json.dumps(
            {
                "holders": {"0": {"id": "999", "pgid": dead.pid, "name": "dead"}},
                "queue": [],
            }
        )
    )
    output = gpu_root / "visible"
    LocalCompute().submit(
        replace(_spec(command=f'echo "$CUDA_VISIBLE_DEVICES" > {output}'), gpus=2)
    )
    assert output.read_text().strip() == "0,1"


@pytest.mark.parametrize("requested", [0, 5])
def test_zero_gpus_preserves_environment(gpu_root, monkeypatch, requested):
    monkeypatch.setenv("OUTERLOOP_LOCAL_GPUS", "0")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "parent")
    output = gpu_root / "visible"
    LocalCompute().submit(
        replace(
            _spec(command=f'echo "${{CUDA_VISIBLE_DEVICES-unset}}" > {output}'),
            gpus=requested,
        )
    )
    assert output.read_text().strip() == "unset"
    assert not (gpu_root / "local_jobs/gpus.json").exists()
    assert LocalCompute().queue_snapshot() == []


@pytest.mark.parametrize("command, state", [("exit 3", "FAILED"), ("sleep 10", "TIMEOUT")])
def test_gpu_release_on_terminal_state(gpu_root, command, state):
    lc = LocalCompute(minute_s=1)
    job = lc.submit(replace(_spec(command=command), gpus=2))
    assert lc.status(job) == state
    assert _read_pool(gpu_root) == {"holders": {}, "queue": []}


def test_gpu_release_on_start_failure(gpu_root, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("cannot start")

    monkeypatch.setattr(subprocess, "Popen", fail)
    with pytest.raises(SlurmError, match="cannot start"):
        LocalCompute().submit(replace(_spec(command="true"), gpus=1))
    assert _read_pool(gpu_root) == {"holders": {}, "queue": []}


def test_gpu_detection(monkeypatch):
    monkeypatch.delenv("OUTERLOOP_LOCAL_GPUS", raising=False)

    def detect(argv, **kwargs):
        assert argv == ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"]
        return subprocess.CompletedProcess(argv, 0, "0\n1\n", "")

    monkeypatch.setattr(subprocess, "run", detect)
    assert LocalCompute._gpu_count() == 2

    def missing(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", missing)
    assert LocalCompute._gpu_count() == 0
    monkeypatch.setenv("OUTERLOOP_LOCAL_GPUS", "4")
    assert LocalCompute._gpu_count() == 4
    monkeypatch.setenv("OUTERLOOP_LOCAL_GPUS", "0")
    assert LocalCompute._gpu_count() == 0


def test_pool_files_survive_state_pruning(gpu_root):
    lc = LocalCompute()
    lc.submit(replace(_spec(command="true"), gpus=1))
    files = [gpu_root / "local_jobs" / name for name in ("gpus.json", "gpus.lock")]
    inode = files[1].stat().st_ino
    for path in files:
        os.utime(path, (time.time() - 172800,) * 2)
    lc.submit(_spec(command="true"))
    assert all(path.exists() for path in files)
    assert files[1].stat().st_ino == inode


def test_submit_waits_for_another_process(gpu_root):
    ready, release, output = (gpu_root / name for name in ("ready", "release", "next"))
    command = f"touch {ready}; while [ ! -f {release} ]; do sleep 0.05; done"
    code = (
        "from outerloop.compute import LocalCompute, JobSpec; "
        f"LocalCompute().submit(JobSpec('holder', '', '', 1, command={command!r}, gpus=2))"
    )
    child = subprocess.Popen([sys.executable, "-c", code])
    try:
        assert wait_until(ready.exists)
        lc = LocalCompute()
        assert wait_until(lambda: lc.active_job_names() == ["holder"])
        with ThreadPoolExecutor(1) as executor:
            job = executor.submit(
                lc.submit,
                replace(_spec(command=f'echo "$CUDA_VISIBLE_DEVICES" > {output}'), gpus=1),
            )
            try:
                time.sleep(0.2)
                assert not job.done()
                assert not output.exists()
            finally:
                release.touch()
            assert lc.status(job.result(timeout=10)) == "COMPLETED"
        assert output.read_text().strip() == "0"
        assert child.wait(timeout=10) == 0
    finally:
        release.touch()
        child.wait(timeout=10)


def test_memory_pool_is_shared(tmp_path, monkeypatch):
    monkeypatch.delenv("OUTERLOOP_ROOT", raising=False)
    monkeypatch.setenv("OUTERLOOP_LOCAL_GPUS", "2")
    first, second = LocalCompute(), LocalCompute()
    with first._pool() as pool:
        pool["holders"]["0"] = {
            "id": "999",
            "pgid": os.getpgrp(),
            "name": "holder",
            "start_time": "",
        }
    try:
        output = tmp_path / "visible"
        second.submit(replace(_spec(command=f'echo "$CUDA_VISIBLE_DEVICES" > {output}'), gpus=1))
        assert output.read_text().strip() == "1"
        assert second.active_job_names() == ["holder"]
    finally:
        with first._pool() as pool:
            pool["holders"].clear()
            pool["queue"].clear()


def _submit_process(root, name, command, *, array=""):
    code = (
        "from outerloop.compute import LocalCompute, JobSpec; "
        f"LocalCompute().submit(JobSpec({name!r}, '', '', 1, "
        f"command={command!r}, gpus=1, array={array!r}))"
    )
    return subprocess.Popen([sys.executable, "-c", code])


def _read_pool(root):
    path = root / "local_jobs/gpus.json"
    return json.loads(path.read_text()) if path.exists() else {"holders": {}, "queue": []}


def test_process_race_and_ticket_order(gpu_root, monkeypatch):
    monkeypatch.setenv("OUTERLOOP_LOCAL_GPUS", "1")
    go = gpu_root / "go"
    children = []
    try:
        for name in ("a", "b"):
            command = (
                f"touch {gpu_root / name}; "
                f"while [ ! -f {gpu_root / (name + '_release')} ]; "
                "do sleep 0.05; done"
            )
            code = (
                "import pathlib, time; "
                f"p=pathlib.Path({str(go)!r}); "
                "exec('while not p.exists(): time.sleep(0.01)'); "
                "from outerloop.compute import LocalCompute, JobSpec; "
                f"LocalCompute().submit(JobSpec({name!r}, '', '', 1, gpus=1, command="
                f"{command!r}))"
            )
            children.append(subprocess.Popen([sys.executable, "-c", code]))
        go.touch()
        assert wait_until(lambda: len(_read_pool(gpu_root)["queue"]) == 1)
        assert wait_until(lambda: any((gpu_root / n).exists() for n in ("a", "b")))
        winners = [n for n in ("a", "b") if (gpu_root / n).exists()]
        assert len(winners) == 1
        winner = winners[0]
        loser = "b" if winner == "a" else "a"
        first_ticket = _read_pool(gpu_root)["queue"][0]["id"]
        children.append(_submit_process(gpu_root, "new", f"touch {gpu_root / 'new'}"))
        assert wait_until(lambda: len(_read_pool(gpu_root)["queue"]) == 2)
        assert _read_pool(gpu_root)["queue"][0]["id"] == first_ticket
        (gpu_root / (winner + "_release")).touch()
        assert wait_until((gpu_root / loser).exists)
        assert not (gpu_root / "new").exists()
        (gpu_root / (loser + "_release")).touch()
        assert wait_until((gpu_root / "new").exists)
        assert all(child.wait(timeout=10) == 0 for child in children)
    finally:
        for name in ("a", "b"):
            (gpu_root / (name + "_release")).touch()
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)


@pytest.mark.parametrize("terminal_first", [False, True])
def test_job_ownership_survives_dead_submitter(gpu_root, monkeypatch, terminal_first):
    monkeypatch.setenv("OUTERLOOP_LOCAL_GPUS", "1")
    job = subprocess.Popen(["sleep", "30"], start_new_session=True)
    dead = subprocess.Popen(["true"])
    dead.wait()
    state_dir = gpu_root / "local_jobs"
    state_dir.mkdir()
    (state_dir / "gpus.json").write_text(
        json.dumps(
            {
                "holders": {"0": {"id": "999", "pgid": job.pid, "pid": dead.pid, "name": "orphan"}},
                "queue": [{"id": "998", "pid": dead.pid}],
            }
        )
    )
    contender = None
    try:
        if terminal_first:
            (state_dir / "999").write_text("COMPLETED")
        contender = _submit_process(gpu_root, "next", f"touch {gpu_root / 'next'}")
        assert wait_until(
            lambda: (
                bool(_read_pool(gpu_root)["queue"])
                and _read_pool(gpu_root)["queue"][0]["id"] != "998"
            )
        )
        assert not (gpu_root / "next").exists()
        assert LocalCompute().active_job_names() == ["orphan"]
        assert job.poll() is None
        (state_dir / "999").write_text("FAILED")
        job.kill()
        job.wait()
        assert wait_until((gpu_root / "next").exists)
        assert contender.wait(timeout=10) == 0
    finally:
        job.kill()
        job.wait()
        if contender is not None:
            if contender.poll() is None:
                contender.kill()
            contender.wait()


@pytest.mark.parametrize("failure", ["exit", "garbage", "timeout"])
def test_gpu_detection_failures(monkeypatch, failure):
    monkeypatch.delenv("OUTERLOOP_LOCAL_GPUS", raising=False)

    def detect(argv, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 10)
        return subprocess.CompletedProcess(
            argv, int(failure == "exit"), "garbage" if failure == "garbage" else "0\n", ""
        )

    monkeypatch.setattr(subprocess, "run", detect)
    assert LocalCompute._gpu_count() == 0


def test_gpu_job_does_not_modify_submitter_or_following_cpu_env(gpu_root, monkeypatch):
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    lc = LocalCompute()
    lc.submit(replace(_spec(command="true"), gpus=1))
    assert "CUDA_VISIBLE_DEVICES" not in os.environ
    output = gpu_root / "cpu_env"
    lc.submit(_spec(command=f'echo "${{CUDA_VISIBLE_DEVICES-unset}}" > {output}'))
    assert output.read_text().strip() == "unset"


@pytest.mark.parametrize("error", [KeyboardInterrupt, RuntimeError])
def test_array_exception_kills_running_and_cancels_queued(gpu_root, monkeypatch, error):
    observed: list[int] = []

    def interrupt(futures):
        assert wait_until(lambda: len(list(gpu_root.glob("started_*"))) == 2)
        observed.extend(int(p.read_text()) for p in gpu_root.glob("started_*"))
        raise error("stop array")

    monkeypatch.setattr("outerloop.compute.as_completed", interrupt)
    lc = LocalCompute()
    command = f"echo $$ > {gpu_root}/started_$SLURM_ARRAY_TASK_ID; exec sleep 30"
    with pytest.raises(error, match="stop array"):
        lc.submit(replace(_spec(command=command), gpus=1, array="0-5"))
    assert len(observed) == 2
    for pgid in observed:
        with pytest.raises(ProcessLookupError):
            os.killpg(pgid, 0)
    assert len(list(gpu_root.glob("started_*"))) == 2
    assert _read_pool(gpu_root) == {"holders": {}, "queue": []}
    array_states = [p for p in (gpu_root / "local_jobs").iterdir() if p.name.isdigit()]
    assert len(array_states) == 1
    assert array_states[0].read_text() == "CANCELLED"


def test_zero_gpu_warning_once_per_process(gpu_root, monkeypatch, caplog):
    monkeypatch.setenv("OUTERLOOP_LOCAL_GPUS", "0")
    monkeypatch.setattr(LocalCompute, "_zero_gpu_warning_pid", None)
    LocalCompute().submit(_spec(command="true"))
    assert not caplog.records
    for _ in range(2):
        LocalCompute().submit(replace(_spec(command="true"), gpus=1))
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "OUTERLOOP_LOCAL_GPUS" in warnings[0].message
    assert "CUDA_VISIBLE_DEVICES" in warnings[0].message


def test_stale_waiter_dropped_and_heartbeat_refreshed(gpu_root, monkeypatch):
    from outerloop.compute import _LocalTasks

    lc = LocalCompute()
    tasks = _LocalTasks()
    with lc._pool() as pool:
        pool["queue"] = [
            {"id": "stale", "pid": os.getpid(), "heartbeat": time.time() - 31},
            {"id": "live", "pid": os.getpid(), "heartbeat": time.time()},
        ]
    polls = []

    def poll(seconds):
        assert seconds == 2
        with lc._pool() as pool:
            assert [w["id"] for w in pool["queue"]] == ["live", "next"]
            heartbeat = pool["queue"][1]["heartbeat"]
            polls.append(heartbeat)
            if len(polls) == 1:
                pool["queue"][1]["heartbeat"] = time.time() - 10
            else:
                assert heartbeat >= polls[0]
                pool["queue"][0]["heartbeat"] = time.time() - 31

    monkeypatch.setattr(tasks.cancelled, "wait", poll)
    proc = lc._allocate(
        replace(_spec(command="true"), gpus=1),
        "next",
        1,
        lambda allocated: subprocess.Popen(["true"], start_new_session=True, text=True),
        tasks,
    )
    assert proc is not None
    proc.wait()
    lc._release("next")
    assert len(polls) == 2
    assert _read_pool(gpu_root) == {"holders": {}, "queue": []}


@pytest.mark.parametrize("current, live", [("old", True), ("new", False), ("", True)])
def test_holder_start_time_identity(monkeypatch, current, live):
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: None)
    monkeypatch.setattr(LocalCompute, "_start_time", lambda pid: current)
    assert LocalCompute._live({"id": "1", "pgid": 123, "name": "job", "start_time": "old"}) is live


def test_process_start_time(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        Path, "read_text", lambda self: "123 (a tricky ) name) " + " ".join(map(str, range(3, 23)))
    )
    assert LocalCompute._start_time(123) == "22"
    monkeypatch.setattr(sys, "platform", "darwin")

    def ps(argv, **kwargs):
        assert argv == ["ps", "-o", "lstart=", "-p", "123"]
        return subprocess.CompletedProcess(argv, 0, "  start time\n", "")

    monkeypatch.setattr(subprocess, "run", ps)
    assert LocalCompute._start_time(123) == "start time"

    def missing(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", missing)
    assert LocalCompute._start_time(123) == ""


def test_background_child_retains_gpu_after_shell_exits(gpu_root):
    import signal

    pidfile = gpu_root / "child"
    lc = LocalCompute()
    job_id = lc.submit(
        replace(_spec(command=f"sleep 30 >/dev/null 2>&1 & echo $! > {pidfile}"), gpus=2)
    )
    holders = _read_pool(gpu_root)["holders"]
    try:
        assert lc.status(job_id) == "COMPLETED"
        assert len(holders) == 2
        assert all("start_time" in holder for holder in holders.values())
        assert lc.active_job_names() == ["t"]
        output = gpu_root / "next"
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(lc.submit, replace(_spec(command=f"touch {output}"), gpus=1))
            try:
                assert wait_until(lambda: bool(_read_pool(gpu_root)["queue"]))
                assert not output.exists()
            finally:
                os.killpg(holders["0"]["pgid"], signal.SIGKILL)
            assert lc.status(future.result(timeout=10)) == "COMPLETED"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(int(pidfile.read_text()), signal.SIGKILL)
