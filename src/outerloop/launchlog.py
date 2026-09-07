"""The per-run launch ledger: append-only JSON lines in the run directory, one
record when a sleep's launches are submitted and one when each job's result
comes back at the wake. It is what `history` reads and what labels a launch in
the queue view (docs/design/session-watcher.md, "History"). Kernel-owned: the
run directory is never the session's to write."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from outerloop.syscall import Launch, LaunchResult, launch_jobs

LEDGER = "launches.jsonl"
# generous: a run is depth_k launches x sleep_k sleeps x the array width, far below this
MAX_LEDGER_BYTES = 4_000_000


def _append(run_dir: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / LEDGER
    # a crash mid-append leaves a torn last line; start on a fresh one so the
    # torn line is the only record lost, never the next one too
    torn = False
    try:
        with path.open("rb") as fh:
            fh.seek(-1, 2)
            torn = fh.read(1) != b"\n"
    except OSError:
        pass
    with path.open("a", encoding="utf-8") as fh:
        if torn:
            fh.write("\n")
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def append_submitted(
    run_dir: Path, *, sleep: int, launches: tuple[Launch, ...], job_ids: list[str], at: float
) -> None:
    """One record per launch of a sleep, with the job ids it fanned out to. The
    ids are positional over `launch_jobs` order, exactly as the park recorded
    them; a launch whose ids are missing (an older park) gets none."""
    rows: list[dict[str, Any]] = []
    k = 0
    for launch in launches:
        n = len(launch_jobs(launch))
        ids = job_ids[k : k + n]
        k += n
        rows.append(
            {
                "event": "submitted",
                "sleep": sleep,
                "name": launch.name,
                "why": launch.why,
                "minutes": launch.minutes,
                "array": launch.array,
                "job_ids": list(ids),
                "at": at,
            }
        )
    _append(run_dir, rows)


def append_ended(
    run_dir: Path, *, sleep: int, results: tuple[LaunchResult, ...], at: float
) -> None:
    """One record per job that came back at the wake, keyed to its sleep."""
    _append(
        run_dir,
        [
            {
                "event": "ended",
                "sleep": sleep,
                "name": r.name,
                "exit_code": r.exit_code,
                "state": r.slurm_state,
                "at": at,
            }
            for r in results
        ],
    )


def read_ledger(run_dir: Path) -> list[dict[str, Any]]:
    """Every record, oldest first; a malformed line is skipped, a missing file
    is an empty history."""
    try:
        with (run_dir / LEDGER).open("rb") as fh:
            raw = fh.read(MAX_LEDGER_BYTES)
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def history(run_dir: Path) -> list[dict[str, Any]]:
    """Submitted launches in order, each with the ended records of its jobs.
    The identity is (sleep, name): a name is unique within one sleep only."""
    rows = read_ledger(run_dir)
    entries: dict[tuple[int, str], dict[str, Any]] = {}
    for row in rows:
        if row.get("event") != "submitted":
            continue
        key = (int(row.get("sleep") or 0), str(row.get("name") or ""))
        entries[key] = {
            "sleep": row.get("sleep"),
            "name": row.get("name"),
            "why": row.get("why", ""),
            "minutes": row.get("minutes"),
            "array": row.get("array", 1),
            "job_ids": list(row.get("job_ids") or []),
            "submitted_at": row.get("at"),
            "jobs": [],
        }
    for row in rows:
        if row.get("event") != "ended":
            continue
        # an array member is `<name>.<i>`; the dot is outside the name alphabet
        launch_name = str(row.get("name") or "").split(".", 1)[0]
        entry = entries.get((int(row.get("sleep") or 0), launch_name))
        if entry is not None:
            entry["jobs"].append(
                {
                    "name": row.get("name"),
                    "exit_code": row.get("exit_code"),
                    "state": row.get("state", ""),
                    "ended_at": row.get("at"),
                }
            )
    return list(entries.values())


def why_by_job(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Slurm job id -> the launch it belongs to (name, why, sleep): how the
    queue view labels a launch job for every agent."""
    out: dict[str, dict[str, Any]] = {}
    for row in read_ledger(run_dir):
        if row.get("event") != "submitted":
            continue
        for job_id in row.get("job_ids") or []:
            out[str(job_id)] = {
                "name": row.get("name", ""),
                "why": row.get("why", ""),
                "sleep": row.get("sleep"),
            }
    return out
