"""The dispatched-eval primitive: an eval as its own Slurm job.

This module owns the three pieces docs/design/dispatcher.md specifies,
and nothing else:

  * snapshot_tree  — a retained snapshot of a dirty workspace, taken
    against a TEMPORARY unique index seeded from the base commit (working
    index untouched, tracked-vs-ignored parity with the drift fingerprint),
    kept reachable under a unique ref so gc cannot prune it before a queued
    job runs. Release with drop_snapshot after the eval is read.
  * write_eval_job — the orchestrator-authored job script: materialize the
    snapshot by CHECKOUT (git worktree add, then delete the .git gitfile so
    the jail sees a plain faithful directory — checkout keeps .gitattributes
    and applies no export processing), run the contract command under the
    SAME jail as the in-job evaluator, capture stdout OUTSIDE the containment
    into the run directory (the jailed process never sees the run dir).
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
# see limits.ATTEMPT_OVERHEAD_MINUTES); anything longer is dispatched.
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


def afterany_ids(afterany: str) -> list[str]:
    """The job ids inside an ``afterany:<id>:<id>...`` dependency string, or
    ``[]`` when there are none (a blind park carries an empty dependency)."""
    return afterany.split(":")[1:] if afterany else []


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
    # ~/.gitconfig read), and disables agent-defined filters via
    # _filter_neutral_env (GIT_CONFIG_* env, which handles driver names with
    # '=' or dots that `-c` cannot). The neutralizers ride EVERY index op
    # because the clean filter runs lazily at write-tree, not add.
    base_git = ["git", "-C", str(ws.root), *SAFE_GIT_FLAGS]
    try:
        # Neutralize agent-defined filters via GIT_CONFIG_KEY_n/VALUE_n env
        # injection, NOT `-c`: a driver name containing '=' (a legal git
        # subsection char) defeats `-c filter.<driver>.clean=cat` because git
        # splits `-c` at the FIRST '='. The env form takes key and value as
        # SEPARATE strings, immune to that — and to dots. See _filter_neutral_env.
        neutral = _filter_neutral_env(base_git, env)
        run_env = {**env, **neutral}

        def run(args: list[str], timeout: int) -> str:
            return subprocess.run(
                args,
                env=_git_env(run_env),
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            ).stdout.strip()

        git = base_git  # neutralizers now ride the ENV, not argv

        # seed from the base so ignore rules apply as they do to a populated
        # index (a fresh empty index would drop tracked-but-ignored files)
        run([*git, "read-tree", base_sha], 60)
        run([*git, "add", "-A"], 120)
        # .gitattributes are KEPT: the job materializes the tree by CHECKOUT
        # (git worktree), which reproduces content faithfully — including
        # .gitattributes — and does NOT apply export-ignore/export-subst
        # (those are `git archive` only). So fidelity and integrity hold
        # together; the filter side is already neutralized via GIT_CONFIG env.
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
        # the index and any .lock a timed-out git left beside it (unique per
        # token, so it never blocks a future snapshot — just tidiness)
        index.unlink(missing_ok=True)
        index.with_name(index.name + ".lock").unlink(missing_ok=True)


def _filter_neutral_env(base_git: list[str], env: dict[str, str]) -> dict[str, str]:
    """GIT_CONFIG_* env that overrides every configured filter driver to a
    passthrough and disables attribute files. Robust where `-c` is not:
    keys/values are separate env vars, so a driver name with '=' or dots is
    handled correctly. Used by BOTH the snapshot (index ops) and the job
    script's archive — the repo config and .gitattributes are agent-written
    on both paths."""
    pairs: list[tuple[str, str]] = [("core.attributesFile", "/dev/null")]
    listing = subprocess.run(
        [*base_git, "config", "-z", "--get-regexp", r"^filter\..*\.(clean|smudge|process)$"],
        env=_git_env(env),
        capture_output=True,
        text=True,
        timeout=30,
    )
    # -z: NUL-separated records, each "key\nvalue" — so a value containing a
    # newline can never masquerade as a second record.
    for record in listing.stdout.split("\0"):
        key = record.split("\n", 1)[0]
        if not key.startswith("filter.") or "." not in key[len("filter.") :]:
            continue
        driver = key[len("filter.") : key.rindex(".")]
        pairs += [
            (f"filter.{driver}.clean", "cat"),
            (f"filter.{driver}.smudge", "cat"),
            (f"filter.{driver}.process", ""),
        ]
    out = {"GIT_CONFIG_COUNT": str(len(pairs))}
    for i, (k, v) in enumerate(pairs):
        out[f"GIT_CONFIG_KEY_{i}"] = k
        out[f"GIT_CONFIG_VALUE_{i}"] = v
    return out


def drop_snapshot(ws: Workspace, snapshot: Snapshot) -> None:
    """Release the retaining ref (the commit becomes gc-eligible again). Called
    once the eval result has been read. Best-effort — it never RAISES, so a
    caller's ending sequence cannot hinge on it — but not SILENT: a ref that
    fails to drop keeps its commit alive forever, so the failure is logged."""
    try:
        result = subprocess.run(
            ["git", "-C", str(ws.root), *SAFE_GIT_FLAGS, "update-ref", "-d", snapshot.ref],
            env=_git_env({}),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            log.warning("snapshot ref drop failed for %s: %s", snapshot.ref, result.stderr[:200])
    except Exception as exc:
        log.warning("snapshot ref drop errored for %s: %s", snapshot.ref, exc)


def _git_env(extra: dict[str, str]) -> dict[str, str]:
    import os

    # No HOME, and global/system config pinned to /dev/null: git must honor
    # ONLY the repo's local config while operating on an agent-written tree.
    # A globally-configured filter driver (in the user's ~/.gitconfig or the
    # system config) could otherwise be SELECTED by an agent-authored
    # .gitattributes and execute on the host — dropping HOME alone misses the
    # system config and XDG paths.
    env = {k: os.environ[k] for k in ("PATH", "LANG") if k in os.environ}
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
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
    artifacts: tuple[str, ...] = (),
    artifact_max_bytes: int = 0,
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

    With `artifacts` (author-syscall launches, research-loop-buildout.md
    Phase A), each declared repo-relative FILE the jailed command produced is
    copied out of the throwaway tree into `<job dir>/artifacts/` — outside the
    jail, after the command, size-capped at `artifact_max_bytes` — with every
    skip recorded in `artifacts.log`. Callers validate the paths (relative, no
    traversal) before passing them; this writer additionally quotes them so
    they cross the script boundary inert.
    """
    ev = run_dir / f"eval-{name}"
    ev.mkdir(parents=True, exist_ok=True)
    # a resubmitted eval must never be read as its predecessor: every prior
    # artifact — including a leftover extracted tree — goes before submission
    for stale in ("exit-code", "stdout", "stderr", "setup.log", "submitted", "artifacts.log"):
        (ev / stale).unlink(missing_ok=True)
    shutil.rmtree(ev / "tree", ignore_errors=True)
    shutil.rmtree(ev / "artifacts", ignore_errors=True)
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
    # the checkout runs on the agent-written repo too: same filter neutralizers
    # as the snapshot, injected as GIT_CONFIG_* env (robust to '=' in a driver
    # name, unlike -c) so a smudge filter cannot execute during checkout
    neutral = _filter_neutral_env(["git", "-C", str(repo_root), *SAFE_GIT_FLAGS], {})
    lines = [
        "#!/bin/sh",
        "set -u",
        f"EV={shlex.quote(str(ev))}",
        f"REPO={shlex.quote(str(repo_root))}",
        # the extracted tree lives on NODE-LOCAL scratch, not the shared run
        # dir: it dies with the job (nothing to reap on the shared FS), and
        # the wake needs only stdout/exit-code, which stay in $EV
        'SCRATCH="${SLURM_TMPDIR:-${TMPDIR:-/tmp}}/dispatch-eval-$$"',
        'TREE="$SCRATCH/tree"',
        'mkdir -p "$SCRATCH/cache" "$SCRATCH/home" "$TREE"',
        # trap FIRST, before anything that can exit, so $SCRATCH never leaks.
        # prune reaps the stale worktree admin entry in $REPO/.git/worktrees
        # left when we deleted $TREE/.git (worktree remove can't run without it)
        'cleanup() { rm -rf "$SCRATCH"; '
        f'git -C "$REPO" {safe_git} worktree prune >/dev/null 2>&1 || true; }}',
        "trap 'cleanup' EXIT",
        "trap 'echo 143 > \"$EV/exit-code\"; cleanup; trap - EXIT; exit 0' TERM INT HUP",
        # the command file must exist and be non-empty, or sh -c "" would
        # exit 0 with empty output and read as a clean eval that measured
        # nothing
        '[ -s "$EV/command.txt" ] || { echo 96 > "$EV/exit-code"; exit 0; }',
        # git on the node runs with the job user's HOME present, so pin global
        # AND system config to /dev/null: a globally-configured filter driver
        # could otherwise be selected by the agent-authored .gitattributes and
        # execute here, outside the jail. Local repo config still applies and
        # is neutralized by the GIT_CONFIG_KEY_* overrides below.
        "export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null",
    ]
    for k, v in neutral.items():
        lines.append(f"export {k}={shlex.quote(v)}")
    lines += [
        # Materialize the snapshot by CHECKOUT, not `git archive`: a checkout
        # reproduces content faithfully — INCLUDING .gitattributes — and does
        # NOT apply export-ignore/export-subst (archive-only), so the measured
        # tree equals both the workspace (fidelity) and the fingerprint
        # (integrity). Smudge filters during checkout are neutralized by the
        # GIT_CONFIG_* env above. The worktree's .git gitfile (which would
        # point into $REPO/.git/worktrees, unbound in the jail) is DELETED
        # after checkout, leaving a plain directory; the stale admin entry is
        # pruned on cleanup.
        f'if git -C "$REPO" {safe_git} worktree add --detach "$TREE" '
        f'{shlex.quote(snapshot_sha)} >> "$EV/setup.log" 2>&1; then '
        'rm -f "$TREE/.git"; '  # plain dir now — nothing points back into $REPO
        'else echo 97 > "$EV/exit-code"; exit 0; fi',
        'export UV_CACHE_DIR="$SCRATCH/cache" UV_LINK_MODE=copy '
        'UV_PROJECT_ENVIRONMENT="$SCRATCH/cache/venv"',
        'export APPTAINERENV_UV_CACHE_DIR="$UV_CACHE_DIR" '
        "APPTAINERENV_UV_LINK_MODE=copy "
        'APPTAINERENV_UV_PROJECT_ENVIRONMENT="$UV_PROJECT_ENVIRONMENT"',
    ]
    for key, value in injected.items():
        lines.append(f"export {key}={shlex.quote(value)} APPTAINERENV_{key}={shlex.quote(value)}")
    if image:
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
        ]
    else:
        # UNCONTAINED (dev/tests, no image): run directly in the throwaway
        # tree under `env -i` with the same allowlist shape the uncontained
        # evaluator used — the submitting process's env (which can hold live
        # keys) must never reach the agent-authored command
        bare_env = " ".join(
            [
                'HOME="$SCRATCH/home"',
                'PATH="$PATH"',
                'LANG="${LANG:-C.UTF-8}"',
                'TMPDIR="$SCRATCH"',
                'UV_CACHE_DIR="$UV_CACHE_DIR"',
                "UV_LINK_MODE=copy",
                'UV_PROJECT_ENVIRONMENT="$UV_PROJECT_ENVIRONMENT"',
            ]
            + [f"{key}={shlex.quote(value)}" for key, value in injected.items()]
        )
        lines += [
            f'cd "$TREE" && env -i {bare_env} '
            'sh -c "$(cat "$EV/command.txt")" '
            '> "$EV/stdout" 2> "$EV/stderr"',
            'echo $? > "$EV/exit-code"',
        ]
    if artifacts:
        # copy-out runs OUTSIDE the jail, after the command: only declared,
        # caller-validated repo-relative FILES, each size-capped; every skip is
        # recorded so the wake can tell the author exactly what happened.
        # Two hostile shapes to defeat, since the jailed (agent) command wrote
        # this tree and picked these names:
        #   * INJECTION — a name like `$(cmd)` must never reach the shell as
        #     code. Every path goes in single-quoted (shlex.quote) argument
        #     position, and every log line is a single-quoted LITERAL (a name
        #     interpolated into a double-quoted `echo` would execute), so the
        #     name is inert everywhere.
        #   * ESCAPE via symlink — a declared file (or an intermediate dir) may
        #     be a symlink to a host file; the host-side cp would dereference
        #     it. `realpath` resolves the whole path and we copy ONLY when the
        #     resolved target stays under $TREE (and copy the RESOLVED path, so
        #     there is no resolve-then-copy gap). The command has already
        #     finished, so the tree is quiescent — no TOCTOU.
        lines.append('mkdir -p "$EV/artifacts"')
        lines.append('TREE_REAL=$(realpath "$TREE" 2>/dev/null || echo "$TREE")')
        for art in artifacts:
            q = shlex.quote(art)  # safe in argument position (single-quoted)
            skip_type = shlex.quote(f"skipped (not a regular file in the tree): {art}")
            skip_big = shlex.quote(f"skipped (over {int(artifact_max_bytes)} bytes): {art}")
            fail_cp = shlex.quote(f"copy failed: {art}")
            lines.append(
                f'AP=$(realpath "$TREE"/{q} 2>/dev/null || true); '
                f'case "$AP" in "$TREE_REAL"/*) '
                f'if [ -f "$AP" ] && [ "$(wc -c < "$AP")" -le {int(artifact_max_bytes)} ]; then '
                f'mkdir -p "$EV/artifacts/$(dirname {q})" && cp "$AP" "$EV/artifacts"/{q} '
                f'|| echo {fail_cp} >> "$EV/artifacts.log"; '
                f'elif [ -f "$AP" ]; then echo {skip_big} >> "$EV/artifacts.log"; '
                f'else echo {skip_type} >> "$EV/artifacts.log"; fi ;; '
                f'*) echo {skip_type} >> "$EV/artifacts.log" ;; esac'
            )
    lines += [
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
    """The JobSpec for one dispatched eval: the hint CLAMPED to our ceiling
    plus setup slack — a contract value above EVAL_JOB_MINUTES_CEILING must
    not create a longer Slurm job than the ceiling allows. GPU counts arrive
    with later contract fields; the parameter exists so the seam does
    not change shape then."""
    return JobSpec(
        job_name=job_name[:60],
        account=account,
        partition=partition,
        time_minutes=effective_eval_minutes(eval_minutes) + EVAL_JOB_SETUP_MINUTES,
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
