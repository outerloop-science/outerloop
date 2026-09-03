"""Disk housekeeping: ended runs shed their workspaces.

A run's workspace (`ws/`, the clone the session worked in, and `ws-home/`,
the session's home with its tool caches) is the bulk of what a run leaves
on the state filesystem, in files far more than in bytes: 2026-09-03 the
scratch quota on Torch hit its 5M-file ceiling with 1.67M of them under
state/runs, and no tick could start for two hours. Everything the record
keeps for the research record lives outside those two directories: the
run's state.json, its report, its transcripts, and the ledger entries; the
tree itself is on GitHub (the PR branch, or the research line's snapshot).

Rules (docs/design/disk-maintenance.md):

- only ENDED runs shed, and only the two directories `ws` and `ws-home`;
- after a grace period (default 24 h, for post-mortems), oldest first;
- when the state filesystem's write probe FAILS, the grace is waived: the
  tick sheds until the probe passes again, so a full quota heals itself;
- a workspace whose top-level entry is a symlink is not removed (a session
  could aim it anywhere); it is logged and left for a human;
- the record notes when it shed (`workspace_shed`), so nothing runs twice.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import replace
from pathlib import Path

from autoresearch.runstate import ENDED, RunRecord, list_runs, run_dir, save_record

log = logging.getLogger(__name__)

WORKSPACE_DIRS = ("ws", "ws-home")
DEFAULT_SHED_GRACE_S = 24 * 3600.0


def shed_candidates(root: Path, now: float, grace_s: float, force: bool) -> list[RunRecord]:
    """Ended runs whose workspaces may go now, oldest ending first. With
    `force` (the disk is failing) the grace period does not apply."""
    due: list[RunRecord] = []
    for record in list_runs(root):
        if record.state != ENDED or record.workspace_shed:
            continue
        if not force and now - record.updated < grace_s:
            continue
        if not any((run_dir(root, record.run_id) / d).exists() for d in WORKSPACE_DIRS):
            continue
        due.append(record)
    due.sort(key=lambda r: r.updated)
    return due


def shed_workspace(root: Path, record: RunRecord, now: float) -> bool:
    """Remove the run's `ws` and `ws-home` directories and stamp the record.
    Returns False (and removes nothing) when either is a symlink."""
    base = run_dir(root, record.run_id)
    targets = [base / d for d in WORKSPACE_DIRS if (base / d).exists() or (base / d).is_symlink()]
    for path in targets:
        if path.is_symlink():
            log.warning("not shedding %s: %s is a symlink", record.run_id, path.name)
            return False
    for path in targets:
        shutil.rmtree(path, ignore_errors=True)
    remaining = [p.name for p in targets if p.exists()]
    if remaining:
        log.warning("shed %s incompletely: %s remain", record.run_id, ", ".join(remaining))
        return False
    save_record(root, replace(record, workspace_shed=now), now)
    return True


def shed_ended_workspaces(
    root: Path,
    now: float,
    *,
    grace_s: float = DEFAULT_SHED_GRACE_S,
    force: bool = False,
    limit: int = 50,
    until_ok: object = None,
) -> list[str]:
    """Shed due workspaces, at most `limit` per call. With `until_ok` (a
    callable returning True once the disk is healthy again) the sweep stops
    as soon as it reports True, so a forced sweep frees only what it must."""
    shed: list[str] = []
    for record in shed_candidates(root, now, grace_s, force):
        if len(shed) >= limit:
            break
        if until_ok is not None and callable(until_ok) and until_ok():
            break
        if shed_workspace(root, record, now):
            shed.append(record.run_id)
    if shed:
        log.info(
            "shed %d ended workspace(s)%s: %s",
            len(shed),
            " (forced)" if force else "",
            ", ".join(shed),
        )
    return shed
