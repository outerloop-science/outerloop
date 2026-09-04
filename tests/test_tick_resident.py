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
echo "$@" >> "{shimlog}/uv"
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
    assert "shim changed; successor 501 replaced by 502" in log
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


def _resident_env(home: Path, root: Path, bindir: Path, **extra: str) -> dict[str, str]:
    return _env(
        home,
        root,
        bindir,
        AUTORESEARCH_RESIDENT="1",
        AUTORESEARCH_RESIDENT_CADENCE_S="1",
        AUTORESEARCH_RESIDENT_MINUTES="360",
        SLURM_JOB_ID="42",
        **extra,
    )


def test_a_shim_change_on_the_first_deploy_replaces_the_successor(tmp_path: Path) -> None:
    """The successor is queued before the first deploy; a deploy that changes
    the shim on that very iteration must still replace it (r1)."""
    home, root, bindir, shimlog = _install(tmp_path)
    # the deploy's `uv sync` edits the shim (a deploy that pulled a new shim);
    # the fake tick pauses on its first call
    (bindir / "uv").write_text(
        f"""#!/bin/sh
case "$1" in
  sync) echo "# shim edited by the first deploy" >> "{home}/scripts/tick_chain.sbatch"; exit 0 ;;
  run) echo tick >> "{shimlog}/ticks"; touch "{root}/PAUSE"; exit 0 ;;
esac
"""
    )
    proc = _run_chain(home, _resident_env(home, root, bindir))
    assert proc.returncode == 0, proc.stderr
    sbatch = (shimlog / "sbatch").read_text().splitlines()
    assert len(sbatch) == 2  # initial + the replacement, before the first tick
    assert (shimlog / "scancel").read_text().split() == ["501", "502"]  # stale, then pause
    log = next(root.joinpath("logs").glob("tick-*.log")).read_text()
    assert "shim changed; successor 501 replaced by 502" in log
    assert log.index("replaced by 502") < log.index("(resident)")


def test_a_refused_cancellation_keeps_the_stale_successor_and_drops_the_replacement(
    tmp_path: Path,
) -> None:
    """Never two successors, never none: if Slurm refuses to cancel the stale
    successor, the replacement is withdrawn and the stale one kept (r1)."""
    home, root, bindir, shimlog = _install(tmp_path)
    (bindir / "scancel").write_text(
        f'#!/bin/sh\ncase "$1" in 501) exit 1 ;; esac\necho "$1" >> "{shimlog}/scancel"\n'
    )
    proc = _run_chain(home, _resident_env(home, root, bindir))
    assert proc.returncode == 0, proc.stderr
    sbatch = (shimlog / "sbatch").read_text().splitlines()
    assert len(sbatch) == 2  # initial + one replacement attempt
    # the replacement (502) was withdrawn; at PAUSE the stale 501 is cancelled
    # (refused again by the shim, so it does not appear in the log)
    assert (shimlog / "scancel").read_text().split() == ["502"]
    log = next(root.joinpath("logs").glob("tick-*.log")).read_text()
    assert "could not cancel stale successor 501; keeping it" in log
    assert "cancelling successor 501 and exiting" in log


def test_the_walltime_margin_hands_over_with_a_successor_and_never_sleeps_past_it(
    tmp_path: Path,
) -> None:
    """Inside the margin the loop queues a successor if it has none and
    exits at once — no tick, no sleep (r1)."""
    from datetime import datetime, timedelta

    home, root, bindir, shimlog = _install(tmp_path)
    soon = (datetime.now() + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
    (bindir / "scontrol").write_text(
        f'#!/bin/sh\necho "JobId=42 EndTime={soon} JobState=RUNNING"\n'
    )
    proc = _run_chain(home, _resident_env(home, root, bindir))
    assert proc.returncode == 0, proc.stderr
    assert not (shimlog / "ticks").exists()
    assert len((shimlog / "sbatch").read_text().splitlines()) == 1
    log = next(root.joinpath("logs").glob("tick-*.log")).read_text()
    assert "successor 501 queued at handover" in log
    assert "walltime margin reached; handing over to successor 501" in log


def test_pause_wins_over_the_walltime_margin(tmp_path: Path) -> None:
    """PAUSE set while the loop slept up to the margin: the handover branch
    must not run — the successor is cancelled and nothing is queued (r2)."""
    from datetime import datetime, timedelta

    home, root, bindir, shimlog = _install(tmp_path)
    soon = (datetime.now() + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
    (bindir / "scontrol").write_text(
        f'#!/bin/sh\necho "JobId=42 EndTime={soon} JobState=RUNNING"\n'
    )
    (root / "PAUSE").touch()
    proc = _run_chain(home, _resident_env(home, root, bindir))
    assert proc.returncode == 0, proc.stderr
    assert not (shimlog / "sbatch").exists() and not (shimlog / "ticks").exists()
    log = next(root.joinpath("logs").glob("tick-*.log")).read_text()
    assert "pause sentinel present" in log and "handing over" not in log


def test_the_handover_keeps_retrying_a_failed_submission_through_the_margin(tmp_path: Path) -> None:
    """A scheduler outage inside the margin that clears before walltime still
    yields a successor: the handover retries instead of giving up (r3)."""
    from datetime import datetime, timedelta

    home, root, bindir, shimlog = _install(tmp_path)
    soon = (datetime.now() + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
    (bindir / "scontrol").write_text(
        f'#!/bin/sh\necho "JobId=42 EndTime={soon} JobState=RUNNING"\n'
    )
    # sbatch fails its first four calls (the loop's 3 attempts + 1), then works
    (bindir / "sbatch").write_text(
        f"""#!/bin/sh
echo "$@" >> "{shimlog}/sbatch"
n=$(wc -l < "{shimlog}/sbatch" | tr -d " ")
[ "$n" -le 4 ] && exit 1
echo "$((500 + n))"
"""
    )
    proc = _run_chain(
        home, _resident_env(home, root, bindir, AUTORESEARCH_RESIDENT_RETRY_S="0"), timeout=120
    )
    assert proc.returncode == 0, proc.stderr
    assert len((shimlog / "sbatch").read_text().splitlines()) == 5
    log = next(root.joinpath("logs").glob("tick-*.log")).read_text()
    assert "submit failed at handover; retrying" in log
    assert "handing over to successor 505" in log


def test_a_misconfigured_resident_start_fails_loudly_before_queuing_anything(
    tmp_path: Path,
) -> None:
    home, root, bindir, shimlog = _install(tmp_path)
    env = _resident_env(home, root, bindir)
    env.pop("AUTORESEARCH_ROOT")
    proc = _run_chain(home, env)
    assert proc.returncode == 1
    assert "resident tick misconfigured; missing: AUTORESEARCH_ROOT" in proc.stderr
    assert not (shimlog / "sbatch").exists() and not (shimlog / "ticks").exists()


def test_the_tick_runs_the_installed_environment_without_resyncing(tmp_path: Path) -> None:
    """Every tick invocation is `uv run --no-sync ...`: the deploy step owns
    syncing, so a sync that fails (a full quota, 2026-09-03) cannot stop the
    tick from starting on the environment that is installed."""
    home, root, bindir, shimlog = _install(tmp_path)
    proc = _run_chain(home, _resident_env(home, root, bindir))
    assert proc.returncode == 0, proc.stderr
    runs = [ln for ln in (shimlog / "uv").read_text().splitlines() if ln.startswith("run ")]
    assert runs, "no tick was run"
    assert all(ln.startswith("run --no-sync python -m outerloop.tick") for ln in runs), runs


def test_a_failed_sync_rolls_back_when_dependencies_changed(tmp_path: Path) -> None:
    """A merge that changes uv.lock whose sync fails: the checkout returns to
    the previous commit and that commit's environment is reinstalled, so the
    `--no-sync` tick never runs new source against a half-synced venv."""
    home = tmp_path / "home"
    (home / "scripts").mkdir(parents=True)
    for name in ("tick_deploy.sh", "sweep_git_locks.sh"):
        (home / "scripts" / name).write_text((ROOT / "scripts" / name).read_text())
    root = tmp_path / "root"
    root.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "gitlog"
    pat = tmp_path / "pat"
    pat.write_text("token\n")
    # locks DIFFER between OLD and HEAD -> the deploy must roll back
    (bindir / "git").write_text(
        f"""#!/bin/sh
echo "$@" >> "{log}"
case "$*" in
  *"rev-parse OLD:uv.lock"*) echo lockOLD ;;
  *"rev-parse HEAD:uv.lock"*) echo lockNEW ;;
  *"rev-parse HEAD"*) if [ -f "{tmp_path}/reset" ]; then echo NEW; else echo OLD; fi ;;
  *"reset --hard --quiet FETCH_HEAD"*) touch "{tmp_path}/reset" ;;
  *"reset --hard --quiet OLD"*) rm -f "{tmp_path}/reset" ;;
esac
exit 0
"""
    )
    syncs = tmp_path / "syncs"
    (bindir / "uv").write_text(
        f'#!/bin/sh\ncase "$1" in sync) echo x >> "{syncs}"; '
        f'[ "$(wc -l < "{syncs}" | tr -d " ")" -eq 1 ] && '
        '{ echo "error: Disk quota exceeded" >&2; exit 2; } ;; esac\n'
        "exit 0\n"
    )
    for p in bindir.iterdir():
        os.chmod(p, 0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "AUTORESEARCH_HOME": str(home),
        "AUTORESEARCH_ROOT": str(root),
        "AUTORESEARCH_PAT_FILE": str(pat),
        "HOME": str(tmp_path),
    }
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'. "{home}/scripts/tick_deploy.sh"; echo "BROKEN=$AUTORESEARCH_DEPLOY_BROKEN"',
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "back on OLD with its environment" in proc.stdout
    assert "BROKEN=\n" in proc.stdout  # consistent again: the tick runs
    gitlog = log.read_text()
    assert "reset --hard --quiet OLD" in gitlog
    assert syncs.read_text().count("x") == 2  # NEW's sync, then OLD's


def test_a_failed_rollback_marks_the_deploy_broken_so_no_tick_runs(tmp_path: Path) -> None:
    """When the sync fails AND the reset back to the previous commit fails,
    the deploy exports AUTORESEARCH_DEPLOY_BROKEN=1 and the tick callers skip
    the tick: new source must never run against the old environment."""
    home = tmp_path / "home"
    (home / "scripts").mkdir(parents=True)
    for name in ("tick_deploy.sh", "sweep_git_locks.sh"):
        (home / "scripts" / name).write_text((ROOT / "scripts" / name).read_text())
    root = tmp_path / "root"
    root.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    pat = tmp_path / "pat"
    pat.write_text("token\n")
    (bindir / "git").write_text(
        f"""#!/bin/sh
case "$*" in
  *"rev-parse OLD:uv.lock"*) echo lockOLD ;;
  *"rev-parse HEAD:uv.lock"*) echo lockNEW ;;
  *"rev-parse HEAD"*) if [ -f "{tmp_path}/reset" ]; then echo NEW; else echo OLD; fi ;;
  *"reset --hard --quiet FETCH_HEAD"*) touch "{tmp_path}/reset" ;;
  *"reset --hard --quiet OLD"*) exit 1 ;;
esac
exit 0
"""
    )
    (bindir / "uv").write_text('#!/bin/sh\ncase "$1" in sync) exit 2 ;; esac\nexit 0\n')
    for p in bindir.iterdir():
        os.chmod(p, 0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "AUTORESEARCH_HOME": str(home),
        "AUTORESEARCH_ROOT": str(root),
        "AUTORESEARCH_PAT_FILE": str(pat),
        "HOME": str(tmp_path),
    }
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'. "{home}/scripts/tick_deploy.sh"; echo "BROKEN=$AUTORESEARCH_DEPLOY_BROKEN"',
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "environment could not be made consistent; tick skipped" in proc.stdout
    assert "BROKEN=1" in proc.stdout
    # and the chain honours it: no tick, successors untouched, clean exit
    shim = (ROOT / "scripts" / "tick_chain.sbatch").read_text()
    assert 'AUTORESEARCH_DEPLOY_BROKEN:-}" = "1"' in shim and "tick skipped" in shim
    resident = (ROOT / "scripts" / "tick_resident.sh").read_text()
    assert 'AUTORESEARCH_DEPLOY_BROKEN:-}" = "1"' in resident and "tick skipped" in resident


def test_a_failed_sync_with_unchanged_lock_keeps_ticking_on_the_current_env(tmp_path: Path) -> None:
    """The 2026-09-03 quota case: a new commit whose uv.lock is identical to
    the previous one, sync fails on the full disk. The installed environment
    already satisfies the new code, so the deploy neither rolls back nor
    marks itself broken — the tick runs."""
    home = tmp_path / "home"
    (home / "scripts").mkdir(parents=True)
    for name in ("tick_deploy.sh", "sweep_git_locks.sh"):
        (home / "scripts" / name).write_text((ROOT / "scripts" / name).read_text())
    root = tmp_path / "root"
    root.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "gitlog"
    pat = tmp_path / "pat"
    pat.write_text("token\n")
    (bindir / "git").write_text(
        f"""#!/bin/sh
echo "$@" >> "{log}"
case "$*" in
  *"rev-parse OLD:uv.lock"*) echo samelock ;;
  *"rev-parse HEAD:uv.lock"*) echo samelock ;;
  *"rev-parse HEAD"*) if [ -f "{tmp_path}/reset" ]; then echo NEW; else echo OLD; fi ;;
  *"reset --hard --quiet FETCH_HEAD"*) touch "{tmp_path}/reset" ;;
esac
exit 0
"""
    )
    (bindir / "uv").write_text('#!/bin/sh\ncase "$1" in sync) exit 2 ;; esac\nexit 0\n')
    for p in bindir.iterdir():
        os.chmod(p, 0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "AUTORESEARCH_HOME": str(home),
        "AUTORESEARCH_ROOT": str(root),
        "AUTORESEARCH_PAT_FILE": str(pat),
        "HOME": str(tmp_path),
    }
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'. "{home}/scripts/tick_deploy.sh"; echo "BROKEN=$AUTORESEARCH_DEPLOY_BROKEN"',
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "uv.lock is unchanged; running new code on the current environment" in proc.stdout
    assert "BROKEN=\n" in proc.stdout
    assert "reset --hard --quiet OLD" not in log.read_text()  # no rollback: HEAD kept
