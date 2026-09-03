"""Ended runs shed ws/ and ws-home/ after a grace period, at once when the
disk is failing; nothing else is touched."""

from __future__ import annotations

import os
from pathlib import Path

from autoresearch.housekeeping import (
    DEFAULT_SHED_GRACE_S,
    shed_candidates,
    shed_ended_workspaces,
    shed_workspace,
)
from autoresearch.runstate import ENDED, WAITING, RunRecord, load_record, run_dir, save_record

NOW = 2_000_000.0


def _run(root: Path, run_id: str, state: str, updated: float, with_ws: bool = True) -> RunRecord:
    rec = RunRecord(
        run_id=run_id,
        target="o/r",
        task_title="t",
        state=state,
        ending="aborted" if state == ENDED else "",
    )
    save_record(root, rec, updated)
    d = run_dir(root, run_id)
    if with_ws:
        (d / "ws" / ".git").mkdir(parents=True)
        (d / "ws" / "train.py").write_text("x\n")
        (d / "ws-home" / ".cache").mkdir(parents=True)
        (d / "ws-home" / ".cache" / "blob").write_text("y\n")
    (d / "report.md").write_text("Outcome: aborted\n")
    (d / "ws-codex.jsonl").write_text("{}\n")
    return load_record(root, run_id)


def test_only_ended_runs_past_the_grace_shed_and_only_the_two_directories(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _run(root, "old-ended", ENDED, NOW - DEFAULT_SHED_GRACE_S - 1)
    _run(root, "fresh-ended", ENDED, NOW - 60)
    _run(root, "live", WAITING, NOW - DEFAULT_SHED_GRACE_S - 1)
    assert [r.run_id for r in shed_candidates(root, NOW, DEFAULT_SHED_GRACE_S, force=False)] == [
        "old-ended"
    ]
    shed = shed_ended_workspaces(root, NOW)
    assert shed == ["old-ended"]
    d = run_dir(root, "old-ended")
    assert not (d / "ws").exists() and not (d / "ws-home").exists()
    assert (
        (d / "report.md").exists()
        and (d / "ws-codex.jsonl").exists()
        and (d / "state.json").exists()
    )
    assert load_record(root, "old-ended").workspace_shed == NOW
    for kept in ("fresh-ended", "live"):
        assert (run_dir(root, kept) / "ws" / "train.py").exists()
    # idempotent: nothing left to shed
    assert shed_ended_workspaces(root, NOW) == []


def test_a_failing_disk_waives_the_grace_oldest_first_until_the_probe_passes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    _run(root, "b", ENDED, NOW - 120)
    _run(root, "a", ENDED, NOW - 300)
    _run(root, "c", ENDED, NOW - 30)
    _run(root, "live", WAITING, NOW - 9000)
    calls = {"n": 0}

    def until_ok() -> bool:
        calls["n"] += 1
        return calls["n"] > 2  # healthy again after two sheds

    shed = shed_ended_workspaces(root, NOW, force=True, until_ok=until_ok)
    assert shed == ["a", "b"]  # oldest first, stopped once the probe passed
    assert (run_dir(root, "c") / "ws").exists()
    assert (run_dir(root, "live") / "ws").exists()


def test_a_symlinked_workspace_is_left_alone_and_logged(tmp_path: Path) -> None:
    root = tmp_path / "state"
    rec = _run(root, "linked", ENDED, NOW - DEFAULT_SHED_GRACE_S - 1, with_ws=False)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "precious").mkdir(parents=True)
    d = run_dir(root, "linked")
    os.symlink(elsewhere, d / "ws")
    (d / "ws-home").mkdir()
    assert shed_workspace(root, rec, NOW) is False
    assert (elsewhere / "precious").exists()
    assert (d / "ws-home").exists()  # nothing removed when any target is a link
    assert load_record(root, "linked").workspace_shed == 0.0


def test_the_per_call_limit_bounds_a_tick(tmp_path: Path) -> None:
    root = tmp_path / "state"
    for i in range(5):
        _run(root, f"r{i}", ENDED, NOW - DEFAULT_SHED_GRACE_S - 100 + i)
    assert len(shed_ended_workspaces(root, NOW, limit=2)) == 2
    assert len(shed_ended_workspaces(root, NOW, limit=2)) == 2
    assert len(shed_ended_workspaces(root, NOW, limit=2)) == 1
