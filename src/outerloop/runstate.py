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
import fcntl
import json
import logging
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TextIO

log = logging.getLogger(__name__)

# Lifecycle states.
RUNNING = "running"
PARKED = "parked"
ENDED = "ended"

STATES = (RUNNING, PARKED, ENDED)

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

# How long the session-spawning lanes stay paused after an API outage is
# stamped. 45 minutes skips roughly one tick, so during a sustained outage
# one canary session per ~hour re-probes the API instead of every lane
# burning attempts every half hour. Throttling (429/529) is transient by
# nature and gets a short pause instead — a momentary spike must not idle
# the orchestrator for most of an hour (review finding).
OUTAGE_COOLDOWN_S = 45 * 60
THROTTLE_COOLDOWN_S = 5 * 60
_THROTTLE_HINTS = ("rate_limit", "overloaded")
# Stamps are written on compute nodes and read on other hosts: a stamp a
# few seconds "in the future" is NTP skew and must count as active, while
# a far-future timestamp is corruption and must not pause forever.
MAX_CLOCK_SKEW_S = 5 * 60

MAX_WAKE_ATTEMPTS = 3


def _outage_path(root: Path, role: str) -> Path:
    # per-ROLE latches: roles hold separate keys, and a permanently dead
    # steward key re-stamping its latch every cooldown must not keep the
    # solver lanes paused forever (review finding). Role names are ours
    # ("solver"/"steward"), sanitized only as filename hygiene.
    safe = "".join(ch for ch in role if ch.isalnum() or ch == "-") or "solver"
    return root / f"outage-{safe}.json"


def stamp_outage(root: Path, detail: str, now: float, role: str = "solver") -> None:
    """Record that the API refused this ROLE's key (atomic rename)."""
    root.mkdir(parents=True, exist_ok=True)
    # pid in the tmp name, like save_record: several lanes' jobs can fail
    # to the same outage in one window, and interleaved writers must not
    # install a truncated stamp — an unreadable latch reads as NO pause,
    # which is exactly the failure the latch exists to prevent
    path = _outage_path(root, role)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    cooldown = (
        THROTTLE_COOLDOWN_S
        if any(hint in detail.casefold() for hint in _THROTTLE_HINTS)
        else OUTAGE_COOLDOWN_S
    )
    tmp.write_text(json.dumps({"detail": detail[:300], "time": now, "cooldown_s": cooldown}))
    os.replace(tmp, path)


def outage_active(root: Path, now: float, role: str = "solver") -> str:
    """The stamped detail while this role's cooldown holds, else "". The
    cooldown lives IN the stamp (decided at stamp time from the failure
    class); unreadable or stale stamps read as inactive — a corrupt latch
    must never brick the loop; cross-host clock skew within
    MAX_CLOCK_SKEW_S counts as active, anything further future as
    corrupt."""
    path = _outage_path(root, role)
    try:
        data = json.loads(path.read_text())
        stamped = float(data["time"])
        detail = str(data.get("detail", ""))
        cooldown_s = float(data.get("cooldown_s", OUTAGE_COOLDOWN_S))
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    if -MAX_CLOCK_SKEW_S <= now - stamped < cooldown_s:
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
    run_job_id: str = ""  # slurm job running the attempt itself; lets the
    # sweep end records whose job was KILLED (walltime/preemption/node
    # death) rather than crashed — signals leave no exception to contain.
    # INVARIANT: any future path that re-enters `running` from a NEW
    # job must re-stamp this field, or the sweep will judge the run by a
    # stale terminal job. (No such path exists today.)
    resume_session_id: str = ""  # harness session to resume on wake
    pr_url: str = ""  # the run's open PR, once one exists
    benchmark: str = ""  # contract benchmark this run works on
    # The author this run was STARTED with ("" backend = legacy/claude). A wake or
    # follow-up reproduces the run's OWN author from these, not the current fleet
    # default, so a fleet backend flip never resumes a run on the wrong backend,
    # model, or key. backend and model are a PAIR — a claude backend needs a
    # claude model and vice versa — so both are persisted together.
    author_backend: str = ""
    author_model: str = ""
    # The resolved author key FILE PATH (not the key) this run used, so a wake or
    # follow-up reproduces the exact key — an explicit --key-file survives, and an
    # in-flight run is immune to a later env change. "" = resolve per backend
    # (legacy records, and the common config-driven case).
    author_key_file: str = ""
    inbox_seq: int = 0  # last message delivered by a completed session leg
    # The exact PR head the auto-arm may merge: set at publish to the pushed
    # head when the PR was published UNDER merge:auto with a CLEAN panel
    # (#171's arming condition), carried forward by signature-clean syncs
    # (same measured bytes), cleared by any code-changing push. Binding the
    # blessing to a sha — not a flag — means a crashed responder, a live
    # one, or any unrecorded push simply fails the equality: the tick arms
    # only when GitHub's head IS this sha. Empty = never arm (legacy too).
    auto_blessed_head: str = ""
    auto_bless_reason: str = ""
    issue_number: int = 0  # the requesting issue, when the requested lane started this run
    wake_attempts: int = 0
    deadline: float = 0.0  # unix; submit+walltime+slack, re-based on start
    terminal_seen: float = 0.0  # when the sweep first saw the experiment terminal
    # A PARKED climb's re-entry point: the committed shas, drawn seeds, and the
    # candidate snapshot ref a fresh process reconstructs the measure-and-decide
    # phase from. `phase` says WHICH park (baseline, before the session; or
    # candidate, after it). A JSON dict — small, forward-compatible — not the
    # session state (that is `resume_session_id`).
    stage: dict[str, object] = field(default_factory=dict)
    ending: str = ""  # one of ENDINGS once state == ENDED
    ending_note: str = ""
    created: float = 0.0
    updated: float = 0.0
    # when housekeeping removed this ended run's ws/ and ws-home/ (0 = never);
    # the record, report, transcripts, and ledger stay (housekeeping.py)
    workspace_shed: float = 0.0

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
    """Serialize record writers; a persisted ending cannot be replaced."""
    directory = run_dir(root, record.run_id)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".record-lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            latest = load_record(root, record.run_id)
        except FileNotFoundError:
            latest = None
        if latest is not None and latest.state == ENDED:
            return
        _save_record(root, record, now)


def mark_launches_cancelled(root: Path, run_id: str, now: float) -> None:
    """Stamp terminal cleanup without allowing stale wake writes."""
    directory = run_dir(root, run_id)
    with (directory / ".record-lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = load_record(root, run_id)
        if record.state == ENDED and not record.stage.get("launches_cancelled"):
            _save_record(
                root, replace(record, stage={**record.stage, "launches_cancelled": True}), now
            )


def mark_workspace_shed(root: Path, run_id: str, now: float) -> None:
    """Stamp workspace cleanup on the latest terminal record."""
    directory = run_dir(root, run_id)
    with (directory / ".record-lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = load_record(root, run_id)
        if record.state == ENDED and not record.workspace_shed:
            _save_record(root, replace(record, workspace_shed=now), now)


def _save_record(root: Path, record: RunRecord, now: float) -> None:
    """Atomic write: a crash mid-save leaves the previous record intact."""
    if record.state not in STATES:
        raise ValueError(f"unknown state {record.state!r}")
    if record.state == ENDED and record.ending not in ENDINGS:
        raise ValueError(f"ended run needs a valid ending, got {record.ending!r}")
    if record.state == PARKED and record.experiment_job_id and record.deadline <= 0:
        # A waiting run without a deadline is invisible to the deadline floor
        # — the exact "silently immortal run" the fail-safe design forbids.
        raise ValueError("waiting run with an experiment needs a deadline")
    directory = run_dir(root, record.run_id)
    directory.mkdir(parents=True, exist_ok=True)
    stamped = replace(record, updated=now, created=record.created or now)
    # unique tmp name: two concurrent writers must not interleave into the
    # same tmp file before the atomic replace
    tmp = directory / f".{RECORD_NAME}.{os.getpid()}.tmp"
    payload = asdict(stamped)
    path = directory / RECORD_NAME
    if path.exists():
        old = json.loads(path.read_text())
        for key in (
            "last_comment_id",
            "last_review_id",
            "last_review_comment_id",
            "panel_wake_text",
        ):
            if key in old:
                payload[key] = old[key]
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    os.replace(tmp, directory / RECORD_NAME)


def load_record(root: Path, run_id: str) -> RunRecord:
    raw = json.loads((run_dir(root, run_id) / RECORD_NAME).read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"record is not a JSON object: {type(raw).__name__}")
    old_state = str(raw.get("state", ""))
    if old_state == "concluding":
        log.warning("run %s: migrating concluding to parked", run_id)
    raw["state"] = {
        "implementing": RUNNING,
        "waiting": PARKED,
        "in-review": PARKED,
        "concluding": PARKED,
    }.get(old_state, old_state)
    # Unknown fields must not blind an older kernel to a live run.
    known = {k: v for k, v in raw.items() if k in RunRecord.__dataclass_fields__}
    record = RunRecord(**known)
    return record


def migrate_inbox(root: Path, run_id: str, now: float) -> None:
    """Move legacy inbox data once, while the caller holds the wake lease."""
    from outerloop.inbox import Message, advance_github_positions, append, thread_for

    if read_lease(root, run_id) is None:
        raise RuntimeError("inbox migration requires the run lease")
    directory = run_dir(root, run_id)
    with (directory / ".record-lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / RECORD_NAME
        raw = json.loads(path.read_text())
        if raw.get("state") == ENDED:
            return
        keys = ("last_comment_id", "last_review_id", "last_review_comment_id")
        # the follow-up job and its dispatched re-measure no longer exist:
        # an old record still naming them would count as legacy forever
        jobs = ("followup_job_id", "followup_stage")
        if not any(key in raw for key in (*keys, "panel_wake_text", *jobs)):
            return
        if (
            any(key in raw for key in keys)
            and not (directory / "inbox" / "positions.json").exists()
        ):
            advance_github_positions(
                directory,
                dict(
                    zip(
                        ("comment", "review", "review_comment"),
                        (int(raw.get(key, 0)) for key in keys),
                        strict=True,
                    )
                ),
            )
        record = load_record(root, run_id)
        if raw.get("panel_wake_text"):
            append(
                run_dir(root, run_id),
                Message(
                    0,
                    "panel-verdict",
                    "panel",
                    thread_for(record),
                    float(raw.get("updated", 0)),
                    "panel:legacy",
                    {
                        "findings": [
                            {
                                "blocking": True,
                                "summary": "Pending panel findings",
                                "detail": raw["panel_wake_text"],
                            }
                        ]
                    },
                ),
            )
        for key in (*keys, "panel_wake_text", *jobs):
            if raw.pop(key, None):
                log.info("run %s: legacy %s dropped by the migration", run_id, key)
        tmp = directory / f".migration.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(raw, indent=2, sort_keys=True))
        os.replace(tmp, path)


def list_runs(root: Path) -> list[RunRecord]:
    """Every readable run record; unreadable ones are logged, not fatal —
    one corrupt file must not stop the sweep."""
    records = []
    runs_root = root / "runs"
    if not runs_root.is_dir():
        return []
    for directory in sorted(runs_root.iterdir()):
        # Skip entries that are not runs (no record file) — e.g. the `baselines`
        # eval cache lives under runs/ but has no state.json and is not a run.
        # A dir that HAS a record which fails to parse still logs below: a
        # corrupt run is a real signal; a missing record is not.
        if not (directory / RECORD_NAME).is_file():
            continue
        try:
            records.append(load_record(root, directory.name))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            log.warning("unreadable run record %s: %s", directory, exc)
    return records


# --- leases ---


def _tick_lease_state(lease: TextIO) -> tuple[str, float, float]:
    """(holder, heartbeat, ttl) written in TICK; ("", 0, 0) when it was released."""
    lease.seek(0)
    raw = lease.read()
    if not raw:
        return "", 0.0, 0.0
    try:
        record = json.loads(raw)
        return (
            str(record.get("holder", "unknown")),
            float(record.get("heartbeat", 0)),
            float(record.get("ttl", 0)),
        )
    except (ValueError, TypeError, AttributeError):
        return "unreadable", os.fstat(lease.fileno()).st_mtime, 0.0


def _tick_lease_free(previous: str, heartbeat: float, host: str, now: float, ttl_s: float) -> bool:
    """A free lock left by a process on our own host is proof it died; from
    another host the last heartbeat must be older than the TTL, since file
    locks may not reach across nodes."""
    if not previous:
        return True
    return previous.rsplit(":", 1)[0] == host or now - heartbeat > ttl_s


def tick_lease_holder(root: Path, now: float, ttl_s: float, host: str) -> str:
    """Who holds the root's tick lease, or "" when nobody does (read-only)."""
    path = root / "TICK"
    if not path.exists():
        return ""
    with path.open("a+") as lease:
        try:
            fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _tick_lease_state(lease)[0] or "unknown"
        previous, heartbeat, held_ttl = _tick_lease_state(lease)
        # the holder's own cadence decides staleness, never the reader's
        free = _tick_lease_free(previous, heartbeat, host, now, max(ttl_s, held_ttl))
        return "" if free else previous


def acquire_tick_lease(
    root: Path, holder: str, now: float, ttl_s: float, lease: TextIO | None = None
) -> TextIO:
    """Take or heartbeat TICK; keep its file lock until the loop exits.

    `holder` is host:pid. The lock fences a live holder however old its
    heartbeat; a dead one is taken over at once on the same host and after
    its own TTL from another. A heartbeat that finds another holder's record
    (written from a node the lock did not reach) raises instead of
    overwriting it. Never unlink TICK: every contender must lock the same
    inode.
    """
    heartbeat_only = lease is not None
    if lease is None:
        root.mkdir(parents=True, exist_ok=True)
        lease = (root / "TICK").open("a+")
        try:
            locked = True
            try:
                fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                locked = False
            previous, heartbeat, held_ttl = _tick_lease_state(lease)
            host = holder.rsplit(":", 1)[0]
            free = _tick_lease_free(previous, heartbeat, host, now, max(ttl_s, held_ttl))
            if not locked or not free:
                raise RuntimeError(f"tick lease held by {previous or 'unknown'}")
            if previous:
                log.warning("taking over the tick lease left by %s", previous)
        except BaseException:
            lease.close()
            raise
    try:
        if heartbeat_only:
            current = _tick_lease_state(lease)[0]
            if current not in ("", holder):
                raise RuntimeError(f"tick lease taken by {current}")
        lease.seek(0)
        lease.truncate()
        lease.write(json.dumps({"holder": holder, "heartbeat": now, "ttl": ttl_s}))
        lease.flush()
        return lease
    except BaseException:
        lease.close()
        raise


def release_tick_lease(lease: TextIO, holder: str = "") -> None:
    """Release the root lock; clear the record only while it is still ours."""
    try:
        if not lease.closed and (not holder or _tick_lease_state(lease)[0] in ("", holder)):
            lease.seek(0)
            lease.truncate()
            lease.flush()
    finally:
        lease.close()


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
    """A lease is stale when its holder is known-dead, or — when Slurm cannot
    say — too old. A holder Slurm reports alive is never stale by age: a wake
    armed at park time waits in the queue for as long as the evals run, and
    its walltime bounds it once it starts.

    `holder_alive` is None when Slurm could not answer (query failure) — in
    that case only the TTL can prove staleness, never the holder check:
    an outage must not look like a dead holder.
    """
    if holder_alive is False:
        return True
    if holder_alive is True:
        return False
    return (now - lease.acquired) > ttl_s
