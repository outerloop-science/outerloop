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
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from outerloop.compute import (
    GONE,
    Compute,
    JobSpec,
    LocalCompute,
    SlurmError,
    SlurmQueryError,
    compute_from_env,
    is_pending,
    is_terminal,
    local_mode,
    quote_command,
)
from outerloop.disk import DEFAULT_MIN_FREE_BYTES, check_disk
from outerloop.harness import DEFAULT_MAX_TURNS, redact
from outerloop.housekeeping import shed_ended_workspaces
from outerloop.limits import EffectiveLimits, effective_limits
from outerloop.markers import has_marker, marker
from outerloop.runstate import (
    ABORTED,
    ENDED,
    IMPLEMENTING,
    IN_REVIEW,
    MAX_WAKE_ATTEMPTS,
    STUCK,
    WAITING,
    Lease,
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
# env-var surgery on a live tick. The OUTERLOOP_DISPATCH_WAKE env var still
# works too (either arms it); the sentinel is the reversible, restart-free path.
DISPATCH_WAKE_SENTINEL = "DISPATCH_WAKE"
HEARTBEAT_NAME = "heartbeat.json"
# Written at a full tick's END (not its start) — the coalesce guard's signal, so
# a tick that crashes mid-work cannot suppress the next (recovery) tick.
WORK_MARKER_NAME = "last_worked.json"

# Grace between "experiment terminal" and the sweep stepping in: the afterany
# job gets this long to deliver before the backup assumes it lost.
DEFAULT_GRACE_S = 15 * 60
# a blind park (no job ids to poll) waits its eval walltime plus this queue
# slack before a follow-up is sent to look for the result
BLIND_PARK_SLACK_MIN = 12 * 60

# Slurm pending reasons that mean "the job will run when its turn comes": the
# queue is busy, a reservation or dependency is ahead of it, or the account's
# OWN jobs hold a per-user/per-account/group cap that clears as they finish. A
# job in this state keeps accruing priority age; cancelling and resubmitting
# would put it at the back of the queue behind itself. Per-job limits
# (`...PerJob...`), a dependency that can never be satisfied, an invalid
# account/QOS or a held job are NOT waits: those never clear on their own.
QUEUE_WAIT_REASONS = frozenset(
    {"Priority", "Resources", "Reservation", "Dependency", "ReqNodeNotAvail"}
)


def is_queue_wait(reason: str) -> bool:
    """Whether a squeue pending reason describes a healthy wait (see
    QUEUE_WAIT_REASONS). Reasons arrive as `Name` or `Name, detail...`."""
    head = reason.strip().split(",")[0].split(" ")[0].strip()
    if not head:
        return False
    if head in QUEUE_WAIT_REASONS:
        return True
    if "PerJob" in head:
        return False
    return any(tag in head for tag in ("PerUser", "PerAccount", "Grp"))


# A held lease is stale after the session timeout plus slack.
DEFAULT_LEASE_TTL_S = 3600 + 15 * 60
# Coalesce guard: skip a tick's work if another ran within this window. Under
# partition congestion, queued ticks bunch up and become eligible together
# (serialized by the singleton dependency), so they would run back-to-back and
# redundantly re-sweep. The chain schedules ticks a full cadence apart by
# begin-time, so only late-bunched pile-ups fall inside this window; keep it
# well BELOW the cadence (default 30 min). 0 disables. Env: OUTERLOOP_MIN_TICK_MINUTES.
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
    shed: tuple[str, ...] = ()  # ended runs whose workspaces housekeeping removed


# The submitted walltime must never exceed the job partition's MaxTime —
# sbatch REJECTS a longer request outright, which would ground every climb.
# The DEFAULT matches cpu_short (6 h); an operator moving work jobs to a
# longer partition (OUTERLOOP_JOB_PARTITION=cpu48) raises the cap with
# OUTERLOOP_MAX_JOB_MINUTES. Code-side ceiling: the cap must stay under
# STRANDED_IMPLEMENTING_S or the picker declares live runs stranded — jobs
# longer than 10 h need that window made spec-aware first (named gap). The
# self-deadline arms at the CLAMPED value, so a job that wanted more time
# fails safe mid-panel instead of never starting.
MAX_ATTEMPT_JOB_MINUTES = 6 * 60
MAX_JOB_MINUTES_CEILING = 10 * 60


def _bot_login_default() -> str:
    """FollowupSpec's login default, resolved at construction (the tick reads
    the chain's env, jobs inherit it); github is imported here on purpose —
    the tick module stays importable without it."""
    from outerloop.github import bot_login_from_env

    return bot_login_from_env()


@dataclass(frozen=True)
class FollowupSpec:
    """How the tick launches follow-up jobs for in-review runs."""

    account: str
    partition: str
    run_root: Path
    image: str
    home: Path  # OUTERLOOP_HOME: cwd for the submitted job
    bot_login: str = field(default_factory=_bot_login_default)
    time_minutes: int = 90  # min()'d with the contract's followup_job_minutes
    max_turns: int = DEFAULT_MAX_TURNS  # session turn budget for follow-up jobs
    pat_file: str = ""  # forwarded to the job; "" = the followup CLI default
    # GitHub App config path; jobs inherit OUTERLOOP_GITHUB_APP_FILE from
    # the tick environment, so it is never threaded through argv
    github_app_file: str = ""
    target: str = ""  # the repo the intake pass scans for requested-lane issues
    # the STEWARD'S OWN key (role separation): the steward lane stays off
    # until the operator provisions it
    steward_key_file: str = ""
    # Pre-PR verification panel for climb jobs (docs/design/orchestrator-verify.md).
    # DEFAULT ON — the flip is code, the off-switch is OUTERLOOP_PANEL="".
    # The climb CLI fails LOUDLY on a bad panel config (a configured gate must
    # never silently vanish); the tick preflights the same rules — lens
    # grammar AND key file — before claiming or submitting so nothing is
    # stranded. The panel's walltime is the orchestrator's own overhead: the
    # tick ADDS a panel allowance to the contract-clamped job budget
    # (_panel_job_minutes) rather than eating the author's time; a residual
    # overrun still fails safe through the self-deadline.
    panel: str = "verify,review"
    panel_key_file: str = ""  # "" = the climb CLI's default verifier-key path
    # The GPU lane for benchmarks whose contract sets `gpus > 0` (their evals
    # and author launches); empty = this deployment cannot place GPU jobs,
    # and the launch lanes refuse such benchmarks (a queue that can never
    # run is worse than a loud refusal). gpu_account "" = same as `account`.
    gpu_partition: str = ""
    gpu_account: str = ""
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
    if not (home / ".git").exists():
        # not a checkout (the local loop on an installed package): nothing to
        # pin, the job runs from the home directory itself
        return home
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


def _benchmark_gpus(contract: Any, benchmark: str) -> int:
    bench = next((b for b in getattr(contract, "benchmarks", []) if b.name == benchmark), None)
    return int(getattr(bench, "gpus", 0) or 0)


def _gpu_lane_error(contract: Any, benchmark: str, spec: FollowupSpec) -> str:
    """Why an attempt on `benchmark` cannot launch here, or "": a contract
    with GPU benchmarks needs this deployment to name a GPU lane — otherwise
    evals would queue into jobs that can never run (the climb would then
    park forever on a phantom eval). ANY GPU benchmark in the contract
    counts, not just the climbed one: the suite gate measures siblings.
    Local compute has no lanes — jobs run on whatever GPUs the machine
    has — so the check is waived there."""
    if spec.gpu_partition or local_mode():
        return ""
    gpu_benches = [
        b.name for b in getattr(contract, "benchmarks", []) if int(getattr(b, "gpus", 0) or 0)
    ]
    if gpu_benches:
        return (
            f"contract has GPU benchmarks ({', '.join(gpu_benches)}) but no GPU lane is "
            "configured (set OUTERLOOP_GPU_PARTITION)"
        )
    return ""


_UNCONTAINED_WARNED = False  # the local-mode warning is said once per process


def _containment(image: str) -> list[str]:
    """The job's containment flags: the image when there is one, else the
    explicit uncontained flag every entry point requires in its absence."""
    return ["--image", image] if image else ["--uncontained"]


def _interpreter(home: Path) -> list[str]:
    """How a job runs Python. From a source checkout, `uv run python` resolves
    the project's own environment, as the cluster's flights do. From a plain
    home directory (the local loop on an installed package) the job runs the
    interpreter this tick runs under, which is where the package is."""
    if (home / "pyproject.toml").is_file():
        return ["uv", "run", "python"]
    return [sys.executable]


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


CONTRACT_ALARM_MARKER = marker("contract-alarm")
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
                "outerloop: launch lanes are paused",
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


def _contract_text(github: Any, target: str, ref: str) -> str | None:
    """The target's contract at `ref` — `.outerloop.yaml`, else the legacy
    `.autoresearch.yaml` — or None when it has neither."""
    from outerloop.contract import find_contract

    found = find_contract(lambda name: github.get_file_content(target, name, ref))
    return found[1] if found else None


def _fence(content: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", content)), default=0)
    return "`" * max(3, longest + 1)


def _find_alarm_issue(github: Any, target: str, bot_login: str) -> int:
    """Only the BOT'S own marker'd issue counts: the marker is a public
    string, and adopting a stranger's issue would let anyone suppress the
    real alarm or get their issue closed by the bot."""
    from outerloop.github import is_own_login

    return next(
        (
            int(issue.get("number", 0))
            for issue in github.list_open_issues(target, max_pages=10)
            if has_marker(str(issue.get("body", "")), "contract-alarm")
            and is_own_login(str((issue.get("user") or {}).get("login", "")), bot_login)
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


def _base_dial(
    github: Any, target: str, pr: dict, main_contract: Any, main_target: str = ""
) -> str:
    """The merge dial that governs THIS PR: its own target's base-branch
    contract. The tick's contract is read from ITS configured target's main;
    it applies only to a main-based PR of that same target — any other
    target or base is fetched from the PR's own coordinates, and unreadable
    or unparsable means "manual" (never arm on doubt)."""
    base_ref = str((pr.get("base") or {}).get("ref", "")) or "main"
    if base_ref == "main" and target == main_target:
        return str(getattr(main_contract, "merge", "manual"))
    try:
        from outerloop.contract import load_contract

        raw = _contract_text(github, target, base_ref)
        if raw is None:
            return "manual"
        return str(getattr(load_contract(raw, target), "merge", "manual"))
    except Exception as exc:
        log.warning("base-contract read failed for %s@%s: %s", target, base_ref, exc)
        return "manual"


def service_in_review(
    root: Path,
    github: Any,  # GitHubClient (Any keeps tick importable without github deps)
    compute: Compute,
    spec: FollowupSpec,
    now: float,
    dry_run: bool = False,
    allow_submit: bool = True,
    contract: Any = None,
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
    from outerloop.followup import (
        _pr_number,
        close_if_done,
        conflict_wake_action,
        has_new_comments,
        panel_wake_pending,
    )

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
            try:
                pr = github.get_pull_request(record.target, _pr_number(record.pr_url))
            except Exception:
                continue  # unreadable PR: nothing to decide this tick
            # Idempotent auto-arm: once GitHub reports the PR CLEAN (green
            # checks AND up-to-date with the CURRENT base — GitHub's own
            # freshness proof), the kernel-read contract STILL says auto, and
            # A dispatched re-measure in flight: nothing else is serviced (the
            # sealed change lands first, so the next comment is answered on
            # the tree it will actually see) and nothing is armed. Once every
            # eval job is terminal, a follow-up is submitted to finish it.
            measure_ready = False
            if record.followup_stage:
                raw_ids = record.followup_stage.get("job_ids")
                job_ids = [str(j) for j in raw_ids] if isinstance(raw_ids, list) else []
                if job_ids:
                    try:
                        states = [compute.status(j) for j in job_ids]
                    except SlurmQueryError:
                        continue  # unknown: neither service nor arm
                    if not all(is_terminal(s) or s == GONE for s in states):
                        continue
                    measure_ready = True
                else:
                    # a BLIND park (the measurer could not read the queue at
                    # dispatch): no ids to poll, so the eval walltime plus the
                    # climb's queue slack is the floor before a follow-up is
                    # sent to look — never one per tick (terra #241 r1)
                    from outerloop.dispatch import effective_eval_minutes

                    parked_at = float(record.followup_stage.get("parked_at", 0.0) or 0.0)  # type: ignore[arg-type]
                    floor_min = int(record.followup_stage.get("eval_minutes", 0) or 0)  # type: ignore[call-overload]
                    floor_s = (effective_eval_minutes(floor_min) + BLIND_PARK_SLACK_MIN) * 60
                    if now - parked_at < floor_s:
                        continue
                    measure_ready = True
            wake_action = conflict_wake_action(record, pr)
            if wake_action == "clear":
                # the PR is clean again: re-arm the wake for this head — the
                # base can move and conflict the SAME head a second time
                try:
                    save_record(root, replace(record, dirty_wake_head=""), now)
                except OSError as exc:
                    log.warning("conflict cursor clear failed for %s: %s", record.run_id, exc)
            if (
                not measure_ready
                and not has_new_comments(record, github, spec.bot_login)
                and wake_action != "wake"
                and not panel_wake_pending(record, pr)
            ):
                # NOTHING awaits servicing — only a fully quiet PR may
                # self-merge (pending reviewer feedback always wins over
                # arming: a followup must service it first, and a pushed
                # change would kill the blessing anyway). The RECORD says the
                # publish was auto-eligible (published under merge:auto with
                # a clean panel — #171's exact arming condition; a manual
                # publish never consented, and contracts alone cannot prove
                # either fact after a dial flip); GitHub's own CLEAN state is
                # the freshness proof; the PR's base-branch contract is the
                # governing dial. Running every tick survives any crash
                # between a sync push and this step; the helper direct-merges
                # when nothing is pending to arm against.
                # ...and no follow-up job may be LIVE: a running responder can
                # have pushed a code change whose record write (clearing the
                # blessing) has not landed yet — arming on that head would
                # merge code the panel never saw (terra #228 r7)
                followup_live = False
                if record.followup_job_id:
                    try:
                        state = compute.status(record.followup_job_id)
                        followup_live = not (is_terminal(state) or state == GONE)
                    except SlurmQueryError:
                        followup_live = True  # unknown = assume live, never arm
                if (
                    not dry_run
                    and not is_steward
                    and not followup_live
                    and record.auto_blessed_head
                    and str((pr.get("head") or {}).get("sha", "")) == record.auto_blessed_head
                    and contract is not None
                    and pr.get("state") == "open"
                    and not pr.get("merged")
                    and not pr.get("draft")
                    and pr.get("mergeable_state") == "clean"
                    and _base_dial(github, record.target, pr, contract, spec.target) == "auto"
                ):
                    try:
                        # the mutation itself is bound to the blessed head: a
                        # push racing this check is refused by GitHub, not
                        # merged (terra #228 r9)
                        github.arm_auto_merge_auto_mode(
                            record.target,
                            _pr_number(record.pr_url),
                            expected_head=record.auto_blessed_head,
                        )
                    except Exception as exc:
                        log.warning("auto-arm failed for %s: %s", record.run_id, exc)
                continue
            if record.followup_job_id:
                try:
                    state = compute.status(record.followup_job_id)
                    if not (is_terminal(state) or state == GONE):
                        continue  # a follow-up job is already queued/running
                except SlurmQueryError:
                    continue  # unknown — do not stack another job
            # the wake-attempt counter caps follow-up retries too: a responder
            # that cannot advance its cursors must not burn a session per tick.
            # A LANDED re-measure gets its own allowance: the sessions that
            # synced the base and dispatched it were progress, and capping the
            # follow-up that pushes the sealed number parks the result forever
            # (speedrun agent-03, 2026-09-04: three syncs spent the cap, the
            # measure completed, and the run idled for two days on this
            # branch). The allowance is counted on the stage itself, so a
            # finishing session that reverts and leaves the stage intact is
            # retried MAX_WAKE_ATTEMPTS times, not once per tick; a stage that
            # finishes is cleared, and with it the count.
            finish_attempts = (
                int(record.followup_stage.get("finish_attempts", 0) or 0)  # type: ignore[call-overload]
                if measure_ready
                else 0
            )
            if measure_ready and finish_attempts >= MAX_WAKE_ATTEMPTS:
                log.warning(
                    "run %s: %d follow-ups failed to finish the landed re-measure; parked",
                    record.run_id,
                    finish_attempts,
                )
                continue
            if record.wake_attempts >= MAX_WAKE_ATTEMPTS and not measure_ready:
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
            # The author follow-up carries the climb's panel so a pushed code
            # change is RE-READ before the tick may arm it (followup.py). The
            # panel brings its own walltime, like the climb's allowance — the
            # contract's followup budget caps the author, not the gate. A
            # panel that would die at startup is left off: the reply still
            # goes out, the PR simply stays human-merged (the tick's contract
            # alarm already names the misconfig).
            panel_argv: list[str] = []
            panel_minutes = 0
            if not is_steward and spec.panel.strip():
                panel_error = _panel_preflight_error(spec)
                if panel_error:
                    log.warning(
                        "follow-up for %s runs without the panel: %s", record.run_id, panel_error
                    )
                else:
                    from outerloop.panel import panel_read_minutes

                    panel_argv = _climb_panel_argv(spec)
                    panel_minutes = panel_read_minutes(spec.panel)
            # the author's budget first, the read on top, both under the
            # partition cap — and the follow-up is told how many minutes the
            # read actually got (--panel-minutes), so a cap that eats the
            # allowance costs the READ (skipped, said so), never the author
            author_minutes = min(spec.time_minutes, spec.max_job_minutes)
            job_minutes = min(author_minutes + panel_minutes, spec.max_job_minutes)
            if panel_argv:
                panel_argv = [*panel_argv, "--panel-minutes", str(job_minutes - author_minutes)]
            argv = [
                *_interpreter(spec.home),
                "-m",
                "outerloop.followup",
                "--run-root",
                str(spec.run_root),
                "--run-id",
                record.run_id,
                *_containment(spec.image),
                "--bot-login",
                spec.bot_login,
                "--job-minutes",
                # the SAME clamped value Slurm gets: a deadline armed past
                # the real walltime is a Slurm kill before a clean ending
                str(job_minutes),
                "--max-turns",
                str(spec.max_turns),
                # the cluster coordinates the climb gets: a GPU benchmark's
                # re-measure is dispatched to the GPU lane, never run here
                "--account",
                spec.account,
                "--partition",
                spec.partition,
                "--gpu-partition",
                spec.gpu_partition,
                "--gpu-account",
                spec.gpu_account,
                *panel_argv,
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
                    time_minutes=job_minutes,
                    command=_flight_command(spec.home, f"followup-{record.run_id}"[:60], now, argv),
                    cpus=4,
                    mem="8G",
                )
            )
            # read-modify-write on the FRESH record: the submitted job may
            # already be saving its own fields
            latest = load_record(root, record.run_id)
            stage = latest.followup_stage
            if measure_ready and stage:
                # one finishing attempt billed against this landed measure
                stage = {**stage, "finish_attempts": finish_attempts + 1}
            save_record(
                root,
                replace(
                    latest,
                    followup_job_id=job_id,
                    followup_stage=stage,
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


def _holder_alive(compute: Compute, lease_job_id: str) -> bool | None:
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
    if holder_job and not local_mode():
        # An async wake job now owns the lease; it releases on completion,
        # and the TTL/holder-dead check reaps it if it dies.
        update_lease_holder(root, record.run_id, f"wake-job:{holder_job}", holder_job, now)
    else:
        # No job to hand the lease to — or a LOCAL dispatch, which ran the
        # whole wake synchronously: the attempt already finished and released
        # its own lease, and recreating one under a terminal job id would
        # make the next sweep reap a corpse instead of delivering.
        release_lease(root, record.run_id)
    return True


WAKE_SPEC_NAME = "wake-spec.json"


def dispatch_wake_armed(root: Path) -> bool:
    """The operator's on-switch for dispatched wakes: the env var, or the
    sentinel file (touch/rm, no chain restart). Read by the tick and by every
    park, so a disarm takes effect at once."""
    return (
        bool(os.environ.get("OUTERLOOP_DISPATCH_WAKE", "").strip())
        or (root / DISPATCH_WAKE_SENTINEL).exists()
    )


def write_wake_spec(root: Path, spec: FollowupSpec) -> None:
    """Publish the tick's wake recipe for the jobs that park runs: a park
    submits its own wake (`arm_wake`) with exactly the tick's settings, so
    dispatched wakes stay one recipe with one owner."""
    data = {k: (str(v) if isinstance(v, Path) else v) for k, v in asdict(spec).items()}
    tmp = root / f".{WAKE_SPEC_NAME}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data))
    os.replace(tmp, root / WAKE_SPEC_NAME)


def remove_wake_spec(root: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        (root / WAKE_SPEC_NAME).unlink()


def load_wake_spec(root: Path) -> FollowupSpec | None:
    """The published wake recipe, or None when dispatched wakes are not armed
    (or the file is unreadable — the sweep still delivers)."""
    try:
        data = json.loads((root / WAKE_SPEC_NAME).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    names = {f.name for f in FollowupSpec.__dataclass_fields__.values()}
    kwargs: dict[str, Any] = {
        k: (Path(v) if k in ("run_root", "home") else v) for k, v in data.items() if k in names
    }
    try:
        return FollowupSpec(**kwargs)
    except TypeError:
        return None


def arm_wake(
    root: Path, record: RunRecord, dispatcher: WakeDispatcher, now: float, *, holder_job_id: str
) -> str:
    """Submit a parked run's wake NOW, depending on the jobs it waits on, so
    it fires the moment they finish instead of a sweep cadence (plus grace)
    later. The same job and lease as a sweep-delivered wake: a wake job that
    parks again hands its own lease to the wake it arms; any other holder (a
    tick mid-delivery) keeps it and the sweep delivers as before. Arming is
    not a redelivery, so it leaves `wake_attempts` — the sweep's stuck
    counter — alone: an eval requeued by preemption fires the afterany
    early, the wake re-parks, and that must not count toward STUCK. Returns
    the wake job id, or "" when nothing was armed. A park with nothing to
    depend on (a checkpoint sleep, a blind park) is not armed: it rides the
    deadline floor, as before."""
    if not _poll_targets(record):
        return ""
    lease = read_lease(root, record.run_id)
    if lease is not None:
        if not holder_job_id or lease.holder_job_id != holder_job_id:
            return ""
    elif not acquire_lease(
        root, record.run_id, f"park:{holder_job_id or os.getpid()}", holder_job_id="", now=now
    ):
        return ""
    if record.deadline <= 0:  # a waiting record always carries a deadline
        record = replace(record, deadline=now)
        save_record(root, record, now)
    try:
        job = dispatcher.dispatch(record, "parked")
    except Exception as exc:
        log.warning("arming the wake failed for %s: %s: %s", record.run_id, type(exc).__name__, exc)
        job = ""
    if job:
        update_lease_holder(root, record.run_id, f"wake-job:{job}", job, now)
    elif lease is None:
        release_lease(root, record.run_id)
    return job


def _armed_wake_lost(
    root: Path,
    compute: Compute,
    record: RunRecord,
    lease: Lease,
    now: float,
    grace_s: float,
    dry_run: bool,
) -> bool:
    """An armed wake that is still PENDING on its dependency after every job
    it waits on has been terminal for a full grace window is not coming
    (Slurm reports the dependency as never satisfiable, or the afterany was
    lost). Cancel it so the sweep redelivers; the lease is then reaped.

    A wake the SITE moved off the partition it was submitted to may be
    starving there — on Torch a pending job can be shifted to a lower-tier
    partition (2026-09-02, wake 16787511 sat on `all` for hours) — but
    relocation alone is routine (jobs move to `cs` while still waiting on
    their dependencies and start on time). So a relocated holder counts as
    lost only once every job it waited on is terminal AND the grace window
    has run out without it starting; then it is cancelled and redelivered
    onto the requested partition."""
    if not lease.holder_job_id:
        return False
    job_ids = _poll_targets(record)
    if not job_ids:
        return False
    try:
        holder_state = compute.status(lease.holder_job_id)
        if not is_pending(holder_state):
            return False
        reason = compute.pending_reason(lease.holder_job_id)
        states = [compute.status(jid) for jid in job_ids]
    except SlurmQueryError:
        return False
    moved = _moved_off_partition(root, compute, lease.holder_job_id)
    if reason == "DependencyNeverSatisfied":
        pass
    elif not all(is_terminal(s) for s in states):
        return False  # its dependencies are still running: nothing to redeliver yet
    elif reason != "Dependency" and not moved:
        return False
    else:
        # Dependency-pending past its dependencies, or RELOCATED and eligible:
        # both get the grace window. Relocation alone is normal (the site
        # moves pending jobs routinely); a relocated wake counts as lost only
        # when every job it waited on is terminal and it still has not
        # started once the grace has run out — cancelling earlier would only
        # reset its queue age and burn a wake attempt.
        if moved:
            log.info(
                "armed wake %s for %s sits on partition %s (asked for %s) past its dependencies",
                lease.holder_job_id,
                record.run_id,
                moved[0],
                moved[1],
            )
        if record.terminal_seen <= 0:
            if not dry_run:
                save_record(root, replace(record, terminal_seen=now), now)
            return False
        if now - record.terminal_seen < grace_s:
            return False
    if dry_run:
        return True
    try:
        compute.cancel(lease.holder_job_id)
        # only a cancellation Slurm confirms lets the sweep redeliver: a
        # still-pending wake would otherwise run beside its replacement
        return not is_pending(compute.status(lease.holder_job_id))
    except Exception:
        return False


def sweep(
    root: Path,
    compute: Compute,
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
RESEARCH_LOG_MARKER = marker("research-log")
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
        state = ""
        with contextlib.suppress(OSError):
            state = marker.read_text()
        if state.startswith(("done", "adopted")) or not report_path.exists():
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
        if _publish_ledger_entry(github, spec.target, root, record, outcome, report, marker, state):
            published += 1
    return published


def _publish_ledger_entry(
    github: Any,
    target: str,
    root: Path,
    record: RunRecord,
    outcome: str,
    report: str,
    marker: Path,
    state: str,
) -> bool:
    """Staged publish whose marker doubles as a WRITABILITY PROBE: stages
    are "archived" -> "pointer-pending" -> "done", and no pointer is ever
    posted in a pass where a marker write is failing (terra #170 r4: a
    persistently unwritable marker must stall the publish, not stream a
    duplicate pointer every tick). A pointer failure retries pointer-only;
    a marker lost after full success re-posts at most once; the residual
    crash-between-probe-and-post window costs at most one duplicate per
    incident, never an unbounded stream."""
    from datetime import UTC, datetime

    date = datetime.fromtimestamp(record.updated or record.created, tz=UTC).strftime("%Y-%m-%d")
    path = f"reports/{date}-{record.run_id}.md"
    # an earlier pass may have archived under an earlier date (an in-review
    # archive whose record re-stamped `updated` at ENDED): the marker's own
    # second line is the authoritative path for retries and pointers
    prior = state.splitlines()
    if len(prior) > 1 and prior[1].startswith("reports/") and prior[1].endswith(".md"):
        path = prior[1]

    def _mark(value: str) -> bool:
        try:
            # the path rides the marker so readers (the board) never have to
            # re-derive the date from a timestamp that may have moved on
            marker.write_text(value + "\n" + path)
            return True
        except OSError as exc:
            log.warning("ledger marker write failed for %s: %s", record.run_id, exc)
            return False

    try:
        if not state.startswith(("archived", "pointer-pending")):
            if not github.ensure_branch(target, RESEARCH_LOG_BRANCH):
                return False
            if not github.put_file(
                target,
                path,
                report,
                RESEARCH_LOG_BRANCH,
                f"research log: {record.run_id} ({outcome})",
            ):
                return False  # retry the whole publish next tick
            if not _mark("archived"):
                return False  # unwritable state: stall BEFORE any pointer
        # the probe: a fresh successful write is the license to post
        if not _mark("pointer-pending"):
            return False
        url = f"https://github.com/{target}/blob/{RESEARCH_LOG_BRANCH}/{path}"
        line = f"**{outcome}** `{record.benchmark}` — [report]({url})"
        if record.pr_url:
            line += f" · {record.pr_url}"
        if record.issue_number:
            _mark("done")  # the claimed issue already received the full finish
            return True
        bench = record.benchmark.casefold()
        posted = False
        if bench:
            for issue in github.list_open_issues(target):
                text = f"{issue.get('title', '')}\n{issue.get('body') or ''}"
                if has_marker(text, "research-log") or issue.get("pull_request"):
                    continue
                if bench in text.casefold():
                    github.comment(target, int(issue["number"]), line)
                    posted = True
                    break
        if not posted:
            cache = _ledger_issue_cache(root, target)
            log_issue = 0
            with contextlib.suppress(OSError, ValueError):
                log_issue = int(cache.read_text().strip())
            if not log_issue:
                # cache miss (first use, or a lost/failed cache write): find
                # the rolling issue by its marker BEFORE creating another —
                # the cache is a fast path, never the source of truth
                # (terra #170 r5: a failed cache write must not duplicate
                # the rolling issue)
                for issue in github.list_open_issues(target):
                    if has_marker(str(issue.get("body") or ""), "research-log"):
                        log_issue = int(issue.get("number", 0))
                        break
            if not log_issue:
                log_issue = github.create_issue(
                    target,
                    "Research log",
                    f"{RESEARCH_LOG_MARKER}\nOne two-line comment per finished "
                    f"autoresearch run — full reports live on the [`{RESEARCH_LOG_BRANCH}`]"
                    f"(https://github.com/{target}/tree/{RESEARCH_LOG_BRANCH}/reports) "
                    "branch. Results relevant to an open order issue are posted "
                    "there instead.",
                )
            if log_issue:
                with contextlib.suppress(OSError):
                    cache.write_text(str(log_issue))
                try:
                    github.comment(target, log_issue, line)
                except Exception:
                    # a stale cached number (locked/deleted/transferred issue)
                    # must not stall delivery forever: drop the cache so the
                    # next pass re-discovers or re-creates (terra #170 r5)
                    with contextlib.suppress(OSError):
                        cache.unlink()
                    raise
        _mark("done")  # write just proved out via the probe; failure = freak
        return True
    except Exception as exc:  # advisory ledger: never fail the tick
        log.warning("research-log publish failed for %s: %s", record.run_id, exc)
        return False


def _kill_stamp(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "attempt-terminal-seen"


def _sweep_implementing(root: Path, compute: Compute, now: float, grace_s: float) -> list[str]:
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


def _moved_off_partition(root: Path, compute: Compute, job_id: str) -> tuple[str, str] | None:
    """(actual, wanted) when a queued kernel job no longer sits in the
    partition the wake recipe asks for, else None. Unknown either way —
    no recipe, a compute without partitions, a failed query — is None:
    never cancel on doubt."""
    spec = load_wake_spec(root)
    if spec is None:
        return None
    wanted = spec.job_partition or spec.partition
    if not wanted:
        return None
    try:
        actual = compute.job_partition(job_id)
    except (SlurmQueryError, ValueError, AttributeError):
        return None
    # both sides may be comma-separated lists: moved only when the job holds
    # NONE of the partitions the recipe asked for; empty is unknown, not moved
    have = {p.strip() for p in actual.split(",") if p.strip()}
    want = {p.strip() for p in wanted.split(",") if p.strip()}
    if not have or have & want:
        return None
    return actual, wanted


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
    compute: Compute,
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
                if not _armed_wake_lost(root, compute, record, lease, now, grace_s, dry_run):
                    return
                record = load_record(root, record.run_id)
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
            # Local compute has no afterany jobs to wait for (jobs are
            # terminal at submit): the sweep IS the delivery, so grace would
            # only cost a whole extra loop iteration.
            if local_mode():
                wake(record, f"experiment {state}", state)
                return
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
            # Past the deadline with a job still queued. Ask Slurm WHY before
            # calling it unschedulable: a busy queue or the account's own cap is
            # a wait a scientist would sit out, so the deadline moves out by one
            # slack window instead (Torch 2026-09-06: four launches pending on
            # QOSMaxGRESPerUser were about to be cancelled and re-launched into
            # the same cap). Local compute has no queue and no reasons.
            pending = [j for j, s in zip(job_ids, states, strict=True) if is_pending(s)]
            reason_of = getattr(compute, "pending_reason", None)
            reasons: dict[str, str] = {}
            if reason_of is not None:
                try:
                    reasons = {j: str(reason_of(j)) for j in pending}
                except SlurmQueryError:
                    deferred.append(record.run_id)  # unknown is never "cancel"
                    return
            if reasons and all(is_queue_wait(r) for r in reasons.values()):
                extended = now + BLIND_PARK_SLACK_MIN * 60
                if not dry_run:
                    save_record(root, replace(record, deadline=extended), now)
                log.info(
                    "sweep: %s waits in the queue (%s); deadline extended by %d min",
                    record.run_id,
                    ", ".join(f"{j}={r}" for j, r in reasons.items()),
                    BLIND_PARK_SLACK_MIN,
                )
                return
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
    compute: Compute,
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
    # Housekeeping: ended runs shed ws/ and ws-home/ after a grace period;
    # when the state filesystem's write probe failed, the grace is waived and
    # the sweep frees oldest-first until the probe passes, then the preflight
    # is taken again so launch lanes can come back this very tick.
    # Force-shed (waive the grace) only when the state root cannot be WRITTEN,
    # not merely when it is below the free-space threshold: a writable disk
    # that is just low keeps the 24 h grace so a post-mortem is not deleted
    # under someone. A dry-run tick sheds nothing (the destructive lane obeys
    # the zero-writes contract, like sweep()).
    shed: list[str] = []
    if not dry_run:
        cannot_write = not disk_health.state_root.writable
        # Bounded per tick so shedding (rm -rf of tens of thousands of files
        # per workspace on a networked FS) never blows the tick's own timeout:
        # a few runs on a healthy disk, more but still time-boxed when it is
        # failing. The backlog drains over several ticks.
        shed = shed_ended_workspaces(
            root,
            now,
            force=cannot_write,
            limit=25 if cannot_write else 3,
            time_budget_s=300.0 if cannot_write else 120.0,
            until_ok=(lambda: check_disk(root, min_free_bytes=min_free_bytes).state_root.writable)
            if cannot_write
            else None,
        )
        if shed and cannot_write:
            disk_health = check_disk(root, min_free_bytes=min_free_bytes)
            write_heartbeat(root, now, disk=disk_health.as_dict())
    if shed:
        from dataclasses import replace as _dc_replace

        report = _dc_replace(report, shed=tuple(shed))
    launch_ok = disk_health.launch_ok()
    if not launch_ok:
        log.warning("disk preflight failed; launch lanes are OFF this tick")
    # Mid-leg sync is serviced regardless of follow-up/board servicing: it
    # only needs the workspace and the PAT (a git fetch, no GitHub REST and
    # no contract), and a live session waiting on `sync` must not depend on
    # whether github/contract loaded this tick.
    if followup_spec is not None:
        service_syncs(root, followup_spec, now)
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
                from outerloop.contract import load_contract

                raw = _contract_text(github, followup_spec.target, "main")
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
            contract=contract,
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
        # AFTER the launch block: a run started this tick is on the strip
        # this tick, not the next one
        service_boards(root, github, spec.target, contract, now, compute)
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


def service_syncs(root: Path, spec: Any, now: float) -> None:
    """Honor mid-leg sync requests: a LIVE session asked for fresh origin/*
    refs and is waiting inside its own clock. The fetch pins the canonical
    URL (never the workspace's mutable remote config) and only refs/remotes
    are written — safe next to the session's local git use. Best-effort per
    run; a failure leaves the request standing for the next cycle."""
    from outerloop.appauth import resolve_bot_auth
    from outerloop.attempt import _target_clone_url
    from outerloop.github import Workspace
    from outerloop.syscall import mark_synced, sync_requested

    for record in list_runs(root):
        if record.state != IMPLEMENTING:
            continue
        workspace = run_dir(root, record.run_id) / "ws"
        if not workspace.is_dir():
            continue
        requested_at = sync_requested(workspace)
        if requested_at is None:
            continue
        try:
            ws = Workspace(
                root=workspace,
                auth=(
                    resolve_bot_auth(spec.pat_file, spec.github_app_file)
                    if (spec.pat_file or spec.github_app_file)
                    else None
                ),
                url=_target_clone_url(record.target),
            )
            ws.fetch_origin()
            mark_synced(workspace, requested_at)
            log.info("synced origin refs for %s", record.run_id)
        except Exception as exc:
            log.warning("sync failed for %s: %s", record.run_id, exc)


def service_boards(
    root: Path, github: Any, target: str, contract: Any, now: float, compute: Any = None
) -> None:
    """The climb board and the live strip, together and advisory: the views
    publish from the first tick (before any run ends), and a failure never
    stops the tick. With a compute backend, the strip also carries the
    kernel's queue (its own Slurm jobs, attributed to agents)."""
    try:
        from outerloop.climbboard import contract_directions, service_climb_board

        service_climb_board(root, github, target, contract_directions(contract))
    except Exception as exc:
        log.warning("climb board service failed: %s", exc)
    try:
        from outerloop.climbboard import service_status

        queue = None
        if compute is not None:
            try:
                queue = compute.queue_snapshot()
            except Exception as exc:  # blind this tick: the strip publishes without a queue
                log.warning("queue snapshot failed: %s", exc)
        service_status(root, github, target, now, contract, queue=queue)
    except Exception as exc:  # each is advisory ALONE: one failing never mutes the other
        log.warning("status strip service failed: %s", exc)


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
# the crash-loop floor: a launch that died pre-record backs off at least
# this long regardless of the contract's cooldown dial
DEAD_LAUNCH_BACKOFF_S = 30 * 60
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
    dead_attempts: dict[str, float] | None = None,
    live_pendings: list[tuple[str, float]] | None = None,
) -> str | None:
    """The benchmark to climb next on `target`, or None.

    Deliberately boring (the planning agent upgrades this later): serialize
    to one active run per target, respect the contract's weekly budget and a
    per-benchmark cooldown, then choose the benchmark least recently
    attempted — untouched ones first. Only this target's runs count toward
    any of it. `dead_attempts` maps benchmark -> submitted_at for launches
    that died BEFORE writing a run record (per-benchmark tombstones) — each
    counts toward cooldown with a crash-loop floor, so alternating
    pre-record failures can't ping-pong every tick (terra #172 r2/r3).
    """
    mine = [r for r in records if r.target == target]

    def stranded(r: RunRecord) -> bool:
        return r.state == IMPLEMENTING and now - max(r.updated, r.created) > STRANDED_IMPLEMENTING_S

    active = [r for r in mine if r.state != ENDED and not stranded(r)]
    if len(active) >= _attempt_width(contract):
        return None
    week_ago = now - 7 * 24 * 3600
    # queued slots count toward the weekly budget BEFORE their records
    # exist, or a width-N target with one run left could submit N (terra
    # #173 r2); when a marker lands, the service clears it before calling
    # here, so a run is never counted twice
    queued = sum(1 for _, submitted_at in live_pendings or [] if submitted_at >= week_ago)
    if sum(1 for r in mine if r.created >= week_ago) + queued >= contract.budgets.runs_per_week:
        return None
    last_attempt: dict[str, float] = {}
    for r in mine:
        if r.benchmark:
            last_attempt[r.benchmark] = max(last_attempt.get(r.benchmark, 0.0), r.created)
    for bench_name, submitted_at in (dead_attempts or {}).items():
        last_attempt[bench_name] = max(last_attempt.get(bench_name, 0.0), submitted_at)
    for bench_name, submitted_at in live_pendings or []:
        # a queued sibling starts its benchmark's cooldown clock too:
        # width spreads across benchmarks first, and re-picking the same
        # one needs the contract to have set cooldown to 0 (portfolio)
        if bench_name:
            last_attempt[bench_name] = max(last_attempt.get(bench_name, 0.0), submitted_at)
    cooldown_min = getattr(contract.budgets, "attempt_cooldown_minutes", None)
    cooldown_s = SELF_INITIATED_COOLDOWN_S if cooldown_min is None else cooldown_min * 60
    # A launch that died BEFORE writing a run record is invisible to the
    # runs_per_week cap, so its cooldown attribution is the ONLY crash-loop
    # guard — it keeps a floor even when the contract dials cooldown to 0
    # (terra #172: zero cooldown otherwise resubmits a crashing launch
    # every tick, uncapped).
    dead_benches = set(dead_attempts or ())
    candidates = sorted(
        contract.benchmarks,
        key=lambda b: (last_attempt.get(b.name, 0.0), b.name),
    )
    for bench in candidates:
        floor_s = cooldown_s
        if bench.name in dead_benches:
            floor_s = max(cooldown_s, DEAD_LAUNCH_BACKOFF_S)
        if now - last_attempt.get(bench.name, 0.0) >= floor_s:
            return str(bench.name)
    return None


def _tombstone_path(root: Path, target: str, benchmark: str) -> Path:
    safe = f"{target.replace('/', '__')}__{benchmark}"
    return root / "pending-dead" / (safe + ".json")


def write_tombstone(root: Path, target: str, benchmark: str, submitted_at: float) -> None:
    """Per-benchmark crash memory: a launch died before writing a record, so
    nothing else (runs_per_week, cooldown-by-records) can see it. The
    tombstone persists independently of the live pending marker — a second
    benchmark's launch must not erase it (terra #172 r3)."""
    path = _tombstone_path(root, target, benchmark)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"submitted_at": submitted_at}))
    except OSError as exc:
        log.warning("tombstone write failed for %s/%s: %s", target, benchmark, exc)


def read_tombstones(root: Path, target: str, contract: Any, now: float) -> dict[str, float]:
    """benchmark -> submitted_at for unserved crash tombstones; entries past
    their window (the larger of the crash floor and the contract cooldown)
    are pruned on read."""
    cooldown_min = getattr(contract.budgets, "attempt_cooldown_minutes", None)
    cooldown_s = SELF_INITIATED_COOLDOWN_S if cooldown_min is None else cooldown_min * 60
    window = max(DEAD_LAUNCH_BACKOFF_S, cooldown_s)
    out: dict[str, float] = {}
    prefix = target.replace("/", "__") + "__"
    dead_dir = root / "pending-dead"
    if not dead_dir.is_dir():
        return out
    for path in dead_dir.glob(prefix + "*.json"):
        bench = path.stem[len(prefix) :]
        try:
            submitted_at = float(json.loads(path.read_text())["submitted_at"])
        except (OSError, ValueError, KeyError, TypeError):
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        if now - submitted_at > window:
            with contextlib.suppress(OSError):
                path.unlink()  # backoff served
            continue
        out[bench] = submitted_at
    return out


# WIDTH slots are the only suffixed marker names; the pattern also fences
# list_pendings against a longer target that shares this one's file-name
# prefix (org/foo vs org/foobar — "/" encodes as "__", so glob alone is
# ambiguous)
_SLOT_AGENT_RE = re.compile(r"agent-\d+")


def _pending_path(root: Path, target: str, agent: str = "") -> Path:
    # agent "" is the legacy single-slot name, still read for back-compat
    # with a marker written before the width dial deployed. The slot
    # separator is "@" because it CANNOT appear in a GitHub owner/repo
    # name — any character legal in repo names ("_", ".", "-") would make
    # org/pilot's slot file collide with some other target's legacy file
    # (org/pilot__agent-01 is a valid repo).
    suffix = f"@{agent}" if agent else ""
    return root / "pending" / (target.replace("/", "__") + suffix + ".json")


def read_pending(root: Path, target: str, agent: str = "") -> dict[str, Any] | None:
    """The submit-time marker for a climb whose run record may not exist yet."""
    path = _pending_path(root, target, agent)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "submitted_at" in data else None


def list_pendings(root: Path, target: str) -> list[tuple[str, dict[str, Any]]]:
    """(agent, marker) for every live pending marker of `target` — one per
    WIDTH slot, plus the legacy un-suffixed marker from a pre-width deploy
    (attributed to agent-01)."""
    out: list[tuple[str, dict[str, Any]]] = []
    stem = target.replace("/", "__")
    pending_dir = root / "pending"
    if not pending_dir.is_dir():
        return out
    for path in sorted(pending_dir.glob(stem + "*.json")):
        name = path.stem
        if name == stem:
            agent = ""
        elif name.startswith(stem + "@") and _SLOT_AGENT_RE.fullmatch(name[len(stem) + 1 :]):
            agent = name[len(stem) + 1 :]
        else:
            continue  # a longer target sharing this prefix (org/foo vs org/foobar)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and "submitted_at" in data:
            out.append((agent or str(data.get("agent_id") or "agent-01"), data))
    return out


def write_pending(
    root: Path, target: str, benchmark: str, job_id: str, now: float, agent: str = ""
) -> None:
    path = _pending_path(root, target, agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {"benchmark": benchmark, "job_id": job_id, "submitted_at": now, "agent_id": agent}
        )
    )
    os.replace(tmp, path)


def clear_pending(root: Path, target: str, agent: str = "") -> None:
    _pending_path(root, target, agent).unlink(missing_ok=True)


def _attempt_width(contract: Any) -> int:
    width = getattr(getattr(contract, "budgets", None), "max_active_attempts", None)
    return int(width) if width else MAX_ACTIVE_RUNS_PER_TARGET


def _free_agent_slot(occupied: set[str], width: int) -> str | None:
    for i in range(1, width + 1):
        agent = f"agent-{i:02d}"
        if agent not in occupied:
            return agent
    return None


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
    (e.g. OUTERLOOP_AUTHOR_BACKEND=codex with no non-claude model) never
    strands a claimed intake issue. Reads the fleet author config from env — the
    same source the climb defaults from — and the image the tick already knows."""
    from outerloop.attempt import codex_author_config_error

    backend = os.environ.get("OUTERLOOP_AUTHOR_BACKEND") or "claude"
    model = os.environ.get("OUTERLOOP_AUTHOR_MODEL") or "claude-opus-5"
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
        from outerloop.attempt import PANEL_KEY_DEFAULT, resolve_author_key_file
        from outerloop.github import FileTokenProvider
        from outerloop.panel import parse_lenses

        try:
            lenses = parse_lenses(spec.panel)
        except ValueError as exc:
            return str(exc)
        # non-claude (shelled) lenses: mirror the climb's rules exactly, per
        # backend — image required, the judge's OWN key (set + absolute +
        # neither the author's nor the claude panel key + readable), and for
        # hermes its pinned clone.
        shelled = {
            "codex": "OUTERLOOP_PANEL_CODEX_KEY_FILE",
            "hermes": "OUTERLOOP_PANEL_HERMES_KEY_FILE",
        }
        for lens_backend, key_env in shelled.items():
            if not any(backend == lens_backend for _, backend, _ in lenses):
                continue
            if not spec.image or not Path(spec.image).is_file():
                return (
                    f"a {lens_backend} panel lens requires a real container image "
                    f"(OUTERLOOP_IMAGE={spec.image!r})"
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
                from outerloop.role_runner import _HERMES_PROVIDERS

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
        fleet_backend = os.environ.get("OUTERLOOP_AUTHOR_BACKEND") or "claude"
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
        from outerloop.role_runner import role_key

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
    from outerloop.limits import ATTEMPT_OVERHEAD_MINUTES

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
    from outerloop.roles import reviewer_spec, verifier_spec

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
    from outerloop.limits import ATTEMPT_OVERHEAD_MINUTES, SESSION_MINUTES_FLOOR

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
    compute: Compute,
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
        width = _attempt_width(contract)
        # WIDTH: every live pending marker occupies a slot; landed ones
        # clear; dead ones become per-benchmark tombstones and free theirs.
        occupied: set[str] = set()
        live_pendings: list[tuple[str, float]] = []
        nonslot_busy = False
        for agent, pending in list_pendings(root, spec.target):
            marker_agent = "" if not pending.get("agent_id") else agent
            submitted_at = float(pending["submitted_at"])
            # A slotted marker lands only when ITS OWN record appears —
            # matching on target+time alone would let a sibling slot's
            # record clear a still-live marker (terra #173). A legacy
            # marker names no slot, so it keeps the lax match.
            landed = any(
                r.target == spec.target
                and r.created >= submitted_at - 60
                and (not marker_agent or r.agent_id == marker_agent)
                for r in records
            )
            expired = now - submitted_at > PENDING_TTL_S
            if landed:
                clear_pending(root, spec.target, marker_agent)
            elif (alive := _holder_alive(compute, str(pending.get("job_id", "")))) is True or (
                not expired and alive is not False
            ):
                # climb queued or starting; its record isn't written yet. A
                # provably-alive job holds its SLOT regardless of the
                # marker's TTL — queue wait can exceed it — the TTL only
                # breaks ties when Slurm can't say.
                occupied.add(agent)
                live_pendings.append((str(pending.get("benchmark", "")), submitted_at))
                if not marker_agent:
                    # a live un-slotted marker is another lane's submit
                    # (steward/intake, or a pre-width deploy): serial
                    nonslot_busy = True
            else:
                # Died before writing a record: persist the crash memory as a
                # PER-BENCHMARK tombstone (a sibling launch must not erase
                # this — terra #172 r3), then free the slot.
                write_tombstone(root, spec.target, str(pending.get("benchmark", "")), submitted_at)
                clear_pending(root, spec.target, marker_agent)
        stranded_cutoff = now - STRANDED_IMPLEMENTING_S
        for r in records:
            if r.target == spec.target and r.state != ENDED:
                if r.state == IMPLEMENTING and max(r.updated, r.created) <= stranded_cutoff:
                    continue  # stranded: pick ignores it, so must occupancy
                occupied.add(r.agent_id)
                if not _SLOT_AGENT_RE.fullmatch(r.agent_id):
                    nonslot_busy = True
        if nonslot_busy:
            # steward and intake keep their pre-width one-run-per-target
            # exclusivity: width applies AMONG self-initiated slots, it
            # does not license launching beside another lane (terra #173)
            return None
        if len(occupied) >= width:
            return None
        slot_agent = _free_agent_slot(occupied, width)
        if slot_agent is None:
            return None
        dead_attempts = read_tombstones(root, spec.target, contract, now)
        benchmark = pick_self_initiated(
            records, contract, spec.target, now, dead_attempts, live_pendings
        )
        if benchmark is None:
            return None
        if getattr(contract, "merge", "manual") == "auto" and not spec.panel:
            # auto merge mode means gate+PANEL clean self-merges; a
            # deployment with no panel configured must not launch attempts
            # that would publish panel-less self-merging PRs (terra #171)
            log.error(
                "attempt on %s not launched: contract sets merge:auto but "
                "no panel is configured (set OUTERLOOP_PANEL, or the "
                "contract back to merge:manual)",
                benchmark,
            )
            return None
        if lane_error := _gpu_lane_error(contract, benchmark, spec):
            log.error("attempt on %s not launched: %s", benchmark, lane_error)
            return None
        author_error = _author_config_error(spec)
        if author_error:
            log.error(
                "climb on %s not launched: author misconfigured — %s "
                "(fix OUTERLOOP_AUTHOR_BACKEND/_MODEL)",
                benchmark,
                author_error,
            )
            return None
        panel_error = _panel_preflight_error(spec)
        if panel_error:
            log.error(
                "climb on %s not launched: panel misconfigured — %s "
                "(fix it, or set OUTERLOOP_PANEL='' to disable the panel)",
                benchmark,
                panel_error,
            )
            return None
        if dry_run:
            return (benchmark, "dry-run")
        job_minutes = _attempt_job_minutes(spec, limits)
        argv = [
            *_interpreter(spec.home),
            "-m",
            "outerloop.attempt",
            "--target",
            spec.target,
            "--benchmark",
            benchmark,
            "--run-root",
            str(spec.run_root),
            *_containment(spec.image),
            "--agent-id",
            slot_agent,
            *_climb_limit_argv(limits, job_minutes),
            *_climb_panel_argv(spec),
        ]
        if spec.pat_file:
            argv += ["--pat-file", spec.pat_file]
        # config-driven author: climb resolves the author backend/model/key from
        # OUTERLOOP_AUTHOR_* env (inherited by the job), so the tick threads
        # neither the backend nor its key — a new backend needs zero tick change.
        job_id = compute.submit(
            JobSpec(
                job_name=f"climb-{benchmark}-{slot_agent}"[:60],
                account=spec.account,
                partition=spec.job_partition or spec.partition,
                time_minutes=job_minutes,
                command=_flight_command(
                    spec.home, f"climb-{benchmark}-{slot_agent}"[:60], now, argv
                ),
                cpus=4,
                mem="8G",
            )
        )
        write_pending(root, spec.target, benchmark, job_id, now, agent=slot_agent)
        log.info("self-initiated climb on %s: job %s", benchmark, job_id)
        return (benchmark, job_id)
    except Exception as exc:  # one bad pass must not break the tick
        log.warning("self-initiated pass failed: %s", exc)
        return None


def service_steward(
    root: Path,
    github: Any,
    compute: Compute,
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
    from outerloop.steward import pick_steward_issue

    target = spec.target
    if not target or not spec.steward_key_file:
        return None
    if getattr(contract, "steward", None) is None:
        return None
    try:
        from outerloop.steward import release_orphaned_claims

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
        # the SAME per-target pending markers the self-initiated lane uses
        # — ALL of them, slotted included: a width slot queued without a
        # record yet must block a stewardship the same way an active run
        # does. Liveness first, TTL only breaks unknown ties (queue wait
        # can outlive the TTL).
        for slot, pending in list_pendings(root, target):
            marker_agent = "" if not pending.get("agent_id") else slot
            submitted_at = float(pending.get("submitted_at", 0.0))
            landed = any(
                r.target == target
                and r.created >= submitted_at - 60
                and (not marker_agent or r.agent_id == marker_agent)
                for r in records
            )
            expired = now - submitted_at > PENDING_TTL_S
            alive = _holder_alive(compute, str(pending.get("job_id", "")))
            if not landed and (alive is True or (not expired and alive is not False)):
                return None
        task = pick_steward_issue(github, target, contract, spec.bot_login)
        if task is None:
            return None
        if _benchmark_gpus(contract, task.benchmark) > 0:
            # the stewardship validates its rewrite IN-JOB (SubprocessEvaluator
            # inside the CPU work job — no GPUs, no --nv), so a GPU benchmark
            # cannot be stewarded yet; refuse rather than launch a validation
            # that can only fail (terra #174 r2)
            log.error(
                "stewardship on %s not launched: GPU benchmarks validate in-job "
                "and the steward job has no GPU allocation",
                task.benchmark,
            )
            return None
        if dry_run:
            return (f"steward-issue-{task.number}", "dry-run")
        from outerloop.intake import CLAIM_MARKER, issue_hypothesis

        github.comment(
            target,
            task.number,
            f"{CLAIM_MARKER}\nClaimed by the steward for benchmark "
            f"`{task.benchmark}`; a run is queued and a report will follow here.",
        )
        import base64 as _b64

        work_order_b64 = _b64.b64encode(issue_hypothesis(task).encode()).decode()
        argv = [
            *_interpreter(spec.home),
            "-m",
            "outerloop.steward",
            "--target",
            target,
            "--benchmark",
            task.benchmark,
            "--run-root",
            str(spec.run_root),
            *_containment(spec.image),
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
            from outerloop.steward import RELEASE_MARKER

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
    compute: Compute,
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
    from outerloop.contract import load_contract
    from outerloop.intake import issue_hypothesis, pick_issue

    target = spec.target
    if not target:
        return None
    paused = outage_active(root, now, role="solver")
    if paused:
        log.info("intake lane paused (api outage: %s)", paused)
        return None
    try:
        if contract is None:
            contract_raw = _contract_text(github, target, "main")
            if contract_raw is None:
                return None
            contract = load_contract(contract_raw, target)
        limits = limits if limits is not None else effective_limits(contract.budgets)
        task = pick_issue(github, target, contract, spec.bot_login)
        if task is None:
            return None
        if getattr(contract, "merge", "manual") == "auto" and not spec.panel:
            # auto merge mode means gate+PANEL clean self-merges; a
            # deployment with no panel configured must not launch attempts
            # that would publish panel-less self-merging PRs (terra #171)
            log.error(
                "attempt on %s not launched: contract sets merge:auto but "
                "no panel is configured (set OUTERLOOP_PANEL, or the "
                "contract back to merge:manual)",
                task.benchmark,
            )
            return None
        if lane_error := _gpu_lane_error(contract, task.benchmark, spec):
            log.error("attempt on %s not launched: %s", task.benchmark, lane_error)
            return None
        author_error = _author_config_error(spec)
        if author_error:
            log.error(
                "issue #%d not claimed: author misconfigured — %s "
                "(fix OUTERLOOP_AUTHOR_BACKEND/_MODEL)",
                task.number,
                author_error,
            )
            return None
        panel_error = _panel_preflight_error(spec)
        if panel_error:
            log.error(
                "issue #%d not claimed: panel misconfigured — %s "
                "(fix it, or set OUTERLOOP_PANEL='' to disable the panel)",
                task.number,
                panel_error,
            )
            return None
        if dry_run:
            return (f"issue-{task.number}", "dry-run")
        job_minutes = _attempt_job_minutes(spec, limits)
        # claim BEFORE submit: Slurm queueing can take minutes, and the next
        # tick must not re-claim the same issue in that window
        from outerloop.intake import CLAIM_MARKER, MAX_INTAKE_ATTEMPTS, RELEASE_MARKER

        github.comment(
            target,
            task.number,
            f"{CLAIM_MARKER}\nClaimed for benchmark `{task.benchmark}`; a run "
            "is queued and a report will follow here.",
        )
        import base64 as _b64

        hypothesis_b64 = _b64.b64encode(issue_hypothesis(task).encode()).decode()
        argv = [
            *_interpreter(spec.home),
            "-m",
            "outerloop.attempt",
            "--target",
            target,
            "--benchmark",
            task.benchmark,
            "--run-root",
            str(spec.run_root),
            *_containment(spec.image),
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
        # OUTERLOOP_AUTHOR_* env by backend; the tick does not thread it.
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

    compute: Compute
    spec: FollowupSpec
    now: float
    wake_minutes: int = 20

    def dispatch(self, record: RunRecord, reason: str) -> str:
        argv = [
            *_interpreter(self.spec.home),
            "-m",
            "outerloop.attempt",
            "--resume",
            record.run_id,
            "--run-root",
            str(self.spec.run_root),
            *_containment(self.spec.image),
            "--account",
            self.spec.account,
            "--partition",
            self.spec.partition,
            "--gpu-partition",
            self.spec.gpu_partition,
            "--gpu-account",
            self.spec.gpu_account,
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
        from outerloop.limits import ATTEMPT_OVERHEAD_MINUTES
        from outerloop.roles import author_spec

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
    from outerloop.panel import panel_read_minutes
    from outerloop.roles import author_spec

    read_minutes = panel_read_minutes(spec.panel)
    if not read_minutes:
        return 0
    return read_minutes + author_spec().budget.walltime_s // 60


def _wake_dispatcher_from_env(
    compute: Compute, followup_spec: FollowupSpec | None, now: float, root: Path
) -> tuple[WakeDispatcher, bool]:
    """The wake delivery for this tick, behind an EXPLICIT on-switch so the
    dispatched-wake path lands DARK. Returns `(dispatcher, live)`:

    * armed (the `OUTERLOOP_DISPATCH_WAKE` env var OR a `<root>/DISPATCH_WAKE`
      sentinel file) AND the chain env carries what a wake job needs -> the real
      `JobWakeDispatcher` and a LIVE sweep;
    * otherwise -> the `LoggingDispatcher` and a DRY sweep.

    The sentinel mirrors PAUSE: an operator arms/disarms with a touch/rm, no
    chain restart. So dispatched climbing is turned on deliberately, and a
    half-configured environment fails safe to dry rather than to a wake job
    that cannot run."""
    if not dispatch_wake_armed(root):
        return LoggingDispatcher(), False
    if followup_spec is None:
        log.warning("dispatch-wake armed but the chain env is incomplete; wake stays dry")
        return LoggingDispatcher(), False
    log.info("dispatched-wake ON: the waiting-run sweep delivers real wakes this tick")
    return JobWakeDispatcher(compute, followup_spec, now), True


def _max_job_minutes_from_env() -> int:
    """OUTERLOOP_MAX_JOB_MINUTES, clamped into what the code can honor:
    at least the climb-job floor (an operator on a short-MaxTime partition
    must be able to LOWER the cap below cpu_short's 6h, or every submit is
    rejected), at most the ceiling the stranded window allows. A clamped
    value logs — a silently-changed cap would read as the partition
    rejecting jobs for no reason."""
    from outerloop.limits import ATTEMPT_JOB_MINUTES_FLOOR

    raw = os.environ.get("OUTERLOOP_MAX_JOB_MINUTES", "").strip()
    if not raw:
        return MAX_ATTEMPT_JOB_MINUTES
    try:
        value = int(raw)
    except ValueError:
        log.warning("OUTERLOOP_MAX_JOB_MINUTES=%r is not an integer; using default", raw)
        return MAX_ATTEMPT_JOB_MINUTES
    clamped = max(ATTEMPT_JOB_MINUTES_FLOOR, min(value, MAX_JOB_MINUTES_CEILING))
    if clamped != value:
        log.warning("OUTERLOOP_MAX_JOB_MINUTES=%d clamped to %d", value, clamped)
    return clamped


def _cadence_s() -> float:
    """The chain's tick cadence in seconds (OUTERLOOP_CADENCE_MIN, the same
    knob tick_chain.sbatch uses), defaulting to 30 min when unset/invalid."""
    raw = os.environ.get("OUTERLOOP_CADENCE_MIN", "").strip()
    try:
        cadence_s = float(raw) * 60 if raw else 30 * 60
    except ValueError:
        cadence_s = 30 * 60
    return cadence_s if (math.isfinite(cadence_s) and cadence_s > 0) else 30 * 60


def _coalesce_ceiling_s() -> float:
    """The largest SAFE coalesce window, bounding both the default and an
    explicit OUTERLOOP_MIN_TICK_MINUTES: half the cadence (so an on-cadence
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
    """OUTERLOOP_MIN_TICK_MINUTES -> the coalesce window in seconds. Unset
    derives a cadence-aware default; non-numeric/non-finite also fall back to it;
    negative clamps to 0 (coalesce disabled); a value at/above the safe ceiling
    (half the cadence, capped at MAX_MIN_TICK_S) clamps down so it cannot stall
    the loop."""
    raw = os.environ.get("OUTERLOOP_MIN_TICK_MINUTES", "").strip()
    if not raw:
        return _default_min_tick_s()
    try:
        minutes = float(raw)
    except ValueError:
        log.warning("OUTERLOOP_MIN_TICK_MINUTES=%r is not a number; using default", raw)
        return _default_min_tick_s()
    # reject inf/nan: an infinite window would coalesce every future tick and
    # freeze the loop (a finite elapsed time is always < inf)
    if not math.isfinite(minutes):
        log.warning("OUTERLOOP_MIN_TICK_MINUTES=%r is not finite; using default", raw)
        return _default_min_tick_s()
    seconds = max(0.0, minutes * 60)
    ceiling = _coalesce_ceiling_s()
    if seconds > ceiling:
        log.warning(
            "OUTERLOOP_MIN_TICK_MINUTES=%s exceeds the safe ceiling "
            "(%.0f min, ~half the cadence); clamping so normal ticks are not coalesced",
            raw,
            ceiling / 60,
        )
        return ceiling
    return seconds


def _default_image() -> str:
    """~/outerloop-images/agent-py312.sif, or the pre-rename ~/autoresearch-images
    path when only that one exists. The fallback is dropped in the release after
    0.1."""
    new = os.path.expanduser("~/outerloop-images/agent-py312.sif")
    old = os.path.expanduser("~/autoresearch-images/agent-py312.sif")
    return old if (not os.path.isfile(new) and os.path.isfile(old)) else new


def _followup_spec_from_env(root: Path) -> tuple[Any, FollowupSpec | None]:
    """GitHub client + FollowupSpec from the chain environment, or Nones when
    the environment is incomplete (the tick then runs without in-review
    servicing, and logs what is absent)."""
    pat_file = os.environ.get("OUTERLOOP_PAT_FILE", "")
    app_file = os.environ.get("OUTERLOOP_GITHUB_APP_FILE", "")
    account = os.environ.get("OUTERLOOP_ACCOUNT", "")
    partition = os.environ.get("OUTERLOOP_PARTITION", "")
    image = os.environ.get("OUTERLOOP_IMAGE", _default_image())
    home = os.environ.get("OUTERLOOP_HOME", "")
    # Account and partition are optional on Slurm: empty ones leave the billing
    # association and the partition to Slurm's defaults, as `start` already
    # does for the resident. Local compute has no placement at all.
    target = os.environ.get("OUTERLOOP_TARGET", "")
    image_ok = Path(image).is_file()
    panel = os.environ.get("OUTERLOOP_PANEL", "verify,review")
    if not image_ok and local_mode():
        # The local loop on a machine with no container: sessions run under
        # the harness's own sandbox and evaluations run bare, on the
        # operator's own machine with the operator's own keys. The panel is
        # off unless the operator opts in, because an uncontained judge holds
        # a shell next to its own key file. Said once per process. Contained
        # local mode needs apptainer and the image (docs/install.md).
        global _UNCONTAINED_WARNED
        if not _UNCONTAINED_WARNED:
            _UNCONTAINED_WARNED = True
            log.warning(
                "local mode: no container image at %s; sessions run under the harness "
                "sandbox and evaluations run bare on this machine; the panel is %s; a "
                "codex author needs the image (docs/install.md, local mode)",
                image,
                "on by OUTERLOOP_PANEL_UNCONTAINED=1"
                if os.environ.get("OUTERLOOP_PANEL_UNCONTAINED") == "1"
                else "off (OUTERLOOP_PANEL_UNCONTAINED=1 turns it on)",
            )
        if os.environ.get("OUTERLOOP_PANEL_UNCONTAINED") != "1":
            panel = ""
        # the jobs must not inherit a path to an image that is not there
        os.environ.pop("OUTERLOOP_IMAGE", None)
        image, image_ok = "", True
    if (pat_file or app_file) and home and target and image_ok:
        from outerloop.appauth import resolve_bot_auth
        from outerloop.github import GitHubClient

        try:
            github = GitHubClient(auth=resolve_bot_auth(pat_file, app_file))
            followup_spec = FollowupSpec(
                account=account,
                partition=partition,
                run_root=root,
                image=image,
                home=Path(home),
                pat_file=pat_file,
                github_app_file=app_file,
                target=target,
                steward_key_file=os.environ.get("OUTERLOOP_STEWARD_KEY_FILE", ""),
                panel=panel,
                panel_key_file=os.environ.get("OUTERLOOP_PANEL_KEY_FILE", ""),
                job_partition=os.environ.get("OUTERLOOP_JOB_PARTITION", ""),
                gpu_partition=os.environ.get("OUTERLOOP_GPU_PARTITION", ""),
                gpu_account=os.environ.get("OUTERLOOP_GPU_ACCOUNT", ""),
                max_job_minutes=_max_job_minutes_from_env(),
            )
            return github, followup_spec
        except Exception as exc:
            log.warning("in-review servicing disabled: %s", exc)
            return None, None
    absent = [
        name
        for name, value in [
            ("OUTERLOOP_PAT_FILE or _GITHUB_APP_FILE", pat_file or app_file),
            ("OUTERLOOP_HOME", home),
            ("OUTERLOOP_TARGET", target),
        ]
        if not value
    ]
    if not image_ok:
        absent.append(f"image:{image}")
    log.info("in-review servicing disabled (missing: %s)", ", ".join(absent))
    return None, None


def _loop_cadence_s(cadence_min: float) -> float:
    """The --loop sleep, clamped to [60s, 24h]: argparse accepts inf (which
    would OverflowError out of time.sleep) and sub-minute values would spin.
    A non-positive argument defers to OUTERLOOP_CADENCE_MIN."""
    return min(24 * 3600.0, max(60.0, cadence_min * 60 if cadence_min > 0 else _cadence_s()))


def main() -> int:
    import argparse
    import time

    parser = argparse.ArgumentParser(
        prog="outerloop tick",
        description="One tick of the loop: service the open runs, launch new work, wake "
        "parked runs. The chain runs this every cadence; --loop does the same in the "
        "foreground.",
    )
    parser.add_argument("--root", required=True, type=Path, help="state root on the shared FS")
    parser.add_argument(
        "--grace-s",
        type=float,
        default=DEFAULT_GRACE_S,
        help="seconds a finished experiment's delivery job gets before the sweep steps in",
    )
    parser.add_argument(
        "--lease-ttl-s",
        type=float,
        default=DEFAULT_LEASE_TTL_S,
        help="seconds after which a held run lease counts as stale",
    )
    parser.add_argument(
        "--min-free-gb",
        type=float,
        default=DEFAULT_MIN_FREE_BYTES / 1024**3,
        help="skip launching new work when the state filesystem has less than this many GB free",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="run a tick every cadence in the foreground — the local-mode "
        "chain (Slurm deployments use tick_chain.sbatch instead)",
    )
    parser.add_argument(
        "--cadence-min",
        type=float,
        default=0.0,
        help="minutes between --loop ticks; unset defers to "
        "OUTERLOOP_CADENCE_MIN via the chain's own parser (default 30)",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    args.root.mkdir(parents=True, exist_ok=True)
    # The tick's --root is the authority; children (and this process's own
    # LocalCompute) read OUTERLOOP_ROOT, so a bare `tick --loop --root X`
    # must not split-brain them: local job states would land nowhere and
    # every finished job would read GONE until the park deadline.
    # RESOLVED: local jobs cd into flight checkouts, so a relative root
    # would scatter their state dirs across working directories
    if os.environ.get("OUTERLOOP_ROOT", "") != str(args.root.resolve()):
        os.environ["OUTERLOOP_ROOT"] = str(args.root.resolve())
    # In-review servicing is LIVE when credentials + image are available in the
    # chain environment. The waiting-run sweep delivers real wakes only when the
    # operator arms it — the OUTERLOOP_DISPATCH_WAKE env var or a
    # <root>/DISPATCH_WAKE sentinel — and the env is complete; by default it
    # stays dry with the LoggingDispatcher — dispatched climbing lands DARK.
    # ONE compute for the process: LocalCompute remembers its jobs' states
    # in memory, so a --loop deployment must not discard them between ticks.
    compute = compute_from_env()

    def run_once() -> None:
        github, followup_spec = _followup_spec_from_env(args.root)
        now = time.time()
        dispatcher, wake_live = _wake_dispatcher_from_env(compute, followup_spec, now, args.root)
        # parks arm their own wake from this recipe; without it the sweep delivers.
        # Local compute never arms: jobs are synchronous, so an afterany wake's
        # dependencies are terminal before submit returns — the next loop
        # iteration's sweep delivers every wake instead (wake latency = cadence).
        if wake_live and followup_spec is not None and not isinstance(compute, LocalCompute):
            write_wake_spec(args.root, followup_spec)
        else:
            remove_wake_spec(args.root)

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
            "disk=%s launch_blocked=%s shed=%d",
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
            len(report.shed),
        )

    if not args.loop:
        run_once()
        return 0
    # The local-mode chain: same stateless tick, a foreground loop instead of
    # sbatch successors. Records on disk carry all state, so killing and
    # restarting the loop resumes exactly like the Slurm chain would.
    cadence_s = _loop_cadence_s(args.cadence_min)
    while True:
        started = time.time()
        try:
            run_once()
        except Exception:
            log.exception("tick failed; the loop continues")
        time.sleep(max(0.0, cadence_s - (time.time() - started)))


if __name__ == "__main__":
    raise SystemExit(main())
