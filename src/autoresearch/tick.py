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

import contextlib
import json
import logging
import os
import socket
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from autoresearch.compute import (
    GONE,
    JobSpec,
    SlurmCompute,
    SlurmError,
    SlurmQueryError,
    is_pending,
    is_terminal,
    quote_command,
)
from autoresearch.disk import DEFAULT_MIN_FREE_BYTES, check_disk
from autoresearch.limits import EffectiveLimits, effective_limits
from autoresearch.runstate import (
    ABORTED,
    ENDED,
    IMPLEMENTING,
    IN_REVIEW,
    MAX_WAKE_ATTEMPTS,
    STUCK,
    WAITING,
    RunRecord,
    acquire_lease,
    lease_is_stale,
    list_runs,
    load_record,
    read_lease,
    reap_lease,
    release_lease,
    run_dir,
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
    lease (released by that job on completion; reaped by TTL if it dies).

    Contract for real (phase-5) dispatchers: a wake that RESULTS IN PROGRESS
    must either move the run out of `waiting` or reset `wake_attempts` —
    the counter means "wakes since the run last made progress", and layer 5
    ends the run as stuck when it reaches MAX_WAKE_ATTEMPTS."""

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
    implementing_ended: tuple[str, ...] = ()  # killed climbs the sweep closed out
    review_ended: tuple[tuple[str, str], ...] = ()  # (run_id, ending)
    followups_submitted: tuple[tuple[str, str], ...] = ()  # (run_id, job_id)
    intake: tuple[str, str] = ("", "")  # (issue tag, job_id) when one was claimed
    self_initiated: tuple[str, str] = ("", "")  # (benchmark, job_id) when one launched
    steward: tuple[str, str] = ("", "")  # (issue tag, job_id) when a stewardship launched
    disk: tuple[str, ...] = ()  # preflight warnings (home entries are warn-only)
    launch_blocked: bool = False  # True when the preflight turned launch lanes off


@dataclass(frozen=True)
class FollowupSpec:
    """How the tick launches follow-up jobs for in-review runs."""

    account: str
    partition: str
    run_root: Path
    image: str
    home: Path  # AUTORESEARCH_HOME: cwd for the submitted job
    bot_login: str = "agentic-learning-bot"
    time_minutes: int = 60
    pat_file: str = ""  # forwarded to the job; "" = the followup CLI default
    key_file: str = ""
    target: str = ""  # the repo the intake pass scans for requested-lane issues
    # the STEWARD'S OWN key (role separation): the steward lane stays off
    # until the operator provisions it
    steward_key_file: str = ""


def service_in_review(
    root: Path,
    github: Any,  # GitHubClient (Any keeps tick importable without github deps)
    compute: SlurmCompute,
    spec: FollowupSpec,
    now: float,
    dry_run: bool = False,
    allow_submit: bool = True,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """PR-state transitions + follow-up job submission for in-review runs.

    allow_submit=False (disk preflight failed) keeps the cheap state
    transitions — ending merged/closed runs still matters — but submits no
    new session jobs.

    The tick only READS GitHub here (cheap, every cycle); the session-running
    work happens in a submitted job, which takes the run lease itself — a
    duplicate submission no-ops on the lease, and `followup_job_id` keeps the
    tick from queueing duplicates in the first place.
    """
    from autoresearch.followup import close_if_done, has_new_comments

    ended: list[tuple[str, str]] = []
    submitted: list[tuple[str, str]] = []
    for record in list_runs(root):
        if record.state != IN_REVIEW or not record.pr_url:
            continue
        try:
            ending = close_if_done(root, record, github, now)
            if ending:
                ended.append((record.run_id, ending))
                continue
            # Steward records are serviced with the STEWARD'S key and the
            # steward scope check (respond_once derives the mode from the
            # record's agent id); without a provisioned steward key the
            # lane stays human-answered.
            is_steward = record.agent_id.startswith("steward")
            if is_steward and not spec.steward_key_file:
                continue
            if not has_new_comments(record, github, spec.bot_login):
                continue
            if record.followup_job_id:
                try:
                    state = compute.status(record.followup_job_id)
                    if not (is_terminal(state) or state == GONE):
                        continue  # a follow-up job is already queued/running
                except SlurmQueryError:
                    continue  # unknown — do not stack another job
            # the wake-attempt counter caps follow-up retries too: a responder
            # that cannot advance its cursors must not burn a session per tick
            if record.wake_attempts >= MAX_WAKE_ATTEMPTS:
                log.warning(
                    "run %s: %d follow-up attempts without progress; not resubmitting",
                    record.run_id,
                    record.wake_attempts,
                )
                continue
            if not allow_submit:
                log.warning("run %s has new comments but disk preflight failed", record.run_id)
                continue
            if dry_run:
                submitted.append((record.run_id, "dry-run"))
                continue
            argv = [
                "uv",
                "run",
                "python",
                "-m",
                "autoresearch.followup",
                "--run-root",
                str(spec.run_root),
                "--run-id",
                record.run_id,
                "--image",
                spec.image,
                "--bot-login",
                spec.bot_login,
                "--job-minutes",
                str(spec.time_minutes),
            ]
            if spec.pat_file:
                argv += ["--pat-file", spec.pat_file]
            key_file = spec.steward_key_file if is_steward else spec.key_file
            if key_file:
                argv += ["--key-file", key_file]
            command = quote_command(argv)
            job_id = compute.submit(
                JobSpec(
                    job_name=f"followup-{record.run_id}"[:60],
                    account=spec.account,
                    partition=spec.partition,
                    time_minutes=spec.time_minutes,
                    command=f"cd {quote_command([str(spec.home)])} && {command}",
                    cpus=4,
                    mem="8G",
                )
            )
            # read-modify-write on the FRESH record: the submitted job may
            # already be saving its own fields
            latest = load_record(root, record.run_id)
            save_record(
                root,
                replace(
                    latest,
                    followup_job_id=job_id,
                    wake_attempts=latest.wake_attempts + 1,
                ),
                now,
            )
            submitted.append((record.run_id, job_id))
        except (SlurmError, Exception) as exc:
            log.warning("in-review service failed for %s: %s", record.run_id, exc)
    return ended, submitted


def write_heartbeat(root: Path, now: float, disk: dict[str, object] | None = None) -> None:
    """Best-effort: a heartbeat that cannot be written (full disk) must not
    kill the tick — the tick can still end runs and post to GitHub."""
    payload: dict[str, object] = {"ts": now, "host": socket.gethostname(), "pid": os.getpid()}
    if disk is not None:
        payload["disk"] = disk
    try:
        tmp = root / f".{HEARTBEAT_NAME}.tmp"
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, root / HEARTBEAT_NAME)
    except OSError as exc:
        log.warning("heartbeat write failed: %s", exc)


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
        # NOT the global dry_run: that flag exists because the WAKE
        # dispatcher is still a placeholder; ending killed climbs' records
        # dispatches nothing and must run live even while wakes stay dry.
        implementing_ended=tuple(_sweep_implementing(root, compute, now, grace_s)),
    )


def _kill_stamp(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "climb-terminal-seen"


def _sweep_implementing(root: Path, compute: SlurmCompute, now: float, grace_s: float) -> list[str]:
    """End `implementing` records whose climb job died without a verdict.

    A climb that CRASHES contains its own ending (climb.py); a climb that is
    KILLED — walltime, preemption, scancel after the SIGTERM grace, node
    death — leaves no exception to contain, and before this pass its record
    stranded in `implementing` forever (the picker's stranded guard freed
    the lane but nothing ever recorded the ending). Slurm truth decides:
    job terminal or GONE, plus a grace so a just-finished healthy climb can
    write its own final state first. Outage never reads as dead. Legacy
    records without a job id age out on the stranded window instead.
    """
    ended: list[str] = []
    for record in list_runs(root):
        if record.state != IMPLEMENTING:
            continue
        try:
            if record.climb_job_id:
                try:
                    state = compute.status(record.climb_job_id)
                except SlurmQueryError:
                    continue  # Slurm outage must not read as a dead job
                if not (is_terminal(state) or state == GONE):
                    continue  # alive; the climb owns its own record
                # Grace runs from FIRST OBSERVED terminal: during Slurm's
                # KillWait the job already reports terminal while the
                # SIGTERM containment is still writing its honest ending —
                # record age would be hours and protect nothing. The stamp
                # is a write-once SIDECAR file, never a record write: while
                # the climb may still be alive the sweep must not touch the
                # record at all (a load-modify-replace here could revert a
                # concurrently written ending), and the waiting sweep's
                # terminal_seen field stays reserved for the EXPERIMENT job.
                stamp = _kill_stamp(root, record.run_id)
                if not stamp.exists():
                    try:
                        fd = os.open(stamp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                        try:
                            os.write(fd, f"{now}".encode())
                        finally:
                            os.close(fd)
                    except FileExistsError:
                        pass  # a concurrent tick stamped it; its clock stands
                    except OSError as exc:
                        log.warning("kill-stamp write failed for %s: %s", record.run_id, exc)
                    continue
                # An empty stamp (write failed after create — the disk-full
                # case — or a concurrent tick mid-write) must fall back to
                # mtime, NOT to epoch 0, which would skip the grace outright.
                try:
                    raw = stamp.read_text().strip()
                    seen = float(raw) if raw else stamp.stat().st_mtime
                except (OSError, ValueError):
                    try:
                        seen = stamp.stat().st_mtime
                    except OSError:
                        continue  # stamp vanished mid-read; next tick decides
                if now - seen < grace_s:
                    continue
                note = f"climb job {record.climb_job_id} ended {state} without a verdict"
            else:
                # No Slurm evidence at all (legacy record, or a manual dev
                # invocation without SLURM_JOB_ID): only the run DEADLINE —
                # past which nothing legitimately lives — justifies a
                # terminal verdict; the shorter stranded window merely
                # frees the picker lane and must not author endings.
                deadline = record.deadline if record.deadline > 0 else (record.created + 24 * 3600)
                if now < deadline:
                    continue
                note = "implementing with no recorded climb job, past its run deadline"
            fresh = load_record(root, record.run_id)
            if fresh.state != IMPLEMENTING:
                continue  # the climb landed its own ending meanwhile
            if fresh.experiment_job_id:
                # defensive: no current path records an experiment while
                # still implementing, but an orphan GPU job burning budget
                # after its run is declared dead must never survive one
                with contextlib.suppress(Exception):
                    compute.cancel(fresh.experiment_job_id)
            save_record(
                root,
                replace(
                    fresh,
                    state=ENDED,
                    ending=ABORTED,
                    ending_note=(
                        f"{note} — ended by the sweep (a killed climb "
                        f"leaves no exception to contain)"
                    ),
                ),
                now,
            )
            # every ending produces a report — but never clobber one the
            # climb already wrote before it was killed
            report_path = run_dir(root, record.run_id) / "report.md"
            if not report_path.exists():
                try:
                    report_path.write_text(
                        f"# Run report — {record.target} / {record.benchmark}\n"
                        f"Outcome: **aborted** (climb job killed)\n"
                        f"Note: {note}\n"
                    )
                except OSError as exc:
                    log.warning("sweep report write failed for %s: %s", record.run_id, exc)
            log.warning("sweep ended implementing run %s: %s", record.run_id, note)
            ended.append(record.run_id)
        except Exception as exc:  # per-record isolation, like the waiting sweep
            log.warning("implementing-sweep failed on %s: %s", record.run_id, exc)
    return ended


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
            if not reap_lease(root, record.run_id, reaper=f"{os.getpid()}-{now}", expected=lease):
                return  # a concurrent tick reaped it first; it owns redelivery
            reaped.append(record.run_id)

        # Layer 5: too many failed attempts is a terminal, reported state.
        if record.wake_attempts >= MAX_WAKE_ATTEMPTS:
            if not dry_run:
                ended = replace(
                    record,
                    state=ENDED,
                    ending=STUCK,
                    ending_note=(
                        f"{record.wake_attempts} wake attempts without the run leaving 'waiting'"
                    ),
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
    github: Any = None,
    followup_spec: FollowupSpec | None = None,
    followup_dry_run: bool = False,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
) -> TickReport:
    """One full tick. Pause sentinel wins over everything: a paused loop
    heartbeats (so the watchdog stays quiet) but touches nothing.

    Disk preflight gates every lane that LAUNCHES new work (follow-up jobs,
    intake claims, self-initiated climbs): a session started on a full or
    nearly-full filesystem dies mid-flight in ways that lose data. The sweep
    still runs — its writes are small, per-record contained, and ending runs
    matters more when storage is failing, not less.
    """
    # Heartbeat FIRST, before any probe: check_disk touches $HOME (a
    # different filesystem), and a hung mount there must not starve the
    # watchdog signal. The disk-annotated heartbeat follows once known.
    write_heartbeat(root, now)
    disk_health = check_disk(root, min_free_bytes=min_free_bytes)
    write_heartbeat(root, now, disk=disk_health.as_dict())
    for warning in disk_health.warnings():
        log.warning("disk: %s", warning)
    if (root / PAUSE_SENTINEL).exists():
        log.info("pause sentinel present; tick is a no-op")
        return TickReport(paused=True)
    report = sweep(root, compute, dispatcher, now, grace_s, lease_ttl_s, dry_run=dry_run)
    launch_ok = disk_health.launch_ok()
    if not launch_ok:
        log.warning("disk preflight failed; launch lanes are OFF this tick")
    if github is not None and followup_spec is not None:
        # ONE contract fetch per tick feeds every lane: the requested and
        # self-initiated lanes need its benchmarks, and all three lanes now
        # take their session/job limits from its budgets — clamped by our
        # ceilings (limits.py), so a target shapes spend, never raises it.
        # A failed fetch leaves in-review servicing running on defaults;
        # the launch lanes need the contract and sit out this tick.
        contract = None
        if followup_spec.target:
            try:
                from autoresearch.contract import load_contract

                raw = github.get_file_content(followup_spec.target, ".autoresearch.yaml", "main")
                if raw is not None:
                    contract = load_contract(raw, followup_spec.target)
            except Exception as exc:
                log.warning("contract fetch failed for %s: %s", followup_spec.target, exc)
        limits = effective_limits(contract.budgets if contract is not None else None)
        # The contract's followup walltime only overrides when EXPLICITLY
        # set — and only DOWNWARD from the operator's spec value: strictly-
        # downward shaping must hold against operator config too, not just
        # against the module defaults.
        spec = followup_spec
        if contract is not None and contract.budgets.followup_job_minutes is not None:
            spec = replace(
                followup_spec,
                time_minutes=min(followup_spec.time_minutes, limits.followup_job_minutes),
            )
        ended, submitted = service_in_review(
            root,
            github,
            compute,
            spec,
            now,
            dry_run=followup_dry_run,
            allow_submit=launch_ok,
        )
        intake_job = (
            service_intake(
                root, github, compute, spec, now, contract, limits, dry_run=followup_dry_run
            )
            if launch_ok and contract is not None
            else None
        )
        steward_job = (
            service_steward(
                root, github, compute, spec, now, contract, limits, dry_run=followup_dry_run
            )
            if launch_ok and intake_job is None and contract is not None
            else None
        )
        self_job = None
        if launch_ok and intake_job is None and steward_job is None and contract is not None:
            try:
                self_job = service_self_initiated(
                    root,
                    compute,
                    spec,
                    contract,
                    now,
                    limits=limits,
                    dry_run=followup_dry_run,
                )
            except Exception as exc:
                log.warning("self-initiated selection failed: %s", exc)
        report = replace_report(
            report,
            ended,
            submitted,
            intake_job,
            self_job,
            disk_health.warnings(),
            not launch_ok,
            steward_job,
        )
    return report


def replace_report(
    report: TickReport,
    ended: list[tuple[str, str]],
    submitted: list[tuple[str, str]],
    intake_job: tuple[str, str] | None = None,
    self_job: tuple[str, str] | None = None,
    disk_warnings: list[str] | None = None,
    launch_blocked: bool = False,
    steward_job: tuple[str, str] | None = None,
) -> TickReport:
    from dataclasses import replace as dc_replace

    return dc_replace(
        report,
        review_ended=tuple(ended),
        followups_submitted=tuple(submitted),
        intake=intake_job or ("", ""),
        self_initiated=self_job or ("", ""),
        disk=tuple(disk_warnings or ()),
        launch_blocked=launch_blocked,
        steward=steward_job or ("", ""),
    )


MAX_ACTIVE_RUNS_PER_TARGET = 1
SELF_INITIATED_COOLDOWN_S = 6 * 3600
# An implementing run untouched for this long is a crashed climb job (their
# walltime is 90 min); it must not block the lane forever. Its cooldown
# entry still applies, so the crashed benchmark isn't immediately retried.
STRANDED_IMPLEMENTING_S = 6 * 3600
# A pending marker older than this is dead even if squeue can't be read.
PENDING_TTL_S = 4 * 3600


def pick_self_initiated(
    records: list[RunRecord],
    contract: Any,
    target: str,
    now: float,
    pending_attempt: tuple[str, float] | None = None,
) -> str | None:
    """The benchmark to climb next on `target`, or None.

    Deliberately boring (the planning agent upgrades this later): serialize
    to one active run per target, respect the contract's weekly budget and a
    per-benchmark cooldown, then choose the benchmark least recently
    attempted — untouched ones first. Only this target's runs count toward
    any of it. `pending_attempt` is a (benchmark, submitted_at) from a climb
    job that died before writing a run record — it counts toward cooldown so
    a crash loop can't resubmit every tick.
    """
    mine = [r for r in records if r.target == target]

    def stranded(r: RunRecord) -> bool:
        return r.state == IMPLEMENTING and now - max(r.updated, r.created) > STRANDED_IMPLEMENTING_S

    active = [r for r in mine if r.state != ENDED and not stranded(r)]
    if len(active) >= MAX_ACTIVE_RUNS_PER_TARGET:
        return None
    week_ago = now - 7 * 24 * 3600
    if sum(1 for r in mine if r.created >= week_ago) >= contract.budgets.runs_per_week:
        return None
    last_attempt: dict[str, float] = {}
    for r in mine:
        if r.benchmark:
            last_attempt[r.benchmark] = max(last_attempt.get(r.benchmark, 0.0), r.created)
    if pending_attempt is not None and pending_attempt[0]:
        bench_name, submitted_at = pending_attempt
        last_attempt[bench_name] = max(last_attempt.get(bench_name, 0.0), submitted_at)
    candidates = sorted(
        contract.benchmarks,
        key=lambda b: (last_attempt.get(b.name, 0.0), b.name),
    )
    for bench in candidates:
        if now - last_attempt.get(bench.name, 0.0) >= SELF_INITIATED_COOLDOWN_S:
            return str(bench.name)
    return None


def _pending_path(root: Path, target: str) -> Path:
    return root / "pending" / (target.replace("/", "__") + ".json")


def read_pending(root: Path, target: str) -> dict[str, Any] | None:
    """The submit-time marker for a climb whose run record may not exist yet."""
    path = _pending_path(root, target)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "submitted_at" in data else None


def write_pending(root: Path, target: str, benchmark: str, job_id: str, now: float) -> None:
    path = _pending_path(root, target)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"benchmark": benchmark, "job_id": job_id, "submitted_at": now}))
    os.replace(tmp, path)


def clear_pending(root: Path, target: str) -> None:
    _pending_path(root, target).unlink(missing_ok=True)


def _climb_limit_argv(limits: EffectiveLimits) -> list[str]:
    """Climb-CLI flags carrying the tick-resolved limits: the job's own
    walltime rides along so the climb can arm its self-deadline (Slurm
    delivers no signals to our processes on Torch — measured 2026-08-08)."""
    return [
        "--max-turns",
        str(limits.session_max_turns),
        "--session-minutes",
        str(limits.session_minutes),
        "--job-minutes",
        str(limits.climb_job_minutes),
    ]


def service_self_initiated(
    root: Path,
    compute: SlurmCompute,
    spec: FollowupSpec,
    contract: Any,
    now: float,
    limits: EffectiveLimits | None = None,
    dry_run: bool = False,
) -> tuple[str, str] | None:
    """The default background mode: when nothing else needs doing, climb the
    least-recently-attempted benchmark.

    A pending marker written at submit time bridges the gap between
    `compute.submit` and the climb job writing its run record — without it,
    every tick during Slurm queue latency would launch a duplicate climb.
    """
    limits = limits if limits is not None else effective_limits(getattr(contract, "budgets", None))
    try:
        records = list_runs(root)
        pending = read_pending(root, spec.target)
        pending_attempt: tuple[str, float] | None = None
        if pending is not None:
            submitted_at = float(pending["submitted_at"])
            landed = any(
                r.target == spec.target and r.created >= submitted_at - 60 for r in records
            )
            expired = now - submitted_at > PENDING_TTL_S
            if landed:
                clear_pending(root, spec.target)  # the run record carries it now
            elif (
                not expired and _holder_alive(compute, str(pending.get("job_id", ""))) is not False
            ):
                return None  # climb queued or starting; its record isn't written yet
            else:
                # Died before writing a record. Keep its cooldown so a
                # crash-at-startup loop can't resubmit every tick.
                clear_pending(root, spec.target)
                pending_attempt = (str(pending.get("benchmark", "")), submitted_at)
        benchmark = pick_self_initiated(records, contract, spec.target, now, pending_attempt)
        if benchmark is None:
            return None
        if dry_run:
            return (benchmark, "dry-run")
        argv = [
            "uv",
            "run",
            "python",
            "-m",
            "autoresearch.climb",
            "--target",
            spec.target,
            "--benchmark",
            benchmark,
            "--run-root",
            str(spec.run_root),
            "--image",
            spec.image,
            *_climb_limit_argv(limits),
        ]
        if spec.pat_file:
            argv += ["--pat-file", spec.pat_file]
        if spec.key_file:
            argv += ["--key-file", spec.key_file]
        job_id = compute.submit(
            JobSpec(
                job_name=f"climb-{benchmark}"[:60],
                account=spec.account,
                partition=spec.partition,
                time_minutes=limits.climb_job_minutes,
                command=f"cd {quote_command([str(spec.home)])} && {quote_command(argv)}",
                cpus=4,
                mem="8G",
            )
        )
        write_pending(root, spec.target, benchmark, job_id, now)
        log.info("self-initiated climb on %s: job %s", benchmark, job_id)
        return (benchmark, job_id)
    except Exception as exc:  # one bad pass must not break the tick
        log.warning("self-initiated pass failed: %s", exc)
        return None


def service_steward(
    root: Path,
    github: Any,
    compute: SlurmCompute,
    spec: FollowupSpec,
    now: float,
    contract: Any,
    limits: EffectiveLimits,
    dry_run: bool = False,
) -> tuple[str, str] | None:
    """The steward lane: claim at most ONE labeled work-order issue per tick
    and submit a stewardship job. Off until the operator provisions the
    steward's own key (role separation) and the contract declares a steward
    scope."""
    from autoresearch.steward import pick_steward_issue

    target = spec.target
    if not target or not spec.steward_key_file:
        return None
    if getattr(contract, "steward", None) is None:
        return None
    try:
        from autoresearch.steward import release_orphaned_claims

        # ONE active run per target covers stewardships too: an env rewrite
        # must not fly alongside a solver climb (the drift/freshness
        # machinery does not coordinate them) or another stewardship.
        records = list_runs(root)
        # reconcile first: killed jobs never post their own release
        release_orphaned_claims(github, target, records, now)
        if any(r.target == target and r.state != ENDED for r in records):
            return None
        # The queue window (submit -> job writes its record) is bridged by
        # the SAME per-target pending marker the self-initiated lane uses:
        # while a submitted job is alive without a record, no lane launches.
        pending = read_pending(root, target)
        if pending is not None:
            submitted_at = float(pending.get("submitted_at", 0.0))
            landed = any(r.target == target and r.created >= submitted_at - 60 for r in records)
            expired = now - submitted_at > PENDING_TTL_S
            if (
                not landed
                and not expired
                and _holder_alive(compute, str(pending.get("job_id", ""))) is not False
            ):
                return None
        task = pick_steward_issue(github, target, contract, spec.bot_login)
        if task is None:
            return None
        if dry_run:
            return (f"steward-issue-{task.number}", "dry-run")
        from autoresearch.intake import CLAIM_MARKER, issue_hypothesis

        github.comment(
            target,
            task.number,
            f"{CLAIM_MARKER}\nClaimed by the steward for benchmark "
            f"`{task.benchmark}`; a run is queued and a report will follow here.",
        )
        import base64 as _b64

        work_order_b64 = _b64.b64encode(issue_hypothesis(task).encode()).decode()
        argv = [
            "uv",
            "run",
            "python",
            "-m",
            "autoresearch.steward",
            "--target",
            target,
            "--benchmark",
            task.benchmark,
            "--run-root",
            str(spec.run_root),
            "--image",
            spec.image,
            "--issue",
            str(task.number),
            "--work-order-b64",
            work_order_b64,
            "--key-file",
            spec.steward_key_file,
            *_climb_limit_argv(limits),
        ]
        if spec.pat_file:
            argv += ["--pat-file", spec.pat_file]
        try:
            job_id = compute.submit(
                JobSpec(
                    job_name=f"steward-issue-{task.number}",
                    account=spec.account,
                    partition=spec.partition,
                    time_minutes=limits.climb_job_minutes,
                    command=f"cd {quote_command([str(spec.home)])} && {quote_command(argv)}",
                    cpus=4,
                    mem="8G",
                )
            )
        except Exception:
            # release the claim: a claim with no job behind it would orphan
            # the work order forever (pick skips claimed issues)
            from autoresearch.steward import RELEASE_MARKER

            with contextlib.suppress(Exception):
                github.comment(
                    target,
                    task.number,
                    f"{RELEASE_MARKER}\nSubmission failed; claim released — "
                    f"a later tick will retry this work order.",
                )
            raise
        write_pending(root, target, f"steward:{task.benchmark}", job_id, now)
        log.info("steward issue #%s claimed for job %s", task.number, job_id)
        return (f"steward-issue-{task.number}", job_id)
    except Exception as exc:  # the steward lane must not break the tick
        log.warning("steward pass failed: %s", exc)
        return None


def service_intake(
    root: Path,
    github: Any,
    compute: SlurmCompute,
    spec: FollowupSpec,
    now: float,
    contract: Any = None,
    limits: EffectiveLimits | None = None,
    dry_run: bool = False,
) -> tuple[str, str] | None:
    """The requested lane: claim at most ONE qualifying issue per tick and
    submit a climb job for it. The claim comment (posted by the climb job
    before its session) marks an issue taken; one-per-tick keeps a burst of
    issues from bursting the budget. The contract arrives from the tick's
    single per-target fetch; None (fetch failed) sits the lane out."""
    from autoresearch.contract import load_contract
    from autoresearch.intake import issue_hypothesis, pick_issue

    target = spec.target
    if not target:
        return None
    try:
        if contract is None:
            contract_raw = github.get_file_content(target, ".autoresearch.yaml", "main")
            if contract_raw is None:
                return None
            contract = load_contract(contract_raw, target)
        limits = limits if limits is not None else effective_limits(contract.budgets)
        task = pick_issue(github, target, contract, spec.bot_login)
        if task is None:
            return None
        if dry_run:
            return (f"issue-{task.number}", "dry-run")
        # claim BEFORE submit: Slurm queueing can take minutes, and the next
        # tick must not re-claim the same issue in that window
        from autoresearch.intake import CLAIM_MARKER

        github.comment(
            target,
            task.number,
            f"{CLAIM_MARKER}\nClaimed for benchmark `{task.benchmark}`; a run "
            "is queued and a report will follow here.",
        )
        import base64 as _b64

        hypothesis_b64 = _b64.b64encode(issue_hypothesis(task).encode()).decode()
        argv = [
            "uv",
            "run",
            "python",
            "-m",
            "autoresearch.climb",
            "--target",
            target,
            "--benchmark",
            task.benchmark,
            "--run-root",
            str(spec.run_root),
            "--image",
            spec.image,
            "--issue",
            str(task.number),
            "--hypothesis-b64",
            hypothesis_b64,
            *_climb_limit_argv(limits),
        ]
        if spec.pat_file:
            argv += ["--pat-file", spec.pat_file]
        if spec.key_file:
            argv += ["--key-file", spec.key_file]
        job_id = compute.submit(
            JobSpec(
                job_name=f"climb-issue-{task.number}",
                account=spec.account,
                partition=spec.partition,
                time_minutes=limits.climb_job_minutes,
                command=f"cd {quote_command([str(spec.home)])} && {quote_command(argv)}",
                cpus=4,
                mem="8G",
            )
        )
        log.info("issue #%s claimed for climb job %s", task.number, job_id)
        return (f"issue-{task.number}", job_id)
    except Exception as exc:  # intake must not break the tick
        log.warning("intake pass failed: %s", exc)
        return None


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
    parser.add_argument(
        "--min-free-gb",
        type=float,
        default=DEFAULT_MIN_FREE_BYTES / 1024**3,
        help="skip launching new work when the state filesystem has less free",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    args.root.mkdir(parents=True, exist_ok=True)
    # The waiting-run sweep stays dry until the experiment dispatcher lands;
    # in-review servicing is LIVE when credentials + image are available in
    # the chain environment.
    import os

    github = None
    followup_spec = None
    pat_file = os.environ.get("AUTORESEARCH_PAT_FILE", "")
    account = os.environ.get("AUTORESEARCH_ACCOUNT", "")
    partition = os.environ.get("AUTORESEARCH_PARTITION", "")
    image = os.environ.get(
        "AUTORESEARCH_IMAGE",
        os.path.expanduser("~/autoresearch-images/agent-py312.sif"),
    )
    home = os.environ.get("AUTORESEARCH_HOME", "")
    if pat_file and account and partition and home and Path(image).is_file():
        from autoresearch.github import FileTokenProvider, GitHubClient

        try:
            github = GitHubClient(auth=FileTokenProvider(Path(pat_file)))
            followup_spec = FollowupSpec(
                account=account,
                partition=partition,
                run_root=args.root,
                image=image,
                home=Path(home),
                pat_file=pat_file,
                key_file=os.environ.get("AUTORESEARCH_HARNESS_KEY_FILE", ""),
                target=os.environ.get(
                    "AUTORESEARCH_TARGET", "agentic-learning-ai-lab/autoresearch-pilot"
                ),
                steward_key_file=os.environ.get("AUTORESEARCH_STEWARD_KEY_FILE", ""),
            )
        except Exception as exc:
            log.warning("in-review servicing disabled: %s", exc)
    else:
        absent = [
            name
            for name, value in [
                ("AUTORESEARCH_PAT_FILE", pat_file),
                ("AUTORESEARCH_ACCOUNT", account),
                ("AUTORESEARCH_PARTITION", partition),
                ("AUTORESEARCH_HOME", home),
            ]
            if not value
        ]
        if not Path(image).is_file():
            absent.append(f"image:{image}")
        log.info("in-review servicing disabled (missing: %s)", ", ".join(absent))

    report = tick(
        args.root,
        SlurmCompute(),
        LoggingDispatcher(),
        now=time.time(),
        grace_s=args.grace_s,
        lease_ttl_s=args.lease_ttl_s,
        dry_run=True,
        github=github,
        followup_spec=followup_spec,
        followup_dry_run=False,
        min_free_bytes=int(args.min_free_gb * 1024**3),
    )
    log.info(
        "tick done: paused=%s swept=%d woken=%d deferred=%d reaped=%d stuck=%d impl_ended=%s "
        "review_ended=%s followups=%s intake=%s self_initiated=%s steward=%s disk=%s "
        "launch_blocked=%s",
        report.paused,
        report.swept,
        len(report.woken),
        len(report.deferred),
        len(report.reaped_leases),
        len(report.stuck),
        report.implementing_ended or "-",
        report.review_ended,
        report.followups_submitted,
        report.intake,
        report.self_initiated,
        report.steward,
        report.disk or "ok",
        report.launch_blocked,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
