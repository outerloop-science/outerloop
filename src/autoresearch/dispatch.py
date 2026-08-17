"""The dispatched-eval primitive: an eval as its own Slurm job.

Phase 1 of docs/design/dispatcher.md. This module owns the three pieces the
design specifies, and nothing else (the resumable climb transaction and the
wake wiring build on these in the next stage):

  * snapshot_tree  — a retained snapshot of a dirty workspace, taken
    against a TEMPORARY unique index seeded from the base commit (working
    index untouched, tracked-vs-ignored parity with the drift fingerprint),
    kept reachable under a unique ref so gc cannot prune it before a queued
    job runs. Release with drop_snapshot after the eval is read.
  * write_eval_job — the orchestrator-authored job script: EXTRACT the
    snapshot's tree (git archive | tar, no worktree metadata to bind or
    reap), run the contract command under the SAME jail as the in-job
    evaluator, capture stdout OUTSIDE the containment into the run
    directory (the jailed process never sees the run dir).
  * read_eval_result — the wake side: exit code + the same last-JSON-line
    metric contract the in-job evaluator parses.

Dispatch is chosen per benchmark: `eval_minutes` is a contract HINT with
its own code-side ceiling; under the in-job threshold nothing here runs.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from autoresearch.compute import JobSpec
from autoresearch.github import SAFE_GIT_FLAGS, Workspace
from autoresearch.orchestrator import EvalError, _metric_from_output, managed_eval_env

log = logging.getLogger(__name__)

# extra_env keys are exported unquoted into the job script; keep them to a
# shell identifier shape (the values ARE shlex-quoted).
_SHELL_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

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


@dataclass(frozen=True)
class Snapshot:
    """A retained snapshot of a dirty workspace. `ref` keeps the commit
    reachable so gc cannot prune it while a queued job still needs it;
    `tree` is the drift fingerprint. Release the ref via `drop_snapshot`
    once the eval has been read."""

    commit: str
    tree: str
    ref: str


def snapshot_tree(ws: Workspace, base_sha: str) -> Snapshot:
    """Snapshot the workspace's current CONTENT as a commit parented on
    `base_sha`, without touching the working index, and retain it under a
    unique ref so gc cannot prune it before a queued job materializes it.
    """
    # Unique per snapshot: two snapshots against the SAME base run
    # concurrently (the design's paired baseline/candidate fan-out) and must
    # not collide on one index file.
    token = uuid4().hex
    index = Path(ws.root) / ".git" / f"dispatch-index-{token}"
    ref = f"refs/dispatch/{token}"
    env = {"GIT_INDEX_FILE": str(index)}
    # The workspace is AGENT-WRITTEN: every git call carries SAFE_GIT_FLAGS
    # (hooks/fsmonitor/ssh/credential neutralizers), drops HOME (no
    # ~/.gitconfig read), and disables filters. git -c takes no globs, so a
    # session-defined clean filter is neutralized by ENUMERATING the
    # configured filter drivers and overriding each to a passthrough (`cat`
    # is byte-for-byte; empty would error). The overrides ride EVERY index op
    # — the clean filter runs lazily at write-tree, not add.
    base_git = ["git", "-C", str(ws.root), *SAFE_GIT_FLAGS]
    no_filters: list[str] = ["-c", "core.attributesFile=/dev/null"]
    try:
        listing = subprocess.run(
            [*base_git, "config", "--get-regexp", r"^filter\..*\.(clean|smudge|process)$"],
            env=_git_env(env),
            capture_output=True,
            text=True,
            timeout=30,
        )
        for line in listing.stdout.splitlines():
            key = line.split(" ", 1)[0]  # filter.<driver>.<op>; <driver> may contain dots
            if not key.startswith("filter.") or "." not in key[7:]:
                continue  # a wrapped config value on its own line — skip
            driver = key[len("filter.") : key.rindex(".")]  # strip prefix AND the .<op> suffix
            no_filters += [
                "-c",
                f"filter.{driver}.clean=cat",
                "-c",
                f"filter.{driver}.smudge=cat",
                "-c",
                f"filter.{driver}.process=",
            ]
        git = [*base_git, *no_filters]  # every index op carries the neutralizers

        def run(args: list[str], timeout: int) -> str:
            return subprocess.run(
                args, env=_git_env(env), check=True, capture_output=True, text=True, timeout=timeout
            ).stdout.strip()

        # seed from the base so ignore rules apply as they do to a populated
        # index (a fresh empty index would drop tracked-but-ignored files)
        run([*git, "read-tree", base_sha], 60)
        run([*git, "add", "-A"], 120)
        tree = run([*git, "write-tree"], 60)
        commit = run(
            [
                *git,
                "-c",
                "user.name=dispatch",
                "-c",
                "user.email=dispatch@localhost",
                "commit-tree",
                tree,
                "-p",
                base_sha,
                "-m",
                "dispatch snapshot",
            ],
            60,
        )
        # retain: an unreachable commit-tree object can be pruned by gc while
        # the eval job is still queued
        run([*base_git, "update-ref", ref, commit], 30)
        return Snapshot(commit=commit, tree=tree, ref=ref)
    except subprocess.CalledProcessError as exc:
        raise EvalError(f"snapshot failed: {exc.stderr.strip()[:300]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise EvalError(f"snapshot timed out: {exc}") from exc
    finally:
        index.unlink(missing_ok=True)


def drop_snapshot(ws: Workspace, snapshot: Snapshot) -> None:
    """Release the retaining ref (best-effort; the commit becomes gc-eligible
    again). Called once the eval result has been read."""
    with contextlib.suppress(Exception):
        subprocess.run(
            ["git", "-C", str(ws.root), *SAFE_GIT_FLAGS, "update-ref", "-d", snapshot.ref],
            env=_git_env({}),
            capture_output=True,
            text=True,
            timeout=30,
        )


def _git_env(extra: dict[str, str]) -> dict[str, str]:
    import os

    # no HOME: git must not read the orchestrator user's ~/.gitconfig while
    # operating on an agent-written tree
    env = {k: os.environ[k] for k in ("PATH", "LANG") if k in os.environ}
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
    # a resubmitted eval must never be read as its predecessor: every prior
    # artifact — including a leftover extracted tree — goes before submission
    for stale in ("exit-code", "stdout", "stderr", "setup.log"):
        (ev / stale).unlink(missing_ok=True)
    shutil.rmtree(ev / "tree", ignore_errors=True)
    (ev / "command.txt").write_text(command)
    # extra_env matches the in-job evaluator's contract: managed keys (HOME,
    # UV_*, PATH...) are DROPPED, never allowed to override the isolation, and
    # keys must be shell-identifier shaped (they are exported unquoted).
    injected = {
        k: v
        for k, v in (extra_env or {}).items()
        if _SHELL_IDENT.match(k) and not managed_eval_env(k)
    }
    safe_git = " ".join(shlex.quote(f) for f in SAFE_GIT_FLAGS)
    lines = [
        "#!/bin/sh",
        "set -u",
        f"EV={shlex.quote(str(ev))}",
        f"REPO={shlex.quote(str(repo_root))}",
        'TREE="$EV/tree"',
        'SCRATCH="${SLURM_TMPDIR:-${TMPDIR:-/tmp}}/dispatch-eval-$$"',
        'mkdir -p "$SCRATCH/cache" "$SCRATCH/home" "$TREE"',
        # Materialize the snapshot by EXTRACTING its tree (git archive | tar),
        # not `git worktree add`: a worktree's .git gitfile points back into
        # $REPO/.git/worktrees, which the jail does not bind (so in-jail git
        # would break), and it leaves an admin entry to reap. An extracted
        # tree is a plain directory — nothing to bind back, nothing to clean
        # in $REPO. SAFE_GIT_FLAGS still ride the archive (agent-written repo).
        f'git -C "$REPO" {safe_git} archive --format=tar {shlex.quote(snapshot_sha)} '
        '| tar -x -C "$TREE" >> "$EV/setup.log" 2>&1 '
        '|| { echo 97 > "$EV/exit-code"; exit 0; }',
        'cleanup() { rm -rf "$SCRATCH"; }',
        # TERM/INT/HUP too: Slurm's walltime kill is SIGTERM, and a POSIX
        # shell dying on a signal does not run its EXIT trap
        "trap 'cleanup' EXIT",
        "trap 'echo 143 > \"$EV/exit-code\"; cleanup; trap - EXIT; exit 0' TERM INT HUP",
        'export UV_CACHE_DIR="$SCRATCH/cache" UV_LINK_MODE=copy '
        'UV_PROJECT_ENVIRONMENT="$SCRATCH/cache/venv"',
        'export APPTAINERENV_UV_CACHE_DIR="$UV_CACHE_DIR" '
        "APPTAINERENV_UV_LINK_MODE=copy "
        'APPTAINERENV_UV_PROJECT_ENVIRONMENT="$UV_PROJECT_ENVIRONMENT"',
    ]
    for key, value in injected.items():
        lines.append(f"export {key}={shlex.quote(value)} APPTAINERENV_{key}={shlex.quote(value)}")
    lines += [
        # the jail: identical flags to SubprocessEvaluator._run; stdout is
        # redirected OUTSIDE apptainer, so the result lands in the run dir
        # without the jailed process ever seeing it
        f"{shlex.quote(apptainer_binary)} exec --containall --cleanenv "
        '--bind "$TREE:$TREE" --home "$SCRATCH/home:$SCRATCH/home" '
        '--bind "$SCRATCH/cache:$SCRATCH/cache" --pwd "$TREE" '
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
    with contextlib.suppress(OSError, ValueError):
        stdout = (ev / "stdout").read_text(errors="replace")
    if code != 0:
        tail = ""
        with contextlib.suppress(OSError, ValueError):
            tail = (ev / "stderr").read_text(errors="replace")[-300:]
        raise EvalError(f"dispatched eval {name} failed ({code}): {tail}")
    value = _metric_from_output(stdout, metric)
    if value is None:
        raise EvalError(f"dispatched eval {name}: no readable {metric!r} in output")
    if not math.isfinite(value):
        # same rule as the in-job evaluator: json parses bare NaN/Infinity,
        # and a NaN score entering a comparison is worse than a failure
        raise EvalError(f"dispatched eval {name}: non-finite {metric!r} ({value})")
    return value


def result_summary(run_dir: Path, name: str) -> str:
    """One line for reports/logs; never raises."""
    ev = run_dir / f"eval-{name}"
    try:
        code = (ev / "exit-code").read_text(errors="replace").strip()
    except (OSError, ValueError):
        code = "?"
    try:
        last = [
            ln for ln in (ev / "stdout").read_text(errors="replace").splitlines() if ln.strip()
        ][-1]
    except (OSError, ValueError, IndexError):
        last = ""
    return f"eval-{name}: exit={code} {last[:160]}"


def parse_result_json(run_dir: Path, name: str) -> dict:
    """The full JSON line (margins, per-encoder blocks) for report embedding;
    empty dict when absent."""
    ev = run_dir / f"eval-{name}"
    try:
        for line in reversed((ev / "stdout").read_text(errors="replace").splitlines()):
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
