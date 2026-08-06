"""Run state on the shared filesystem: the agent's durable half.

A run is one hypothesis (docs/design/architecture.md, "The life of a run").
Its record is a single JSON file written by atomic rename; the sweep reasons
only from these files plus Slurm — never from process memory — so a crash
anywhere leaves a file that says what happens next.

Leases serialize wake delivery: whoever wants to wake a run acquires the
lease first (atomic O_EXCL create). Leases expire — a holder that died keeps
the lease only until the sweep notices (holder job dead, or age past TTL) —
so a wake killed mid-session delays the retry by one grace window; it cannot
strand the run.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path

log = logging.getLogger(__name__)

# Live states.
IMPLEMENTING = "implementing"  # a session is (or will be) working
WAITING = "waiting"  # experiment submitted; hibernating until results
IN_REVIEW = "in-review"  # PR open; wakes on qualifying comments
CONCLUDING = "concluding"  # results in hand; final session(s)
ENDED = "ended"

STATES = (IMPLEMENTING, WAITING, IN_REVIEW, CONCLUDING, ENDED)

# The six endings ("The life of a run" — every one produces a report).
MERGED = "merged"
REJECTED = "rejected"
NEGATIVE_RESULT = "negative-result"
BUDGET_EXHAUSTED = "budget-exhausted"
ABORTED = "aborted"
STUCK = "stuck"

ENDINGS = (MERGED, REJECTED, NEGATIVE_RESULT, BUDGET_EXHAUSTED, ABORTED, STUCK)

RECORD_NAME = "state.json"
LEASE_NAME = "lease.json"

MAX_WAKE_ATTEMPTS = 3


@dataclass(frozen=True)
class RunRecord:
    """Everything the sweep needs to act on a run, and nothing more."""

    run_id: str
    target: str  # owner/repo
    task_title: str
    state: str
    agent_id: str = "agent-01"
    experiment_job_id: str = ""
    wake_job_id: str = ""  # the afterany dependency job, when one exists
    resume_session_id: str = ""  # harness session to resume on wake
    wake_attempts: int = 0
    deadline: float = 0.0  # unix; submit+walltime+slack, re-based on start
    ending: str = ""  # one of ENDINGS once state == ENDED
    ending_note: str = ""
    created: float = 0.0
    updated: float = 0.0

    def ended(self) -> bool:
        return self.state == ENDED


@dataclass(frozen=True)
class Lease:
    holder: str  # e.g. "wake-job:12345" or "tick:12345"
    holder_job_id: str  # Slurm job id of the holder, "" if none
    acquired: float  # unix timestamp


def run_dir(root: Path, run_id: str) -> Path:
    return root / "runs" / run_id


def save_record(root: Path, record: RunRecord, now: float) -> None:
    """Atomic write: a crash mid-save leaves the previous record intact."""
    if record.state not in STATES:
        raise ValueError(f"unknown state {record.state!r}")
    if record.state == ENDED and record.ending not in ENDINGS:
        raise ValueError(f"ended run needs a valid ending, got {record.ending!r}")
    directory = run_dir(root, record.run_id)
    directory.mkdir(parents=True, exist_ok=True)
    stamped = replace(record, updated=now, created=record.created or now)
    tmp = directory / f".{RECORD_NAME}.tmp"
    tmp.write_text(json.dumps(asdict(stamped), indent=2, sort_keys=True))
    os.replace(tmp, directory / RECORD_NAME)


def load_record(root: Path, run_id: str) -> RunRecord:
    raw = json.loads((run_dir(root, run_id) / RECORD_NAME).read_text())
    return RunRecord(**raw)


def list_runs(root: Path) -> list[RunRecord]:
    """Every readable run record; unreadable ones are logged, not fatal —
    one corrupt file must not stop the sweep."""
    records = []
    runs_root = root / "runs"
    if not runs_root.is_dir():
        return []
    for directory in sorted(runs_root.iterdir()):
        try:
            records.append(load_record(root, directory.name))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            log.warning("unreadable run record %s: %s", directory, exc)
    return records


# --- leases ---


def acquire_lease(root: Path, run_id: str, holder: str, holder_job_id: str, now: float) -> bool:
    """Take the run's wake lease. True if acquired; False if held.

    O_EXCL makes acquisition atomic on the shared filesystem: exactly one
    contender wins, the rest see False and no-op (double delivery is
    harmless by design).
    """
    directory = run_dir(root, run_id)
    directory.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(Lease(holder, holder_job_id, now)))
    try:
        fd = os.open(directory / LEASE_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as handle:
        handle.write(payload)
    return True


def read_lease(root: Path, run_id: str) -> Lease | None:
    try:
        raw = json.loads((run_dir(root, run_id) / LEASE_NAME).read_text())
        return Lease(**raw)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, KeyError):
        # An unreadable lease is treated as held-but-unknown; the TTL path
        # in `lease_is_stale` cannot run without a timestamp, so the sweep
        # falls back to reaping it after the grace window via mtime.
        return None


def update_lease_holder(
    root: Path, run_id: str, holder: str, holder_job_id: str, now: float
) -> None:
    """Hand a HELD lease to a new holder (e.g. tick → the wake job it just
    submitted). Atomic replace; only valid while the caller holds the lease."""
    directory = run_dir(root, run_id)
    tmp = directory / f".{LEASE_NAME}.tmp"
    tmp.write_text(json.dumps(asdict(Lease(holder, holder_job_id, now))))
    os.replace(tmp, directory / LEASE_NAME)


def release_lease(root: Path, run_id: str) -> None:
    try:
        (run_dir(root, run_id) / LEASE_NAME).unlink()
    except FileNotFoundError:
        pass


def lease_is_stale(lease: Lease, now: float, ttl_s: float, holder_alive: bool | None) -> bool:
    """A lease is stale when its holder is known-dead, or too old.

    `holder_alive` is None when Slurm could not answer (query failure) — in
    that case only the TTL can prove staleness, never the holder check:
    an outage must not look like a dead holder.
    """
    if holder_alive is False:
        return True
    return (now - lease.acquired) > ttl_s
