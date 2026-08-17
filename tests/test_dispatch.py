"""The dispatched-eval primitive: snapshot semantics, job-script trust
layout, result parsing — the seams the design doc's review rounds hardened."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from autoresearch.dispatch import (
    EVAL_JOB_MINUTES_CEILING,
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
    commit, tree = snapshot_tree(ws, base)  # type: ignore[arg-type]
    assert ws.git("diff", "--cached", "--stat") == staged_before  # index intact
    files = ws.git("ls-tree", "-r", "--name-only", commit).splitlines()
    assert "added.py" in files and "ignored.txt" not in files
    assert ws.git("show", f"{commit}:kept.py") == "x = 2"
    assert ws.git("rev-parse", f"{commit}^") == base  # parented on base
    assert ws.git("rev-parse", f"{commit}^{{tree}}") == tree
    # deterministic: same content -> same tree hash (the drift fingerprint)
    _, tree2 = snapshot_tree(ws, base)  # type: ignore[arg-type]
    assert tree2 == tree


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
        extra_env={"PILOT_SEED": "1234"},
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
    # worktree is cleaned up on every exit
    assert "worktree remove --force" in text and "trap" in text


def test_eval_job_spec_carries_setup_slack(tmp_path):
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/sh\n")
    spec = eval_job_spec(script, job_name="eval-x", account="a", partition="p", eval_minutes=30)
    assert spec.time_minutes == 40 and spec.script == str(script)


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
