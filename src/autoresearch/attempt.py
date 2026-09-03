"""One live climb, end to end: clone → attempt_once → commit/push/PR → report.

This is the glue `orchestrator.attempt_once` deliberately does not own: the git
side (bot-auth clone, veto-checked commit, push, PR) and the run's durable
record. One invocation = one run = at most one PR.

Credential separation holds throughout: the bot PAT is read orchestrator-side
and used only by Workspace network calls and the PR client, after the session
has ended; the session sees only its own capped API key inside its container.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from dataclasses import replace as dc_replace
from functools import partial
from pathlib import Path
from typing import Any, cast

from autoresearch.appauth import resolve_bot_auth
from autoresearch.brief import BudgetState, distill_lessons
from autoresearch.compute import LocalCompute, local_mode
from autoresearch.contract import Benchmark, Contract, load_contract
from autoresearch.dispatch import (
    Snapshot,
    afterany_ids,
    drop_snapshot,
    should_dispatch,
    snapshot_tree,
)
from autoresearch.github import (
    GitHubClient,
    TokenProvider,
    Workspace,
)
from autoresearch.harness import Harness, SessionResult, redact
from autoresearch.measure import DispatchedMeasurer, DispatchSettings
from autoresearch.orchestrator import (
    AttemptResult,
    EvalError,
    Measurer,
    RunConfig,
    RunParked,
    _benchmark,
    attempt_once,
    pr_body,
    resume_attempt,
)
from autoresearch.panel import PanelLens, PanelVerdict, run_panel
from autoresearch.progress import (
    PROGRESS_PATHS,
    load_leader,
    update_leader,
    write_progress,
)
from autoresearch.review import PullRequest
from autoresearch.role_runner import build_harness, role_key
from autoresearch.roles import author_spec
from autoresearch.rolespec import RoleSpec
from autoresearch.runstate import (
    ABORTED,
    BUDGET_EXHAUSTED,
    ENDED,
    IN_REVIEW,
    NEGATIVE_RESULT,
    STUCK,
    WAITING,
    RunRecord,
    list_runs,
    load_record,
    save_record,
    stamp_outage,
)
from autoresearch.syscall import MAX_ARTIFACT_BYTES, SYSCALL_DIR, SyscallRequest
from autoresearch.syscall import ensure_excluded as syscall_excluded
from autoresearch.syscall import install_tool as syscall_install_tool
from autoresearch.syscall import write_budget as syscall_write_budget
from autoresearch.syscall import write_siblings as syscall_write_siblings
from autoresearch.verifier import MAX_CLAIM_CHARS

log = logging.getLogger(__name__)

# Where a climb job reads its keys unless the CLI flags say otherwise. The
# tick preflights the panel key (and compares it against the author key —
# role separation) before claiming/submitting.
PANEL_KEY_DEFAULT = "~/.config/autoresearch/verifier_key"
HARNESS_KEY_DEFAULT = "~/.config/autoresearch/harness_key"  # the claude author key
CODEX_KEY_DEFAULT = "~/.config/autoresearch/codex_key"  # the codex author key


def resolve_author_key_file(backend: str, explicit: str = "") -> str:
    """The author key file for `backend`. Per-backend keys COEXIST (claude's and
    codex's both on disk), selected by backend — so the author backend is a
    config choice, not a key swap, and an in-flight run of either backend can
    still be woken/serviced after a fleet flip. An explicit path always wins;
    otherwise the per-backend env var, then the packaged default path. The result
    is always ~-expanded, so every caller gets a real path (an env value like
    "~/.config/..." must not reach the token provider verbatim)."""
    if not explicit:
        if backend == "codex":
            explicit = os.environ.get("AUTORESEARCH_CODEX_KEY_FILE") or CODEX_KEY_DEFAULT
        else:
            explicit = os.environ.get("AUTORESEARCH_HARNESS_KEY_FILE") or HARNESS_KEY_DEFAULT
    return os.path.expanduser(explicit)


def codex_author_config_error(backend: str, model: str, image: str) -> str:
    """Why a codex author would die at startup ("" when it won't). Validates the
    EFFECTIVE (backend, model) — the fresh climb passes args; a wake/follow-up
    passes the PARKED RUN's persisted pair — so backend and model are checked as
    a unit and never a fleet backend against a run's model. codex writes+executes,
    so it must be contained (--image) and needs a non-claude model."""
    if backend not in ("claude", "codex"):
        # a typo'd AUTORESEARCH_AUTHOR_BACKEND passes the env DEFAULT silently
        # (argparse validates the flag, not its default) and the climb rejects it
        # at build_harness — catch it on the tick host so a claimed intake
        # issue never strands on it
        return f"unknown author backend {backend!r} (expected 'claude' or 'codex')"
    if backend == "claude":
        # symmetric to the codex check: a claude harness 404s on a non-claude
        # model (e.g. AUTORESEARCH_AUTHOR_MODEL left on a codex id while the
        # backend is claude) — catch that misconfig before spend
        if model and not model.startswith("claude"):
            return f"author-backend claude needs a claude model (got {model!r})"
        return ""
    if not image:
        return "author-backend codex requires --image (it runs contained)"
    if not model or model.startswith("claude"):
        return (
            "author-backend codex needs a codex/openai model "
            f"(e.g. gpt-5.6-terra), not the claude default (got {model!r})"
        )
    return ""


def resume_author(record: object, fleet_model: str) -> tuple[str, str, str]:
    """The (backend, model, key_file) a wake/follow-up must reproduce for a parked
    run — all from the RECORD, not the current fleet.

    An empty backend is a legacy record (written before the field) and is
    therefore CLAUDE, never the fleet default; the model pairs with that backend
    (a claude backend falls back to the claude default, a codex backend to the
    fleet model only as a last resort — codex records always carry their model);
    the key file is the exact resolved path the run used (so an explicit
    --key-file survives), falling back to the per-backend resolution for legacy
    records that never recorded it."""
    backend = getattr(record, "author_backend", "") or "claude"
    model = getattr(record, "author_model", "") or (
        "claude-opus-5" if backend == "claude" else fleet_model
    )
    key_file = getattr(record, "author_key_file", "") or resolve_author_key_file(backend)
    return backend, model, key_file


class WorkspaceDrift(RuntimeError):
    """The tree changed between measurement and commit."""


def _target_clone_url(target: str) -> str:
    """The canonical HTTPS clone URL for `owner/repo`. The one source of truth
    for where a run's git pushes go — derived from the target, never read from
    the session-writable `remote.origin.url`."""
    return f"https://github.com/{target}.git"


def _blessed_head(ws: Workspace, result: Any, contract: Any) -> str:
    """The pushed PR head the tick may later self-merge — only when this
    publish was under merge:auto with a CLEAN panel (#171's arming
    condition); "" otherwise. Best-effort: an unreadable HEAD blesses
    nothing (never arm on doubt)."""
    if not (
        result.panel_rounds > 0
        and not (result.panel_blocking_open or result.panel_degraded)
        and getattr(contract, "merge", "manual") == "auto"
    ):
        return ""
    try:
        return ws.git("rev-parse", "HEAD").strip()
    except Exception:
        return ""


def _arm_unless_base_moved(
    github: GitHubClient,
    ws: Workspace,
    target: str,
    pr_number: str,
    base_branch: str,
    measured_base_sha: str,
    secrets: tuple[str, ...],
    merge_mode: str = "manual",
    panel_ran: bool = False,
) -> None:
    """Arm auto-merge only while origin/<base_branch> still equals the base the
    claim was measured against. A moved base still OPENS the PR — review owns
    staleness — but never ARMS it: merging a tree whose gate/suite/panel read
    is stale must be a human's deliberate act, not an armed automation. A
    failed freshness fetch also declines to arm (fail-safe: un-armed is just a
    normal PR). Best-effort throughout, like arming itself."""

    def _check_and_arm() -> None:
        ws.git_network("fetch", str(ws.url or ws.remote_url()), base_branch)
        fresh = ws.git("rev-parse", "FETCH_HEAD").strip()
        if fresh != measured_base_sha:
            log.info(
                "not arming auto-merge on %s#%s: %s moved since the claim was "
                "measured (%s -> %s); a human merges this one",
                target,
                pr_number,
                base_branch,
                measured_base_sha[:12],
                fresh[:12],
            )
            return
        if merge_mode == "auto" and not panel_ran:
            # the dial's own precondition: auto means GATE+PANEL clean, so a
            # publish that ran no panel must not self-merge — fall back to
            # the manual guard and say so (terra #171: a panel-less
            # deployment could otherwise self-merge on the metric gate alone)
            log.warning(
                "merge mode auto on %s#%s but no panel ran this attempt; "
                "arming manual-mode instead",
                target,
                pr_number,
            )
        if merge_mode == "auto" and panel_ran:
            # the contract's autonomy dial: the owner opted this repo into
            # self-merging gate-clean PRs — arm, or merge directly when
            # nothing is pending to arm against
            github.arm_auto_merge_auto_mode(target, int(pr_number))
        else:
            github.arm_auto_merge_when_review_required(target, int(pr_number))

    _best_effort("auto-merge arming", _check_and_arm, secrets)


def _title_pair(a: float, b: float) -> str:
    """Compact but never ambiguous: widen precision until the two numbers
    render differently (a title reading '10.00 -> 10.00' looks like no
    change even when the improvement is real)."""
    for precision in range(4, 12):
        fa, fb = f"{a:.{precision}g}", f"{b:.{precision}g}"
        if fa != fb:
            return f"{fa} -> {fb}"
    return f"{a} -> {b}"


RULER = (
    "The metric is computed by the contract's eval command over a frozen "
    "instance pool. Your claim is verified by the orchestrator re-running "
    "that exact command on your tree — and again by CI after the PR opens. "
    "Only changes inside the contract's allowed paths are ever measured."
)

_ENDINGS_BY_OUTCOME = {
    "no-improvement": NEGATIVE_RESULT,
    # the improvement was real but bought by regressing a sibling benchmark —
    # an honest negative with a named cause, not a malfunction
    "suite-regression": NEGATIVE_RESULT,
    "session-error": ABORTED,
    "session-budget": BUDGET_EXHAUSTED,
    "session-outage": STUCK,  # infrastructure failure, nothing about the run
    "eval-error": ABORTED,
    "scope-violation": ABORTED,
}


@dataclass(frozen=True)
class AttemptOutcome:
    run_id: str
    outcome: str
    pr_url: str = ""
    report_path: str = ""


def _best_effort(what: str, fn: Callable[[], object], secrets: tuple[str, ...] = ()) -> bool:
    """One ending step; a failure is logged, never raised.

    The terminal sequence (record, report, issue post) must degrade
    independently: a full disk must not block the GitHub post, and a network
    failure must not block the record.
    """
    try:
        fn()
        return True
    except Exception as exc:
        log.warning("%s failed: %s", what, redact(f"{type(exc).__name__}: {exc}", secrets))
        return False


def _clear_stage(record: RunRecord) -> RunRecord:
    """Strip the WAITING-only bookkeeping from a record leaving `waiting` for a
    terminal state. Otherwise a dispatched run's `stage`, `deadline`, and
    especially `wake_attempts` ride into `in-review`, where in-review follow-up
    servicing reuses `wake_attempts` as its OWN retry cap — so a run that woke
    once would reach review with a shrunk follow-up budget."""
    # the run's spend survives the wipe: terminal reporting (the climb
    # board) reads it after the transition
    kept = {k: record.stage[k] for k in ("gpu_hours_used",) if record.stage and k in record.stage}
    return dc_replace(
        record,
        stage=kept,
        experiment_job_id="",
        deadline=0.0,
        terminal_seen=0.0,
        wake_attempts=0,
    )


def _post_issue_finished(
    github: GitHubClient,
    target: str,
    issue_number: int,
    run_id: str,
    outcome_name: str,
    pr_url: str,
    summary: str,
    secrets: tuple[str, ...],
) -> None:
    """Post a run's terminal result back to the issue that requested it. When
    the run ends WITHOUT a PR (a negative or an error), include the
    `RELEASE_MARKER` so `intake.pick_issue` can re-select the issue — a comment
    alone does NOT un-claim it. An improved run KEEPS the claim: its PR
    (`Addresses #N`) is the ongoing work, and `followup` releases the claim if
    that PR later closes unmerged."""
    if not issue_number:
        return
    from autoresearch.intake import RELEASE_MARKER

    link = f"\n\nPull request: {pr_url}" if pr_url else ""
    # no PR opened -> nothing will ever resolve this issue, so free the claim
    # (bounded by intake's per-issue attempt cap).
    release = f"{RELEASE_MARKER}\n" if not pr_url else ""
    _best_effort(
        "issue report",
        lambda: github.comment(
            target,
            issue_number,
            f"{release}Run `{run_id}` finished ({outcome_name}).{link}\n\n{summary}",
        ),
        secrets,
    )


# A parked run's deadline is the FLOOR beneath the afterany wake: submit +
# eval walltime + a generous queue/grace allowance. It must exceed the time a
# healthy eval can legitimately sit queued-then-running, or `tick._sweep_one`
# would cancel a still-queued job as "unschedulable".
PARK_QUEUE_SLACK_MIN = 12 * 60
# a jobless checkpoint sleep only needs to survive to the next sweep pass:
# one cadence + coalescing headroom, not queue slack
CHECKPOINT_SLEEP_SLACK_MIN = 45


def _park_run(
    run_root: Path,
    record: RunRecord,
    parked: RunParked,
    candidate_ref: str,
    eval_minutes: int | None,
    now: float,
    secrets: tuple[str, ...] = (),
    keep_wake_attempts: bool = False,
    base_branch: str = "main",
    panel_reads: int = 0,
    dispatch: DispatchSettings | None = None,
) -> None:
    """Persist a dispatched climb's re-entry point as a WAITING record: the
    committed shas, drawn seeds, candidate snapshot ref, and afterany set a
    fresh process reconstructs the measure-and-decide phase from. The caller
    passes the EXACT `candidate_ref` it will keep alive (never re-derive it from
    the commit — two snapshots can share a commit)."""
    from autoresearch.dispatch import effective_eval_minutes

    job_ids = afterany_ids(parked.afterany)
    stage: dict[str, object] = {
        "phase": parked.phase,
        "base_sha": parked.base_sha,
        "candidate_sha": parked.candidate_sha,
        "candidate_ref": candidate_ref,
        "seed": parked.seed,
        "suite_seed": parked.suite_seed,
        "afterany": parked.afterany,
        "launch_afterany": parked.launch_afterany,
        # the branch the run targets, so a wake opens its PR against the SAME
        # branch a non-default `--base-branch` selected — the wake CLI otherwise
        # defaults to main and would mis-target.
        "base_branch": base_branch,
        # verification-panel reads so far — persisted so the next wake
        # continues the count.
        "panel_reads": panel_reads,
        # the session's write-up + spend, saved so a candidate wake can build
        # the PR body / panel claim and report the real cost WITHOUT re-running
        # the session (its edits are already in candidate_sha). REDACTED before
        # it lands in the durable record, like every other persisted final_text
        # — a session that echoed a credential must not leave it in record.json.
        # Empty/zero for a baseline park (the session has not run yet).
        "report": redact(parked.session.final_text, secrets)[:MAX_CLAIM_CHARS]
        if parked.session
        else "",
        "session_cost_usd": parked.session.cost_usd if parked.session else 0.0,
        "session_turns": parked.session.num_turns if parked.session else 0,
    }
    if parked.submitted:
        # a SUBMITTED candidate park (buildout Phase B): the wake delivers the
        # gate + panel results back to the author instead of deciding by policy
        stage["submitted"] = True
    if parked.syscall is not None:
        # A syscall park — an author-directed sleep (research-loop-buildout.md
        # Phase A) or a submitted candidate carrying sibling launches: the wake
        # gathers each launch's results by NAME from the run dir, delivers the
        # declared artifacts, and resumes the SAME session — so the stage must
        # carry the launch names/artifacts, the author's note, and the budget
        # counts as of this park.
        stage["syscall_launches"] = [
            # minutes ride along so a RE-PARK's deadline floor still covers the
            # longest launch — without them a rebuilt descriptor would
            # undershoot the floor and the sweep could cancel a healthy
            # queued sibling as "pending past deadline"
            {
                "name": launch.name,
                "minutes": launch.minutes,
                "artifacts": list(launch.artifacts),
                **({"array": launch.array} if launch.array > 1 else {}),
            }
            for launch in parked.syscall.launches
        ]
        stage["syscall_note"] = redact(parked.syscall.note, secrets)
        # (the session id the wake resumes is the record's own
        # resume_session_id, set below for every park — no stage duplicate)
        stage["launches_used"] = parked.launches_used
        stage["sleeps_used"] = parked.sleeps_used
        stage["gpu_hours_used"] = parked.gpu_hours_used
        if parked.judged is not None:
            # the gate's last negative rides the park: a wake that ends on the
            # same tree reuses it instead of measuring again
            judged_sha, verdict = parked.judged
            stage["judged"] = {
                "sha": judged_sha,
                "outcome": verdict.outcome,
                "baseline": verdict.baseline,
                "candidate": verdict.candidate,
                "note": redact(verdict.note, secrets),
            }
        if parked.eval_minutes:
            # the author's declared eval walltime rides the park: the wake's
            # measurer and deadline floor must use it, not the contract's
            stage["eval_minutes"] = parked.eval_minutes
    # A single-job park records its one pollable id; a MULTI-job park records
    # none — the sweep falls back to polling every id in the stage's `afterany`
    # string and wakes only when ALL are done (tick._poll_targets).
    experiment_job_id = job_ids[0] if len(job_ids) == 1 else ""
    # The deadline is a FLOOR: park time (`now` here is the park moment, passed
    # by the caller) + the eval walltime + a generous queue/grace slack, so a
    # healthy queued-then-running eval never trips the sweep's cancel-on-pending.
    floor_minutes = effective_eval_minutes(parked.eval_minutes or eval_minutes)
    if parked.phase == "author-sleep" and parked.syscall is not None:
        # an author launch's walltime is the LAUNCH's ask, not the benchmark's
        # eval hint — the floor must sit past the LONGEST launch, or the sweep
        # cancels still-queued author jobs (a benchmark can be in-job cheap,
        # eval_minutes=None, while its author trains for hours). A checkpoint
        # sleep has no jobs: floor 0 wakes it at the first deadline pass.
        floor_minutes = max((la.minutes for la in parked.syscall.launches), default=0)
    elif parked.syscall is not None:
        # a submitted candidate with sibling launches waits on gate evals AND
        # launches — the floor must sit past the longest of either
        floor_minutes = max(floor_minutes, *(la.minutes for la in parked.syscall.launches), 0)
    checkpoint_sleep = (
        parked.phase == "author-sleep"
        and parked.syscall is not None
        and not parked.syscall.launches
    )
    if checkpoint_sleep:
        # a CHECKPOINT SLEEP has nothing in any queue, so the 12h queue slack
        # (sized to protect queued Slurm jobs from cancel-on-pending) does not
        # apply — the deadline needs only to reach the sweep's next pass.
        # Observed live (yolo heldout_probe, 2026-08-27): a jobless nap
        # inherited the queue slack and became a 12h coma.
        deadline = now + CHECKPOINT_SLEEP_SLACK_MIN * 60
    else:
        deadline = now + (floor_minutes + PARK_QUEUE_SLACK_MIN) * 60
    waiting = RunRecord(
        **{
            **record.__dict__,
            "state": WAITING,
            "experiment_job_id": experiment_job_id,
            "resume_session_id": parked.session.session_id if parked.session else "",
            "deadline": deadline,
            "stage": stage,
            # wake_attempts = "wakes since the run last made progress"; the
            # stuck cap ends a run that keeps waking without advancing. Reset on
            # a PRODUCTIVE park (the IMPLEMENTING->park entry ran a session; a
            # wake that resolved its measures and dispatched NEW ones). A
            # no-progress re-park — results still pending, or a blind re-park
            # (squeue unreachable, nothing new dispatched) — must KEEP the
            # counter (`keep_wake_attempts`), or the loop never reaches the cap.
            "wake_attempts": record.wake_attempts if keep_wake_attempts else 0,
            "terminal_seen": 0.0,
        }
    )
    save_record(run_root, waiting, now)
    if dispatch is not None:
        _arm_park_wake(run_root, record.run_id, now, dispatch)


def _arm_park_wake(run_root: Path, run_id: str, now: float, dispatch: DispatchSettings) -> str:
    """Submit the parked run's wake right away, depending on the jobs it
    waits on (tick.arm_wake), when the tick has published its wake recipe.
    Without the recipe — dispatched wakes not armed, or a local compute —
    the sweep delivers as before."""
    from autoresearch.compute import SlurmCompute
    from autoresearch.tick import JobWakeDispatcher, arm_wake, dispatch_wake_armed, load_wake_spec

    if not dispatch_wake_armed(run_root):
        return ""  # disarmed: a recipe the tick has not yet removed is not used
    spec = load_wake_spec(run_root)
    if spec is None or not isinstance(dispatch.compute, SlurmCompute):
        return ""
    try:
        record = load_record(run_root, run_id)
        dispatcher = JobWakeDispatcher(dispatch.compute, spec, now)
        return arm_wake(
            run_root, record, dispatcher, now, holder_job_id=os.environ.get("SLURM_JOB_ID", "")
        )
    except Exception as exc:
        log.warning("park-time wake not armed for %s: %s: %s", run_id, type(exc).__name__, exc)
        return ""


def _lease_held_by_another_job(run_root: Path, run_id: str) -> str:
    """The job id of a wake that holds this run's lease and is not us, or "".
    A resume with no job id of its own (a manual run) never counts as the
    holder of a job-held lease."""
    from autoresearch.runstate import read_lease

    lease = read_lease(run_root, run_id)
    mine = os.environ.get("SLURM_JOB_ID", "")
    if lease is not None and lease.holder_job_id and lease.holder_job_id != mine:
        return lease.holder_job_id
    return ""


def _release_own_lease(run_root: Path, run_id: str) -> None:
    """Release the run's lease only while this job still holds it: a park
    hands the lease to the wake it arms, and that wake must keep it."""
    from autoresearch.runstate import release_lease

    if _lease_held_by_another_job(run_root, run_id):
        return
    release_lease(run_root, run_id)


def _dispatch_settings(args: argparse.Namespace) -> DispatchSettings:
    """The cluster coordinates from the CLI, read in ONE place for both the
    fresh climb and the wake (a second constructor drifted once — terra
    #174: the wake dropped the GPU lane)."""
    from autoresearch.compute import compute_from_env

    return DispatchSettings(
        compute=compute_from_env(),
        image=args.image,
        account=args.account,
        partition=args.partition,
        gpu_partition=getattr(args, "gpu_partition", ""),
        gpu_account=getattr(args, "gpu_account", ""),
    )


def _make_launcher(
    dispatch: DispatchSettings, run_dir: Path, workspace: Path, run_id: str, gpus: int = 0
):
    """The launch side of the author syscalls, shared by the first pass
    (live_attempt) and the author-sleep wake: each launch becomes a jailed job on
    the sealed snapshot (write_eval_job's copy-out handles artifacts), and a
    partially-submitted batch is reaped rather than orphaned. `gpus` is the
    benchmark's: an author's experiments run on the same lane as its evals."""
    account, partition = dispatch.placement(gpus)

    def launcher(sha: str, request: SyscallRequest) -> str:
        from autoresearch.dispatch import eval_job_spec, write_eval_job
        from autoresearch.syscall import launch_jobs

        ids: list[str] = []
        try:
            for launch in request.launches:
                # an array launch is N jobs of one command, each with its
                # SWEEP_INDEX; one afterany wake covers them all
                for job_name, extra_env in launch_jobs(launch):
                    script = write_eval_job(
                        run_dir,
                        f"launch-{job_name}",
                        repo_root=workspace,
                        snapshot_sha=sha,
                        command=launch.command,
                        image=dispatch.image,
                        extra_env=extra_env,
                        artifacts=launch.artifacts,
                        artifact_max_bytes=MAX_ARTIFACT_BYTES,
                        gpus=gpus,
                    )
                    ids.append(
                        dispatch.compute.submit(
                            eval_job_spec(
                                script,
                                job_name=f"{run_id}-launch-{job_name}",
                                account=account,
                                partition=partition,
                                eval_minutes=launch.minutes,
                                gpus=gpus,
                            )
                        )
                    )
        except Exception:
            # a partial batch must not orphan: no park record was written yet,
            # so nothing would ever wake or cancel the jobs that DID submit —
            # reap them here, then let the caller end the run as the error it
            # is (same stance as the failed-_park_run cancel).
            for job_id in ids:
                with contextlib.suppress(Exception):
                    dispatch.compute.cancel(job_id)
            raise
        # a checkpoint sleep (no launches) parks with no dependency and wakes
        # on the sweep's deadline floor — slow but correct; a fast requeue wake
        # is a follow-up.
        return "afterany:" + ":".join(ids) if ids else ""

    return launcher


def _wake_author_sleep(
    *,
    run_root: Path,
    run_id: str,
    record: RunRecord,
    ws: Workspace,
    workspace: Path,
    run_dir: Path,
    dispatch: DispatchSettings,
    github: GitHubClient,
    now: float,
    secrets: tuple[str, ...],
    base_branch: str,
    base_sha: str,
    sleep_ref: str,
    contract_text: str,
    contract: Contract,
    bench: Benchmark,
    config: RunConfig,
    measurer: Measurer,
    harness: Harness | None,
    spec: RoleSpec | None,
    panel_lenses: tuple[PanelLens, ...],
    issue_number: int,
    eval_minutes: int | None,
    extra_update: str = "",
    judged: tuple[str, AttemptResult] | None = None,
) -> AttemptOutcome:
    """Wake a syscall park and resume the AUTHOR: deliver the launches' results
    into the sandbox, resume the SAME session through the climb's resume-entry
    with them (data-fenced), and let the climb run — it may sleep again
    (re-park), submit, finish into the gate (whose dispatched measures park it
    as a CANDIDATE), or end on a terminal. The session's workspace persisted on
    disk exactly as the author left it (the launches ran on node-local
    checkouts of the sealed sha), so the resumed session continues its own tree
    — cumulative depth. `extra_update` leads the wake text — a SUBMITTED park's
    gate result or panel verdict (buildout Phase B), delivered back to the
    author to act on; `sleep_ref` is whichever snapshot ref this park holds
    (the sleep seal, or the submitted candidate)."""
    from autoresearch.syscall import Launch as SyscallLaunch
    from autoresearch.syscall import gather_results, render_wake, write_budget

    def _end(result: AttemptResult, drop_refs: list[str]) -> AttemptOutcome:
        # a terminal from the resumed climb: report, ending record, issue note —
        # the same ending shape every other terminal takes. The line notebook
        # records it first, while the tree is still the session's final tree.
        _push_line_snapshot(
            ws, _line_ref_for(bench, config.agent_id), run_id, result.outcome, secrets
        )
        for ref in drop_refs:
            drop_snapshot(ws, Snapshot(commit="", tree="", ref=ref))
        report_path = run_dir / "report.md"
        _best_effort(
            "run report",
            lambda: report_path.write_text(result.report(config, redact_secrets=secrets)),
            secrets,
        )
        ending = _ENDINGS_BY_OUTCOME.get(result.outcome, ABORTED)
        final = _clear_stage(
            RunRecord(
                **{
                    **record.__dict__,
                    "state": ENDED,
                    "ending": ending,
                    "ending_note": redact(result.note, secrets),
                }
            )
        )
        _best_effort("final record", lambda: save_record(run_root, final, now), secrets)
        _post_issue_finished(
            github,
            config.target,
            issue_number,
            run_id,
            result.outcome,
            "",
            redact(result.report(config, redact_secrets=secrets), secrets)[:8000],
            secrets,
        )
        return AttemptOutcome(run_id=run_id, outcome=result.outcome, report_path=str(report_path))

    # The wake NEEDS the author harness (it resumes the session). Fail as a
    # named ending, not a crash: the run cannot proceed and re-waking will not
    # help without the harness, so leaving it WAITING would just hit the stuck
    # cap slowly.
    if (
        harness is None
        or spec is None
        or not record.resume_session_id
        or not getattr(harness, "supports_resume", True)
    ):
        return _end(
            AttemptResult(
                outcome="session-error",
                note="author-sleep wake needs the author harness/spec and a resumable session",
            ),
            drop_refs=[sleep_ref],
        )

    # Deliver: read each launch's job output, copy declared artifacts into the
    # excluded channel, and render the data-fenced wake text. The stage stores
    # only what the wake needs (names + artifacts); command/minutes placeholders
    # never reach the author.
    launches = tuple(
        SyscallLaunch(
            name=str(item.get("name", "")),
            command="(ran)",
            minutes=int(item.get("minutes") or 1),
            artifacts=tuple(str(a) for a in item.get("artifacts", [])),
            array=int(item.get("array") or 1),
        )
        for item in _stage_launches(record)
    )
    results = gather_results(run_dir, workspace, launches)
    launches_used = int(record.stage.get("launches_used", 0))  # type: ignore[call-overload]
    sleeps_used = int(record.stage.get("sleeps_used", 0))  # type: ignore[call-overload]
    gpu_hours_used = _reconcile_launch_hours(record, dispatch, bench.gpus, launches)
    wake_text = render_wake(
        results,
        str(record.stage.get("syscall_note", "")),
        launches_used=launches_used,
        launch_budget=bench.depth_k,
        sleeps_used=sleeps_used,
        sleep_budget=bench.sleep_k,
        gpu_hours_remaining=(
            max(0.0, contract.budgets.gpu_hours_per_run - gpu_hours_used) if bench.gpus else None
        ),
    )
    if extra_update:
        # a submitted park's gate/panel feedback leads; launch results follow
        wake_text = f"{extra_update}\n\n{wake_text}"
    _best_effort(
        "budget refresh",
        lambda: write_budget(
            workspace,
            launches_remaining=max(0, bench.depth_k - launches_used),
            sleeps_remaining=max(0, bench.sleep_k - sleeps_used),
            gpu_hours_remaining=(
                max(0.0, contract.budgets.gpu_hours_per_run - gpu_hours_used)
                if bench.gpus
                else None
            ),
        ),
    )

    # The wake's climb IO: measures go through the DISPATCHED measurer (this is
    # a wake job with bounded walltime — the gate's evals run as their own jobs
    # and park the run as a CANDIDATE); snapshots parent on base (same as the
    # first pass: the clone was at base).
    snapshots: list[Snapshot] = []
    wake_line = _line_ref_for(bench, config.agent_id)

    def snapshot() -> str:
        snap = snapshot_tree(ws, base_sha, exclude=LINE_MEMORY_PATHS if wake_line else ())
        snapshots.append(snap)
        return snap.commit

    def changed_paths() -> list[str]:
        return _paths_changed_from_base(ws, base_sha, wake_line)

    panel_runner = (
        build_panel_runner(
            ws,
            run_dir,
            base_sha,
            panel_lenses,
            contract_text,
            config.target,
            config.benchmark,
            config.bot_login,
            _utc_date(now),
            exclude=LINE_MEMORY_PATHS if wake_line else (),
        )
        if panel_lenses
        else None
    )

    parked: RunParked | None = None
    kept_ref = ""
    try:
        result = attempt_once(
            config,
            contract_text,
            workspace,
            harness,
            measurer,
            base_sha,
            snapshot,
            ruler=RULER,
            changed_paths=changed_paths,
            spec=spec,
            panel_runner=panel_runner,
            resume_session_id=record.resume_session_id,
            improve_prompt=wake_text,
            launcher=_make_launcher(dispatch, run_dir, workspace, run_id, gpus=bench.gpus),
            tree_of=lambda sha: ws.git("rev-parse", f"{sha}^{{tree}}").strip(),
            judged=judged or _stage_judged(record),
            launches_used=launches_used,
            sleeps_used=sleeps_used,
            gpu_hours_used=gpu_hours_used,
        )
    except RunParked as p:
        # slept again, or the gate dispatched its measures (a candidate park the
        # existing wake path decides). Keep the NEW park's snapshot ref; the OLD
        # sleep ref is superseded once the new park persists.
        kept_ref = next((s.ref for s in snapshots if s.commit == p.candidate_sha), "")
        import time

        try:
            _park_run(
                run_root,
                record,
                p,
                kept_ref,
                eval_minutes,
                time.time(),
                secrets,
                dispatch=dispatch,
                base_branch=base_branch,
            )
        except Exception:
            for job_id in afterany_ids(p.afterany):
                dispatch.compute.cancel(job_id)
            raise
        parked = p
        drop_snapshot(ws, Snapshot(commit="", tree="", ref=sleep_ref))
        return AttemptOutcome(run_id=run_id, outcome="parked")
    finally:
        for snap in snapshots:
            if parked and kept_ref and snap.ref == kept_ref:
                continue
            drop_snapshot(ws, snap)

    # a terminal from the resumed session (session-error/-outage/-budget,
    # scope-violation, eval-error; a dispatched gate never returns improved
    # inline): end the run and release the sleep snapshot.
    return _end(result, drop_refs=[sleep_ref])


def _stage_judged(record: RunRecord) -> tuple[str, AttemptResult] | None:
    """The gate verdict a park carried (written by `_park_run`), or None."""
    j = (record.stage or {}).get("judged")
    if not isinstance(j, dict) or not j.get("sha"):
        return None

    def num(v: object) -> float | None:
        return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None

    return (
        str(j["sha"]),
        AttemptResult(
            outcome=str(j.get("outcome") or "no-improvement"),
            baseline=num(j.get("baseline")),
            candidate=num(j.get("candidate")),
            note=str(j.get("note") or ""),
        ),
    )


def _stage_syscall_launches(record: RunRecord) -> tuple:
    """The park's launches as `Launch` values (command elided: they ran)."""
    from autoresearch.syscall import Launch

    return tuple(
        Launch(
            name=str(item.get("name", "")),
            command="(ran)",
            minutes=int(item.get("minutes") or 1),
            artifacts=tuple(str(a) for a in item.get("artifacts", [])),
            array=int(item.get("array") or 1),
        )
        for item in _stage_launches(record)
    )


def _stage_launch_job_ids(record: RunRecord) -> list[str]:
    """The park's launch jobs: `launch_afterany` when the park recorded it;
    for an older author-sleep park every waited job was a launch; for an
    older candidate park the gate's evals are mixed in, so none."""
    stage = record.stage or {}
    if "launch_afterany" in stage:
        return afterany_ids(str(stage.get("launch_afterany") or ""))
    if stage.get("phase") == "author-sleep":
        return afterany_ids(str(stage.get("afterany") or ""))
    return []


def _reconcile_launch_hours(
    record: RunRecord, dispatch: DispatchSettings, gpus: int, launches: tuple
) -> float:
    """The run's GPU-hours after handing back the unused walltime of the
    park's launch jobs — once: the stage remembers the refund, so a wake
    that follows a gate decision on the same park does not refund twice.
    Returns the (possibly corrected) `gpu_hours_used`."""
    stage = record.stage or {}
    used = float(stage.get("gpu_hours_used", 0.0))  # type: ignore[arg-type]
    if not gpus or stage.get("launch_hours_refunded"):
        return used
    refund = _launch_refund(dispatch, launches, _stage_launch_job_ids(record), gpus)
    if refund > 0:
        log.info("%s: refunding %.2f GPU-hours of unused launch walltime", record.run_id, refund)
        used = max(0.0, used - refund)
        stage["gpu_hours_used"] = used
    stage["launch_hours_refunded"] = True
    return used


def _launch_refund(
    dispatch: DispatchSettings, launches: tuple, job_ids: list[str], gpus: int
) -> float:
    """The unused walltime of a park's launch jobs, in GPU-hours, or 0 when
    the compute cannot say how long they ran (nothing is refunded on a
    guess)."""
    from autoresearch.syscall import launch_hours_refund

    query = getattr(dispatch.compute, "elapsed_seconds", None)
    if query is None or not job_ids:
        return 0.0
    try:
        elapsed = [query(jid) for jid in job_ids]
    except Exception as exc:
        log.warning("launch walltime unknown (%s: %s); nothing refunded", type(exc).__name__, exc)
        return 0.0
    return launch_hours_refund(launches, elapsed, gpus=gpus)


RESEARCH_LOG_BRANCH = "research-log"
MAX_ARCHIVED_REPORTS = 30  # materialized for the session to read; newest first
MAX_ARCHIVED_REPORT_CHARS = 100_000  # per report; branch content is remote-controlled


def _fetch_research_reports(ws: Workspace, count: int) -> list[tuple[str, str]]:
    """The newest `count` reports from the target's research-log branch, as
    (name, text), newest first — the shared memory of every attempt on this
    target, wherever it ran. Fail-soft: a target with no research log yet
    (or an unreachable remote) is an empty memory, never a dead attempt."""
    try:
        ws.fetch_branch(RESEARCH_LOG_BRANCH)
        listing = ws.git("ls-tree", "-r", "--name-only", "FETCH_HEAD", "reports/")
        # only direct children (the publisher's layout): a nested path would
        # flatten to a basename that overwrites another archived report
        names = [
            line.strip()
            for line in listing.splitlines()
            if line.strip().endswith(".md") and line.strip().count("/") == 1
        ]
        # report files are dated (reports/<YYYY-MM-DD>-<run_id>.md): the name
        # sorts by day; same-day order is arbitrary and does not matter
        out: list[tuple[str, str]] = []
        for name in sorted(names, reverse=True):
            if len(out) >= count:
                break
            # size BEFORE content: `git show` would load the whole blob, and
            # the branch's content is remote-controlled
            if int(ws.git("cat-file", "-s", f"FETCH_HEAD:{name}").strip()) > (
                MAX_ARCHIVED_REPORT_CHARS
            ):
                log.info("research report %s exceeds the size cap; skipped", name)
                continue
            out.append((Path(name).name, ws.git("show", f"FETCH_HEAD:{name}")))
        return out
    except Exception as exc:
        log.info("research log unavailable (%s: %s); starting without it", type(exc).__name__, exc)
        return []


def _exclude_merge_artifacts(workspace: Path) -> None:
    """Ignore *.orig and *.rej — git's merge/patch conflict backups, which
    should never be committed — via .git/info/exclude (repo-local, never a
    tracked edit). A line run merges main at start and the agent resolves
    conflicts as its first task; a leftover train.py.orig would otherwise
    read as an out-of-scope edit and abort the run at launch. Idempotent,
    and effective for the `git add -A` behind changed-paths and every seal."""
    exclude = workspace / ".git" / "info" / "exclude"
    wanted = ["*.orig", "*.rej"]
    try:
        existing = exclude.read_text()
    except OSError:
        existing = ""
    lines = existing.splitlines()
    missing = [p for p in wanted if p not in lines]
    if missing:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        sep = "" if existing.endswith("\n") or not existing else "\n"
        exclude.write_text(existing + sep + "\n".join(missing) + "\n")


def _reset_instruction_files(ws: Workspace, workspace: Path, base_ref: str) -> None:
    """Make the checkout's instruction-bearing files EQUAL the base branch's
    reviewed versions (review_agent.INSTRUCTION_FILES owns the list): a line
    is an author-written tree, and must never instruct its own successor
    sessions — or a sibling's (docs/design/research-lines.md)."""
    from autoresearch.review_agent import INSTRUCTION_FILES

    # bottom-up so a removed directory doesn't orphan paths found beneath it
    for path in sorted(workspace.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if ".git" in path.parts or path.name not in INSTRUCTION_FILES:
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    base_paths = [
        p
        for p in ws.git("ls-tree", "-r", "--name-only", base_ref).splitlines()
        if any(part in INSTRUCTION_FILES for part in Path(p).parts)
    ]
    if base_paths:
        ws.git("checkout", base_ref, "--", *base_paths)


# The agent's memory on its line (docs/design/research-lines.md): the bounded
# index at the branch root plus the topic-file folder. They ride the NOTEBOOK
# seal (the line branch is exactly where they live) and are excluded from
# every MEASURABLE seal and from changed-path accounting — never a scope
# violation, never claimable work, never part of a main-PR candidate.
LINE_MEMORY_PATHS = ("AGENT_MEMORY.md", "agent_memory")


def _is_line_memory(path: str) -> bool:
    return path in LINE_MEMORY_PATHS or path.startswith("agent_memory/")


def _without_line_memory(paths: Iterable[str], line: str) -> list[str]:
    """The run's OWN changes: with a line active, its memory paths are dropped.
    Every sealed tree excludes them by construction, and the run's base is the
    line tip that carries them — so a base..candidate diff lists them as
    deletions that are not the run's change (live: a dispatched wake on
    gpt-speedrun read eight memory files as out of scope and aborted a run
    whose paired eval had already finished)."""
    return [p for p in paths if not (line and _is_line_memory(p))]


def _line_ref_for(bench: Benchmark | None, agent_id: str) -> str:
    """The agent's line branch when the benchmark opted in, or the empty
    string when the feature is off. Wake paths recompute this from the
    record — a malformed agent id could never have created a line at run
    start, so empty (not an error) is right there too."""
    if bench is None or not bench.lines or not agent_id:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", agent_id):
        return ""
    return f"agents/{agent_id}"


def _push_line_snapshot(
    ws: Workspace, line_ref: str, run_id: str, outcome: str, secrets: tuple[str, ...] = ()
) -> None:
    """Publish the session's final tree to the agent's line as a sealed
    snapshot commit — every terminal path, any outcome
    (docs/design/research-lines.md). The seal parents on the LOCAL line ref
    and advances it, so sequential terminals within one run chain as
    fast-forwards; one slot never runs twice concurrently, so the remote
    cannot have moved under us. Best-effort throughout: the notebook never
    changes a run's outcome. An unchanged tree is not pushed (the run-start
    push already holds it)."""
    if not line_ref:
        return

    def _seal_and_push() -> None:
        # raises if the ref is absent (e.g. a park that predates the line
        # feature) — _best_effort turns that into a logged skip
        parent = ws.git("rev-parse", f"refs/heads/{line_ref}").strip()
        memory = tuple(p for p in LINE_MEMORY_PATHS if (Path(ws.root) / p).exists())
        snap = snapshot_tree(ws, parent, force=memory)
        try:
            # seal only when the tree moved past the local ref; the PUSH runs
            # either way — a session that COMMITTED its work advanced the
            # local ref without dirtying the tree, and that commit must still
            # reach the remote (an already-current ref push is a no-op).
            if snap.tree != ws.git("rev-parse", f"{parent}^{{tree}}").strip():
                sealed = ws.git(
                    "-c",
                    "user.name=autoresearch",
                    "-c",
                    "user.email=autoresearch@localhost",
                    "commit-tree",
                    snap.tree,
                    "-p",
                    parent,
                    "-m",
                    f"line snapshot: {run_id} ({outcome})",
                ).strip()
                ws.git("update-ref", f"refs/heads/{line_ref}", sealed)
            ws.push(line_ref)
        finally:
            drop_snapshot(ws, snap)

    _best_effort(f"line push ({outcome})", _seal_and_push, secrets)


def _checkout_line(ws: Workspace, workspace: Path, agent_id: str, base_branch: str) -> str:
    """Check out the agent's research line: the persistent branch
    `agents/<agent-id>`, created from the base branch when absent, with the
    base branch merged in when it exists — a conflicted merge is left in the
    tree as the session's first task. Instruction-bearing files are reset to
    the base branch's reviewed versions and the hygiene is committed (never
    left to masquerade as agent edits). Returns the line ref name; raises on
    anything unrecoverable (the caller falls back to the base branch)."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", agent_id):
        raise ValueError(f"agent id {agent_id!r} cannot shape a line ref")
    line = f"agents/{agent_id}"
    base_ref = f"origin/{base_branch}"
    if not ws.git("branch", "--list", "-r", f"origin/{line}").strip():
        ws.git("checkout", "-q", "-B", line, base_ref)
        ws.push(line)  # the line is durable from its first run
        return line
    ws.git("checkout", "-q", "-B", line, f"origin/{line}")
    conflicted = False
    try:
        ws.git(
            "-c",
            "user.name=autoresearch",
            "-c",
            "user.email=autoresearch@localhost",
            "merge",
            "--no-edit",
            base_ref,
        )
    except Exception:
        conflicted = True
        log.info(
            "line %s: merging %s conflicts; left as the session's first task", line, base_branch
        )
    _reset_instruction_files(ws, workspace, base_ref)
    if not conflicted:
        ws.git("add", "-A")
        if ws.git("status", "--porcelain").strip():
            ws.git(
                "-c",
                "user.name=autoresearch",
                "-c",
                "user.email=autoresearch@localhost",
                "commit",
                "-q",
                "-m",
                f"line hygiene: instruction files reset to {base_branch}",
            )
        # Run-START persistence: the branch exists on the remote from its
        # first run, and the merge-main + hygiene state survives a crashed
        # run. One slot never runs twice concurrently, so this is a fast-
        # forward. The run-END push of the sealed session tree is the next
        # phase PR (it requires all-terminal sealing; the push must publish
        # a sealed sha, never invent a commit). A conflicted merge is not
        # pushed: the conflict is session work, not line state.
        ws.push(line)
    return line


def _paths_changed_from_base(ws: Workspace, base_sha: str, line_ref: str) -> list[str]:
    """The paths the tree changed, measured against the BASE, not HEAD. On a
    research line HEAD can be behind main: a run starts by merging main in,
    and when that merge conflicts it stays uncommitted, so the files git
    auto-merged (the kernel's own BENCHMARKS.md, results/leader.json) sit
    staged against the stale HEAD while being identical to main. Those are
    main's edits, not the agent's, and must not read as scope violations
    (gpt-speedrun, 2026-09-03: agent-01's first run after its own win ended
    scope-violation on exactly those two files). Line memory is excluded
    as before."""
    ws.git("add", "-A")
    try:
        staged = ws.staged_paths()
        if not staged:
            return []
        # index vs base, restricted to what moved against HEAD: a path identical
        # to base drops out, a new or deleted file still counts
        differs = {
            entry
            for entry in ws.git(
                "diff", "--cached", "--name-only", "-z", base_sha, "--", *staged
            ).split("\0")
            if entry
        }
    finally:
        ws.git("reset")
    return _without_line_memory([p for p in staged if p in differs], line_ref)


def _sibling_entries(ws: Workspace, self_agent: str) -> list[dict]:
    """The other agents' live directions from the research-log's
    status.json (already fetched: the SAME FETCH_HEAD the reports came
    from). Size-checked BEFORE show like the report blobs, entries and
    fields bounded — the branch is bot-written but never trusted with
    unbounded memory. Any failure means no siblings known, never a crash."""
    try:
        blob = "FETCH_HEAD:climb/status.json"
        if int(ws.git("cat-file", "-s", blob).strip()) > 1_000_000:
            raise ValueError("status snapshot oversized; skipped")
        fleet = json.loads(ws.git("show", blob))
        return [
            {
                "agent": str(r.get("agent", ""))[:64],
                "state": str(r.get("state", ""))[:32],
                "phase": str(r.get("phase", ""))[:32],
                "direction": str(r.get("direction", ""))[:160],
            }
            for r in fleet.get("runs", [])[:64]
            if isinstance(r, dict) and r.get("agent") != self_agent
        ]
    except Exception as exc:
        log.info("no sibling snapshot for this session (%s)", exc)
        return []


def _install_report_archive(workspace: Path, reports: list[tuple[str, str]]) -> None:
    """Materialize the fetched reports under the kernel-owned channel
    (`.autoresearch/reports/`) so the session can read and search the full
    texts with its own tools; the brief inlines only the newest few."""
    dest = workspace / SYSCALL_DIR / "reports"
    dest.mkdir(parents=True, exist_ok=True)
    for name, text in reports:
        if Path(name).name != name:  # branch content is remote-controlled
            continue
        (dest / name).write_text(text)


def _stage_launches(record: RunRecord) -> list[dict]:
    """The persisted launch descriptors of an author-sleep stage (name +
    artifacts), tolerating a malformed entry by skipping it (the job dirs are
    keyed by name; a nameless entry has nothing to gather)."""
    raw = record.stage.get("syscall_launches", [])
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict) and item.get("name")]


def _utc_date(now: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")


def resume_run(
    run_root: Path,
    run_id: str,
    *,
    dispatch: DispatchSettings,
    github: GitHubClient,
    bot_auth: TokenProvider,
    now: float,
    secrets: tuple[str, ...] = (),
    base_branch: str = "main",
    panel_lenses: tuple[PanelLens, ...] = (),
    harness: Harness | None = None,
    spec: RoleSpec | None = None,
) -> AttemptOutcome:
    """Wake a parked dispatched climb and re-enter its decision WITHOUT the
    session (`orchestrator.resume_attempt`), from the record `_park_run` wrote.
    The three exits:

    * **re-park** — the wake dispatched a measure that is not done yet (the
      suite pairs an improving candidate fans out, "another round of
      experiments"): `resume_attempt` raises `RunParked`, and this re-persists
      the WAITING stage on the new afterany, keeping the same candidate
      snapshot;
    * **a negative terminal** (no-improvement / suite-regression / eval-error):
      drop the candidate snapshot and end the record;
    * **improved** — branch the SEALED `candidate_sha` (never the live tree,
      which may have drifted since the park; the diff was scope-checked so it
      carries only in-scope changes), layer the ledger update on top, push, and
      open the PR. A moved base is NOT merged and re-measured here
      (docs/design/research-loop.md): a stale PR is a re-wake, not an
      orchestrator auto-merge.
    """
    run_dir = run_root / "runs" / run_id
    workspace = run_dir / "ws"
    record = load_record(run_root, run_id)
    stage = record.stage
    # Push to the CANONICAL target URL, never the workspace's remote.origin.url:
    # the session could have rewritten that config to exfil the bot token / code
    # to another remote. Passing `url` here means `Workspace.push` uses it
    # instead of reading `remote.origin.url`.
    ws = Workspace(root=workspace, auth=bot_auth, url=_target_clone_url(record.target))
    # Re-establish the merge-artifact exclude on the wake too: the workspace
    # persisted across the park, but a session could have removed the exclude,
    # and this wake's changed_paths / seal run `git add -A`. Idempotent.
    _exclude_merge_artifacts(workspace)
    # Refresh origin refs on EVERY wake: the clone's refs froze at run
    # start, and this is the one credential-free freshness point — the
    # kernel fetches (from the canonical URL, never the session-writable
    # remote config), the session only ever reads local refs. `sleep`
    # thereby doubles as the author's sync primitive. Best-effort: a fetch
    # outage must not cost the wake.
    try:
        ws.fetch_origin()
    except Exception as exc:
        log.warning("wake fetch failed for %s: %s", run_id, exc)

    # Two park kinds reach the wake: a CANDIDATE park (the gate's measures were
    # dispatched) and an AUTHOR-SLEEP park (the author launched work and slept —
    # research-loop-buildout.md Phase A). Both carry a sealed sha. Anything else
    # is a stray record: guard rather than crash on `git diff base ""`.
    if stage.get("phase") not in ("candidate", "author-sleep") or not stage.get("candidate_sha"):
        raise EvalError(f"resume_run: run {run_id} is not a wakeable park (stage={stage!r})")

    base_sha = str(stage["base_sha"])
    candidate_sha = str(stage["candidate_sha"])
    candidate_ref = str(stage["candidate_ref"])
    issue_number = record.issue_number
    # the run's target branch rides the stage, so a wake opens its PR against
    # the branch the ORIGINAL climb selected — not the CLI's default (the wake
    # job carries no --base-branch).
    base_branch = str(stage.get("base_branch") or base_branch)

    # The contract gates scope and names the eval command, so read it from the
    # BASE commit (the tree the run started on), NOT the working tree the
    # session left dirty — a session that widened its own scope in
    # `.autoresearch.yaml` must not have the wake gate on the doctored rules.
    contract_text = ws.git("show", f"{base_sha}:.autoresearch.yaml")
    contract = load_contract(contract_text, record.target)
    bench = _benchmark(contract, record.benchmark)
    config = RunConfig(target=record.target, benchmark=record.benchmark, agent_id=record.agent_id)
    eval_minutes = next(
        (b.eval_minutes for b in contract.benchmarks if b.name == record.benchmark), None
    )
    # a submitted park carries the author's declared eval walltime: the
    # wake's measurer (a re-dispatch) and deadline floor honor it
    declared = int(record.stage.get("eval_minutes", 0) or 0)  # type: ignore[call-overload]
    if declared:
        eval_minutes = declared
    measurer = dispatch.measurer(
        run_dir, repo_root=workspace, eval_minutes=int(eval_minutes or 0), run_tag=run_id
    )
    # measured_paths from the COMMITTED base..candidate diff — the sealed
    # candidate, never `changed_paths()` on a live tree that may have drifted.
    # NUL-delimited (like Workspace.staged_paths) so a path with a space is one
    # entry, not two that could each slip past the scope check. The same
    # line-memory rule as the climb's changed_paths(): the base is the line
    # tip, the seal excluded the memory, the diff must not read it as a change.
    measured_paths = tuple(
        _without_line_memory(
            (
                p
                for p in ws.git("diff", "--name-only", "-z", base_sha, candidate_sha).split("\0")
                if p
            ),
            _line_ref_for(bench, config.agent_id),
        )
    )
    seed = int(stage["seed"])  # type: ignore[call-overload]
    suite_seed = int(stage["suite_seed"])  # type: ignore[call-overload]
    panel_reads = int(stage.get("panel_reads", 0))  # type: ignore[call-overload]

    if stage.get("phase") == "author-sleep":
        # The author slept on launches: deliver their results and RESUME the
        # same session through the climb's resume-entry. Every exit is a park
        # (slept again, or the gate dispatched its measures -> a candidate park
        # the NEXT wake decides through the path below) or a terminal ending —
        # never a publish, so the publish tail stays candidate-only.
        return _wake_author_sleep(
            run_root=run_root,
            run_id=run_id,
            record=record,
            ws=ws,
            workspace=workspace,
            run_dir=run_dir,
            dispatch=dispatch,
            github=github,
            now=now,
            secrets=secrets,
            base_branch=base_branch,
            base_sha=base_sha,
            sleep_ref=candidate_ref,
            contract_text=contract_text,
            contract=contract,
            bench=bench,
            config=config,
            measurer=measurer,
            harness=harness,
            spec=spec,
            panel_lenses=panel_lenses,
            issue_number=issue_number,
            eval_minutes=eval_minutes,
        )

    # rebuild the session from what the park saved: the (redacted) write-up and
    # its real spend, so the report shows true cost/turns. It is never re-run.
    session = SessionResult(
        stop_reason="resumed",
        is_error=False,
        cost_usd=float(stage.get("session_cost_usd", 0.0)),  # type: ignore[arg-type]
        num_turns=int(stage.get("session_turns", 0)),  # type: ignore[call-overload]
        session_id=record.resume_session_id,
        final_text=str(stage.get("report", "")),
        transcript_path="",
    )
    try:
        result = resume_attempt(
            contract,
            bench,
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            seed=seed,
            suite_seed=suite_seed,
            measured_paths=measured_paths,
            session=session,
            measurer=measurer,
            min_relative_improvement=config.min_relative_improvement,
        )
    except RunParked as parked:
        # another measure this wake dispatched is not done — re-park on the new
        # afterany, keeping the SAME candidate snapshot the next wake reads.
        # PROGRESS only if this wake dispatched a NEW job set (e.g. the candidate
        # resolved and the suite pairs fanned out); a blind re-park (empty
        # afterany) or the same jobs still pending is NO progress, so the stuck
        # cap must keep counting.
        if stage.get("submitted"):
            # No author was woken yet, so a SUBMITTED park's re-park (the suite
            # fanned out) still owes the author the gate+panel results and its
            # sibling launches' results — carry the submit context forward, or
            # the next wake drafts instead of waking the author and the launch
            # descriptors are lost. Commands/minutes are spent
            # history; the wake needs only names + artifacts (as persisted).
            from autoresearch.syscall import Launch as _Launch
            from autoresearch.syscall import SyscallRequest as _SyscallRequest

            parked.submitted = True
            parked.launches_used = int(stage.get("launches_used", 0))  # type: ignore[call-overload]
            parked.sleeps_used = int(stage.get("sleeps_used", 0))  # type: ignore[call-overload]
            parked.gpu_hours_used = float(stage.get("gpu_hours_used", 0.0))  # type: ignore[arg-type]
            parked.eval_minutes = int(stage.get("eval_minutes", 0) or 0) or None  # type: ignore[call-overload]
            parked.judged = parked.judged or _stage_judged(record)
            parked.launch_afterany = parked.launch_afterany or str(stage.get("launch_afterany", ""))
            if parked.syscall is None:
                parked.syscall = _SyscallRequest(
                    launches=tuple(
                        _Launch(
                            name=str(item.get("name", "")),
                            command="(ran)",
                            # the persisted walltime, so the re-park's deadline
                            # floor still covers the longest launch
                            minutes=int(item.get("minutes") or 1),
                            artifacts=tuple(str(a) for a in item.get("artifacts", [])),
                            array=int(item.get("array") or 1),
                        )
                        for item in _stage_launches(record)
                    ),
                    note=str(stage.get("syscall_note", "")),
                    submit=True,
                )
        old_afterany = str(record.stage.get("afterany", ""))
        made_progress = bool(parked.afterany) and parked.afterany != old_afterany
        _park_run(
            run_root,
            record,
            parked,
            candidate_ref,
            eval_minutes,
            now,
            secrets,
            dispatch=dispatch,
            keep_wake_attempts=not made_progress,
            base_branch=base_branch,
            panel_reads=panel_reads,
        )
        return AttemptOutcome(run_id=run_id, outcome="parked")

    # A SUBMITTED park (the author's `submit` syscall, buildout Phase B): gate
    # and panel results go back to the AUTHOR — it revises and resubmits, runs
    # more experiments, or concludes — instead of being decided by policy here.
    # Falls back to the plain-finish behavior (negative terminal / draft PR)
    # when the session cannot be resumed.
    submitted_park = bool(stage.get("submitted"))
    author_resumable = (
        harness is not None
        and spec is not None
        and bool(record.resume_session_id)
        and getattr(harness, "supports_resume", True)
    )

    # the park's sibling launches are done too: settle their charge before
    # any path — publish or hand back to the author — reads the budget
    if _stage_launches(record):
        _reconcile_launch_hours(record, dispatch, bench.gpus, _stage_syscall_launches(record))

    def _wake_author(
        extra_update: str, judged: tuple[str, AttemptResult] | None = None
    ) -> AttemptOutcome:
        # resume the submitted park's author with the gate/panel feedback
        # leading its wake text; the candidate ref is this park's held snapshot
        return _wake_author_sleep(
            run_root=run_root,
            run_id=run_id,
            record=record,
            ws=ws,
            workspace=workspace,
            run_dir=run_dir,
            dispatch=dispatch,
            github=github,
            now=now,
            secrets=secrets,
            base_branch=base_branch,
            base_sha=base_sha,
            sleep_ref=candidate_ref,
            contract_text=contract_text,
            contract=contract,
            bench=bench,
            config=config,
            measurer=measurer,
            harness=harness,
            spec=spec,
            panel_lenses=panel_lenses,
            issue_number=issue_number,
            eval_minutes=eval_minutes,
            extra_update=extra_update,
            judged=judged,
        )

    if (
        submitted_park
        and author_resumable
        and result.outcome in ("no-improvement", "suite-regression", "eval-error")
    ):
        # the submitted candidate failed the gate — including an eval that
        # errored: feedback, never a silent terminal — the author decides
        # what happens next (rounds stay bounded by sleep_k)
        return _wake_author(
            "Your `submit` did NOT clear the gate: "
            f"{result.note or result.outcome} "
            f"(baseline {result.baseline}, candidate {result.candidate}). "
            "Revise and submit again, run more experiments, or finish with an "
            "honest negative report.",
            # the verdict rides the resume: the same tree, sealed again after
            # the author concludes, is not measured twice; only an explicit
            # resubmit runs an errored eval again
            judged=(candidate_sha, result),
        )

    def _notebook(outcome: str) -> None:
        # Research lines: record the tree AS OF THIS DECIDED TERMINAL — never
        # earlier, because a blocking panel verdict can still resume the
        # author (a continuation, not a terminal).
        _push_line_snapshot(ws, _line_ref_for(bench, config.agent_id), run_id, outcome, secrets)

    if result.outcome == "improved":
        # Publish: branch the SEALED candidate sha, fold in the ledger, push,
        # open the PR. No moved-base merge (research-loop.md) — a stale PR is a
        # re-wake, not an auto-merge.
        from datetime import UTC, datetime

        assert result.baseline is not None and result.candidate is not None
        baseline, candidate = result.baseline, result.candidate
        # a zero-change "improvement" is metric noise, not progress — never a PR
        # (defense in depth; measure_and_decide already requires a real delta,
        # and an empty base..candidate diff implies baseline == candidate).
        if not measured_paths:
            result = dc_replace(
                result, outcome="no-improvement", note="no code change; metric noise"
            )
            drop_snapshot(ws, Snapshot(commit=candidate_sha, tree="", ref=candidate_ref))
            final = _clear_stage(
                RunRecord(**{**record.__dict__, "state": ENDED, "ending": NEGATIVE_RESULT})
            )
            _best_effort("final record", lambda: save_record(run_root, final, now), secrets)
            _notebook("no-improvement")
            return AttemptOutcome(run_id=run_id, outcome="no-improvement")

        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        # seal the notebook before any checkout mutates the persisted session
        # tree (the memory files are excluded from the sealed candidate and
        # would not survive the force-checkout + clean below)
        _notebook("improved")

        branch = f"{config.branch_prefix}/{run_id}"

        # IDEMPOTENCY: a prior wake may have opened the PR but died before
        # recording it (leaving the run WAITING). On re-entry, if a PR is already
        # open for this head->base, reconcile to it — do NOT re-push
        # (non-fast-forward) or open a duplicate. The reconcile does the FULL
        # terminal (branch checkout, arm, report, issue, in-review record); it
        # only SKIPS the push + create_pull the prior wake already did. A lookup
        # failure just falls through to the normal publish.
        existing: dict[str, object] | None = None
        try:
            existing = github.find_open_pull_for_head(config.target, branch, base_branch)
        except Exception as exc:
            log.warning(
                "idempotency PR lookup failed for %s: %s",
                run_id,
                redact(f"{type(exc).__name__}: {exc}", secrets),
            )
        if existing:
            pr_url = str(existing.get("html_url", ""))
            log.info("run %s: PR %s already open; reconciling the record", run_id, pr_url)
            # put the workspace on the branch (a later follow-up expects it) and
            # finish the steps the prior wake may have died before completing.
            _best_effort(
                "reconcile checkout", lambda: ws.git("checkout", "-f", "-B", branch, candidate_sha)
            )
            pr_number = pr_url.rstrip("/").rsplit("/", 1)[-1]
            if pr_number.isdigit() and not existing.get("draft"):
                _arm_unless_base_moved(
                    github,
                    ws,
                    config.target,
                    pr_number,
                    base_branch,
                    base_sha,
                    secrets,
                    merge_mode=getattr(contract, "merge", "manual"),
                    panel_ran=result.panel_rounds > 0,
                )
            report_path = run_dir / "report.md"
            _best_effort(
                "run report",
                lambda: report_path.write_text(result.report(config, redact_secrets=secrets)),
                secrets,
            )
            final = _clear_stage(
                RunRecord(
                    **{
                        **record.__dict__,
                        "state": IN_REVIEW,
                        "pr_url": pr_url,
                        "auto_blessed_head": _blessed_head(ws, result, contract),
                        "resume_session_id": result.session.session_id if result.session else "",
                        "ending_note": pr_url,
                    }
                )
            )
            if _best_effort("final record", lambda: save_record(run_root, final, now), secrets):
                drop_snapshot(ws, Snapshot(commit=candidate_sha, tree="", ref=candidate_ref))
            _post_issue_finished(
                github,
                config.target,
                issue_number,
                run_id,
                "improved",
                pr_url,
                redact(result.report(config, redact_secrets=secrets), secrets)[:8000],
                secrets,
            )
            return AttemptOutcome(
                run_id=run_id, outcome="improved", pr_url=pr_url, report_path=str(report_path)
            )

        try:
            # FORCE-checkout the sealed candidate: at wake the workspace still
            # holds the session's dirty tree (HEAD is pre_session_sha), so a
            # plain checkout could be blocked; the sha already captured exactly
            # the measured content.
            ws.git("checkout", "-f", "-B", branch, candidate_sha)
            # `checkout -f` does NOT remove untracked files, and the panel's
            # `git add -A` (in build_panel_runner) would sweep any post-snapshot
            # cruft into the tree it judges. Clean untracked (non-ignored) files
            # so the panel reads EXACTLY candidate_sha. The ledger commit stages
            # only PROGRESS_PATHS, so it was never affected.
            ws.git("clean", "-fd")

            # Verification panel on the credited claim — the SAME gate the
            # inline path runs (docs/design/orchestrator-verify.md), so a
            # dispatched improvement is not published unverified. It reads the
            # workspace tree, now checked out to the SEALED candidate_sha (the
            # dispatched evals ran on node-local scratch, so the tree is exactly
            # what was measured), over base_sha. A blocking or degraded
            # verdict opens a DRAFT PR carrying the findings and never arms
            # auto-merge; a clean verdict (or no panel) arms.
            if panel_lenses:
                # A panel ERROR (a git op in build_panel_runner, not a finding)
                # must NOT abort the publish and drop the candidate snapshot —
                # the improvement is real and measured. Fail closed to DEGRADED:
                # open a DRAFT for a human, keep the candidate. (run_panel itself
                # already fails closed per-lens; this catches the git setup.)
                try:
                    verdict = build_panel_runner(
                        ws,
                        run_dir,
                        base_sha,
                        panel_lenses,
                        contract_text,
                        config.target,
                        config.benchmark,
                        config.bot_login,
                        _dt.fromtimestamp(now, _UTC).strftime("%Y-%m-%d"),
                        exclude=(
                            LINE_MEMORY_PATHS if _line_ref_for(bench, config.agent_id) else ()
                        ),
                    )(baseline, candidate, str(stage.get("report", "")))
                except Exception as exc:
                    log.warning(
                        "wake panel errored for %s (%s); opening a DRAFT",
                        run_id,
                        redact(f"{type(exc).__name__}: {exc}", secrets),
                    )
                    verdict = PanelVerdict(
                        blocking=(),
                        transcript="panel setup failed — NOT a clean read",
                        wake_text="",
                        degraded=True,
                    )
                reads = panel_reads + 1
                # DEPTH AXIS (docs/design/research-loop.md): blocking findings
                # on a SUBMITTED claim go back to the AUTHOR (buildout Phase B)
                # — it revises and resubmits (a fresh seal + gate + panel), or
                # concludes. A plain finish (or an unresumable session) DRAFTs
                # the PR with the findings open for a human to triage.
                if bool(verdict.blocking) and submitted_park and author_resumable:
                    return _wake_author(verdict.wake_text)
                result = dc_replace(
                    result,
                    panel_transcript=verdict.transcript,
                    panel_rounds=reads,
                    panel_blocking_open=bool(verdict.blocking),
                    panel_degraded=verdict.degraded,
                )

            entries = update_leader(
                load_leader(workspace),
                benchmark=bench.name,
                metric=bench.metric,
                direction=bench.direction,
                baseline=baseline,
                candidate=candidate,
                run_id=run_id,
                date=datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d"),
                run_seed=result.run_seed,
            )
            write_progress(
                workspace,
                entries,
                config.target,
                digits={b.name: b.display_digits for b in contract.benchmarks if b.display_digits},
            )
            # Stage ONLY the ledger files on top of the sealed candidate — never
            # `git add -A`, which would sweep in untracked cruft the session left
            # (eval caches) that was neither measured nor scope-checked. The
            # candidate content is already vetted (measure_and_decide's scope
            # check on measured_paths); assert nothing but the ledger is staged.
            ws.git("add", "--", *PROGRESS_PATHS)
            staged = ws.staged_paths()
            extra = [p for p in staged if p not in PROGRESS_PATHS]
            if extra:
                raise WorkspaceDrift(f"wake commit would stage non-ledger paths: {extra[:10]}")
            # Commit the ledger update on top of the sealed candidate ONLY when
            # it actually moved. When the candidate beat its baseline but not the
            # recorded best, update_leader is a no-op (the ledger's `best` does
            # not advance) — a valid composable win with no leaderboard change,
            # so push the candidate as-is rather than an empty commit.
            if staged:
                ws.git(
                    "-c",
                    f"user.name={config.bot_login}",
                    "-c",
                    f"user.email={config.bot_login}@users.noreply.github.com",
                    "commit",
                    "-m",
                    f"agent: improve {config.benchmark} ({_title_pair(baseline, candidate)})"
                    f"\n\nAgent: {config.agent_id}",
                )
            ws.push(branch)
            body = pr_body(
                result, config, redact_secrets=secrets, display_digits=bench.display_digits
            )
            if issue_number:
                body = f"Addresses #{issue_number}.\n\n{body}"
            # blocking findings still open at the panel, or a degraded final
            # read, mean a human must look: open a DRAFT and never arm. A clean
            # verdict (or no panel configured) opens non-draft and arms
            # auto-merge only where branch protection requires a review — same
            # policy as the inline path.
            draft = result.panel_blocking_open or result.panel_degraded
            pr_url = github.create_pull(
                config.target,
                title=f"[agent] {config.benchmark}: {_title_pair(baseline, candidate)}",
                head=branch,
                base=base_branch,
                body=body,
                draft=draft,
            )
            pr_number = pr_url.rstrip("/").rsplit("/", 1)[-1]
            if pr_number.isdigit() and not draft:
                _arm_unless_base_moved(
                    github,
                    ws,
                    config.target,
                    pr_number,
                    base_branch,
                    base_sha,
                    secrets,
                    merge_mode=getattr(contract, "merge", "manual"),
                    panel_ran=result.panel_rounds > 0,
                )
        except Exception as exc:
            # push / PR / commit failed — end as an error. Save the ENDED record
            # BEFORE dropping the snapshot (same ordering as the other terminals):
            # a failed save then leaves the run WAITING with its snapshot intact
            # (recoverable), never WAITING with the candidate already gone. On a
            # successful end, drop the snapshot — ENDED runs are never swept, so
            # keeping it would only leak the ref (a retry is a fresh climb, not a
            # re-wake). Never delete a remote branch (a push may have
            # half-succeeded).
            note = redact(f"{type(exc).__name__}: {exc}", secrets)[:480]
            log.warning("wake publish failed for %s: %s", run_id, note)
            failed = _clear_stage(
                RunRecord(
                    **{**record.__dict__, "state": ENDED, "ending": ABORTED, "ending_note": note}
                )
            )
            if _best_effort("ending record", lambda: save_record(run_root, failed, now), secrets):
                drop_snapshot(ws, Snapshot(commit=candidate_sha, tree="", ref=candidate_ref))
            _notebook("publish-error")
            return AttemptOutcome(run_id=run_id, outcome="publish-error")
        # PR opened. Record IN_REVIEW *before* dropping the snapshot: if the save
        # fails, the record stays `waiting` with the snapshot intact, so the run
        # is recoverable rather than an ABORTED record over a live PR.
        report_path = run_dir / "report.md"
        _best_effort(
            "run report",
            lambda: report_path.write_text(result.report(config, redact_secrets=secrets)),
            secrets,
        )
        final = _clear_stage(
            RunRecord(
                **{
                    **record.__dict__,
                    "state": IN_REVIEW,
                    "pr_url": pr_url,
                    "auto_blessed_head": _blessed_head(ws, result, contract),
                    "resume_session_id": result.session.session_id if result.session else "",
                    "ending_note": pr_url,
                }
            )
        )
        if _best_effort("final record", lambda: save_record(run_root, final, now), secrets):
            drop_snapshot(ws, Snapshot(commit=candidate_sha, tree="", ref=candidate_ref))
        else:
            log.warning(
                "run %s: PR %s opened but in-review record unsaved; snapshot kept", run_id, pr_url
            )
        _post_issue_finished(
            github,
            config.target,
            issue_number,
            run_id,
            "improved",
            pr_url,
            redact(result.report(config, redact_secrets=secrets), secrets)[:8000],
            secrets,
        )
        return AttemptOutcome(
            run_id=run_id, outcome="improved", pr_url=pr_url, report_path=str(report_path)
        )

    # a negative terminal: end the record, THEN release the snapshot. Save
    # BEFORE dropping (same ordering as the improved path): if the save fails,
    # the run stays WAITING with its snapshot intact, so a re-wake can still
    # reconstruct — never WAITING with the snapshot already gone.
    _notebook(result.outcome)
    report_path = run_dir / "report.md"
    _best_effort(
        "run report",
        lambda: report_path.write_text(result.report(config, redact_secrets=secrets)),
        secrets,
    )
    final = _clear_stage(
        RunRecord(
            **{
                **record.__dict__,
                "state": ENDED,
                "ending": _ENDINGS_BY_OUTCOME[result.outcome],
                "ending_note": redact(result.note, secrets),
            }
        )
    )
    if _best_effort("final record", lambda: save_record(run_root, final, now), secrets):
        drop_snapshot(ws, Snapshot(commit=candidate_sha, tree="", ref=candidate_ref))
    else:
        log.warning(
            "run %s: ended negative but record unsaved; snapshot kept for a re-wake", run_id
        )
    _post_issue_finished(
        github,
        config.target,
        issue_number,
        run_id,
        result.outcome,
        "",
        redact(result.report(config, redact_secrets=secrets), secrets)[:8000],
        secrets,
    )
    return AttemptOutcome(run_id=run_id, outcome=result.outcome, report_path=str(report_path))


def _judge_lens_key(
    *,
    backend: str,
    key_file_env: str,
    author_backend: str,
    claude_panel_path: Path,
    image: str,
) -> str:
    """Resolve a non-claude judge lens's OWN key file, enforcing the three
    separations every shelled judge shares (codex, hermes, any future
    backend): the image is required (a judge never runs uncontained next to
    key files), the key must be named explicitly, and it must differ from BOTH
    the author's key of the same provider AND the claude panel key (a
    cross-provider send would leak an anthropic credential to another login).
    Returns the redacted key (or "" under an ADC-covered deployment)."""
    if not image:
        raise ValueError(
            f"a {backend} panel lens requires --image (a shelled judge only "
            "ever runs inside the container)"
        )
    raw = os.environ.get(key_file_env, "").strip()
    if not raw:
        raise ValueError(
            f"a {backend} panel lens needs {key_file_env} "
            "(role separation: the judge's own key, never the author's)"
        )
    path = Path(raw).expanduser()
    author_path = Path(resolve_author_key_file(author_backend)).expanduser()
    if path.resolve() == author_path.resolve():
        raise ValueError(
            f"{backend} panel key file {path} is the {author_backend} author key "
            "(role separation: the judge needs its own key)"
        )
    if path.resolve() == claude_panel_path.resolve():
        raise ValueError(
            f"{backend} panel key file {path} is the claude panel key file "
            "(an anthropic key must never reach another provider's login)"
        )
    return role_key(raw, author_backend)


def _panel_lenses_from_args(args: Any) -> tuple[tuple[PanelLens, ...], tuple[str, ...]]:
    """Build the verification-panel lenses from the CLI args (empty `--panel`
    disables it), returning `(lenses, panel_secrets)` — the ONE owner of
    panel credentials: each backend's judge key is read only when a lens uses
    it, role separation is enforced HERE (a manual climb gets the same rule
    as the tick preflight), and every key a judge holds joins the caller's
    redaction set via `panel_secrets`. Shared by the fresh-climb and the
    `--resume` wake paths so a dispatched improvement runs the SAME panel as
    an inline one. Raises ValueError on a bad panel/backend config — a
    configured gate must never silently vanish."""
    import os

    if not args.panel.strip():
        return (), ()
    from autoresearch.panel import parse_lenses
    from autoresearch.roles import reviewer_spec

    parsed = parse_lenses(args.panel)
    # the anthropic panel key is read only when a claude lens will use it —
    # a codex-only panel must not demand an unrelated credential
    panel_key = role_key(args.panel_key_file) if any(b == "claude" for _, b, _ in parsed) else ""
    lenses = []
    secrets: list[str] = [panel_key] if panel_key else []
    for kind, backend, model in parsed:
        hermes_repo_env = os.environ.get("REVIEW_HERMES_REPO", "").strip()
        # per-backend judge keys coexist — a codex lens is never handed the
        # anthropic panel key, and role separation forbids defaulting to the
        # AUTHOR's codex key: the judge key is its own, named explicitly
        claude_panel_path = Path(args.panel_key_file or PANEL_KEY_DEFAULT).expanduser()
        if backend == "codex":
            lens_key = _judge_lens_key(
                backend="codex",
                key_file_env="AUTORESEARCH_PANEL_CODEX_KEY_FILE",
                author_backend="codex",
                claude_panel_path=claude_panel_path,
                image=args.image,
            )
            if lens_key:
                secrets.append(lens_key)
        elif backend == "hermes":
            # hermes reads its key from its provider's env var, but the FILE
            # is resolved and separated exactly like codex's (the key still
            # lands next to the session). The author's OpenAI key coexists, so
            # separate against the codex author key.
            lens_key = _judge_lens_key(
                backend="hermes",
                key_file_env="AUTORESEARCH_PANEL_HERMES_KEY_FILE",
                author_backend="codex",
                claude_panel_path=claude_panel_path,
                image=args.image,
            )
            if lens_key:
                secrets.append(lens_key)
        else:
            lens_key = panel_key
        try:
            judge = build_harness(
                lens_key,
                reviewer_spec(),
                backend=backend,
                binary=args.claude_bin if backend == "claude" else args.codex_bin,
                model=model or None,
                # ALWAYS contained: the panel runs on the climb host next to key
                # files, and a judge now holds a shell (codex `danger-full-access`),
                # so it must run inside the image. `parse_lenses` gates panel
                # backends to those containable here (claude today); passing the
                # image unconditionally means codex is safe the moment it is
                # enabled, never accidentally uncontained.
                container_image=args.image,
                hermes_repo=Path(hermes_repo_env) if hermes_repo_env else None,
                hermes_provider=os.environ.get("REVIEW_HERMES_PROVIDER", "openrouter"),
            )
        except ValueError as exc:
            raise ValueError(f"panel entry {kind}:{backend}: {exc}") from exc
        lenses.append(PanelLens(kind=kind, harness=judge))
    return tuple(lenses), tuple(dict.fromkeys(secrets))


def _panel_claim_body(
    benchmark: str, baseline: float, candidate: float, report: str, *, lines: bool
) -> str:
    """The synthetic claim the panel judges. On a research-lines target the
    one-contribution mandate is part of the claim itself: the panel is the
    backstop against a line's accumulated tweaks reaching main as one PR
    (docs/design/research-lines.md)."""
    mandate = (
        "\n\nThis target runs research lines: a PR to main must be ONE "
        "clean contribution, extracted onto the base branch. A diff that "
        "bundles unrelated or unablated changes is a BLOCKING finding — "
        "name the pieces that should be separated."
        if lines
        else ""
    )
    return (
        f"Automated improvement claim (pre-PR): {benchmark} "
        f"{baseline} -> {candidate}, measured by the orchestrator.{mandate}\n\n"
        f"## Research report\n\n*Session prose, written before "
        f"the orchestrator measured.*\n\n{report[:MAX_CLAIM_CHARS]}"
    )


def build_panel_runner(
    ws: Workspace,
    run_dir: Path,
    base_sha: str,
    lenses: tuple[PanelLens, ...],
    contract_text: str,
    target: str,
    benchmark: str,
    bot_login: str,
    today: str,
    start_round: int = 0,
    exclude: tuple[str, ...] = (),
    claim_body: Callable[[float, float, str], str] | None = None,
) -> Callable[[float, float, str], PanelVerdict]:
    """The git half of the pre-PR panel: prepare the two read-only checkouts
    and the synthetic claim, then hand off to `run_panel` (which owns no git).

    Each call snapshots the CURRENT workspace tree as a detached commit and
    checks it out as `pr-head/` (sanitized — the candidate is an untrusted
    tree), next to `base/` (the trusted pre-session commit: contract and
    ruler). Worktrees are removed after the read; a fresh pair is built per
    round because the tree changes with every revision.

    `claim_body` renders the claim the panel judges from (baseline,
    candidate, report); the default is the pre-PR improvement claim, a
    follow-up re-read passes its own wording."""
    from autoresearch.review_agent import sanitize_checkout

    reads = {"n": start_round}
    render_claim = claim_body or (
        lambda baseline, candidate, report: _panel_claim_body(
            benchmark, baseline, candidate, report, lines=bool(exclude)
        )
    )

    def runner(baseline: float, candidate: float, report: str) -> PanelVerdict:
        reads["n"] += 1
        panel_ws = run_dir / "panel"
        shutil.rmtree(panel_ws, ignore_errors=True)
        panel_ws.mkdir(parents=True, exist_ok=True)
        ws.git("add", "-A")
        if exclude:
            # the panel judges the CLAIM — the same tree the gate measured,
            # which excludes line memory (docs/design/research-lines.md)
            ws.git("rm", "--cached", "-r", "-q", "--ignore-unmatch", "--", *exclude)
        tree = ws.git("write-tree").strip()
        ws.git("reset")
        snapshot = ws.git(
            "-c",
            "user.name=panel",
            "-c",
            "user.email=panel@localhost",
            "commit-tree",
            tree,
            "-p",
            base_sha,
            "-m",
            "panel snapshot (never pushed)",
        ).strip()
        try:
            ws.git("worktree", "add", "--detach", str(panel_ws / "base"), base_sha)
            ws.git("worktree", "add", "--detach", str(panel_ws / "pr-head"), snapshot)
            _renamed, failed = sanitize_checkout(panel_ws / "pr-head")
            if failed:
                # fail closed for the read, loudly in the transcript: an
                # unsanitizable tree is never judged, and never certified
                return PanelVerdict(
                    blocking=(),
                    transcript=(
                        f"**Verification round {reads['n']}**\n- panel skipped: "
                        f"the candidate tree could not be sanitized "
                        f"({failed} instruction file(s) left) — NOT a clean read"
                    ),
                    wake_text="",
                    degraded=True,
                )
            claim = PullRequest(
                repo=target,
                number=0,
                title=f"[agent] {benchmark}: {_title_pair(baseline, candidate)}",
                body=render_claim(baseline, candidate, report),
                # base..snapshot, never base..worktree: the snapshot commit
                # includes newly ADDED files, which a working-tree diff omits.
                # Excluded (line-memory) paths are excluded from the diff too:
                # the snapshot dropped them, so against a line-tip base they
                # would read as deletions the author never made
                diff=ws.git(
                    "diff",
                    f"{base_sha}..{snapshot}",
                    *(["--", ".", *(f":(exclude){p}" for p in exclude)] if exclude else []),
                ),
                author=bot_login,
            )
            return run_panel(lenses, panel_ws, claim, contract_text, today, reads["n"])
        finally:
            for name in ("base", "pr-head"):
                _best_effort(
                    "panel worktree cleanup",
                    partial(ws.git, "worktree", "remove", "--force", str(panel_ws / name)),
                )
            _best_effort("panel dir removal", lambda: shutil.rmtree(panel_ws, ignore_errors=True))
            _best_effort("panel worktree prune", lambda: ws.git("worktree", "prune"))

    return runner


def live_attempt(
    config: RunConfig,
    run_root: Path,
    run_id: str,
    harness: Harness,
    github: GitHubClient,
    bot_auth: TokenProvider,
    now: float,
    created: str,
    secrets: tuple[str, ...] = (),
    base_branch: str = "main",
    issue_number: int = 0,
    author_backend: str = "claude",
    author_model: str = "",
    author_key_file: str = "",
    task_hypothesis: str = "",
    spec: RoleSpec | None = None,
    panel_lenses: tuple[PanelLens, ...] = (),
    dispatch: DispatchSettings | None = None,
    eval_image: str = "",
) -> AttemptOutcome:
    """Run one climb against the real target repo. With `panel_lenses`, the
    pre-PR verification panel gates the claim before any PR exists
    (docs/design/orchestrator-verify.md); blocking findings still open at
    the cap open a DRAFT PR carrying them."""
    run_dir = run_root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace = run_dir / "ws"

    # The record exists before any network or clone work: every crash from
    # here on has a record to end.
    import os as _os

    record = RunRecord(
        run_id=run_id,
        target=config.target,
        task_title=f"improve {config.benchmark}",
        benchmark=config.benchmark,
        state="implementing",
        agent_id=config.agent_id,
        deadline=now + 24 * 3600,
        issue_number=issue_number,
        author_backend=author_backend,
        author_model=author_model,
        author_key_file=author_key_file,
        run_job_id=_os.environ.get("SLURM_JOB_ID", ""),
    )
    try:
        save_record(run_root, record, now)
    except Exception as exc:
        # No record could be written, so the run must not proceed invisibly:
        # nothing would ever end it. Submit-time evidence (the claim comment
        # or the pending marker) plus this post keep the failure visible.
        exc_name = type(exc).__name__
        log.warning(
            "could not create run record for %s: %s",
            run_id,
            redact(f"{exc_name}: {exc}", secrets),
        )
        if issue_number:
            _best_effort(
                "issue report",
                lambda: github.comment(
                    config.target,
                    issue_number,
                    f"Run `{run_id}` could not start ({exc_name} while writing its run record).",
                ),
                secrets,
            )
        return AttemptOutcome(run_id=run_id, outcome="attempt-error")

    # what the attempt-error handler needs to salvage the line notebook: the
    # exception path cannot rely on names bound inside the try
    salvage: dict[str, object] = {}
    try:
        ws = Workspace.clone(_target_clone_url(config.target), workspace, auth=bot_auth)
        # Build ON the requested PR base: the clone checks out the remote
        # DEFAULT branch, which need not be `base_branch` — the session must
        # edit, and the gate must measure, the tree the PR will land on.
        # A missing base branch fails loudly as attempt-error.
        ws.git("checkout", "-q", "-B", base_branch, f"origin/{base_branch}")
        _exclude_merge_artifacts(workspace)
        contract_text = (workspace / ".autoresearch.yaml").read_text()
        contract = load_contract(contract_text, config.target)
        # Load the brief budget from the contract and run state: callers do
        # not supply it (the dataclass default rendered "0.0 GPU-hours" and
        # honest agents refused to launch). Same weekly counting rule as the
        # tick's cap: records plus live pending markers, minus this run's own.
        from autoresearch.tick import list_pendings

        week_ago = now - 7 * 24 * 3600
        recent = [
            r for r in list_runs(run_root) if r.target == config.target and r.created >= week_ago
        ]
        # a marker whose job already has a record (this run's included) is
        # the same attempt, not a second one — count each job once
        recorded_jobs = {r.run_job_id for r in recent if r.run_job_id} | {record.run_job_id}
        # every unrecorded week-fresh marker counts: the tick reaps dead
        # markers on its own cadence (with the squeue liveness reads a brief
        # must not make), so an unreaped marker is either a live queued run
        # the weekly cap WILL count, or dead for at most a sweep — the brief
        # stays on the cap's conservative side either way
        used_week = len(recent) + sum(
            1
            for _agent, marker in list_pendings(run_root, config.target)
            if float(marker.get("submitted_at", 0) or 0) >= week_ago
            and str(marker.get("job_id", "")) not in recorded_jobs
        )
        _budget_bench = next((b for b in contract.benchmarks if b.name == config.benchmark), None)
        config = dc_replace(
            config,
            budget=BudgetState(
                gpu_hours_remaining=(
                    float(contract.budgets.gpu_hours_per_run or 0.0)
                    if _budget_bench is not None and _budget_bench.gpus
                    else 0.0
                ),
                runs_remaining_this_week=max(0, int(contract.budgets.runs_per_week) - used_week),
            ),
        )
        # Author syscalls (research-loop.md, "one syscall") are CONTRACT-DRIVEN:
        # armed whenever the deployment can deliver them — dispatch coords (the
        # launches and the gate run as Slurm jobs) and a resumable backend (the
        # wake resumes the SAME session) — and the benchmark has not opted out
        # (`depth_k: 0`). With the channel (`.autoresearch/`) armed it never
        # enters diffs or scope — repo-local exclude. With the feature off, an
        # untracked `.autoresearch/` file must be staged and judged like any
        # other agent edit, not silently hidden by a magic dir name (the off
        # state stays byte-identical).
        _bench = next((b for b in contract.benchmarks if b.name == config.benchmark), None)
        # Research lines: move HEAD to the agent's own branch BEFORE anything
        # reads the tree — the contract above came from the base branch (a
        # line must not shape its own budgets), and the syscall-channel check
        # below must see the line's tree. A failed checkout falls back to the
        # base branch: a run is never lost to its notebook.
        lines_active = _bench is not None and _bench.lines and bool(config.agent_id)
        line_ref = ""
        if lines_active:
            try:
                line_ref = _checkout_line(ws, workspace, config.agent_id, base_branch)
            except Exception as exc:
                log.warning(
                    "line checkout failed (%s); running on %s",
                    redact(f"{type(exc).__name__}: {exc}", secrets),
                    base_branch,
                )
                _best_effort("line merge abort", lambda: ws.git("merge", "--abort"))
                ws.git("checkout", "-q", "-B", base_branch, f"origin/{base_branch}")
        line_memory = ""
        line_divergence = ""
        if line_ref:
            salvage.update(ws=ws, line_ref=line_ref)
            try:
                # the line's own memory index, rendered into the brief
                # (data-fenced there); topic files are read on demand from
                # the checkout, never rendered
                memory_path = workspace / "AGENT_MEMORY.md"
                if memory_path.is_file() and not memory_path.is_symlink():
                    # byte-mode bounded read: never load an oversized file
                    with memory_path.open("rb") as fh:
                        line_memory = fh.read(65_536).decode("utf-8", errors="replace")
            except OSError as exc:
                log.warning("could not read AGENT_MEMORY.md: %s", exc)
            try:
                # divergence debt, made visible each session (a conflicted
                # merge skips it — the diff is not meaningful mid-merge)
                if not ws.git("diff", "--name-only", "--diff-filter=U").strip():
                    line_divergence = ws.git(
                        "diff", "--shortstat", f"origin/{base_branch}", "HEAD"
                    ).strip()
            except Exception:
                line_divergence = ""
        author_syscalls = (
            dispatch is not None
            and getattr(harness, "supports_resume", True)
            and _bench is not None
            and _bench.depth_k > 0
        )
        # The `.autoresearch/` channel must be KERNEL-OWNED. In a fresh clone,
        # anything already at that path was committed by the TARGET — a symlink
        # (install would write through it to a host path with our permissions),
        # a tracked request (free cluster compute), or
        # any other booby trap. If the path pre-exists in ANY form, disable the
        # feature for the run, loudly; otherwise we create a dir we own.
        channel = workspace / SYSCALL_DIR
        if author_syscalls and (channel.is_symlink() or channel.exists()):
            log.warning(
                "target ships a %s path (symlink=%s); author syscalls disabled for this run",
                SYSCALL_DIR,
                channel.is_symlink(),
            )
            author_syscalls = False
        reports = _fetch_research_reports(ws, MAX_ARCHIVED_REPORTS)
        if author_syscalls:
            assert _bench is not None
            syscall_excluded(workspace)
            # the author's interface is the TOOL (`python .autoresearch/syscall
            # launch ... -- <cmd>`; `... sleep`), never the raw ABI file —
            # install it plus the informational budget its `status` shows.
            syscall_install_tool(workspace)
            syscall_write_budget(
                workspace,
                launches_remaining=_bench.depth_k,
                sleeps_remaining=_bench.sleep_k,
                gpu_hours_remaining=(
                    float(contract.budgets.gpu_hours_per_run) if _bench.gpus else None
                ),
            )
            # AFTER install_tool: installing the tool recreates the channel
            # dir it owns, which would delete an archive written earlier
            _install_report_archive(workspace, reports)
            # the fleet snapshot the `siblings` command shows — read from the
            # research-log's status.json (the SAME branch the reports came
            # from, so it works across clusters) and best-effort throughout:
            # a missing or malformed snapshot just means no siblings known
            syscall_write_siblings(workspace, _sibling_entries(ws, config.agent_id))

        def changed_paths() -> list[str]:
            ws.git("add", "-A")
            paths = ws.staged_paths()
            ws.git("reset")
            if lines_active:
                paths = [p for p in paths if not _is_line_memory(p)]
            return paths

        if issue_number:
            from autoresearch.intake import CLAIM_MARKER

            already = any(
                CLAIM_MARKER in str(c.get("body", ""))
                for c in github.list_comments(config.target, issue_number)
            )
            if not already:  # manual CLI runs claim here; tick runs claimed at submit
                github.comment(
                    config.target,
                    issue_number,
                    f"{CLAIM_MARKER}\nPicked up as run `{run_id}` "
                    f"(benchmark `{config.benchmark}`). A report will follow here.",
                )

        # Expensive benchmarks measure as dispatched cluster jobs; cheap ones
        # (and any run with no cluster coordinates) measure inline. The choice
        # is the benchmark's eval-time hint against the in-job runway, decided
        # ONCE here so the baseline setup, the measurer, and the park deadline
        # all agree on it.
        eval_minutes = next(
            (b.eval_minutes for b in contract.benchmarks if b.name == config.benchmark), None
        )
        wants_dispatch = should_dispatch(eval_minutes)
        dispatched = dispatch is not None and wants_dispatch
        if wants_dispatch and dispatch is None:
            # a benchmark asked to be dispatched but no cluster coordinates
            # reached us — never silently: name it, then measure inline
            log.warning(
                "benchmark %s wants dispatched eval (eval_minutes=%s) but no cluster "
                "coordinates (image/account/partition) are set; measuring inline",
                config.benchmark,
                eval_minutes,
            )

        # the panel's base is the PRE-SESSION commit — the exact tree the
        # baseline was measured on — never origin/<base_branch>, which can
        # name a different branch than the clone's checkout
        pre_session_sha = ws.git("rev-parse", "HEAD").strip()
        panel_runner = (
            build_panel_runner(
                ws,
                run_dir,
                pre_session_sha,
                panel_lenses,
                contract_text,
                config.target,
                config.benchmark,
                config.bot_login,
                created[:10],
                exclude=LINE_MEMORY_PATHS if lines_active else (),
            )
            if panel_lenses
            else None
        )
        # ONE measurer either way: every measure is a job that checks its
        # tree sha out fresh from this workspace's `refs/dispatch/*` and
        # writes its result to the run dir. DISPATCHED: the jobs go to the
        # cluster and a not-yet-done measure PARKS the climb. LOCAL: the SAME
        # jobs run synchronously in this allocation (LocalCompute), so every
        # measure is done when checked and nothing parks. `snapshot` commits
        # the workspace's current content to a candidate sha and we own the
        # ref lifecycle, keeping the one candidate ref a park needs and
        # dropping the rest when the climb ends.
        measurer: Measurer
        if dispatched:
            assert dispatch is not None and eval_minutes is not None  # should_dispatch(None) False
            measurer = dispatch.measurer(
                run_dir, repo_root=workspace, eval_minutes=eval_minutes, run_tag=run_id
            )
        else:
            measurer = DispatchedMeasurer(
                compute=LocalCompute(),
                run_dir=run_dir,
                repo_root=workspace,
                # a configured image contains LOCAL evals too — the cluster
                # triple being incomplete must not silently drop the jail
                image=dispatch.image if dispatch is not None else eval_image,
                account="",
                partition="",
                eval_minutes=int(eval_minutes or 0),
                run_tag=run_id,
                # an inline gate shares the same target-wide baseline cache
                baseline_cache=run_dir.parent / "baselines",
            )
        snapshots: list[Snapshot] = []

        def snapshot() -> str:
            snap = snapshot_tree(
                ws, pre_session_sha, exclude=LINE_MEMORY_PATHS if lines_active else ()
            )
            snapshots.append(snap)
            return snap.commit

        # `author_syscalls` already folds every enablement condition — dispatch
        # coords, a resumable backend, the benchmark's opt-out, and the
        # channel-ownership guard above (a target-shipped `.autoresearch` —
        # symlink, tracked request, or any other pre-existing form — has
        # disabled the feature for this run).
        launcher = None
        if author_syscalls:
            assert dispatch is not None  # folded into author_syscalls above
            launcher = _make_launcher(
                dispatch, run_dir, workspace, run_id, gpus=_bench.gpus if _bench else 0
            )

        parked: RunParked | None = None
        kept_ref = ""  # the ONE candidate snapshot ref that must outlive a park
        try:
            # the last-known score orients the brief only; the gate re-measures
            # both sides after the session, so None (a first run) is fine.
            prior_best = load_leader(workspace).get(config.benchmark)
            result = attempt_once(
                config,
                contract_text,
                workspace,
                harness,
                measurer,
                pre_session_sha,
                snapshot,
                ruler=RULER,
                changed_paths=changed_paths,
                created=created,
                task_hypothesis=task_hypothesis,
                recent_reports=tuple(text for _name, text in reports),
                lessons=distill_lessons(reports),
                report_archive=author_syscalls,
                spec=spec,
                panel_runner=panel_runner,
                brief_baseline=prior_best.best if prior_best else None,
                line_ref=line_ref,
                line_memory=line_memory,
                line_divergence=line_divergence,
                launcher=launcher,
                tree_of=lambda sha: ws.git("rev-parse", f"{sha}^{{tree}}").strip(),
            )
        except RunParked as p:
            # The climb dispatched its measures and hibernated. Persist the
            # re-entry stage as a WAITING record (not an error), keep the
            # candidate snapshot alive for the wake, and end. The wake re-enters
            # from the record. `parked` is set only
            # AFTER a successful write: if _park_run raises, it stays None so the
            # finally drops every snapshot (no leak) and the outer handler ends
            # the run as an error rather than a half-written hibernation.
            if p.phase in ("candidate", "author-sleep"):
                # keep exactly ONE snapshot for that sha (two can share a
                # commit); record and keep that same ref, drop the rest. An
                # author-sleep's snapshot is the tree the wake re-delivers.
                kept_ref = next((s.ref for s in snapshots if s.commit == p.candidate_sha), "")
            import time

            # anchor the deadline to the PARK (when the evals were submitted),
            # not the run's start `now` — a session lasting hours would otherwise
            # eat the queue budget and let the sweep cancel a still-queued eval.
            try:
                _park_run(
                    run_root,
                    record,
                    p,
                    kept_ref,
                    eval_minutes,
                    time.time(),
                    secrets,
                    dispatch=dispatch,
                    base_branch=base_branch,
                )
            except Exception:
                # The WAITING record did not persist, so nothing will ever wake
                # the eval jobs this park already submitted. Cancel them so they
                # don't sit in the queue as orphans (best-effort, self-logging),
                # then fall through to the error handler — `parked` stays None,
                # so the finally still drops every snapshot. A park only happens
                # on the dispatched path, so `dispatch` is set here.
                assert dispatch is not None
                for job_id in afterany_ids(p.afterany):
                    dispatch.compute.cancel(job_id)
                raise
            parked = p
            return AttemptOutcome(run_id=run_id, outcome="parked")
        finally:
            for snap in snapshots:
                # a candidate park must OUTLIVE the wake — keep the ONE recorded
                # snapshot (matched by ref, not commit); drop every other one.
                if parked and kept_ref and snap.ref == kept_ref:
                    continue
                drop_snapshot(ws, snap)  # best-effort + self-logging; never raises
    except Exception as exc:
        exc_name = type(exc).__name__
        note = redact(f"{exc_name}: {exc}", secrets)[:500]
        log.warning("climb failed for %s: %s", run_id, note)
        if salvage:
            # a crashed attempt's tree is still notebook-worthy (best-effort)
            _push_line_snapshot(
                cast(Workspace, salvage["ws"]),
                str(salvage["line_ref"]),
                run_id,
                "attempt-error",
                secrets,
            )
        failed = RunRecord(
            **{
                **record.__dict__,
                "state": ENDED,
                "ending": ABORTED,
                "ending_note": note,
            }
        )
        report_path = run_dir / "report.md"
        _best_effort("ending record", lambda: save_record(run_root, failed, now), secrets)
        wrote = _best_effort(
            "error report",
            lambda: report_path.write_text(
                f"# Run report — {config.target} / {config.benchmark}\n"
                f"Outcome: **attempt-error**\n"
                f"Note: {note}\n"
            ),
            secrets,
        )
        if issue_number:
            # Exception detail stays in the local record and report: redact()
            # only knows the secrets it was handed, and raw messages can carry
            # paths or tokens the tuple does not cover. The issue gets the
            # exception TYPE only.
            _best_effort(
                "issue report",
                lambda: github.comment(
                    config.target,
                    issue_number,
                    f"Run `{run_id}` finished (attempt-error): {exc_name}. "
                    f"Details are in the run's record and report on the orchestrator.",
                ),
                secrets,
            )
        return AttemptOutcome(
            run_id=run_id,
            outcome="attempt-error",
            # an outcome must never point at a report that was not written
            report_path=str(report_path) if wrote else "",
        )

    if result.outcome == "improved" and not result.measured_paths:
        # a zero-change "improvement" is metric noise, not progress — never a
        # PR (same rule as the wake publish)
        result = dc_replace(result, outcome="no-improvement", note="no code change; metric noise")

    report = result.report(config, redact_secrets=secrets)
    report_path = run_dir / "report.md"
    wrote_report = _best_effort("run report", lambda: report_path.write_text(report), secrets)

    # Research lines: seal the notebook NOW, while the tree is still the
    # session's final tree — the publish below force-checkouts the sealed
    # candidate and cleans untracked files, which would drop the agent's
    # memory (it is excluded from measurable seals by design). The label is
    # the GATE outcome, correct at this moment; a publish failure appends a
    # publish-error snapshot at the tail.
    _push_line_snapshot(ws, line_ref, run_id, result.outcome, secrets)

    pr_url = ""
    outcome_name = result.outcome
    branch = ""
    pushed = False
    if result.outcome == "improved":
        try:
            # Publish the SEALED candidate sha — never the live tree, which
            # may have drifted since the snapshot (eval caches, stray writes);
            # the sha is exactly the measured, scope-checked content. A base
            # branch that moved during the climb is NOT merged and re-measured
            # here: a stale PR is review's to handle (research-loop.md).
            branch = f"{config.branch_prefix}/{run_id}"
            if result.baseline is None or result.candidate is None or not result.candidate_sha:
                raise EvalError("improved result missing measurements or the sealed sha")
            bench = next(b for b in contract.benchmarks if b.name == config.benchmark)
            baseline, candidate = result.baseline, result.candidate
            # FORCE-checkout: the workspace still holds the session's dirty
            # tree. The snapshot commit is anchored by the new branch (the
            # dropped dispatch ref left it unreferenced; nothing pruned it in
            # this process). clean -fd drops post-snapshot cruft so the
            # pushed tree is exactly candidate_sha plus the ledger commit.
            ws.git("checkout", "-f", "-B", branch, result.candidate_sha)
            ws.git("clean", "-fd")
            entries = update_leader(
                load_leader(workspace),
                benchmark=bench.name,
                metric=bench.metric,
                direction=bench.direction,
                baseline=baseline,
                candidate=candidate,
                run_id=run_id,
                date=created[:10],
                run_seed=result.run_seed,
            )
            write_progress(
                workspace,
                entries,
                config.target,
                digits={b.name: b.display_digits for b in contract.benchmarks if b.display_digits},
            )
            # Stage ONLY the ledger files on top of the sealed candidate —
            # never `git add -A`, which would sweep in anything a session or
            # eval left behind (same rule as the wake publish).
            ws.git("add", "--", *PROGRESS_PATHS)
            staged = ws.staged_paths()
            extra = [p for p in staged if p not in PROGRESS_PATHS]
            if extra:
                raise WorkspaceDrift(f"publish would stage non-ledger paths: {extra[:10]}")
            if staged:
                ws.git(
                    "-c",
                    f"user.name={config.bot_login}",
                    "-c",
                    f"user.email={config.bot_login}@users.noreply.github.com",
                    "commit",
                    "-m",
                    f"agent: improve {config.benchmark} ({_title_pair(baseline, candidate)})"
                    f"\n\nAgent: {config.agent_id}",
                )
            ws.push(branch)
            pushed = True
            body = pr_body(
                result, config, redact_secrets=secrets, display_digits=bench.display_digits
            )
            if issue_number:
                body = f"Addresses #{issue_number}.\n\n{body}"
            pr_url = github.create_pull(
                config.target,
                # short precision in the title; full precision lives in the
                # PR body table and the ledger
                title=f"[agent] {config.benchmark}: {_title_pair(baseline, candidate)}",
                head=branch,
                base=base_branch,
                body=body,
                # blocking findings open at the panel, or a degraded final
                # read: visible, plainly not merge-ready
                draft=result.panel_blocking_open or result.panel_degraded,
            )
            # Arm auto-merge, best-effort, and ONLY when branch protection
            # requires a human review — the guard keeps bot-never-merges
            # enforced in code, not in per-repo config. Never arm a draft,
            # and never arm a claim whose base has moved (_arm_unless_base_moved).
            pr_number = pr_url.rstrip("/").rsplit("/", 1)[-1]
            if pr_number.isdigit() and not (result.panel_blocking_open or result.panel_degraded):
                _arm_unless_base_moved(
                    github,
                    ws,
                    config.target,
                    pr_number,
                    base_branch,
                    pre_session_sha,
                    secrets,
                    merge_mode=getattr(contract, "merge", "manual"),
                    panel_ran=result.panel_rounds > 0,
                )
            final = RunRecord(
                **{
                    **record.__dict__,
                    "state": IN_REVIEW,
                    "pr_url": pr_url,
                    "auto_blessed_head": _blessed_head(ws, result, contract),
                    "resume_session_id": result.session.session_id if result.session else "",
                    "ending_note": pr_url,
                }
            )
        except Exception as exc:
            log.warning(
                "publish failed for %s: %s",
                run_id,
                redact(f"{type(exc).__name__}: {exc}", secrets),
            )
            # Never delete the remote branch: an exception from create_pull
            # does not prove no PR exists (a 422-already-exists or a timeout
            # after a successful POST both land here), and deleting the ref
            # would close such a PR and discard the only pushed copy. Leave
            # it and record it; a sweeper can reap confirmed orphans later.
            outcome_name = "publish-error"
            final = RunRecord(
                **{
                    **record.__dict__,
                    "state": ENDED,
                    "ending": ABORTED,
                    "ending_note": (
                        (f"branch left on remote: {branch}; " if pushed else "")
                        + redact(f"{type(exc).__name__}: {exc}", secrets)[:480]
                    ),
                }
            )
    else:
        if result.outcome == "session-outage":
            _best_effort(
                "outage stamp",
                lambda: stamp_outage(run_root, redact(result.note, secrets)[:300], now),
                secrets,
            )
        final = RunRecord(
            **{
                **record.__dict__,
                "state": ENDED,
                "ending": _ENDINGS_BY_OUTCOME[result.outcome],
                "ending_note": redact(result.note, secrets),
            }
        )
    if not _best_effort("final record", lambda: save_record(run_root, final, now), secrets):
        # The on-disk record still says `implementing`, so automated
        # follow-up servicing will not track this run — and if a PR was
        # opened, its humans are the only ones who can act. Say so WHERE
        # they are looking: GitHub is the one store still writable when the
        # local disk is gone.
        pr_number = pr_url.rstrip("/").rsplit("/", 1)[-1] if pr_url else ""
        if pr_number.isdigit():
            _best_effort(
                "pr state warning",
                lambda: github.comment(
                    config.target,
                    int(pr_number),
                    f"State record for run `{run_id}` could not be saved; "
                    f"automated follow-up servicing is offline for this run. "
                    f"A maintainer owns any follow-ups on this PR.",
                ),
                secrets,
            )
    if issue_number:
        _post_issue_finished(
            github,
            config.target,
            issue_number,
            run_id,
            outcome_name,
            pr_url,
            redact(result.report(config, redact_secrets=secrets), secrets)[:8000],
            secrets,
        )
    if outcome_name != result.outcome:
        # the publish failed after the gate credited the tree: the improved
        # snapshot above stands (the measurement was real); append the
        # publish-error marker so the notebook records how the run ended
        _push_line_snapshot(ws, line_ref, run_id, outcome_name, secrets)
    log.info("run %s: %s %s", run_id, outcome_name, pr_url)
    return AttemptOutcome(
        run_id=run_id,
        outcome=outcome_name,
        pr_url=pr_url,
        report_path=str(report_path) if wrote_report else "",
    )


class Terminated(Exception):
    """Slurm sent SIGTERM (walltime, preemption, scancel): raised into the
    main thread so the ordinary exception containment ends the run inside
    the KillWait grace window before SIGKILL arrives."""


# Below this, arming is pointless: the alarm would fire during setup,
# outside containment, and a job this short cannot finish a climb anyway.
MIN_ARM_S = 180


def arm_self_deadline(job_minutes: int, margin_s: float = 120.0) -> int:
    """Arm our own end-of-walltime alarm; returns the armed seconds (0 = off).

    Slurm delivers NO signal to our process on Torch before SIGKILL
    (scancel and walltime timeout both signal the
    batch shell only) — so the only way to end a run richly before the
    wall is our own clock. SIGALRM fires `margin_s` before the job's
    walltime and raises Terminated into the ordinary containment; the
    margin floor covers the containment's own tail (GitHub calls are 30s
    timeout x retries). The walltime clock starts at JOB start, not
    process start — SLURM_JOB_START_TIME anchors the deadline when
    present so startup latency erodes the runway, never the margin.
    """
    if job_minutes <= 0:
        return 0
    import signal
    import time as _time

    margin = max(60.0, margin_s)
    now = _time.time()
    start_raw = os.environ.get("SLURM_JOB_START_TIME", "")
    # Sanity-bounded: the env can carry a STALE value inherited from the
    # submitting job (tick jobs sbatch climb jobs). A start time outside
    # [now - walltime, now] is not this job's — fall back to the process
    # clock rather than silently disarm (past) or overshoot the wall
    # (future).
    if start_raw.isdigit() and now - job_minutes * 60 <= int(start_raw) <= now:
        remaining = int(int(start_raw) + job_minutes * 60 - margin - now)
    else:
        remaining = int(job_minutes * 60 - margin)
    if remaining < MIN_ARM_S:
        log.warning(
            "self-deadline NOT armed: %ds runway is below the %ds floor", remaining, MIN_ARM_S
        )
        return 0

    def _on_alarm(signum: int, frame: object) -> None:
        raise Terminated(
            f"self-deadline: {margin:.0f}s before the job's {job_minutes}-minute walltime"
        )

    signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(remaining)
    return remaining


def arm_sigterm_containment() -> None:
    """Convert the FIRST SIGTERM into a Terminated exception, one-shot.

    Repeats are absorbed by a flag rather than SIG_IGN: a second SIGTERM
    (repeated scancel, site KillWait re-sends) must not abort the very
    containment the first one enabled — and SIG_IGN would be inherited
    across exec by children spawned during containment, leaving them
    unkillable by TERM. A Python-level handler is reset on exec, so
    children keep default signal behavior.
    """
    import signal

    fired = {"done": False}

    def _on_sigterm(signum: int, frame: object) -> None:
        if fired["done"]:
            return  # containment already unwinding; absorb the repeat
        fired["done"] = True
        raise Terminated("SIGTERM from Slurm (walltime, preemption, or scancel)")

    signal.signal(signal.SIGTERM, _on_sigterm)


def main() -> int:
    import argparse
    import os
    import time
    from datetime import UTC, datetime

    arm_sigterm_containment()

    parser = argparse.ArgumentParser(description="One live attempt on one benchmark.")
    # --target/--benchmark drive a fresh climb; they are read from the record
    # on a --resume wake instead, so they are optional (validated below).
    parser.add_argument("--target", default="")
    parser.add_argument("--benchmark", default="")

    # WIDTH: the tick assigns each concurrent slot its own agent identity;
    # branches, ledger rows, and reports key on it. Resumed runs inherit
    # the identity from their record instead.
    def _agent_id(value: str) -> str:
        # the id shapes refs (feat/auto/<id>/<run>, agents/<id>): slug only —
        # same rule the line checkout enforces
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
            raise argparse.ArgumentTypeError(
                f"agent id {value!r} cannot shape a git ref (want [A-Za-z0-9][A-Za-z0-9_-]*)"
            )
        return value

    parser.add_argument("--agent-id", default="agent-01", type=_agent_id)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument(
        "--resume",
        default="",
        metavar="RUN_ID",
        help="wake a parked dispatched run instead of starting a fresh climb",
    )
    parser.add_argument("--base-branch", default="main")
    # All three default from the chain env the tick sets on the climb job, so
    # a contained run with AUTORESEARCH_{IMAGE,ACCOUNT,PARTITION} set selects
    # dispatched measurement without extra flags. The image also containers the
    # session + inline eval; absent any of the three, measurement stays inline
    # regardless of the benchmark's eval hint.
    parser.add_argument(
        "--image",
        default=os.environ.get("AUTORESEARCH_IMAGE", ""),
        help="apptainer image for session+eval",
    )
    parser.add_argument("--account", default=os.environ.get("AUTORESEARCH_ACCOUNT", ""))
    parser.add_argument("--partition", default=os.environ.get("AUTORESEARCH_PARTITION", ""))
    # the GPU lane for benchmarks with `gpus > 0` (evals + author launches);
    # empty = this deployment cannot place GPU jobs
    parser.add_argument("--gpu-partition", default=os.environ.get("AUTORESEARCH_GPU_PARTITION", ""))
    parser.add_argument("--gpu-account", default=os.environ.get("AUTORESEARCH_GPU_ACCOUNT", ""))
    parser.add_argument(
        "--uncontained",
        action="store_true",
        help="run WITHOUT a container (dev only: sessions can then read "
        "same-user files, including credential files)",
    )
    parser.add_argument("--claude-bin", default=os.path.expanduser("~/.local/bin/claude"))
    parser.add_argument(
        "--codex-bin",
        default=os.path.expanduser(
            os.environ.get("AUTORESEARCH_CODEX_BIN") or "~/.local/bin/codex"
        ),
        help="host codex binary for the codex author; bind-mounted into apptainer "
        "(must be an absolute path).",
    )
    parser.add_argument(
        "--model", default=os.environ.get("AUTORESEARCH_AUTHOR_MODEL") or "claude-opus-5"
    )
    parser.add_argument(
        "--author-backend",
        choices=("claude", "codex"),
        default=os.environ.get("AUTORESEARCH_AUTHOR_BACKEND") or "claude",
        help="agent backend for the author/editor role (config-driven: default "
        "from AUTORESEARCH_AUTHOR_BACKEND). codex runs contained (apptainer + "
        "--sandbox danger-full-access) and REQUIRES --image and a codex/openai "
        "--model (e.g. gpt-5.6-terra).",
    )
    parser.add_argument(
        "--codex-config",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="codex `-c KEY=VALUE` config for the codex author (repeatable), "
        "e.g. --codex-config use_legacy_landlock=true for a host that needs it.",
    )
    parser.add_argument("--max-turns", type=int, default=60)
    parser.add_argument("--session-minutes", type=int, default=60)
    parser.add_argument(
        "--panel",
        default="",
        help=(
            "pre-PR verification lenses, comma-separated kind[:backend[:model]] "
            "entries (e.g. 'verify,review' or 'verify:claude:MODEL'); only the "
            "claude backend is contained on this host so far; empty disables "
            "the panel"
        ),
    )
    parser.add_argument(
        "--panel-key-file",
        default=PANEL_KEY_DEFAULT,
        help="key file for panel judge sessions (the verifier's own key, never the author's)",
    )
    parser.add_argument(
        "--job-minutes",
        type=int,
        default=0,
        help="this job's Slurm walltime; arms the self-deadline (0 = off)",
    )
    parser.add_argument(
        "--deadline-margin-s",
        type=float,
        default=120.0,
        help="how long before the walltime the self-deadline fires (floor 60)",
    )
    parser.add_argument("--pat-file", default=os.path.expanduser("~/.config/autoresearch/bot_pat"))
    parser.add_argument(
        "--github-app-file",
        default=os.environ.get("AUTORESEARCH_GITHUB_APP_FILE", ""),
        help="GitHub App config (JSON: app_id, installation_id, private_key); "
        "when set, installation tokens replace the PAT",
    )
    parser.add_argument(
        "--key-file",
        default="",
        help="author key file; default resolves per backend (config-driven): "
        "AUTORESEARCH_HARNESS_KEY_FILE for claude, AUTORESEARCH_CODEX_KEY_FILE for codex",
    )
    parser.add_argument("--issue", type=int, default=0)
    parser.add_argument(
        "--min-free-gb",
        type=float,
        default=10.0,
        help="refuse to start when the run root has less free space",
    )
    parser.add_argument(
        "--hypothesis-b64", default="", help="base64 task hypothesis (issue text, fenced)"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if not args.image and not args.uncontained:
        parser.error("--image is required (or pass --uncontained explicitly, dev only)")
    # NOTE: the codex author is validated on the EFFECTIVE author per path — the
    # fresh climb on args (below), a wake on the parked run's persisted pair — not
    # here, where args.author_backend is the FLEET default and would misjudge a
    # resume after a fleet flip.
    # each --codex-config KEY=VALUE becomes a `-c KEY=VALUE` pair for codex
    codex_extra = tuple(a for c in args.codex_config for a in ("-c", c))

    bot_auth = resolve_bot_auth(args.pat_file, args.github_app_file)

    # --resume WAKES a parked dispatched run: rebuild the dispatched measurer
    # and re-enter the decision. The wake job the WakeDispatcher submits runs
    # exactly this.
    if args.resume:
        placed = bool(args.account and args.partition) or local_mode()
        if not (placed and args.image and Path(args.image).is_file()):
            parser.error(
                "--resume needs the cluster triple (--account/--partition/--image) "
                "to rebuild the dispatched measurer (local compute waives the "
                "account/partition pair, never the image)"
            )
        from autoresearch.runstate import load_record

        # a wake that is not the lease holder is a straggler (a replacement was
        # dispatched after it was cancelled, or it was armed and then lost):
        # it must not touch the run beside the holder
        other = _lease_held_by_another_job(args.run_root, args.resume)
        if other:
            print(f"run {args.resume}: wake job {other} holds the lease; this one exits")
            return 0

        # Reproduce the PARKED run's author, not the current fleet default: the
        # (backend, model) PAIR is persisted on the record — a fleet flip must not
        # wake a codex run as claude (or with the new fleet's model). A legacy or
        # unreadable record is treated as claude (resume_author). The key then
        # resolves for THAT backend (keys coexist).
        try:
            _wake_record: object | None = load_record(args.run_root, args.resume)
        except Exception:
            # a wake must never crash on an unreadable/odd record — fall back to
            # the claude author (resume_author), same fail-safe as the sweep
            _wake_record = None
        wake_backend, wake_model, wake_key_file = resume_author(_wake_record, args.model)
        # an explicit --key-file still overrides (a manual re-run pinning a key)
        if args.key_file:
            wake_key_file = os.path.expanduser(args.key_file)
        _err = codex_author_config_error(wake_backend, wake_model, args.image)
        if _err:
            # this wake job HOLDS the run's lease (transferred on dispatch); release
            # it before exiting so a misconfig doesn't strand the run until the TTL
            # reap (the resume_run finally below only runs once we reach it)
            _release_own_lease(args.run_root, args.resume)
            parser.error(f"parked run {args.resume}: {_err}")
        # the wake runs the SAME verification panel as a fresh climb, so a
        # dispatched improvement is not published unverified.
        try:
            wake_lenses, wake_panel_secrets = _panel_lenses_from_args(args)
        except ValueError as exc:
            parser.error(str(exc))
        wake_api_key = ""
        wake_harness = None
        wake_spec = None
        # The editor harness is built when the wake may RESUME the session: a
        # panel is configured (a blocking finding wakes the author to revise),
        # or the park is an AUTHOR-SLEEP (that wake always resumes the session
        # with its launches' results). A panel-less candidate wake stays a pure
        # read-decide-publish job and must not require the author key.
        _wake_stage = getattr(_wake_record, "stage", None) or {}
        if (
            wake_lenses
            or _wake_stage.get("phase") == "author-sleep"
            or _wake_stage.get("submitted")
        ):
            wake_api_key = role_key(wake_key_file, wake_backend)
            wake_spec = author_spec(max_turns=args.max_turns, walltime_s=args.session_minutes * 60)
            wake_harness = build_harness(
                wake_api_key,
                wake_spec,
                backend=wake_backend,
                binary=args.claude_bin if wake_backend == "claude" else args.codex_bin,
                model=wake_model,
                container_image=args.image,
                codex_extra_args=codex_extra,
            )
        try:
            resumed = resume_run(
                args.run_root,
                args.resume,
                dispatch=_dispatch_settings(args),
                github=GitHubClient(auth=bot_auth),
                bot_auth=bot_auth,
                now=time.time(),
                secrets=tuple(
                    k for k in (bot_auth.token(), *wake_panel_secrets, wake_api_key) if k
                ),
                base_branch=args.base_branch,
                panel_lenses=wake_lenses,
                harness=wake_harness,
                spec=wake_spec,
            )
        finally:
            # This wake job HOLDS the run's lease (the sweep transferred it on
            # dispatch); release it on every exit so a re-parked run is
            # immediately eligible for the next sweep instead of waiting out the
            # TTL reap. Idempotent (no-op if no lease file).
            _release_own_lease(args.run_root, args.resume)
        print(f"outcome={resumed.outcome} pr={resumed.pr_url or '-'} report={resumed.report_path}")
        return 0

    if not (args.target and args.benchmark):
        parser.error("--target and --benchmark are required for a fresh climb")

    # a fresh climb authors on the FLEET's configured backend; validate it (codex
    # writes+executes, so --image + a non-claude model) before any spend.
    _err = codex_author_config_error(args.author_backend, args.model, args.image)
    if _err:
        parser.error(_err)
    # config-driven: the author key defaults per backend (claude vs codex) so the
    # tick never threads it — see resolve_author_key_file (result is ~-expanded).
    args.key_file = resolve_author_key_file(args.author_backend, args.key_file)
    # same 0600 discipline as the PAT: this key spends real money. A missing
    # file is tolerated only when Vertex (ADC) covers the claude backend.
    api_key = role_key(args.key_file, args.author_backend)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    # the agent id keeps concurrent same-benchmark slots (the width dial's
    # portfolio case) from minting one run directory in the same second
    run_id = f"{args.benchmark}-{stamp}-{args.agent_id}"

    # Disk preflight BEFORE any run state exists: a session started on a
    # full filesystem dies mid-flight in ways that lose its own evidence
    # (quota errors are invisible until a write fails on some clusters).
    from autoresearch.disk import check_mount

    health = check_mount(args.run_root, min_free_bytes=int(args.min_free_gb * 1024**3))
    if not health.ok():
        log.error("disk preflight failed: %s — refusing to start a run", health.describe())
        if args.issue:
            _best_effort(
                "issue report",
                lambda: GitHubClient(auth=bot_auth).comment(
                    args.target,
                    args.issue,
                    "A run for this issue could not start: the orchestrator's "
                    "storage failed its disk preflight. The claim on this issue "
                    "stays until a maintainer removes the claim comment "
                    "(automated claim release is on the roadmap).",
                ),
            )
        return 3

    # Armed LAST, immediately before the contained region — and DISARMED
    # right after it: a run finishing inside the margin must not have the
    # alarm fire during the uncontained epilogue (print/exit).
    import signal as _signal

    armed = arm_self_deadline(args.job_minutes, args.deadline_margin_s)
    if armed:
        log.info("self-deadline armed: Terminated in %ds", armed)

    # the manifest first, the harness from it: budget has one source (the args)
    spec = author_spec(max_turns=args.max_turns, walltime_s=args.session_minutes * 60)

    # Pre-PR panel lenses: judge sessions on the verifier's own key (separate
    # identity from the author). kind[:backend[:model]]; claude by default.
    try:
        panel_lenses, panel_secrets = _panel_lenses_from_args(args)
    except ValueError as exc:
        parser.error(str(exc))

    # Dispatched measurement needs the full cluster triple AND a real image
    # file to bind against; missing any, the climb measures inline (the tick
    # sets these on the climb job's env, a bare CLI run leaves them empty).
    dispatch: DispatchSettings | None = None
    if (bool(args.account and args.partition) or local_mode()) and (
        args.image and Path(args.image).is_file()
    ):
        dispatch = _dispatch_settings(args)
    try:
        try:
            outcome = live_attempt(
                config=RunConfig(
                    target=args.target, benchmark=args.benchmark, agent_id=args.agent_id
                ),
                base_branch=args.base_branch,
                run_root=args.run_root,
                run_id=run_id,
                harness=build_harness(
                    api_key,
                    spec,
                    backend=args.author_backend,
                    binary=args.claude_bin if args.author_backend == "claude" else args.codex_bin,
                    model=args.model,
                    container_image=args.image,
                    codex_extra_args=codex_extra,
                ),
                spec=spec,
                panel_lenses=panel_lenses,
                dispatch=dispatch,
                eval_image=args.image,
                github=GitHubClient(auth=bot_auth),
                bot_auth=bot_auth,
                now=time.time(),
                created=datetime.now(UTC).isoformat(),
                # the panel key joins the redaction set: judge error text can
                # echo request material like any other model error
                secrets=tuple(
                    k
                    for k in (
                        api_key,
                        bot_auth.token(),
                        *panel_secrets,
                    )
                    if k
                ),
                issue_number=args.issue,
                author_backend=args.author_backend,
                author_model=args.model,
                author_key_file=args.key_file,
                task_hypothesis=(
                    __import__("base64").b64decode(args.hypothesis_b64).decode()
                    if args.hypothesis_b64
                    else ""
                ),
            )
        except Terminated as exc:
            # Fired in live_attempt's microseconds-wide pre-containment window:
            # any record it saved strands and the sweep ends it from Slurm
            # truth; here we only avoid dying as an unexplained traceback.
            log.error("self-deadline fired before containment: %s", exc)
            return 3
    finally:
        _signal.alarm(0)
    print(f"outcome={outcome.outcome} pr={outcome.pr_url or '-'} report={outcome.report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
