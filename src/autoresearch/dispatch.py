"""The dispatched-eval primitive: an eval as its own Slurm job.

Phase 1 of docs/design/dispatcher.md. This module owns the three pieces the
design specifies, and nothing else (the resumable climb transaction and the
wake wiring build on these in the next stage):

  * snapshot_tree  — a committable snapshot of a dirty workspace, taken
    against a TEMPORARY index seeded from the base commit, so the working
    index is never touched and tracked-but-ignored files behave exactly as
    they do in the drift fingerprint.
  * write_eval_job — the orchestrator-authored job script: materialize a
    worktree of the snapshot, run the contract command under the SAME jail
    as the in-job evaluator, capture stdout OUTSIDE the containment into
    the run directory (the jailed process never sees the run dir).
  * read_eval_result — the wake side: exit code + the same last-JSON-line
    metric contract the in-job evaluator parses.

Dispatch is chosen per benchmark: `eval_minutes` is a contract HINT with
its own code-side ceiling; under the in-job threshold nothing here runs.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shlex
import subprocess
from pathlib import Path

from autoresearch.compute import JobSpec
from autoresearch.github import Workspace
from autoresearch.orchestrator import EvalError, _metric_from_output

log = logging.getLogger(__name__)

# In-job evals must fit the climb job's overhead runway (a few minutes each,
# see limits.CLIMB_OVERHEAD_MINUTES); anything longer is dispatched.
IN_JOB_EVAL_MINUTES = 5
# Ceiling for the contract's eval_minutes hint: OUR spend cap, not the
# target's to raise (same grammar as every budget ceiling).
EVAL_JOB_MINUTES_CEILING = 240
# Slack added to the job walltime beyond the eval itself: worktree
# materialization + venv build from the lockfile on node-local scratch.
EVAL_JOB_SETUP_MINUTES = 10


def effective_eval_minutes(eval_minutes: int | None) -> int:
    """The contract hint clamped into [1, ceiling]; None means in-job."""
    if eval_minutes is None:
        return 0
    return max(1, min(int(eval_minutes), EVAL_JOB_MINUTES_CEILING))


def should_dispatch(eval_minutes: int | None) -> bool:
    return effective_eval_minutes(eval_minutes) > IN_JOB_EVAL_MINUTES


def snapshot_tree(ws: Workspace, base_sha: str) -> tuple[str, str]:
    """Snapshot the workspace's current CONTENT as a commit parented on
    `base_sha`, without touching the working index.

    Returns (commit_sha, tree_hash). The tree hash is the drift fingerprint
    (write-tree is deterministic for identical trees — what climb.py already
    compares); the commit exists only so a worktree can materialize it.
    """
    index = Path(ws.root) / ".git" / f"dispatch-index-{base_sha[:12]}"
    env = {"GIT_INDEX_FILE": str(index)}
    git = ["git", "-C", str(ws.root)]
    try:
        # seed from the base so ignore rules apply as they do to a populated
        # index (a fresh empty index would drop tracked-but-ignored files)
        subprocess.run(
            [*git, "read-tree", base_sha],
            env=_git_env(env),
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        subprocess.run(
            [*git, "add", "-A"],
            env=_git_env(env),
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        tree = subprocess.run(
            [*git, "write-tree"],
            env=_git_env(env),
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout.strip()
        commit = subprocess.run(
            [*git, "commit-tree", tree, "-p", base_sha, "-m", "dispatch snapshot"],
            env=_git_env(env),
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout.strip()
        return commit, tree
    except subprocess.CalledProcessError as exc:
        raise EvalError(f"snapshot failed: {exc.stderr.strip()[:300]}") from exc
    finally:
        index.unlink(missing_ok=True)


def _git_env(extra: dict[str, str]) -> dict[str, str]:
    import os

    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
    env.update(extra)
    return env


def write_eval_job(
    run_dir: Path,
    name: str,
    *,
    repo_root: Path,
    snapshot_sha: str,
    command: str,
    image: str,
    apptainer_binary: str = "apptainer",
    extra_env: dict[str, str] | None = None,
) -> Path:
    """Write the orchestrator-authored job script for one dispatched eval.

    Trust layout, matching the in-job evaluator exactly:
      * the contract COMMAND goes into its own file, read back with
        `sh -c "$(cat ...)"` INSIDE the jail — it never crosses sbatch or
        shell quoting, and it executes only inside apptainer
        --containall/--cleanenv with worktree-only binds;
      * stdout/stderr are captured OUTSIDE the containment into the run
        directory (the jailed process never sees the run dir);
      * env is the evaluator's allowlist shape: uv cache + private venv on
        node-local scratch, plus the call site's extra_env (paired seeds)
        exported as APPTAINERENV_*.
    Returns the script path; the caller submits it via JobSpec(script=...).
    """
    ev = run_dir / f"eval-{name}"
    ev.mkdir(parents=True, exist_ok=True)
    (ev / "command.txt").write_text(command)
    lines = [
        "#!/bin/sh",
        "set -u",
        f"EV={shlex.quote(str(ev))}",
        f"REPO={shlex.quote(str(repo_root))}",
        'WT="$EV/worktree"',
        'SCRATCH="${SLURM_TMPDIR:-${TMPDIR:-/tmp}}/dispatch-eval-$$"',
        'mkdir -p "$SCRATCH/cache" "$SCRATCH/home"',
        # worktree of the snapshot: detached, disposable, removed on exit
        f'git -C "$REPO" worktree add --detach "$WT" {shlex.quote(snapshot_sha)} '
        '>> "$EV/setup.log" 2>&1 || { echo 97 > "$EV/exit-code"; exit 0; }',
        'trap \'git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1; '
        'rm -rf "$SCRATCH"\' EXIT',
        'export UV_CACHE_DIR="$SCRATCH/cache" UV_LINK_MODE=copy '
        'UV_PROJECT_ENVIRONMENT="$SCRATCH/cache/venv"',
        'export APPTAINERENV_UV_CACHE_DIR="$UV_CACHE_DIR" '
        "APPTAINERENV_UV_LINK_MODE=copy "
        'APPTAINERENV_UV_PROJECT_ENVIRONMENT="$UV_PROJECT_ENVIRONMENT"',
    ]
    for key, value in (extra_env or {}).items():
        lines.append(f"export {key}={shlex.quote(value)} APPTAINERENV_{key}={shlex.quote(value)}")
    lines += [
        # the jail: identical flags to SubprocessEvaluator._run; stdout is
        # redirected OUTSIDE apptainer, so the result lands in the run dir
        # without the jailed process ever seeing it
        f"{shlex.quote(apptainer_binary)} exec --containall --cleanenv "
        '--bind "$WT:$WT" --home "$SCRATCH/home:$SCRATCH/home" '
        '--bind "$SCRATCH/cache:$SCRATCH/cache" --pwd "$WT" '
        f"{shlex.quote(image)} "
        'sh -c "$(cat "$EV/command.txt")" '
        '> "$EV/stdout" 2> "$EV/stderr"',
        'echo $? > "$EV/exit-code"',
        "exit 0",
    ]
    script = ev / "job.sh"
    script.write_text("\n".join(lines) + "\n")
    script.chmod(0o755)
    return script


def eval_job_spec(
    script: Path,
    *,
    job_name: str,
    account: str,
    partition: str,
    eval_minutes: int,
    cpus: int = 4,
    mem: str = "8G",
    gpus: int = 0,
) -> JobSpec:
    """The JobSpec for one dispatched eval: the hint (already clamped) plus
    setup slack. GPU counts arrive with the phase-3 contract fields; the
    parameter exists so the seam does not change shape then."""
    return JobSpec(
        job_name=job_name[:60],
        account=account,
        partition=partition,
        time_minutes=eval_minutes + EVAL_JOB_SETUP_MINUTES,
        script=str(script),
        cpus=cpus,
        mem=mem,
        gpus=gpus,
    )


def read_eval_result(run_dir: Path, name: str, metric: str) -> float:
    """The wake side of one dispatched eval. Raises EvalError with the same
    semantics as the in-job evaluator: nonzero exit or an unreadable metric
    is an eval failure (outcome `eval-error`, ending `aborted`), and a
    MISSING exit-code file means the job died before the wrapper ran —
    also a failure, never a silent skip."""
    ev = run_dir / f"eval-{name}"
    try:
        code = int((ev / "exit-code").read_text().strip())
    except (OSError, ValueError) as exc:
        raise EvalError(f"dispatched eval {name}: no exit code ({exc})") from exc
    stdout = ""
    with contextlib.suppress(OSError):
        stdout = (ev / "stdout").read_text()
    if code != 0:
        tail = ""
        try:
            tail = (ev / "stderr").read_text()[-300:]
        except OSError:
            pass
        raise EvalError(f"dispatched eval {name} failed ({code}): {tail}")
    value = _metric_from_output(stdout, metric)
    if value is None:
        raise EvalError(f"dispatched eval {name}: no readable {metric!r} in output")
    return value


def result_summary(run_dir: Path, name: str) -> str:
    """One line for reports/logs; never raises."""
    ev = run_dir / f"eval-{name}"
    try:
        code = (ev / "exit-code").read_text().strip()
    except OSError:
        code = "?"
    try:
        last = [ln for ln in (ev / "stdout").read_text().splitlines() if ln.strip()][-1]
    except (OSError, IndexError):
        last = ""
    return f"eval-{name}: exit={code} {last[:160]}"


def parse_result_json(run_dir: Path, name: str) -> dict:
    """The full JSON line (margins, per-encoder blocks) for report embedding;
    empty dict when absent."""
    ev = run_dir / f"eval-{name}"
    try:
        for line in reversed((ev / "stdout").read_text().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    return data
    except OSError:
        pass
    return {}
