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

import contextlib
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
OUTAGE_NAME = "outage.json"

# How long the session-spawning lanes stay paused after an API outage is
# stamped. 45 minutes skips roughly one tick, so during a sustained outage
# one canary session per ~hour re-probes the API instead of every lane
# burning attempts every half hour. Throttling (429/529) is transient by
# nature and gets a short pause instead — a momentary spike must not idle
# the orchestrator for most of an hour (review finding).
OUTAGE_COOLDOWN_S = 45 * 60
THROTTLE_COOLDOWN_S = 5 * 60
_THROTTLE_HINTS = ("rate_limit", "overloaded")

MAX_WAKE_ATTEMPTS = 3


def stamp_outage(root: Path, detail: str, now: float) -> None:
    """Record that the API refused us (atomic rename, like the records)."""
    root.mkdir(parents=True, exist_ok=True)
    # pid in the tmp name, like save_record: several lanes' jobs can fail
    # to the same outage in one window, and interleaved writers must not
    # install a truncated stamp — an unreadable latch reads as NO pause,
    # which is exactly the failure the latch exists to prevent
    tmp = root / f".{OUTAGE_NAME}.{os.getpid()}.tmp"
    cooldown = (
        THROTTLE_COOLDOWN_S
        if any(hint in detail.casefold() for hint in _THROTTLE_HINTS)
        else OUTAGE_COOLDOWN_S
    )
    tmp.write_text(json.dumps({"detail": detail[:300], "time": now, "cooldown_s": cooldown}))
    os.replace(tmp, root / OUTAGE_NAME)


def outage_active(root: Path, now: float) -> str:
    """The stamped detail while the cooldown holds, else "". The cooldown
    lives IN the stamp (decided at stamp time from the failure class);
    unreadable or stale stamps read as inactive — a corrupt latch must
    never brick the loop, and time moving backwards reads as expired."""
    path = root / OUTAGE_NAME
    try:
        data = json.loads(path.read_text())
        stamped = float(data["time"])
        detail = str(data.get("detail", ""))
        cooldown_s = float(data.get("cooldown_s", OUTAGE_COOLDOWN_S))
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    if 0 <= now - stamped < cooldown_s:
        return detail or "api outage"
    return ""


@dataclass(frozen=True)
class RunRecord:
    """Everything the sweep needs to act on a run, and nothing more."""

    run_id: str
    target: str  # owner/repo
    task_title: str
    state: str
    agent_id: str = "agent-01"
    experiment_job_id: str = ""
    climb_job_id: str = ""  # slurm job running the climb itself; lets the
    # sweep end records whose job was KILLED (walltime/preemption/node
    # death) rather than crashed — signals leave no exception to contain.
    # INVARIANT: any future path that re-enters `implementing` from a NEW
    # job must re-stamp this field, or the sweep will judge the run by a
    # stale terminal job. (No such path exists today.)
    wake_job_id: str = ""  # the afterany dependency job, when one exists
    resume_session_id: str = ""  # harness session to resume on wake
    pr_url: str = ""  # the run's open PR, once one exists
    benchmark: str = ""  # contract benchmark this run works on
    # Per-source comment cursors: issue comments, top-level reviews, and
    # inline review comments are three REST collections with independent id
    # sequences — one cursor across them drops comments forever.
    last_comment_id: int = 0
    last_review_id: int = 0
    last_review_comment_id: int = 0
    followup_job_id: str = ""  # slurm job servicing this run's review comments
    issue_number: int = 0  # the requesting issue, when the requested lane started this run
    wake_attempts: int = 0
    deadline: float = 0.0  # unix; submit+walltime+slack, re-based on start
    terminal_seen: float = 0.0  # when the sweep first saw the experiment terminal
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
    if record.state == WAITING and record.experiment_job_id and record.deadline <= 0:
        # A waiting run without a deadline is invisible to the deadline floor
        # — the exact "silently immortal run" the fail-safe design forbids.
        raise ValueError("waiting run with an experiment needs a deadline")
    directory = run_dir(root, record.run_id)
    directory.mkdir(parents=True, exist_ok=True)
    stamped = replace(record, updated=now, created=record.created or now)
    # unique tmp name: two concurrent writers must not interleave into the
    # same tmp file before the atomic replace
    tmp = directory / f".{RECORD_NAME}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(asdict(stamped), indent=2, sort_keys=True))
    os.replace(tmp, directory / RECORD_NAME)


def load_record(root: Path, run_id: str) -> RunRecord:
    raw = json.loads((run_dir(root, run_id) / RECORD_NAME).read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"record is not a JSON object: {type(raw).__name__}")
    # Ignore unknown keys: after a bad-merge revert, older code must still be
    # able to read records written by newer code — a "corrupt" verdict here
    # would blind the sweep to the whole run.
    known = {k: v for k, v in raw.items() if k in RunRecord.__dataclass_fields__}
    return RunRecord(**known)


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

    O_EXCL makes acquisition atomic: exactly one contender wins, the rest see
    False and no-op. (O_EXCL is reliable on NFSv4/GPFS/Lustre; if the state
    root ever lands on NFSv3, this needs a link(2)-based lock instead —
    verify the cluster filesystem before trusting the lease.)
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
    path = run_dir(root, run_id) / LEASE_NAME
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise ValueError("lease is not a JSON object")
        known = {k: v for k, v in raw.items() if k in Lease.__dataclass_fields__}
        return Lease(**known)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, KeyError):
        # A crash between O_EXCL create and write leaves an empty/corrupt
        # lease. Synthesize one from the file mtime so the TTL path can
        # still reap it — otherwise the run is stranded forever behind a
        # lease nobody can read.
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None  # vanished between read and stat
        return Lease(holder="unreadable", holder_job_id="", acquired=mtime)


def update_lease_holder(
    root: Path, run_id: str, holder: str, holder_job_id: str, now: float
) -> None:
    """Hand a HELD lease to a new holder (e.g. tick → the wake job it just
    submitted). Atomic replace; only valid while the caller holds the lease."""
    directory = run_dir(root, run_id)
    tmp = directory / f".{LEASE_NAME}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(asdict(Lease(holder, holder_job_id, now))))
    os.replace(tmp, directory / LEASE_NAME)


def release_lease(root: Path, run_id: str) -> None:
    """For the lease HOLDER only. Non-holders must use reap_lease."""
    with contextlib.suppress(FileNotFoundError):
        (run_dir(root, run_id) / LEASE_NAME).unlink()


def reap_lease(root: Path, run_id: str, reaper: str, expected: Lease) -> bool:
    """Remove the stale lease you observed (and do NOT hold). True if THIS
    caller reaped exactly that lease.

    Rename-to-tombstone makes removal atomic (one of N concurrent reapers
    wins the rename); the identity check afterwards makes it a compare-and-
    swap: if the file we renamed is NOT the stale lease we observed — a
    faster reaper already reaped and a fresh lease was written — we restore
    it via link (which cannot clobber a newer lease) and stand down. The
    remaining hole needs a 3-party race inside this microsecond window and
    the singleton tick serialization makes that effectively unreachable;
    if it ever fires, the symptom is one duplicate wake, which the resumed
    session tolerates (sequential re-resume is safe).
    """
    directory = run_dir(root, run_id)
    tombstone = directory / f".{LEASE_NAME}.reaped.{reaper}"
    try:
        os.rename(directory / LEASE_NAME, tombstone)
    except FileNotFoundError:
        return False
    try:
        raw = json.loads(tombstone.read_text())
        got: Lease | None = (
            Lease(**{k: v for k, v in raw.items() if k in Lease.__dataclass_fields__})
            if isinstance(raw, dict)
            else None
        )
    except (OSError, ValueError, TypeError, KeyError):
        got = None  # unreadable — the corrupt lease we came to reap
    if got is not None and (got.holder != expected.holder or got.acquired != expected.acquired):
        # we grabbed someone's FRESH lease; put it back without clobbering
        try:
            os.link(tombstone, directory / LEASE_NAME)
        except FileExistsError:
            log.warning("lease race on %s: fresh lease displaced during reap", run_id)
        tombstone.unlink(missing_ok=True)
        return False
    tombstone.unlink(missing_ok=True)
    return True


def lease_is_stale(lease: Lease, now: float, ttl_s: float, holder_alive: bool | None) -> bool:
    """A lease is stale when its holder is known-dead, or too old.

    `holder_alive` is None when Slurm could not answer (query failure) — in
    that case only the TTL can prove staleness, never the holder check:
    an outage must not look like a dead holder.
    """
    if holder_alive is False:
        return True
    return (now - lease.acquired) > ttl_s
