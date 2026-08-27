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
import math
import os
import re
import socket
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

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
from autoresearch.harness import DEFAULT_MAX_TURNS, redact
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
    outage_active,
    read_lease,
    reap_lease,
    release_lease,
    run_dir,
    save_record,
    update_lease_holder,
)

log = logging.getLogger(__name__)

PAUSE_SENTINEL = "PAUSE"
# Operator on-switch for dispatched-wake, mirroring PAUSE: a root-relative
# sentinel an operator arms/disarms with a touch/rm — no chain restart, no
# env-var surgery on a live tick. The AUTORESEARCH_DISPATCH_WAKE env var still
# works too (either arms it); the sentinel is the reversible, restart-free path.
DISPATCH_WAKE_SENTINEL = "DISPATCH_WAKE"
HEARTBEAT_NAME = "heartbeat.json"
# Written at a full tick's END (not its start) — the coalesce guard's signal, so
# a tick that crashes mid-work cannot suppress the next (recovery) tick.
WORK_MARKER_NAME = "last_worked.json"

# Grace between "experiment terminal" and the sweep stepping in: the afterany
# job gets this long to deliver before the backup assumes it lost.
DEFAULT_GRACE_S = 15 * 60
# A held lease is stale after the session timeout plus slack.
DEFAULT_LEASE_TTL_S = 3600 + 15 * 60
# Coalesce guard: skip a tick's work if another ran within this window. Under
# partition congestion, queued ticks bunch up and become eligible together
# (serialized by the singleton dependency), so they would run back-to-back and
# redundantly re-sweep. The chain schedules ticks a full cadence apart by
# begin-time, so only late-bunched pile-ups fall inside this window; keep it
# well BELOW the cadence (default 30 min). 0 disables. Env: AUTORESEARCH_MIN_TICK_MINUTES.
DEFAULT_MIN_TICK_S = 10 * 60
# Ceiling for the coalesce window: a value above this is almost certainly a typo
# (a window near/over the cadence would coalesce every on-cadence tick and stall
# the loop). Clamp + warn rather than silently freeze. The operator is still
# responsible for keeping it below their configured cadence.
MAX_MIN_TICK_S = 60 * 60


class WakeDispatcher(Protocol):
    """Delivers one wake, called with the lease already held.

    Returns "" when delivery completed synchronously (the caller releases the
    lease), or the Slurm job id of an asynchronous wake job that now owns the
    lease (released by that job on completion; reaped by TTL if it dies).

    Contract for real dispatchers: a wake that RESULTS IN PROGRESS
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
    coalesced: bool = False  # skipped as a redundant pile-up (a tick ran too recently)
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


# The submitted walltime must never exceed the job partition's MaxTime —
# sbatch REJECTS a longer request outright, which would ground every climb.
# The DEFAULT matches cpu_short (6 h); an operator moving work jobs to a
# longer partition (AUTORESEARCH_JOB_PARTITION=cpu48) raises the cap with
# AUTORESEARCH_MAX_JOB_MINUTES. Code-side ceiling: the cap must stay under
# STRANDED_IMPLEMENTING_S or the picker declares live runs stranded — jobs
# longer than 10 h need that window made spec-aware first (named gap). The
# self-deadline arms at the CLAMPED value, so a job that wanted more time
# fails safe mid-panel instead of never starting.
MAX_ATTEMPT_JOB_MINUTES = 6 * 60
MAX_JOB_MINUTES_CEILING = 10 * 60


@dataclass(frozen=True)
class FollowupSpec:
    """How the tick launches follow-up jobs for in-review runs."""

    account: str
    partition: str
    run_root: Path
    image: str
    home: Path  # AUTORESEARCH_HOME: cwd for the submitted job
    bot_login: str = "agentic-learning-bot"
    time_minutes: int = 90  # min()'d with the contract's followup_job_minutes
    max_turns: int = DEFAULT_MAX_TURNS  # session turn budget for follow-up jobs
    pat_file: str = ""  # forwarded to the job; "" = the followup CLI default
    target: str = ""  # the repo the intake pass scans for requested-lane issues
    # the STEWARD'S OWN key (role separation): the steward lane stays off
    # until the operator provisions it
    steward_key_file: str = ""
    # Pre-PR verification panel for climb jobs (docs/design/orchestrator-verify.md).
    # DEFAULT ON — the flip is code, the off-switch is AUTORESEARCH_PANEL="".
    # The climb CLI fails LOUDLY on a bad panel config (a configured gate must
    # never silently vanish); the tick preflights the same rules — lens
    # grammar AND key file — before claiming or submitting so nothing is
    # stranded. The panel's walltime is the orchestrator's own overhead: the
    # tick ADDS a panel allowance to the contract-clamped job budget
    # (_panel_job_minutes) rather than eating the author's time; a residual
    # overrun still fails safe through the self-deadline.
    panel: str = "verify,review"
    panel_key_file: str = ""  # "" = the climb CLI's default verifier-key path
    # Where submitted WORK jobs (climb/steward/followup) run; empty = same as
    # `partition`. The tick chain itself always stays on `partition` — ticks
    # are minutes, work jobs can be hours, and Slurm prices walltime into
    # scheduling priority, so the two deserve independent placement.
    job_partition: str = ""
    # Partition MaxTime for work jobs — the panel-augmented walltime clamps
    # here (see MAX_ATTEMPT_JOB_MINUTES). Raise together with job_partition.
    max_job_minutes: int = MAX_ATTEMPT_JOB_MINUTES


# Generous vs the ~2 h job walltimes plus queue wait, tight enough that
# full trees + per-flight venvs cannot pile up for days; same-day
# forensics is the norm, and the disk preflight is the backstop.
FLIGHT_TTL_S = 24 * 3600


def flight_checkout(home: Path, name: str, now: float) -> Path:
    """A detached git worktree of the checkout's HEAD commit, for one
    submitted job to run from. The shared checkout is reset --hard at
    every tick's deploy, so a queued job that cd's into it can have its
    code swapped mid-flight; a flight pins the deployed commit, and the
    tree survives for forensics after a crash. HEAD, deliberately:
    uncommitted hand-edits in the shared checkout do not fly — only
    deployed code does. Failures fall back to the shared checkout — a
    snapshot must never ground the fleet."""
    flights = home.parent / "flights"
    try:
        flights.mkdir(parents=True, exist_ok=True)
        # same name in the same tick (e.g. two orders on one benchmark, or
        # truncation collisions) must get its own tree, not a silent
        # fallback: suffix until free, with a unique tail as the backstop
        target = flights / f"{name}-{int(now)}"
        for attempt in range(2, 6):
            if not target.exists():
                break
            target = flights / f"{name}-{int(now)}-{attempt}"
        if target.exists():
            target = flights / f"{name}-{int(now)}-{uuid4().hex[:8]}"
        subprocess.run(
            ["git", "-C", str(home), "worktree", "add", "--detach", str(target)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return target
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("flight snapshot failed for %s (%s); using the shared checkout", name, exc)
        return home


def _flight_command(home: Path, job_name: str, now: float, argv: list[str]) -> str:
    """The job's shell command, cd'ing into a fresh flight snapshot.

    The flight is named FROM the job name (one truncation rule, here) so
    the reaper's pending-job immunity — live job name prefixes flight
    name — holds by construction at every submit site. argv must contain
    absolute paths only; every spec path is absolute by construction, and
    a relative path would resolve inside a tree that is reaped later."""
    flight = flight_checkout(home, job_name[:40], now)
    return f"cd {quote_command([str(flight)])} && {quote_command(argv)}"


def reap_flights(
    home: Path,
    now: float,
    ttl_s: float = FLIGHT_TTL_S,
    live_job_names: Sequence[str] = (),
) -> int:
    """Remove flight worktrees older than the TTL — age by directory
    mtime, no name parsing — UNLESS a pending or running job's NAME
    prefixes the flight's (flights are named after their job). Queue wait
    is unbounded (GPU partitions can pend for days), so age alone must
    never delete a tree a job will cd into; the TTL is purely the
    forensics-retention window for flights whose job is gone. Name
    matching is conservative: one live job name protects every flight it
    prefixes. Best-effort: a stubborn flight is logged, not fatal."""
    flights = home.parent / "flights"
    if not flights.is_dir():
        return 0
    reaped = 0
    for entry in flights.iterdir():
        if any(name and entry.name.startswith(name[:40]) for name in live_job_names):
            continue  # a queued or running job still needs this tree
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age < ttl_s:
            continue
        try:
            subprocess.run(
                ["git", "-C", str(home), "worktree", "remove", "--force", str(entry)],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            reaped += 1
        except (OSError, subprocess.SubprocessError):
            # not a registered worktree (a half-created flight, or debris):
            # remove the directory itself and prune the registry, or the
            # entry warns forever without ever going away
            import shutil

            shutil.rmtree(entry, ignore_errors=True)
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(
                    ["git", "-C", str(home), "worktree", "prune"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            if not entry.exists():
                reaped += 1
            else:
                log.warning("could not reap flight %s", entry.name)
    return reaped


CONTRACT_ALARM_MARKER = "<!-- autoresearch:contract-alarm -->"
CONTRACT_ALARM_AFTER = 3  # consecutive failing ticks (~1.5 h) before alarming


def contract_alarm(
    root: Path,
    github: Any,
    target: str,
    error: str | None,
    now: float,
    bot_login: str = "agentic-learning-bot",
) -> None:
    """Persistent contract failure must surface where humans look.

    A rejected or unfetchable contract silently idles every launch lane.
    After CONTRACT_ALARM_AFTER consecutive failing ticks this
    opens ONE issue on the target repo; the next successful load closes
    it and says so. Alarm plumbing is best-effort by construction: it
    must never break the tick it reports for."""
    state_path = root / "contract-alarm.json"
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    if error is None:
        open_alarm = int(state.get("issue", 0))
        if not open_alarm and state:
            # creation may have landed without a recorded number (dry-run,
            # odd response, lost state file): search so recovery can still
            # close it. Only ticks that follow SOME failure signal pay the
            # search; total state loss + instant recovery leaves the issue
            # for the next alarm cycle's search to adopt and close.
            with contextlib.suppress(Exception):
                open_alarm = _find_alarm_issue(github, target, bot_login)
        if open_alarm:
            try:
                github.close_issue(target, open_alarm)
            except Exception as exc:
                # keep the state so the NEXT healthy tick retries the close;
                # unlinking here would orphan the open alarm forever — and
                # no comment yet, or every retry would repeat it
                log.warning("could not close contract alarm #%s: %s", open_alarm, exc)
                return
            with contextlib.suppress(Exception):
                github.comment(
                    target, open_alarm, "The tick loads again cleanly; launch lanes resume."
                )
        if state:
            with contextlib.suppress(OSError):
                state_path.unlink()
        return
    count = int(state.get("count", 0)) + 1
    state["count"] = count
    # redacted (the client's own token is the one secret this process
    # holds) and fenced with a run longer than any backtick run inside —
    # transport errors can echo request material, loader errors can echo
    # contract content, and both are untrusted for a public issue body
    safe_error = redact(error, _client_secrets(github)).replace(str(Path.home()), "~")[:600]
    # A recorded issue a human closed by hand stays closed: closing the
    # alarm is the maintainer's "I know" — re-opening or re-creating it
    # every threshold would be alarm spam, and recovery still clears state.
    if count >= CONTRACT_ALARM_AFTER and not state.get("issue"):
        # search open issues first: state loss must not spawn duplicates
        try:
            number = _find_alarm_issue(github, target, bot_login) or github.create_issue(
                target,
                "autoresearch: launch lanes are paused",
                f"{CONTRACT_ALARM_MARKER}\nThe orchestrator's launch lanes "
                f"(intake, steward, self-initiated) have sat out {count} "
                f"consecutive ticks. The error below names the cause — a "
                f"contract that failed to load, or a panel config the climb "
                f"would reject.\n\n"
                f"{_fence(safe_error)}\n{safe_error}\n{_fence(safe_error)}\n\n"
                f"This issue closes itself when a tick passes cleanly.",
            )
            if number:
                state["issue"] = number
        except Exception as exc:
            log.warning("contract alarm could not post to %s: %s", target, exc)
    with contextlib.suppress(OSError):
        tmp = state_path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, state_path)


def _client_secrets(github: Any) -> tuple[str, ...]:
    try:
        token = github.auth.token()
        if token:
            return (token,)
    except Exception as exc:
        # degraded redaction must not be silent: the error text goes to a
        # public issue, and a renamed auth surface would no-op the redact
        log.warning("alarm redaction has no client token (%s)", exc)
    return ()


def _fence(content: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", content)), default=0)
    return "`" * max(3, longest + 1)


def _find_alarm_issue(github: Any, target: str, bot_login: str) -> int:
    """Only the BOT'S own marker'd issue counts: the marker is a public
    string, and adopting a stranger's issue would let anyone suppress the
    real alarm or get their issue closed by the bot."""
    return next(
        (
            int(issue.get("number", 0))
            for issue in github.list_open_issues(target, max_pages=10)
            if CONTRACT_ALARM_MARKER in str(issue.get("body", ""))
            and str((issue.get("user") or {}).get("login", "")).casefold() == bot_login.casefold()
        ),
        0,
    )


def shape_followup_spec(spec: FollowupSpec, limits: EffectiveLimits, contract: Any) -> FollowupSpec:
    """Clamp the operator's follow-up spec by the contract's effective
    limits. Both knobs clamp only when the contract EXPLICITLY sets them:
    a contract shapes spend downward, but an operator's deliberate config
    is never silently reduced by defaults (raising budgets is operator
    territory)."""
    if contract is None:
        return spec
    # direct attribute access: Budgets is our typed model, and a rename
    # must fail loudly here, not silently stop shaping spend downward
    if contract.budgets.session_max_turns is not None:
        spec = replace(spec, max_turns=min(spec.max_turns, limits.session_max_turns))
    if contract.budgets.followup_job_minutes is not None:
        spec = replace(spec, time_minutes=min(spec.time_minutes, limits.followup_job_minutes))
    return spec


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
            # per-ROLE outage latch: state transitions above still ran,
            # only this record's session spawn sits the cooldown out
            paused = outage_active(root, now, role="steward" if is_steward else "solver")
            if paused:
                log.info("follow-up for %s paused (api outage: %s)", record.run_id, paused)
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
                # the SAME clamped value Slurm gets: a deadline armed past
                # the real walltime is a Slurm kill before a clean ending
                str(min(spec.time_minutes, spec.max_job_minutes)),
                "--max-turns",
                str(spec.max_turns),
            ]
            if spec.pat_file:
                argv += ["--pat-file", spec.pat_file]
            # config-driven author: the author follow-up resolves its key per the
            # RUN's backend (from the record) inside followup.main — the tick does
            # not thread it. The steward is a distinct role with its own key.
            if is_steward and spec.steward_key_file:
                argv += ["--key-file", spec.steward_key_file]
            job_id = compute.submit(
                JobSpec(
                    job_name=f"followup-{record.run_id}"[:60],
                    account=spec.account,
                    partition=spec.job_partition or spec.partition,
                    time_minutes=min(spec.time_minutes, spec.max_job_minutes),
                    command=_flight_command(spec.home, f"followup-{record.run_id}"[:60], now, argv),
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


def _last_worked_ts(root: Path) -> float | None:
    """The `ts` of the last tick that COMPLETED its work, or None if there is no
    readable marker (first tick, or a corrupt/missing file). The coalesce guard
    keys on this, NOT the heartbeat: the heartbeat is stamped at tick START (for
    the watchdog), so a tick that crashes mid-work still leaves a fresh
    heartbeat — coalescing on that would suppress the very recovery tick. The
    work marker is written only at a full tick's END, so a failed tick never
    hides behind it."""
    # The marker is a best-effort optimization we write ourselves; ANY failure
    # reading/parsing/converting a corrupt file (OSError, ValueError,
    # OverflowError on a huge int, RecursionError on deep nesting, ...) must
    # degrade to "no marker" so coalesce simply proceeds — it can never crash the
    # tick before its heartbeat. bool is an int subclass, so exclude it; inf/nan
    # are not usable elapsed anchors.
    try:
        payload = json.loads((root / WORK_MARKER_NAME).read_text())
        ts = payload.get("ts") if isinstance(payload, dict) else None
        if not isinstance(ts, int | float) or isinstance(ts, bool):
            return None
        val = float(ts)
        return val if math.isfinite(val) else None
    except Exception:
        return None


def _mark_worked(root: Path, now: float) -> None:
    """Record that a tick completed its work at `now` — the coalesce signal.
    Best-effort: a marker that cannot be written must not fail the tick."""
    try:
        tmp = root / f".{WORK_MARKER_NAME}.tmp"
        tmp.write_text(json.dumps({"ts": now}))
        os.replace(tmp, root / WORK_MARKER_NAME)
    except OSError as exc:
        log.warning("work-marker write failed: %s", exc)


def mark_tick_complete(root: Path, report: TickReport, now: float) -> None:
    """Stamp the coalesce marker iff the tick actually did work, at real
    COMPLETION time (the caller passes time.time() AFTER tick() returns). A
    paused/coalesced tick leaves it untouched; a tick that raised never reaches
    here — so only a genuinely completed tick can coalesce the next one, and a
    long tick's marker reflects when it finished, not when it started."""
    if not report.paused and not report.coalesced:
        _mark_worked(root, now)


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
    attempt counters, no dispatch.
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
        # NOT the global dry_run: that flag only dries WAKE delivery;
        # ending killed climbs' records dispatches nothing and must run
        # live even while wakes stay dry.
        implementing_ended=tuple(_sweep_implementing(root, compute, now, grace_s)),
    )


RESEARCH_LOG_BRANCH = "research-log"
RESEARCH_LOG_MARKER = "<!-- autoresearch:research-log -->"
RESEARCH_LOG_PER_TICK = 3


def _ledger_marker(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "ledger-published"


def _ledger_since(root: Path, target: str) -> Path:
    return root / ("research-log-since-" + target.replace("/", "__"))


def _ledger_issue_cache(root: Path, target: str) -> Path:
    return root / ("research-log-issue-" + target.replace("/", "__"))


def service_research_log(root: Path, github: Any, spec: FollowupSpec, now: float) -> int:
    """STATE-driven ledger (terra #170 r1: wiring publisher calls at terminal
    sites missed four terminal paths — attempt error, zero-change resume,
    the direct live terminal, steward): any run of this target whose
    terminal report exists but carries no published marker gets archived on
    the `research-log` branch and a two-line pointer routed — to the run's
    claimed issue never (it already got the full finish post), else to an
    open order issue naming the benchmark, else to the rolling log issue
    whose number is CACHED in the state dir (no 300-issue pagination scan,
    no duplicate creation). Running in the single tick also removes the
    concurrent-first-archive races by construction. The pointer posts only
    AFTER a successful archive (no dead links); the marker is written only
    after full success, so a crashed publish retries next tick. Runs ended
    before the feature's first pass are marker-stamped silently (no
    backfill spam).
    """
    since_path = _ledger_since(root, spec.target)
    first_pass = not since_path.exists()
    if first_pass:
        with contextlib.suppress(OSError):
            since_path.write_text(str(now))
    published = 0
    for record in list_runs(root):
        if record.target != spec.target or record.state not in (ENDED, IN_REVIEW):
            continue
        marker = _ledger_marker(root, record.run_id)
        report_path = run_dir(root, record.run_id) / "report.md"
        if marker.exists() or not report_path.exists():
            continue
        if first_pass and (record.updated or record.created) < now:
            # adopt pre-feature history silently: browsable going forward,
            # no retroactive issue spam. Only records OLDER than the since
            # marker — a run that goes terminal during this very pass is new
            # work and publishes normally (terra #170 r2).
            with contextlib.suppress(OSError):
                marker.write_text("adopted-unpublished")
            continue
        if published >= RESEARCH_LOG_PER_TICK:
            break
        try:
            report = report_path.read_text()
        except OSError:
            continue
        outcome = record.ending or ("improved" if record.state == IN_REVIEW else "ended")
        if _publish_ledger_entry(github, spec.target, root, record, outcome, report):
            with contextlib.suppress(OSError):
                marker.write_text(str(now))
            published += 1
    return published


def _publish_ledger_entry(
    github: Any, target: str, root: Path, record: RunRecord, outcome: str, report: str
) -> bool:
    from datetime import UTC, datetime

    date = datetime.fromtimestamp(record.updated or record.created, tz=UTC).strftime("%Y-%m-%d")
    path = f"reports/{date}-{record.run_id}.md"
    try:
        if not github.ensure_branch(target, RESEARCH_LOG_BRANCH):
            return False
        if not github.put_file(
            target, path, report, RESEARCH_LOG_BRANCH, f"research log: {record.run_id} ({outcome})"
        ):
            return False  # pointer only after a live archive — no dead links
        url = f"https://github.com/{target}/blob/{RESEARCH_LOG_BRANCH}/{path}"
        line = f"**{outcome}** `{record.benchmark}` — [report]({url})"
        if record.pr_url:
            line += f" · {record.pr_url}"
        if record.issue_number:
            return True  # the claimed issue already received the full finish
        bench = record.benchmark.casefold()
        if bench:
            for issue in github.list_open_issues(target):
                text = f"{issue.get('title', '')}\n{issue.get('body') or ''}"
                if RESEARCH_LOG_MARKER in text or issue.get("pull_request"):
                    continue
                if bench in text.casefold():
                    github.comment(target, int(issue["number"]), line)
                    return True
        cache = _ledger_issue_cache(root, target)
        log_issue = 0
        with contextlib.suppress(OSError, ValueError):
            log_issue = int(cache.read_text().strip())
        if not log_issue:
            log_issue = github.create_issue(
                target,
                "Research log",
                f"{RESEARCH_LOG_MARKER}\nOne two-line comment per finished "
                f"autoresearch run — full reports live on the [`{RESEARCH_LOG_BRANCH}`]"
                f"(https://github.com/{target}/tree/{RESEARCH_LOG_BRANCH}/reports) branch. "
                "Results relevant to an open order issue are posted there instead.",
            )
            if log_issue:
                with contextlib.suppress(OSError):
                    cache.write_text(str(log_issue))
        if log_issue:
            github.comment(target, log_issue, line)
        return True
    except Exception as exc:  # advisory ledger: never fail the tick
        log.warning("research-log publish failed for %s: %s", record.run_id, exc)
        return False


def _kill_stamp(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "attempt-terminal-seen"


def _sweep_implementing(root: Path, compute: SlurmCompute, now: float, grace_s: float) -> list[str]:
    """End `implementing` records whose climb job died without a verdict.

    A climb that CRASHES contains its own ending (attempt.py); a climb that is
    KILLED — walltime, preemption, scancel after the SIGTERM grace, node
    death — leaves no exception to contain, so this pass records the ending
    (the picker's stranded guard only frees the lane). Slurm truth decides:
    job terminal or GONE, plus a grace so a just-finished healthy climb can
    write its own final state first. Outage never reads as dead. Legacy
    records without a job id age out on the stranded window instead.
    """
    ended: list[str] = []
    for record in list_runs(root):
        if record.state != IMPLEMENTING:
            continue
        try:
            if record.run_job_id:
                try:
                    state = compute.status(record.run_job_id)
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
                note = f"climb job {record.run_job_id} ended {state} without a verdict"
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
            for jid in _poll_targets(fresh):
                # defensive: no current path records an experiment while
                # still implementing, but an orphan GPU job burning budget
                # after its run is declared dead must never survive one
                with contextlib.suppress(Exception):
                    compute.cancel(jid)
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


def _poll_targets(record: RunRecord) -> list[str]:
    """Every Slurm job this run waits on. The single `experiment_job_id` is
    the common case; a MULTI-job park (candidate + siblings, or several author
    launches) records no single id — its jobs live in the stage's `afterany`
    dependency string, the one source that always names them all. Without
    this fallback a multi-job park is blind and rides the deadline floor."""
    if record.experiment_job_id:
        return [record.experiment_job_id]
    afterany = str((record.stage or {}).get("afterany", ""))
    return [t for t in afterany.split(":")[1:] if t]


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

        job_ids = _poll_targets(record)
        if not job_ids:
            # No job ids to poll. A BLIND PARK (the measurer could not read Slurm,
            # so `MeasurementPending` carried no ids) still hibernated with a
            # deadline — the deadline floor is its ONLY wake, so fire on it. A
            # genuinely mid-write record has no deadline and is left alone.
            # (A jobless CHECKPOINT SLEEP arrives here too — its deadline is
            # near-term by construction, attempt.py sizes it to the next sweep
            # pass, not the 12h queue slack that protects queued jobs.)
            if record.deadline > 0 and now > record.deadline:
                wake(record, "blind park past deadline", "deadline")
            return

        try:
            states = [compute.status(jid) for jid in job_ids]
        except SlurmQueryError:
            # Layer 4's rule: query failure is "Slurm unknown", never "gone".
            deferred.append(record.run_id)
            return

        # deadline <= 0 cannot be written by save_record for waiting runs;
        # if one exists anyway (legacy/hand-edited), treat it as already past
        # for GONE — a vanished-experiment wake is safe — but never for
        # PENDING, where the consequence would be cancelling a healthy job.
        past_deadline = record.deadline <= 0 or now > record.deadline

        if all(is_terminal(s) for s in states):
            state = ",".join(sorted(set(states)))
            # Layer 3, with real grace: time runs from when the sweep FIRST
            # saw every job terminal, not from submission — the afterany
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
        elif any(is_pending(s) for s in states) and record.deadline > 0 and now > record.deadline:
            # Unschedulable in practice: cancel every non-terminal job
            # (best-effort — scancel trouble must not abort the sweep), then
            # wake with that fact.
            if not dry_run:
                for jid, s in zip(job_ids, states, strict=True):
                    if is_terminal(s) or s == GONE:
                        continue
                    try:
                        compute.cancel(jid)
                    except Exception as exc:  # scancel trouble is never fatal here
                        log.warning("cancel %s failed: %s", jid, exc)
            wake(record, "experiment unschedulable (pending past deadline)", "unschedulable")
        elif all(is_terminal(s) or s == GONE for s in states):
            # done-or-vanished, at least one GONE (all-terminal handled above)
            if past_deadline:
                wake(record, "experiment vanished from Slurm", "vanished")
            # else: sacct lag right after submission is normal; wait.
        # something RUNNING (or recently pending): nothing to do yet.


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
    min_tick_s: float = DEFAULT_MIN_TICK_S,
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
    # watchdog signal. The disk-annotated heartbeat follows once known. The
    # coalesce guard reads the last COMPLETED tick's marker (not the heartbeat).
    prior_worked = _last_worked_ts(root)
    write_heartbeat(root, now)
    disk_health = check_disk(root, min_free_bytes=min_free_bytes)
    write_heartbeat(root, now, disk=disk_health.as_dict())
    for warning in disk_health.warnings():
        log.warning("disk: %s", warning)
    if (root / PAUSE_SENTINEL).exists():
        log.info("pause sentinel present; tick is a no-op")
        return TickReport(paused=True)
    # Coalesce a congestion pile-up: if a tick COMPLETED its work within
    # min_tick_s, this one is redundant (that recent tick already swept and
    # launched). Keyed on the work marker (stamped by the CALLER at real
    # completion time), not the heartbeat, so a tick that crashed mid-work does
    # not suppress this recovery tick. Heartbeat still written above, so the
    # watchdog stays fed and the chain stays alive.
    if min_tick_s > 0 and prior_worked is not None:
        elapsed = now - prior_worked
        if elapsed < 0:
            # marker dated in the future -> the clock jumped back; never coalesce
            # on it (that could stall the loop), and surface it rather than fail
            # silently.
            log.warning(
                "work marker is %.0fs in the future (clock skew?); not coalescing", -elapsed
            )
        elif elapsed < min_tick_s:
            log.info(
                "coalescing: last completed tick %.0fs ago (< %.0fs); tick is a no-op",
                elapsed,
                min_tick_s,
            )
            return TickReport(coalesced=True)
    report = sweep(root, compute, dispatcher, now, grace_s, lease_ttl_s, dry_run=dry_run)
    launch_ok = disk_health.launch_ok()
    if not launch_ok:
        log.warning("disk preflight failed; launch lanes are OFF this tick")
    if github is not None and followup_spec is not None:
        # expired flight snapshots die with their TTL, not with a human.
        # One home suffices: every lane's spec derives from followup_spec
        # via replace(), so all flights share this checkout's flights/ dir.
        # Blind means delete nothing — but only QUERY failures count as
        # blindness; a compute backend missing the method is a programming
        # error and propagates.
        try:
            live_names = compute.active_job_names()
        except SlurmQueryError as exc:
            log.warning("cannot list live jobs (%s); reaping no flights this tick", exc)
            live_names = None
        if live_names is not None:
            with contextlib.suppress(Exception):
                reaped = reap_flights(followup_spec.home, now, live_job_names=live_names)
                if reaped:
                    log.info("reaped %d expired flight snapshot(s)", reaped)
        # ONE contract fetch per tick feeds every lane: the requested and
        # self-initiated lanes need its benchmarks, and all three lanes now
        # take their session/job limits from its budgets — clamped by our
        # ceilings (limits.py), so a target shapes spend, never raises it.
        # A failed fetch leaves in-review servicing running on defaults;
        # the launch lanes need the contract and sit out this tick.
        contract = None
        if followup_spec.target:
            contract_error: str | None = "contract file missing on main"
            try:
                from autoresearch.contract import load_contract

                raw = github.get_file_content(followup_spec.target, ".autoresearch.yaml", "main")
                if raw is not None:
                    contract = load_contract(raw, followup_spec.target)
                    contract_error = None
            except Exception as exc:
                log.warning("contract fetch failed for %s: %s", followup_spec.target, exc)
                contract_error = f"{type(exc).__name__}: {exc}"
            if contract_error is None:
                # a bad panel config idles the same launch lanes a bad
                # contract does — same silent-idle class, so it rides the
                # same alarm
                panel_error = _panel_preflight_error(followup_spec)
                if panel_error:
                    contract_error = f"panel preflight: {panel_error}"
            try:
                contract_alarm(
                    root,
                    github,
                    followup_spec.target,
                    contract_error,
                    now,
                    bot_login=followup_spec.bot_login,
                )
            except Exception as exc:
                log.warning("contract alarm failed: %s", exc)
        limits = effective_limits(contract.budgets if contract is not None else None)
        # The contract's followup walltime only overrides when EXPLICITLY
        # set — and only DOWNWARD from the operator's spec value: strictly-
        # downward shaping must hold against operator config too, not just
        # against the module defaults.
        spec = shape_followup_spec(followup_spec, limits, contract)
        ended, submitted = service_in_review(
            root,
            github,
            compute,
            spec,
            now,
            dry_run=followup_dry_run,
            allow_submit=launch_ok,
        )
        try:
            service_research_log(root, github, spec, now)
        except Exception as exc:  # the ledger is advisory; the tick continues
            log.warning("research-log service failed: %s", exc)
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
    # The coalesce marker is stamped by the CALLER at real completion time (see
    # main / mark_tick_complete) — not here with the start-of-tick `now`, which
    # a tick longer than the window would leave stale.
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
# An implementing run untouched for this long is a crashed climb job; it must
# not block the lane forever, but the window must exceed the LONGEST honest
# job — the 120-min contract ceiling plus the panel allowance the tick adds
# (~4.5 h at the defaults) plus queue-start slack — or the picker declares a
# live run stranded and starts a second one on the same target, breaking the
# one-active-run serialization. Its cooldown entry still applies, so a
# crashed benchmark isn't immediately retried.
STRANDED_IMPLEMENTING_S = 12 * 3600
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


def _climb_panel_argv(spec: FollowupSpec) -> list[str]:
    """Panel args for a climb job; empty when the operator disabled the panel."""
    if not spec.panel.strip():
        return []
    argv = ["--panel", spec.panel]
    if spec.panel_key_file:
        argv += ["--panel-key-file", spec.panel_key_file]
    return argv


def _author_config_error(spec: FollowupSpec) -> str:
    """Why the config-driven author would die at the climb's startup ("" when it
    won't), checked on the tick host BEFORE a claim/submit so a codex misconfig
    (e.g. AUTORESEARCH_AUTHOR_BACKEND=codex with no non-claude model) never
    strands a claimed intake issue. Reads the fleet author config from env — the
    same source the climb defaults from — and the image the tick already knows."""
    from autoresearch.attempt import codex_author_config_error

    backend = os.environ.get("AUTORESEARCH_AUTHOR_BACKEND") or "claude"
    model = os.environ.get("AUTORESEARCH_AUTHOR_MODEL") or "claude-opus-5"
    return codex_author_config_error(backend, model, spec.image)


def _panel_preflight_error(spec: FollowupSpec) -> str:
    """Why the climb would die at startup on this panel config ("" when it
    won't): the lens spec, then the key file — each checked with the climb's
    OWN rules (parse_lenses for the grammar and claude-only backend;
    FileTokenProvider for exists/mode-600/non-empty), so preflight and climb
    cannot disagree.

    Preflighted BEFORE claiming or submitting: the climb CLI fails loudly,
    but by then an intake issue is already claimed — and pick_issue never
    reclaims — so the strand must be caught on the tick host, which shares
    the home filesystem the climb will read."""
    if not spec.panel.strip():
        return ""
    try:
        from autoresearch.attempt import PANEL_KEY_DEFAULT, resolve_author_key_file
        from autoresearch.github import FileTokenProvider
        from autoresearch.panel import parse_lenses

        try:
            lenses = parse_lenses(spec.panel)
        except ValueError as exc:
            return str(exc)
        # non-claude (shelled) lenses: mirror the climb's rules exactly, per
        # backend — image required, the judge's OWN key (set + absolute +
        # neither the author's nor the claude panel key + readable), and for
        # hermes its pinned clone.
        shelled = {
            "codex": "AUTORESEARCH_PANEL_CODEX_KEY_FILE",
            "hermes": "AUTORESEARCH_PANEL_HERMES_KEY_FILE",
        }
        for lens_backend, key_env in shelled.items():
            if not any(backend == lens_backend for _, backend, _ in lenses):
                continue
            if not spec.image or not Path(spec.image).is_file():
                return (
                    f"a {lens_backend} panel lens requires a real container image "
                    f"(AUTORESEARCH_IMAGE={spec.image!r})"
                )
            key_raw = os.environ.get(key_env, "").strip()
            if not key_raw:
                return (
                    f"a {lens_backend} panel lens needs {key_env} "
                    "(role separation: the judge's own key, never the author's)"
                )
            key_path = Path(key_raw).expanduser()
            if not key_path.is_absolute():
                return (
                    f"{lens_backend} panel key path {key_path} is relative; only absolute paths fly"
                )
            author = Path(resolve_author_key_file("codex")).expanduser()
            if key_path.resolve() == author.resolve():
                return (
                    f"{lens_backend} panel key file {key_path} is the codex author "
                    "key (role separation: the judge needs its own key)"
                )
            claude_panel = Path(spec.panel_key_file or PANEL_KEY_DEFAULT).expanduser()
            if key_path.resolve() == claude_panel.resolve():
                return (
                    f"{lens_backend} panel key file {key_path} is the claude panel "
                    "key file (an anthropic key must never reach another "
                    "provider's login)"
                )
            FileTokenProvider(key_path).token()
            if lens_backend == "hermes":
                repo = os.environ.get("REVIEW_HERMES_REPO", "").strip()
                # a REAL clone, not merely a directory: the harness executes
                # run_agent.py from it with the panel key, so an arbitrary or
                # empty path must fail here, never after a run is claimed
                if not repo or not (Path(repo).expanduser() / "run_agent.py").is_file():
                    return (
                        f"a hermes panel lens needs REVIEW_HERMES_REPO pointing at "
                        f"the pinned clone (run_agent.py not found under {repo!r})"
                    )
                from autoresearch.role_runner import _HERMES_PROVIDERS

                provider = os.environ.get("REVIEW_HERMES_PROVIDER", "").lower() or "openrouter"
                if provider not in _HERMES_PROVIDERS:
                    return (
                        f"unknown REVIEW_HERMES_PROVIDER {provider!r} "
                        f"(have: {sorted(_HERMES_PROVIDERS)})"
                    )
        if not any(backend == "claude" for _, backend, _ in lenses):
            return ""  # codex-only panel: the claude key checks below don't apply
        path = Path(spec.panel_key_file or PANEL_KEY_DEFAULT).expanduser()
        if not path.is_absolute():
            # the climb runs from a flight directory, not the tick's cwd — a
            # relative path that resolves here could still miss there
            return f"panel key path {path} is relative; only absolute paths fly"
        # the AUTHOR key the climb will actually use resolves per the fleet backend
        # (claude vs codex keys coexist), config-driven like the climb itself — so
        # the role-separation check compares the panel key against the RIGHT author
        # key, and a codex run is never judged by a stray Claude key.
        fleet_backend = os.environ.get("AUTORESEARCH_AUTHOR_BACKEND") or "claude"
        author = Path(resolve_author_key_file(fleet_backend))
        if not author.is_absolute():
            # same rule as the panel key: the climb resolves paths from a
            # flight directory, so a relative author path both misconfigures
            # the author AND defeats the role-separation comparison below
            return f"author key path {author} is relative; only absolute paths fly"
        if path.resolve() == author.resolve():
            return (
                f"panel key file {path} is the author key file "
                "(role separation: the verifier needs its own key)"
            )
        # ADC-only deployments (Vertex covering the claude panel) hold no
        # Anthropic key at all — the same tolerance role_key applies at run
        # time, so the preflight and the climb agree.
        from autoresearch.role_runner import role_key

        role_key(path)
        return ""
    except Exception as exc:
        # never raises: an unexpected failure (partial deploy, ELOOP, unset
        # HOME) must fail closed WITH the alarm, not abort the tick that
        # would have written it
        return f"{type(exc).__name__}: {exc}"


def _attempt_job_minutes(spec: FollowupSpec, limits: EffectiveLimits) -> int:
    """The submitted climb walltime: contract budget + panel allowance,
    clamped at the partition cap. Warns when the cap cuts below the session
    budget — the self-deadline would then fire before the author's own
    clock, and that must be a visible operator choice, never a silent
    surprise."""
    from autoresearch.limits import ATTEMPT_OVERHEAD_MINUTES

    wanted = limits.attempt_job_minutes + _panel_job_minutes(spec, limits)
    job = min(wanted, spec.max_job_minutes)
    if job < wanted:
        log.info(
            "climb job clamped to %d min by the partition cap (worst case "
            "wanted %d); slow panel rounds fail safe via the self-deadline",
            job,
            wanted,
        )
    if job < limits.session_minutes + ATTEMPT_OVERHEAD_MINUTES:
        log.warning(
            "work-job cap %d min leaves no runway around the %d-min session "
            "(the orchestrator needs ~%d min); sessions or endings will be "
            "cut short by the self-deadline",
            job,
            limits.session_minutes,
            ATTEMPT_OVERHEAD_MINUTES,
        )
    return job


def _panel_job_minutes(spec: FollowupSpec, limits: EffectiveLimits) -> int:
    """Extra walltime the panel needs, ADDED to the contract-clamped job
    budget: the contract's knobs cap the AUTHOR's spend and their ceilings
    deliberately cannot raise ours (limits.py), so the panel — the
    orchestrator's own gate, flipped on by the tick — brings its own time.
    Worst case: three sequential reads of every lens (initial, post-revision,
    merged-tree) on the judge budget, plus one revision wake on the session
    budget. The revision's re-measure rides the margin the self-deadline
    already fails safe on."""
    lenses = [entry for entry in spec.panel.split(",") if entry.strip()]
    if not lenses:
        return 0
    from autoresearch.roles import reviewer_spec, verifier_spec

    judge_minutes = max(reviewer_spec().budget.walltime_s, verifier_spec().budget.walltime_s) // 60
    return 3 * len(lenses) * judge_minutes + limits.session_minutes


def _climb_limit_argv(limits: EffectiveLimits, job_minutes: int) -> list[str]:
    """Climb-CLI flags carrying the tick-resolved limits: the job's ACTUAL
    walltime rides along (contract budget + any panel allowance, clamped at
    the partition cap — exactly what the JobSpec gets) so the climb arms its
    self-deadline against the real clock (Slurm delivers no signals to our
    processes on Torch). The session shrinks to fit a
    CAPPED job with the same rule limits.effective_limits applies to
    contract values — better a short session that ends cleanly than a full
    one the self-deadline kills mid-flight."""
    from autoresearch.limits import ATTEMPT_OVERHEAD_MINUTES, SESSION_MINUTES_FLOOR

    session = min(limits.session_minutes, job_minutes - ATTEMPT_OVERHEAD_MINUTES)
    return [
        "--max-turns",
        str(limits.session_max_turns),
        "--session-minutes",
        str(max(SESSION_MINUTES_FLOOR, session)),
        "--job-minutes",
        str(job_minutes),
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
    paused = outage_active(root, now, role="solver")
    if paused:
        log.info("self-initiated lane paused (api outage: %s)", paused)
        return None
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
            elif (alive := _holder_alive(compute, str(pending.get("job_id", "")))) is True or (
                not expired and alive is not False
            ):
                # climb queued or starting; its record isn't written yet. A
                # provably-alive job waits regardless of the marker's TTL —
                # queue wait can exceed it — the TTL only breaks ties when
                # Slurm can't say (a dup submit is worse than a slow retry).
                return None
            else:
                # Died before writing a record. Keep its cooldown so a
                # crash-at-startup loop can't resubmit every tick.
                clear_pending(root, spec.target)
                pending_attempt = (str(pending.get("benchmark", "")), submitted_at)
        benchmark = pick_self_initiated(records, contract, spec.target, now, pending_attempt)
        if benchmark is None:
            return None
        author_error = _author_config_error(spec)
        if author_error:
            log.error(
                "climb on %s not launched: author misconfigured — %s "
                "(fix AUTORESEARCH_AUTHOR_BACKEND/_MODEL)",
                benchmark,
                author_error,
            )
            return None
        panel_error = _panel_preflight_error(spec)
        if panel_error:
            log.error(
                "climb on %s not launched: panel misconfigured — %s "
                "(fix it, or set AUTORESEARCH_PANEL='' to disable the panel)",
                benchmark,
                panel_error,
            )
            return None
        if dry_run:
            return (benchmark, "dry-run")
        job_minutes = _attempt_job_minutes(spec, limits)
        argv = [
            "uv",
            "run",
            "python",
            "-m",
            "autoresearch.attempt",
            "--target",
            spec.target,
            "--benchmark",
            benchmark,
            "--run-root",
            str(spec.run_root),
            "--image",
            spec.image,
            *_climb_limit_argv(limits, job_minutes),
            *_climb_panel_argv(spec),
        ]
        if spec.pat_file:
            argv += ["--pat-file", spec.pat_file]
        # config-driven author: climb resolves the author backend/model/key from
        # AUTORESEARCH_AUTHOR_* env (inherited by the job), so the tick threads
        # neither the backend nor its key — a new backend needs zero tick change.
        job_id = compute.submit(
            JobSpec(
                job_name=f"climb-{benchmark}"[:60],
                account=spec.account,
                partition=spec.job_partition or spec.partition,
                time_minutes=job_minutes,
                command=_flight_command(spec.home, f"climb-{benchmark}"[:60], now, argv),
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
        # must not fly alongside a solver climb or another stewardship.
        records = list_runs(root)
        # reconcile first: killed jobs never post their own release — and
        # BEFORE the outage pause below, because a claim orphaned by the
        # very session the outage killed must not stay held all cooldown
        # (reconciliation is model-free bookkeeping; only spawning pauses)
        release_orphaned_claims(github, target, records, now, bot_login=spec.bot_login)
        paused = outage_active(root, now, role="steward")
        if paused:
            log.info("steward lane paused (api outage: %s)", paused)
            return None
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
            alive = _holder_alive(compute, str(pending.get("job_id", "")))
            # liveness first, TTL only breaks unknown ties — same rule as
            # the self-initiated lane (queue wait can outlive the TTL)
            if not landed and (alive is True or (not expired and alive is not False)):
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
            # the SAME clamped walltime the JobSpec requests, so the
            # self-deadline arms against the real clock
            *_climb_limit_argv(limits, min(limits.attempt_job_minutes, spec.max_job_minutes)),
        ]
        if spec.pat_file:
            argv += ["--pat-file", spec.pat_file]
        try:
            job_id = compute.submit(
                JobSpec(
                    job_name=f"steward-issue-{task.number}",
                    account=spec.account,
                    partition=spec.job_partition or spec.partition,
                    time_minutes=min(limits.attempt_job_minutes, spec.max_job_minutes),
                    command=_flight_command(spec.home, f"steward-issue-{task.number}", now, argv),
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
    paused = outage_active(root, now, role="solver")
    if paused:
        log.info("intake lane paused (api outage: %s)", paused)
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
        author_error = _author_config_error(spec)
        if author_error:
            log.error(
                "issue #%d not claimed: author misconfigured — %s "
                "(fix AUTORESEARCH_AUTHOR_BACKEND/_MODEL)",
                task.number,
                author_error,
            )
            return None
        panel_error = _panel_preflight_error(spec)
        if panel_error:
            log.error(
                "issue #%d not claimed: panel misconfigured — %s "
                "(fix it, or set AUTORESEARCH_PANEL='' to disable the panel)",
                task.number,
                panel_error,
            )
            return None
        if dry_run:
            return (f"issue-{task.number}", "dry-run")
        job_minutes = _attempt_job_minutes(spec, limits)
        # claim BEFORE submit: Slurm queueing can take minutes, and the next
        # tick must not re-claim the same issue in that window
        from autoresearch.intake import CLAIM_MARKER, MAX_INTAKE_ATTEMPTS, RELEASE_MARKER

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
            "autoresearch.attempt",
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
            *_climb_limit_argv(limits, job_minutes),
            *_climb_panel_argv(spec),
        ]
        if spec.pat_file:
            argv += ["--pat-file", spec.pat_file]
        # config-driven author: climb resolves the author key from the
        # AUTORESEARCH_AUTHOR_* env by backend; the tick does not thread it.
        try:
            job_id = compute.submit(
                JobSpec(
                    job_name=f"climb-issue-{task.number}",
                    account=spec.account,
                    partition=spec.job_partition or spec.partition,
                    time_minutes=job_minutes,
                    command=_flight_command(spec.home, f"climb-issue-{task.number}", now, argv),
                    cpus=4,
                    mem="8G",
                )
            )
        except Exception:
            # the claim is already posted and pick_issue skips claimed
            # issues, so a failed submit must release it (same pattern as
            # the steward lane) or the issue is stranded forever
            with contextlib.suppress(Exception):
                github.comment(
                    target,
                    task.number,
                    f"{RELEASE_MARKER}\nSubmission failed; claim released — "
                    f"a later tick will retry this issue (intake gives up "
                    f"after {MAX_INTAKE_ATTEMPTS} claim attempts and leaves "
                    f"it for a human).",
                )
            raise
        log.info("issue #%s claimed for climb job %s", task.number, job_id)
        return (f"issue-{task.number}", job_id)
    except Exception as exc:  # intake must not break the tick
        log.warning("intake pass failed: %s", exc)
        return None


@dataclass
class LoggingDispatcher:
    """Never dispatched in production: main() runs the sweep dry unless
    dispatched wake is armed, so no lease is taken and no attempt is
    counted. This exists for the seam."""

    def dispatch(self, record: RunRecord, reason: str) -> str:
        log.info("WOULD WAKE %s (%s) — session dispatch lands in phase 5", record.run_id, reason)
        return ""


@dataclass
class JobWakeDispatcher:
    """Delivers a wake by submitting a Slurm job that runs the wake CLI
    (`climb --resume <run_id>`), depending on the run's eval jobs (the record's
    `afterany`) so it fires when they finish — or immediately if they already
    have. CPU-only and short: a wake reads cached results and opens a PR, it
    never holds a GPU. Returns the wake job id (async: it owns the lease until
    it completes)."""

    compute: SlurmCompute
    spec: FollowupSpec
    now: float
    wake_minutes: int = 20

    def dispatch(self, record: RunRecord, reason: str) -> str:
        argv = [
            "uv",
            "run",
            "python",
            "-m",
            "autoresearch.attempt",
            "--resume",
            record.run_id,
            "--run-root",
            str(self.spec.run_root),
            "--image",
            self.spec.image,
            "--account",
            self.spec.account,
            "--partition",
            self.spec.partition,
            # the wake runs the SAME verification panel as the fresh climb, so a
            # dispatched improvement is verified before it is published.
            *_climb_panel_argv(self.spec),
            # session budget for the depth-axis REVISION (a blocking panel
            # finding wakes the author to revise).
            "--max-turns",
            str(self.spec.max_turns),
        ]
        # An AUTHOR-SLEEP wake resumes a FULL author session (not the short
        # read-decide a candidate wake runs), so the Slurm job must fit that
        # session or walltime kills the resumed session mid-run and the run just
        # waits for another wake. Size the job to the session
        # budget + overhead and pass --session-minutes so the in-job
        # self-deadline fires BEFORE Slurm's walltime. A candidate wake keeps
        # the short budget (read results + panel).
        from autoresearch.limits import ATTEMPT_OVERHEAD_MINUTES
        from autoresearch.roles import author_spec

        if record.stage.get("phase") == "author-sleep":
            session_minutes = author_spec().budget.walltime_s // 60
            argv += ["--session-minutes", str(session_minutes)]
            job_minutes = min(session_minutes + ATTEMPT_OVERHEAD_MINUTES, self.spec.max_job_minutes)
        else:
            job_minutes = self.wake_minutes + _wake_panel_minutes(self.spec)
        if self.spec.pat_file:
            argv += ["--pat-file", self.spec.pat_file]
        # config-driven author: `climb --resume` resolves the author key from the
        # PARKED RUN's backend (persisted on its record) inside climb.main — the
        # tick does not thread the key, so a fleet flip picks the right one.
        name = f"wake-{record.run_id}"[:60]
        afterany = str(record.stage.get("afterany", ""))
        return self.compute.submit(
            JobSpec(
                job_name=name,
                account=self.spec.account,
                partition=self.spec.job_partition or self.spec.partition,
                time_minutes=job_minutes,
                command=_flight_command(self.spec.home, name, self.now, argv),
                dependency=afterany,
                cpus=2,
                mem="4G",
            )
        )


def _wake_panel_minutes(spec: FollowupSpec) -> int:
    """Extra wake walltime for the verification panel it now runs — the base
    `wake_minutes` covers only reading results + opening the PR. Budgeted for
    the worst case a single wake reaches: one read per lens PLUS one revision
    author session (the depth-axis wake-to-revise). The revision's re-measure
    only DISPATCHES (then the job ends, parked), so it needs no extra time.
    Grounded in the same judge/author budgets the climb job uses."""
    lenses = [entry for entry in spec.panel.split(",") if entry.strip()]
    if not lenses:
        return 0
    from autoresearch.roles import author_spec, reviewer_spec, verifier_spec

    judge_minutes = max(reviewer_spec().budget.walltime_s, verifier_spec().budget.walltime_s) // 60
    session_minutes = author_spec().budget.walltime_s // 60
    return len(lenses) * judge_minutes + session_minutes


def _wake_dispatcher_from_env(
    compute: SlurmCompute, followup_spec: FollowupSpec | None, now: float, root: Path
) -> tuple[WakeDispatcher, bool]:
    """The wake delivery for this tick, behind an EXPLICIT on-switch so the
    dispatched-wake path lands DARK. Returns `(dispatcher, live)`:

    * armed (the `AUTORESEARCH_DISPATCH_WAKE` env var OR a `<root>/DISPATCH_WAKE`
      sentinel file) AND the chain env carries what a wake job needs -> the real
      `JobWakeDispatcher` and a LIVE sweep;
    * otherwise -> the `LoggingDispatcher` and a DRY sweep.

    The sentinel mirrors PAUSE: an operator arms/disarms with a touch/rm, no
    chain restart. So dispatched climbing is turned on deliberately, and a
    half-configured environment fails safe to dry rather than to a wake job
    that cannot run."""
    armed = (
        bool(os.environ.get("AUTORESEARCH_DISPATCH_WAKE", "").strip())
        or (root / DISPATCH_WAKE_SENTINEL).exists()
    )
    if not armed:
        return LoggingDispatcher(), False
    if followup_spec is None:
        log.warning("dispatch-wake armed but the chain env is incomplete; wake stays dry")
        return LoggingDispatcher(), False
    log.info("dispatched-wake ON: the waiting-run sweep delivers real wakes this tick")
    return JobWakeDispatcher(compute, followup_spec, now), True


def _max_job_minutes_from_env() -> int:
    """AUTORESEARCH_MAX_JOB_MINUTES, clamped into what the code can honor:
    at least the climb-job floor (an operator on a short-MaxTime partition
    must be able to LOWER the cap below cpu_short's 6h, or every submit is
    rejected), at most the ceiling the stranded window allows. A clamped
    value logs — a silently-changed cap would read as the partition
    rejecting jobs for no reason."""
    from autoresearch.limits import ATTEMPT_JOB_MINUTES_FLOOR

    raw = os.environ.get("AUTORESEARCH_MAX_JOB_MINUTES", "").strip()
    if not raw:
        return MAX_ATTEMPT_JOB_MINUTES
    try:
        value = int(raw)
    except ValueError:
        log.warning("AUTORESEARCH_MAX_JOB_MINUTES=%r is not an integer; using default", raw)
        return MAX_ATTEMPT_JOB_MINUTES
    clamped = max(ATTEMPT_JOB_MINUTES_FLOOR, min(value, MAX_JOB_MINUTES_CEILING))
    if clamped != value:
        log.warning("AUTORESEARCH_MAX_JOB_MINUTES=%d clamped to %d", value, clamped)
    return clamped


def _cadence_s() -> float:
    """The chain's tick cadence in seconds (AUTORESEARCH_CADENCE_MIN, the same
    knob tick_chain.sbatch uses), defaulting to 30 min when unset/invalid."""
    raw = os.environ.get("AUTORESEARCH_CADENCE_MIN", "").strip()
    try:
        cadence_s = float(raw) * 60 if raw else 30 * 60
    except ValueError:
        cadence_s = 30 * 60
    return cadence_s if (math.isfinite(cadence_s) and cadence_s > 0) else 30 * 60


def _coalesce_ceiling_s() -> float:
    """The largest SAFE coalesce window, bounding both the default and an
    explicit AUTORESEARCH_MIN_TICK_MINUTES: half the cadence (so an on-cadence
    tick is never coalesced even when the previous one ran a little late), and
    never above the absolute MAX_MIN_TICK_S. A window at/above the cadence would
    swallow every normal tick and stall the loop — this is what forbids it."""
    return min(float(MAX_MIN_TICK_S), _cadence_s() / 2)


def _default_min_tick_s() -> float:
    """The coalesce window when none is set: the safe ceiling, further capped at
    the 10-min DEFAULT_MIN_TICK_S — small enough to only catch pile-ups, and
    cadence-aware so a short cadence scales it down instead of swallowing every
    tick."""
    return min(DEFAULT_MIN_TICK_S, _coalesce_ceiling_s())


def _min_tick_s_from_env() -> float:
    """AUTORESEARCH_MIN_TICK_MINUTES -> the coalesce window in seconds. Unset
    derives a cadence-aware default; non-numeric/non-finite also fall back to it;
    negative clamps to 0 (coalesce disabled); a value at/above the safe ceiling
    (half the cadence, capped at MAX_MIN_TICK_S) clamps down so it cannot stall
    the loop."""
    raw = os.environ.get("AUTORESEARCH_MIN_TICK_MINUTES", "").strip()
    if not raw:
        return _default_min_tick_s()
    try:
        minutes = float(raw)
    except ValueError:
        log.warning("AUTORESEARCH_MIN_TICK_MINUTES=%r is not a number; using default", raw)
        return _default_min_tick_s()
    # reject inf/nan: an infinite window would coalesce every future tick and
    # freeze the loop (a finite elapsed time is always < inf)
    if not math.isfinite(minutes):
        log.warning("AUTORESEARCH_MIN_TICK_MINUTES=%r is not finite; using default", raw)
        return _default_min_tick_s()
    seconds = max(0.0, minutes * 60)
    ceiling = _coalesce_ceiling_s()
    if seconds > ceiling:
        log.warning(
            "AUTORESEARCH_MIN_TICK_MINUTES=%s exceeds the safe ceiling "
            "(%.0f min, ~half the cadence); clamping so normal ticks are not coalesced",
            raw,
            ceiling / 60,
        )
        return ceiling
    return seconds


def _followup_spec_from_env(root: Path) -> tuple[Any, FollowupSpec | None]:
    """GitHub client + FollowupSpec from the chain environment, or Nones when
    the environment is incomplete (the tick then runs without in-review
    servicing, and logs what is absent)."""
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
                run_root=root,
                image=image,
                home=Path(home),
                pat_file=pat_file,
                target=os.environ.get(
                    "AUTORESEARCH_TARGET", "agentic-learning-ai-lab/autoresearch-pilot"
                ),
                steward_key_file=os.environ.get("AUTORESEARCH_STEWARD_KEY_FILE", ""),
                panel=os.environ.get("AUTORESEARCH_PANEL", "verify,review"),
                panel_key_file=os.environ.get("AUTORESEARCH_PANEL_KEY_FILE", ""),
                job_partition=os.environ.get("AUTORESEARCH_JOB_PARTITION", ""),
                max_job_minutes=_max_job_minutes_from_env(),
            )
            return github, followup_spec
        except Exception as exc:
            log.warning("in-review servicing disabled: %s", exc)
            return None, None
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
    return None, None


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
    # In-review servicing is LIVE when credentials + image are available in the
    # chain environment. The waiting-run sweep delivers real wakes only when the
    # operator arms it — the AUTORESEARCH_DISPATCH_WAKE env var or a
    # <root>/DISPATCH_WAKE sentinel — and the env is complete; by default it
    # stays dry with the LoggingDispatcher — dispatched climbing lands DARK.
    github, followup_spec = _followup_spec_from_env(args.root)
    compute = SlurmCompute()
    now = time.time()
    dispatcher, wake_live = _wake_dispatcher_from_env(compute, followup_spec, now, args.root)

    report = tick(
        args.root,
        compute,
        dispatcher,
        now=now,
        grace_s=args.grace_s,
        lease_ttl_s=args.lease_ttl_s,
        dry_run=not wake_live,
        github=github,
        followup_spec=followup_spec,
        followup_dry_run=False,
        min_free_bytes=int(args.min_free_gb * 1024**3),
        min_tick_s=_min_tick_s_from_env(),
    )
    # Stamp the coalesce marker at REAL completion time (a fresh time.time(),
    # not the start-of-tick `now`), so a long tick does not leave a stale marker.
    mark_tick_complete(args.root, report, time.time())
    log.info(
        "tick done: paused=%s coalesced=%s swept=%d woken=%d deferred=%d reaped=%d stuck=%d "
        "impl_ended=%s review_ended=%s followups=%s intake=%s self_initiated=%s steward=%s "
        "disk=%s launch_blocked=%s",
        report.paused,
        report.coalesced,
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
