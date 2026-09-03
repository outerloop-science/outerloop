"""The chain end to end with Slurm and uv shimmed on PATH: the per-cadence
path still queues two singleton successors and execs the tick; the resident
path loops deploy → tick → sleep, keeps one afterany:self successor,
resubmits it when the shim changes, and exits clean on the pause sentinel."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _install(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """A fake checkout (scripts/ copied), a state root, a PATH of shims and the
    shims' log directory."""
    home = tmp_path / "home"
    (home / "scripts").mkdir(parents=True)
    for f in ROOT.joinpath("scripts").glob("*"):
        if f.is_file():
            shutil.copy(f, home / "scripts" / f.name)
    root = tmp_path / "root"
    root.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shimlog = tmp_path / "shimlog"
    shimlog.mkdir()
    shims = {
        # sbatch prints a fresh id per call and logs its argv
        "sbatch": (
            f'#!/bin/sh\necho "$@" >> "{shimlog}/sbatch"\n'
            f'n=$(wc -l < "{shimlog}/sbatch" | tr -d " ")\necho "$((500 + n))"\n'
        ),
        "scancel": f'#!/bin/sh\necho "$1" >> "{shimlog}/scancel"\n',
        "squeue": "#!/bin/sh\nexit 0\n",  # nothing of ours queued
        "scontrol": '#!/bin/sh\necho "JobId=42 EndTime=2030-01-01T00:00:00 JobState=RUNNING"\n',
        "timeout": '#!/bin/sh\nshift 2\nexec "$@"\n',  # --kill-after=.. 15m cmd...
        # uv: `sync` is a no-op; `run ... tick` is the fake tick — it counts
        # its calls, appends to the shim on the 2nd call, sets PAUSE on the 3rd
        "uv": f'''#!/bin/sh
case "$1" in
  sync) exit 0 ;;
  run)
    echo "tick $(date +%s)" >> "{shimlog}/ticks"
    n=$(wc -l < "{shimlog}/ticks" | tr -d " ")
    [ "$n" -eq 2 ] && echo "# shim edited by a deploy" >> "{home}/scripts/tick_chain.sbatch"
    [ "$n" -eq 3 ] && touch "{root}/PAUSE"
    exit 0 ;;
esac
exit 0
''',
    }
    for name, body in shims.items():
        p = bindir / name
        p.write_text(body)
        os.chmod(p, 0o755)
    return home, root, bindir, shimlog


def _env(home: Path, root: Path, bindir: Path, **extra: str) -> dict[str, str]:
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "USER": os.environ.get("USER", "u"),
        "AUTORESEARCH_HOME": str(home),
        "AUTORESEARCH_ROOT": str(root),
        "AUTORESEARCH_ACCOUNT": "acct",
        "AUTORESEARCH_PARTITION": "cpu_short",
        "HOME": str(home),  # no ~/.config/autoresearch/.env
    }
    env.pop("AUTORESEARCH_PAT_FILE", None)
    env.pop("AUTORESEARCH_RESIDENT", None)
    env.update(extra)
    return env


def _run_chain(
    home: Path, env: dict[str, str], timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(home / "scripts" / "tick_chain.sbatch")],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_per_cadence_chain_queues_two_successors_and_execs_the_tick(tmp_path: Path) -> None:
    home, root, bindir, shimlog = _install(tmp_path)
    proc = _run_chain(home, _env(home, root, bindir))
    assert proc.returncode == 0, proc.stderr
    sbatch = (shimlog / "sbatch").read_text().splitlines()
    assert len(sbatch) == 2
    for line in sbatch:
        assert "--dependency=singleton" in line and "--begin=" in line
        assert "--partition=cpu_short" in line and "--deadline" not in line
        assert "afterany" not in line
    assert (shimlog / "ticks").read_text().count("tick") == 1
    log = next(root.joinpath("logs").glob("tick-*.log")).read_text()
    assert "=== tick" in log and "(resident)" not in log


def test_resident_loop_keeps_one_successor_resubmits_on_shim_change_and_pauses_clean(
    tmp_path: Path,
) -> None:
    home, root, bindir, shimlog = _install(tmp_path)
    env = _env(
        home,
        root,
        bindir,
        AUTORESEARCH_RESIDENT="1",
        AUTORESEARCH_RESIDENT_CADENCE_S="1",
        AUTORESEARCH_RESIDENT_MINUTES="360",
        SLURM_JOB_ID="42",
    )
    proc = _run_chain(home, env)
    assert proc.returncode == 0, proc.stderr
    ticks = (shimlog / "ticks").read_text().count("tick")
    assert ticks == 3  # ran until the fake tick raised the sentinel
    sbatch = (shimlog / "sbatch").read_text().splitlines()
    # one successor at start, one resubmit after the shim changed — never two queued
    assert len(sbatch) == 2
    for line in sbatch:
        assert "--dependency=afterany:42,singleton" in line
        assert "--time=360" in line and "--job-name=autoresearch-resident" in line
        assert "--export=ALL" in line and "--begin" not in line
    scancel = (shimlog / "scancel").read_text().split()
    assert scancel == ["501", "502"]  # the shim-change resubmit, then the pause
    log = next(root.joinpath("logs").glob("tick-*.log")).read_text()
    assert log.count("(resident)") == 3
    assert "successor 501 queued (afterany:42)" in log
    assert "shim changed; successor resubmitted as 502" in log
    assert "pause sentinel present; cancelling successor 502" in log


def test_per_cadence_chain_drains_when_a_resident_exists(tmp_path: Path) -> None:
    home, root, bindir, shimlog = _install(tmp_path)
    # squeue reports a resident job of ours
    (bindir / "squeue").write_text(
        '#!/bin/sh\ncase "$*" in *autoresearch-resident*) echo "777";; esac\nexit 0\n'
    )
    proc = _run_chain(home, _env(home, root, bindir))
    assert proc.returncode == 0, proc.stderr
    assert not (shimlog / "sbatch").exists()  # no successors queued
    # the top-up runs BEFORE the log redirect (successors first, always), so
    # its note goes to the job's stdout
    assert "a resident tick exists; not queuing successors" in proc.stdout
    assert (shimlog / "ticks").read_text().count("tick") == 1  # this tick still ran
