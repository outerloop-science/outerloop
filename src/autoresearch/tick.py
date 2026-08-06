"""One tick of the loop: sentinel, heartbeat, and the fail-safe sweep.

The tick is stateless and bounded — everything durable lives in run-state
files (`runstate`) and Slurm. It implements the backup layers of the wake
design (docs/design/architecture.md, "Wake delivery and fail-safety"); the
primary layer (the afterany dependency job) is submitted by whoever launches
an experiment and needs no help from here.

Wake *delivery* is behind a seam (`WakeDispatcher`) so this module stays
testable and the actual session dispatch (harness + brief) can evolve
independently.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

from autoresearch.compute import GONE, SlurmCompute, SlurmQueryError, is_pending, is_terminal
from autoresearch.runstate import (
    ENDED,
    MAX_WAKE_ATTEMPTS,
    STUCK,
    WAITING,
    RunRecord,
    acquire_lease,
    lease_is_stale,
    list_runs,
    read_lease,
    reap_lease,
    release_lease,
    save_record,
    update_lease_holder,
)

log = logging.getLogger(__name__)

PAUSE_SENTINEL = "PAUSE"
HEARTBEAT_NAME = "heartbeat.json"

# Grace between "experiment terminal" and the sweep stepping in: the afterany
# job gets this long to deliver before the backup assumes it lost.
DEFAULT_GRACE_S = 15 * 60
# A held lease is stale after the session timeout plus slack.
DEFAULT_LEASE_TTL_S = 3600 + 15 * 60


class WakeDispatcher(Protocol):
    """Delivers one wake, called with the lease already held.

    Returns "" when delivery completed synchronously (the caller releases the
    lease), or the Slurm job id of an asynchronous wake job that now owns the
    lease (released by that job on completion; reaped by TTL if it dies)."""

    def dispatch(self, record: RunRecord, reason: str) -> str: ...


@dataclass
class RecordingDispatcher:
    """Test/dry-run dispatcher: records what would have been woken."""

    dispatched: list[tuple[str, str]] = field(default_factory=list)
    holder_job_id: str = ""  # set to simulate async dispatch

    def dispatch(self, record: RunRecord, reason: str) -> str:
        self.dispatched.append((record.run_id, reason))
        return self.holder_job_id


@dataclass(frozen=True)
class TickReport:
    paused: bool = False
    swept: int = 0
    woken: tuple[tuple[str, str], ...] = ()  # (run_id, reason)
    deferred: tuple[str, ...] = ()  # runs skipped on "Slurm unknown"
    reaped_leases: tuple[str, ...] = ()
    stuck: tuple[str, ...] = ()


def write_heartbeat(root: Path, now: float) -> None:
    payload = json.dumps({"ts": now, "host": socket.gethostname(), "pid": os.getpid()})
    tmp = root / f".{HEARTBEAT_NAME}.tmp"
    tmp.write_text(payload)
    os.replace(tmp, root / HEARTBEAT_NAME)


def _holder_alive(compute: SlurmCompute, lease_job_id: str) -> bool | None:
    """True/False when Slurm answered; None when it could not (an outage
    must not look like a dead holder)."""
    if not lease_job_id:
        return None
    try:
        state = compute.status(lease_job_id)
    except SlurmQueryError:
        return None
    return not (is_terminal(state) or state == GONE)


def _wake(
    root: Path,
    record: RunRecord,
    reason: str,
    dispatcher: WakeDispatcher,
    now: float,
    holder: str,
) -> bool:
    """Lease-guarded wake. True when this tick delivered (or handed off) it.

    The attempt counter is bumped BEFORE dispatch, so a dispatcher that dies
    mid-delivery still counts toward the stuck threshold.
    """
    if not acquire_lease(root, record.run_id, holder, holder_job_id="", now=now):
        return False
    bumped = replace(
        record,
        wake_attempts=record.wake_attempts + 1,
        # repair legacy records as we touch them: save_record (rightly)
        # refuses to write a waiting run without a deadline
        deadline=record.deadline if record.deadline > 0 else now,
    )
    save_record(root, bumped, now)
    try:
        holder_job = dispatcher.dispatch(bumped, reason)
    except Exception as exc:
        log.warning("wake dispatch failed for %s: %s: %s", record.run_id, type(exc).__name__, exc)
        release_lease(root, record.run_id)
        return False
    if holder_job:
        # An async wake job now owns the lease; it releases on completion,
        # and the TTL/holder-dead check reaps it if it dies.
        update_lease_holder(root, record.run_id, f"wake-job:{holder_job}", holder_job, now)
    else:
        release_lease(root, record.run_id)
    return True


def sweep(
    root: Path,
    compute: SlurmCompute,
    dispatcher: WakeDispatcher,
    now: float,
    grace_s: float = DEFAULT_GRACE_S,
    lease_ttl_s: float = DEFAULT_LEASE_TTL_S,
    dry_run: bool = False,
) -> TickReport:
    """The backup wake layers, applied to every waiting run.

    dry_run reports what WOULD happen with zero writes — no leases, no
    attempt counters, no dispatch — so the plumbing can run live before the
    real dispatcher exists.
    """
    woken: list[tuple[str, str]] = []
    deferred: list[str] = []
    reaped: list[str] = []
    stuck: list[str] = []
    holder = f"tick:{socket.gethostname()}:{os.getpid()}"
    records = [r for r in list_runs(root) if r.state == WAITING]

    def wake(record: RunRecord, reason: str, tag: str) -> None:
        if dry_run or _wake(root, record, reason, dispatcher, now, holder):
            woken.append((record.run_id, tag))

    for record in records:
        try:
            _sweep_one(
                root,
                compute,
                dispatcher,
                now,
                grace_s,
                lease_ttl_s,
                dry_run,
                record,
                holder,
                wake,
                deferred,
                reaped,
                stuck,
            )
        except Exception as exc:
            log.warning("sweep failed on %s: %s: %s", record.run_id, type(exc).__name__, exc)

    return TickReport(
        swept=len(records),
        woken=tuple(woken),
        deferred=tuple(deferred),
        reaped_leases=tuple(reaped),
        stuck=tuple(stuck),
    )


def _sweep_one(
    root: Path,
    compute: SlurmCompute,
    dispatcher: WakeDispatcher,
    now: float,
    grace_s: float,
    lease_ttl_s: float,
    dry_run: bool,
    record: RunRecord,
    holder: str,
    wake,
    deferred: list[str],
    reaped: list[str],
    stuck: list[str],
) -> None:
    if True:
        # Leases first: a LIVE wake in flight owns this run — even the stuck
        # verdict must wait for it (its session may be the one that succeeds).
        lease = read_lease(root, record.run_id)
        if lease is not None:
            alive = _holder_alive(compute, lease.holder_job_id)
            if not lease_is_stale(lease, now, lease_ttl_s, alive):
                return
            if dry_run:
                reaped.append(record.run_id)
                return
            if not reap_lease(root, record.run_id, reaper=f"{os.getpid()}-{now}"):
                return  # a concurrent tick reaped it first; it owns redelivery
            reaped.append(record.run_id)

        # Layer 5: too many failed attempts is a terminal, reported state.
        if record.wake_attempts >= MAX_WAKE_ATTEMPTS:
            if not dry_run:
                ended = replace(
                    record,
                    state=ENDED,
                    ending=STUCK,
                    ending_note=f"{record.wake_attempts} wake attempts failed",
                )
                save_record(root, ended, now)
            stuck.append(record.run_id)
            return

        if not record.experiment_job_id:
            return  # not yet submitted; not the sweep's business

        try:
            state = compute.status(record.experiment_job_id)
        except SlurmQueryError:
            # Layer 4's rule: query failure is "Slurm unknown", never "gone".
            deferred.append(record.run_id)
            return

        # deadline <= 0 cannot be written by save_record for waiting runs;
        # if one exists anyway (legacy/hand-edited), treat it as already past
        # for GONE — a vanished-experiment wake is safe — but never for
        # PENDING, where the consequence would be cancelling a healthy job.
        past_deadline = record.deadline <= 0 or now > record.deadline

        if is_terminal(state):
            # Layer 3, with real grace: time runs from when the sweep FIRST
            # saw the experiment terminal, not from submission — the afterany
            # job gets the full window to deliver before the backup steps in.
            if record.terminal_seen <= 0:
                if dry_run:
                    # no writes in dry-run: report the would-wake now so the
                    # terminal path is visible to live plumbing checks
                    wake(record, f"experiment {state}", state)
                else:
                    save_record(
                        root,
                        replace(
                            record,
                            terminal_seen=now,
                            # repair legacy records as we touch them (see _wake)
                            deadline=record.deadline if record.deadline > 0 else now,
                        ),
                        now,
                    )
                return
            if now - record.terminal_seen >= grace_s:
                wake(record, f"experiment {state}", state)
        elif state == GONE:
            if past_deadline:
                wake(record, "experiment vanished from Slurm", "vanished")
            # else: sacct lag right after submission is normal; wait.
        elif is_pending(state) and record.deadline > 0 and now > record.deadline:
            # Unschedulable in practice: cancel (best-effort — scancel
            # trouble must not abort the sweep), then wake with that fact.
            if not dry_run:
                try:
                    compute.cancel(record.experiment_job_id)
                except Exception as exc:  # scancel trouble is never fatal here
                    log.warning("cancel %s failed: %s", record.experiment_job_id, exc)
            wake(record, "experiment unschedulable (pending past deadline)", "unschedulable")
        # RUNNING (or recently pending): nothing to do; the afterany job has it.


def tick(
    root: Path,
    compute: SlurmCompute,
    dispatcher: WakeDispatcher,
    now: float,
    grace_s: float = DEFAULT_GRACE_S,
    lease_ttl_s: float = DEFAULT_LEASE_TTL_S,
    dry_run: bool = False,
) -> TickReport:
    """One full tick. Pause sentinel wins over everything: a paused loop
    heartbeats (so the watchdog stays quiet) but touches nothing."""
    write_heartbeat(root, now)
    if (root / PAUSE_SENTINEL).exists():
        log.info("pause sentinel present; tick is a no-op")
        return TickReport(paused=True)
    return sweep(root, compute, dispatcher, now, grace_s, lease_ttl_s, dry_run=dry_run)


@dataclass
class LoggingDispatcher:
    """Never dispatched in production today: main() runs the sweep in
    dry_run mode until the real session dispatcher lands (phase 5), so no
    lease is taken and no attempt is counted. This exists for the seam."""

    def dispatch(self, record: RunRecord, reason: str) -> str:
        log.info("WOULD WAKE %s (%s) — session dispatch lands in phase 5", record.run_id, reason)
        return ""


def main() -> int:
    import argparse
    import time

    parser = argparse.ArgumentParser(description="One tick of the autoresearch loop.")
    parser.add_argument("--root", required=True, type=Path, help="state root on the shared FS")
    parser.add_argument("--grace-s", type=float, default=DEFAULT_GRACE_S)
    parser.add_argument("--lease-ttl-s", type=float, default=DEFAULT_LEASE_TTL_S)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    args.root.mkdir(parents=True, exist_ok=True)
    # dry_run until the phase-5 dispatcher exists: the live loop must not
    # mutate run state it cannot follow through on.
    report = tick(
        args.root,
        SlurmCompute(),
        LoggingDispatcher(),
        now=time.time(),
        grace_s=args.grace_s,
        lease_ttl_s=args.lease_ttl_s,
        dry_run=True,
    )
    log.info(
        "tick done: paused=%s swept=%d woken=%d deferred=%d reaped=%d stuck=%d",
        report.paused,
        report.swept,
        len(report.woken),
        len(report.deferred),
        len(report.reaped_leases),
        len(report.stuck),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
