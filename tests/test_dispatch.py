"""The dispatched-eval primitive: snapshot semantics, job-script trust
layout, result parsing — the seams the design doc's review rounds hardened."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from autoresearch.dispatch import (
    EVAL_JOB_MINUTES_CEILING,
    drop_snapshot,
    effective_eval_minutes,
    eval_job_spec,
    parse_result_json,
    read_eval_result,
    result_summary,
    should_dispatch,
    snapshot_tree,
    write_eval_job,
)
from autoresearch.orchestrator import EvalError


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()

    def g(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)

    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (root / "kept.py").write_text("x = 1\n")
    (root / ".gitignore").write_text("ignored.txt\n")
    g("add", "-A")
    g("commit", "-q", "-m", "base")
    return root


class _WS:
    def __init__(self, root: Path):
        self.root = root

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.root), *args], check=True, capture_output=True, text=True
        ).stdout.strip()


def test_snapshot_captures_dirty_tree_without_touching_the_index(tmp_path):
    root = _repo(tmp_path)
    ws = _WS(root)
    base = ws.git("rev-parse", "HEAD")
    (root / "kept.py").write_text("x = 2\n")
    (root / "added.py").write_text("y = 3\n")  # untracked: must be included
    (root / "ignored.txt").write_text("never\n")  # ignored: must not be
    ws.git("add", "kept.py")  # staged state that must SURVIVE
    staged_before = ws.git("diff", "--cached", "--stat")
    snap = snapshot_tree(ws, base)  # type: ignore[arg-type]
    assert ws.git("diff", "--cached", "--stat") == staged_before  # index intact
    files = ws.git("ls-tree", "-r", "--name-only", snap.commit).splitlines()
    assert "added.py" in files and "ignored.txt" not in files
    assert ws.git("show", f"{snap.commit}:kept.py") == "x = 2"
    assert ws.git("rev-parse", f"{snap.commit}^") == base  # parented on base
    assert ws.git("rev-parse", f"{snap.commit}^{{tree}}") == snap.tree
    # retained under a ref so gc cannot prune it while a job is queued
    assert ws.git("rev-parse", snap.ref) == snap.commit
    # deterministic: same content -> same tree hash (the drift fingerprint)
    snap2 = snapshot_tree(ws, base)  # type: ignore[arg-type]
    assert snap2.tree == snap.tree and snap2.ref != snap.ref  # unique refs
    # drop releases the ref; the commit becomes unreachable again
    drop_snapshot(ws, snap)  # type: ignore[arg-type]
    import subprocess as _sp

    assert (
        _sp.run(
            ["git", "-C", str(root), "rev-parse", "--verify", snap.ref], capture_output=True
        ).returncode
        != 0
    )


def test_drop_snapshot_logs_a_failure_without_raising(tmp_path, caplog):
    import logging

    from autoresearch.dispatch import Snapshot

    ws = _WS(tmp_path / "not-a-repo")  # no git dir -> update-ref fails
    snap = Snapshot(commit="a" * 40, tree="b" * 40, ref="refs/dispatch/nope")
    with caplog.at_level(logging.WARNING):
        drop_snapshot(ws, snap)  # type: ignore[arg-type]  # best-effort: must NOT raise
    assert "snapshot ref drop" in caplog.text  # but the failure is visible, not silent


def test_snapshot_failure_is_an_eval_error(tmp_path):
    root = _repo(tmp_path)
    with pytest.raises(EvalError, match="snapshot failed"):
        snapshot_tree(_WS(root), "0" * 40)  # type: ignore[arg-type]


def test_job_script_trust_layout(tmp_path):
    run_dir = tmp_path / "run"
    command = 'echo "$SEED" && uv run python eval.py; echo done'
    script = write_eval_job(
        run_dir,
        "candidate",
        repo_root=tmp_path / "repo",
        snapshot_sha="a" * 40,
        command=command,
        image="/img/agent.sif",
        extra_env={"PILOT_SEED": "1234", "HOME": "/evil", "bad key": "x"},
    )
    text = script.read_text()
    # the contract command never appears in the script (no quoting surface):
    # it lives in its own file, read back inside the jail
    assert "eval.py" not in text
    assert (run_dir / "eval-candidate" / "command.txt").read_text() == command
    # the jail flags match the in-job evaluator
    for flag in ("--containall", "--cleanenv", "--pwd"):
        assert flag in text
    # stdout is captured OUTSIDE apptainer into the run dir
    assert '> "$EV/stdout"' in text
    # extra_env rides as APPTAINERENV_* (survives --cleanenv)
    assert "APPTAINERENV_PILOT_SEED=1234" in text
    # the wrapper always reports an exit code and never fails the job itself
    assert 'echo $? > "$EV/exit-code"' in text and text.rstrip().endswith("exit 0")
    # cleanup runs on every exit INCLUDING the walltime SIGTERM (a POSIX
    # shell dying on a signal runs no EXIT trap)
    assert "trap 'cleanup' EXIT" in text and "TERM INT HUP" in text
    # the snapshot is materialized by CHECKOUT (worktree add), which keeps
    # .gitattributes and applies no export processing; the .git gitfile is
    # deleted so the jail sees a plain directory
    assert "worktree add --detach" in text and 'rm -f "$TREE/.git"' in text
    assert "worktree prune" in text  # the stale admin entry is reaped
    # the tree lands on node-local scratch (dies with the job), not the run dir
    assert 'TREE="$SCRATCH/tree"' in text
    # global/system git config is pinned off (agent .gitattributes could
    # else select a globally-configured filter to run on the node)
    assert "GIT_CONFIG_GLOBAL=/dev/null" in text and "GIT_CONFIG_SYSTEM=/dev/null" in text
    # a missing/empty command file is a setup failure, not a clean exit-0
    assert '[ -s "$EV/command.txt" ]' in text
    # the checkout on the agent-written repo carries the child-spawn
    # neutralizers
    co = next(ln for ln in text.splitlines() if "worktree add" in ln)
    assert "core.hooksPath=/dev/null" in co
    # a managed key in extra_env is DROPPED, never allowed to override the
    # isolation
    assert "HOME=/evil" not in text and "APPTAINERENV_HOME=/evil" not in text


def test_snapshot_neutralizes_agent_git_config(tmp_path):
    """A session-planted clean filter or hook must not execute on the
    orchestrator host during the snapshot (the instruction-smuggling class:
    the workspace is agent-written)."""
    root = _repo(tmp_path)
    ws = _WS(root)
    base = ws.git("rev-parse", "HEAD")
    canary = tmp_path / "canary"
    # a DOTTED driver name (git subsections may contain dots) — the parse
    # must strip only the trailing .clean/.smudge/.process, not everything
    # after the first dot
    (root / ".gitattributes").write_text("*.py filter=ev.il\n")
    ws.git("config", "filter.ev.il.clean", f"sh -c 'touch {canary}; cat'")
    hook_canary = tmp_path / "hook-canary"
    hooks = root / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "post-checkout").write_text(f"#!/bin/sh\ntouch {hook_canary}\n")
    (hooks / "post-checkout").chmod(0o755)
    ws.git("config", "core.hooksPath", str(hooks))
    (root / "kept.py").write_text("x = 9\n")
    snap = snapshot_tree(ws, base)  # type: ignore[arg-type]
    assert not canary.exists(), "clean filter executed during snapshot"
    assert not hook_canary.exists(), "a hook executed during snapshot"
    assert ws.git("show", f"{snap.commit}:kept.py") == "x = 9"


def test_snapshot_keeps_gitattributes_checkout_is_faithful(tmp_path):
    """The snapshot KEEPS .gitattributes (fidelity — the eval sees the same
    inputs as the workspace); checkout, not archive, materializes the tree,
    so export-ignore/export-subst are never applied (integrity)."""
    root = _repo(tmp_path)
    ws = _WS(root)
    base = ws.git("rev-parse", "HEAD")
    (root / ".gitattributes").write_text("kept.py export-ignore\n")
    (root / "kept.py").write_text("x = 7\n")
    snap = snapshot_tree(ws, base)  # type: ignore[arg-type]
    files = ws.git("ls-tree", "-r", "--name-only", snap.commit).splitlines()
    assert ".gitattributes" in files  # kept, not stripped
    assert "kept.py" in files
    # a checkout of the snapshot reproduces kept.py despite export-ignore
    # (export attrs are archive-only), so the measured tree is faithful
    import subprocess as sp

    wt = tmp_path / "wt"
    sp.run(
        ["git", "-C", str(root), "worktree", "add", "--detach", str(wt), snap.commit],
        check=True,
        capture_output=True,
    )
    assert (wt / "kept.py").read_text() == "x = 7\n"


def test_snapshot_ignores_global_filter_config(tmp_path, monkeypatch):
    """A filter driver configured GLOBALLY (~/.gitconfig) plus an
    agent-authored .gitattributes must not execute during the snapshot —
    global/system config is pinned to /dev/null."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    canary = tmp_path / "globalcanary"
    (fake_home / ".gitconfig").write_text(
        f"[filter \"glob\"]\n\tclean = sh -c 'touch {canary}; cat'\n"
    )
    monkeypatch.setenv("HOME", str(fake_home))
    root = _repo(tmp_path)
    ws = _WS(root)
    base = ws.git("rev-parse", "HEAD")
    (root / ".gitattributes").write_text("*.py filter=glob\n")
    (root / "kept.py").write_text("x = 3\n")
    snap = snapshot_tree(ws, base)  # type: ignore[arg-type]
    assert not canary.exists(), "a global filter executed during snapshot"
    assert ws.git("show", f"{snap.commit}:kept.py") == "x = 3"


def test_snapshot_neutralizes_filter_name_with_equals(tmp_path):
    """A driver name containing '=' defeats `-c filter.<d>.clean=` (git splits
    -c at the first '='); the GIT_CONFIG_* env form must handle it."""
    root = _repo(tmp_path)
    ws = _WS(root)
    base = ws.git("rev-parse", "HEAD")
    canary = tmp_path / "eqcanary"
    (root / ".gitattributes").write_text("*.py filter=ev=il\n")
    ws.git("config", "filter.ev=il.clean", f"sh -c 'touch {canary}; cat'")
    (root / "kept.py").write_text("x = 5\n")
    snap = snapshot_tree(ws, base)  # type: ignore[arg-type]
    assert not canary.exists(), "= in driver name defeated neutralization"
    assert ws.git("show", f"{snap.commit}:kept.py") == "x = 5"


def test_read_result_rejects_non_finite(tmp_path):
    rd = _result(tmp_path, name="n", stdout='{"metric": "r2", "value": NaN}\n')
    with pytest.raises(EvalError, match="non-finite"):
        read_eval_result(rd, "n", "r2")


def test_read_result_survives_binary_output(tmp_path):
    ev = tmp_path / "eval-bin"
    ev.mkdir()
    (ev / "exit-code").write_text("0")
    (ev / "stdout").write_bytes(b"\xff\xfe garbage\n" + b'{"metric": "r2", "value": 0.5}\n')
    assert read_eval_result(tmp_path, "bin", "r2") == 0.5
    assert "exit=0" in result_summary(tmp_path, "bin")


def test_write_eval_job_clears_stale_results(tmp_path):
    run_dir = tmp_path / "run"
    ev = run_dir / "eval-candidate"
    ev.mkdir(parents=True)
    (ev / "exit-code").write_text("0")
    (ev / "stdout").write_text('{"metric": "r2", "value": 0.9}')
    write_eval_job(
        run_dir,
        "candidate",
        repo_root=tmp_path,
        snapshot_sha="a" * 40,
        command="true",
        image="/i.sif",
    )
    assert not (ev / "exit-code").exists() and not (ev / "stdout").exists()


def test_job_script_checkout_failure_reports_not_masks(tmp_path):
    """A failing checkout (bad sha) must yield exit 97, never a silent
    empty-tree run."""
    import subprocess as sp

    root = _repo(tmp_path)  # a real repo so the checkout command can run
    run_dir = tmp_path / "run"
    script = write_eval_job(
        run_dir,
        "c",
        repo_root=root,
        snapshot_sha="0" * 40,  # nonexistent sha
        command="echo SHOULD_NOT_RUN",
        image="/bin/true-not-an-image",
    )
    sp.run(["sh", str(script)], check=True, capture_output=True)
    ev = run_dir / "eval-c"
    assert (ev / "exit-code").read_text().strip() == "97"
    # the jail line never ran -> no stdout with SHOULD_NOT_RUN
    out = (ev / "stdout").read_text() if (ev / "stdout").exists() else ""
    assert "SHOULD_NOT_RUN" not in out


def test_eval_job_spec_carries_setup_slack(tmp_path):
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/sh\n")
    spec = eval_job_spec(script, job_name="eval-x", account="a", partition="p", eval_minutes=30)
    assert spec.time_minutes == 40 and spec.script == str(script)
    # a contract value over the ceiling cannot make a longer job than allowed
    huge = eval_job_spec(script, job_name="e", account="a", partition="p", eval_minutes=10_000)
    assert huge.time_minutes == EVAL_JOB_MINUTES_CEILING + 10


def test_clamps_and_threshold():
    assert effective_eval_minutes(None) == 0 and not should_dispatch(None)
    assert not should_dispatch(3)  # fits the in-job runway
    assert should_dispatch(30)
    assert effective_eval_minutes(10_000) == EVAL_JOB_MINUTES_CEILING


def _result(tmp_path, name="x", code="0", stdout=None):
    ev = tmp_path / f"eval-{name}"
    ev.mkdir(parents=True, exist_ok=True)
    if code is not None:
        (ev / "exit-code").write_text(code)
    if stdout is not None:
        (ev / "stdout").write_text(stdout)
    return tmp_path


def test_read_result_happy_path(tmp_path):
    rd = _result(tmp_path, stdout='progress\n{"metric": "r2", "value": 0.61}\n')
    assert read_eval_result(rd, "x", "r2") == 0.61
    assert parse_result_json(rd, "x")["value"] == 0.61
    assert "exit=0" in result_summary(rd, "x")


def test_read_result_failures(tmp_path):
    with pytest.raises(EvalError, match="no exit code"):
        read_eval_result(_result(tmp_path, name="a", code=None), "a", "r2")
    with pytest.raises(EvalError, match="failed \\(97\\)"):
        read_eval_result(_result(tmp_path, name="b", code="97"), "b", "r2")
    with pytest.raises(EvalError, match="no readable"):
        read_eval_result(_result(tmp_path, name="c", stdout="no json here\n"), "c", "r2")


def test_contract_accepts_and_bounds_eval_minutes():
    from autoresearch.contract import load_contract

    c = load_contract(
        """
benchmarks:
  - {name: big, command: c, metric: m, direction: max, eval_minutes: 45}
  - {name: small, command: c, metric: m, direction: max}
budgets: {gpu_hours_per_run: 1, runs_per_week: 3}
scope: {allowed: [src/]}
roadmap: docs/roadmap.md
""",
        "org/x",
    )
    big, small = c.benchmarks
    assert big.eval_minutes == 45 and small.eval_minutes is None
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="eval_minutes"):
        load_contract(
            """
benchmarks:
  - {name: bad, command: c, metric: m, direction: max, eval_minutes: 0}
budgets: {gpu_hours_per_run: 1, runs_per_week: 3}
scope: {allowed: [src/]}
roadmap: docs/roadmap.md
""",
            "org/x",
        )
