"""One live climb, end to end: clone → climb_once → commit/push/PR → report.

This is the glue `orchestrator.climb_once` deliberately does not own: the git
side (bot-auth clone, veto-checked commit, push, PR) and the run's durable
record. One invocation = one run = at most one PR.

Credential separation holds throughout: the bot PAT is read orchestrator-side
and used only by Workspace network calls and the PR client, after the session
has ended; the session sees only its own capped API key inside its container.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import replace as dc_replace
from functools import partial
from pathlib import Path
from typing import Any

from autoresearch.contract import Benchmark, Contract, load_contract
from autoresearch.dispatch import (
    Snapshot,
    afterany_ids,
    drop_snapshot,
    should_dispatch,
    snapshot_tree,
)
from autoresearch.github import (
    FileTokenProvider,
    GitError,
    GitHubClient,
    Workspace,
)
from autoresearch.harness import Harness, SessionResult, redact
from autoresearch.measure import DispatchSettings, LocalMeasurer
from autoresearch.orchestrator import (
    ClimbConfig,
    ClimbParked,
    ClimbResult,
    EvalError,
    Evaluator,
    Measurer,
    SubprocessEvaluator,
    SuiteMeasurement,
    _benchmark,
    climb_once,
    out_of_scope,
    pr_body,
    resume_climb,
    suite_regressed,
)
from autoresearch.orchestrator import improved as orch_improved
from autoresearch.panel import PanelLens, PanelVerdict, run_panel
from autoresearch.progress import (
    PROGRESS_PATHS,
    load_leader,
    update_leader,
    write_progress,
)
from autoresearch.review import PullRequest
from autoresearch.role_runner import build_harness, run_role
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
    load_record,
    save_record,
    stamp_outage,
)
from autoresearch.syscall import MAX_ARTIFACT_BYTES, SYSCALL_DIR, SyscallRequest
from autoresearch.syscall import ensure_excluded as syscall_excluded
from autoresearch.syscall import install_tool as syscall_install_tool
from autoresearch.syscall import write_budget as syscall_write_budget
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


class SuiteRegressed(RuntimeError):
    """A sibling benchmark regressed beyond its floor on the landing tree —
    the improvement was bought by breaking siblings. An honest negative
    result wherever it is caught, never an abort."""


def _target_clone_url(target: str) -> str:
    """The canonical HTTPS clone URL for `owner/repo`. The one source of truth
    for where a run's git pushes go — derived from the target, never read from
    the session-writable `remote.origin.url`."""
    return f"https://github.com/{target}.git"


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
class LiveClimbOutcome:
    run_id: str
    outcome: str
    pr_url: str = ""
    report_path: str = ""


def _best_effort(what: str, fn: Callable[[], object], secrets: tuple[str, ...] = ()) -> bool:
    """One ending step; a failure is logged, never raised.

    The terminal sequence (record, report, issue post) must degrade
    independently: a full disk must not block the GitHub post, and a network
    failure must not block the record. The 2026-08-07 quota crisis stranded a
    run in `implementing` because the ending record itself hit EDQUOT inside
    the except handler and took the report and issue post down with it.
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
    return dc_replace(
        record,
        stage={},
        experiment_job_id="",
        deadline=0.0,
        terminal_seen=0.0,
        wake_job_id="",
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

# Author syscalls (research-loop-buildout.md Phase A) ship in two parts: the
# sleep side exists, the WAKE (gather launch results, resume the session) is
# part 2. Until part 2 flips this, arming AUTORESEARCH_AUTHOR_SYSCALLS alone
# must not produce an unwakeable author-sleep park (terra, #132 r5) — the
# feature stays off, loudly.
AUTHOR_SLEEP_WAKE_READY = True  # Phase A part 2 shipped: the wake services author-sleep parks


def _park_run(
    run_root: Path,
    record: RunRecord,
    parked: ClimbParked,
    candidate_ref: str,
    eval_minutes: int | None,
    now: float,
    secrets: tuple[str, ...] = (),
    keep_wake_attempts: bool = False,
    base_branch: str = "main",
    panel_reads: int = 0,
) -> None:
    """Persist a dispatched climb's re-entry point as a WAITING record: the
    committed shas, drawn seeds, candidate snapshot ref, and afterany set a
    fresh process reconstructs the measure-and-decide phase from. The caller
    passes the EXACT `candidate_ref` it will keep alive (never re-derive it from
    the commit — two snapshots can share a commit). The wake path (which reads
    this and resumes) is a later PR."""
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
        # the branch the run targets, so a wake opens its PR against the SAME
        # branch a non-default `--base-branch` selected — the wake CLI otherwise
        # defaults to main and would mis-target.
        "base_branch": base_branch,
        # verification-panel revisions taken so far — persisted so the next wake
        # knows the cap position after a panel-driven revision re-park.
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
    if parked.phase == "author-sleep":
        # An author-directed sleep (research-loop-buildout.md Phase A): the wake
        # gathers each launch's results by NAME from the run dir, delivers the
        # declared artifacts, and resumes the SAME session — so the stage must
        # carry the launch names/artifacts, the author's note, and the budget
        # counts as of this park.
        assert parked.syscall is not None
        stage["syscall_launches"] = [
            {"name": launch.name, "artifacts": list(launch.artifacts)}
            for launch in parked.syscall.launches
        ]
        stage["syscall_note"] = redact(parked.syscall.note, secrets)
        # (the session id the wake resumes is the record's own
        # resume_session_id, set below for every park — no stage duplicate)
        stage["launches_used"] = parked.launches_used
        stage["sleeps_used"] = parked.sleeps_used
    # The sweep polls ONE experiment_job_id and wakes on its terminal+grace. That
    # is right for a single-job park (baseline, or a candidate with no siblings):
    # poll it. But a MULTI-job park (candidate + siblings) must not wake when the
    # FIRST job finishes while the rest run — so it records no single job and
    # rides the DEADLINE floor instead (which sits past every eval's walltime).
    # The precise "all jobs done" fast wake is the afterany wake job (a later PR).
    experiment_job_id = job_ids[0] if len(job_ids) == 1 else ""
    # The deadline is a FLOOR: park time (`now` here is the park moment, passed
    # by the caller) + the eval walltime + a generous queue/grace slack, so a
    # healthy queued-then-running eval never trips the sweep's cancel-on-pending.
    floor_minutes = effective_eval_minutes(eval_minutes)
    if parked.phase == "author-sleep" and parked.syscall is not None:
        # an author launch's walltime is the LAUNCH's ask, not the benchmark's
        # eval hint — the floor must sit past the LONGEST launch, or the sweep
        # cancels still-queued author jobs (a benchmark can be in-job cheap,
        # eval_minutes=None, while its author trains for hours). A checkpoint
        # sleep has no jobs: floor 0 wakes it at the first deadline pass.
        floor_minutes = max((la.minutes for la in parked.syscall.launches), default=0)
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
            "wake_job_id": "",
        }
    )
    save_record(run_root, waiting, now)


def _make_launcher(dispatch: DispatchSettings, run_dir: Path, workspace: Path, run_id: str):
    """The launch side of the author syscalls, shared by the first pass
    (live_climb) and the author-sleep wake: each launch becomes a jailed job on
    the sealed snapshot (write_eval_job's copy-out handles artifacts), and a
    partially-submitted batch is reaped rather than orphaned."""

    def launcher(sha: str, request: SyscallRequest) -> str:
        from autoresearch.dispatch import eval_job_spec, write_eval_job

        ids: list[str] = []
        try:
            for launch in request.launches:
                script = write_eval_job(
                    run_dir,
                    f"launch-{launch.name}",
                    repo_root=workspace,
                    snapshot_sha=sha,
                    command=launch.command,
                    image=dispatch.image,
                    artifacts=launch.artifacts,
                    artifact_max_bytes=MAX_ARTIFACT_BYTES,
                )
                ids.append(
                    dispatch.compute.submit(
                        eval_job_spec(
                            script,
                            job_name=f"{run_id}-launch-{launch.name}",
                            account=dispatch.account,
                            partition=dispatch.partition,
                            eval_minutes=launch.minutes,
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
    config: ClimbConfig,
    measurer: Measurer,
    harness: Harness | None,
    spec: RoleSpec | None,
    panel_lenses: tuple[PanelLens, ...],
    panel_revisions: int,
    issue_number: int,
    eval_minutes: int | None,
) -> LiveClimbOutcome:
    """Wake an author-sleep park: deliver the launches' results into the
    sandbox, resume the SAME session through the climb's resume-entry with them
    (data-fenced), and let the climb run — it may sleep again (re-park), finish
    into the gate (whose dispatched measures park it as a CANDIDATE the next
    wake decides), or end on a terminal. The session's workspace persisted on
    disk exactly as the author left it (the launches ran on node-local
    checkouts of the sealed sha), so the resumed session continues its own tree
    — cumulative depth.
    """
    from autoresearch.syscall import Launch as SyscallLaunch
    from autoresearch.syscall import gather_results, render_wake, write_budget

    def _end(result: ClimbResult, drop_refs: list[str]) -> LiveClimbOutcome:
        # a terminal from the resumed climb: report, ending record, issue note —
        # the same ending shape every other terminal takes.
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
        return LiveClimbOutcome(run_id=run_id, outcome=result.outcome, report_path=str(report_path))

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
            ClimbResult(
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
            minutes=1,
            artifacts=tuple(str(a) for a in item.get("artifacts", [])),
        )
        for item in _stage_launches(record)
    )
    results = gather_results(run_dir, workspace, launches)
    launches_used = int(record.stage.get("launches_used", 0))  # type: ignore[call-overload]
    sleeps_used = int(record.stage.get("sleeps_used", 0))  # type: ignore[call-overload]
    wake_text = render_wake(
        results,
        str(record.stage.get("syscall_note", "")),
        launches_used=launches_used,
        launch_budget=bench.depth_k,
        sleeps_used=sleeps_used,
        sleep_budget=bench.sleep_k,
    )
    _best_effort(
        "budget refresh",
        lambda: write_budget(
            workspace,
            launches_remaining=max(0, bench.depth_k - launches_used),
            sleeps_remaining=max(0, bench.sleep_k - sleeps_used),
        ),
    )

    # The wake's climb IO: measures go through the DISPATCHED measurer (this is
    # a wake job with bounded walltime — the gate's evals run as their own jobs
    # and park the run as a CANDIDATE), snapshots parent on base (same as the
    # first pass: the clone was at base), and changed_paths carries no
    # fingerprints (only the live inline publish needs them; every publish from
    # here is the sealed-sha wake publish).
    snapshots: list[Snapshot] = []

    def snapshot() -> str:
        snap = snapshot_tree(ws, base_sha)
        if isinstance(measurer, LocalMeasurer):  # pragma: no cover - wake is dispatched
            measurer.live[snap.commit] = workspace
        snapshots.append(snap)
        return snap.commit

    def changed_paths() -> list[str]:
        ws.git("add", "-A")
        paths = ws.staged_paths()
        ws.git("reset")
        return paths

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
        )
        if panel_lenses
        else None
    )

    parked: ClimbParked | None = None
    kept_ref = ""
    try:
        result = climb_once(
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
            panel_revisions=panel_revisions,
            resume_session_id=record.resume_session_id,
            improve_prompt=wake_text,
            launcher=_make_launcher(dispatch, run_dir, workspace, run_id),
            launches_used=launches_used,
            sleeps_used=sleeps_used,
        )
    except ClimbParked as p:
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
                base_branch=base_branch,
            )
        except Exception:
            for job_id in afterany_ids(p.afterany):
                dispatch.compute.cancel(job_id)
            raise
        parked = p
        drop_snapshot(ws, Snapshot(commit="", tree="", ref=sleep_ref))
        return LiveClimbOutcome(run_id=run_id, outcome="parked")
    finally:
        for snap in snapshots:
            if parked and kept_ref and snap.ref == kept_ref:
                continue
            drop_snapshot(ws, snap)

    # a terminal from the resumed session (session-error/-outage/-budget,
    # scope-violation, eval-error; a dispatched gate never returns improved
    # inline): end the run and release the sleep snapshot.
    return _end(result, drop_refs=[sleep_ref])


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
    bot_auth: FileTokenProvider,
    now: float,
    secrets: tuple[str, ...] = (),
    base_branch: str = "main",
    panel_lenses: tuple[PanelLens, ...] = (),
    harness: Harness | None = None,
    spec: RoleSpec | None = None,
    panel_revisions: int = 1,
) -> LiveClimbOutcome:
    """Wake a parked dispatched climb and re-enter its decision WITHOUT the
    session (`orchestrator.resume_climb`), from the record `_park_run` wrote.
    The three exits:

    * **re-park** — the wake dispatched a measure that is not done yet (the
      suite pairs an improving candidate fans out, "another round of
      experiments"): `resume_climb` raises `ClimbParked`, and this re-persists
      the WAITING stage on the new afterany, keeping the same candidate
      snapshot;
    * **a negative terminal** (no-improvement / suite-regression / eval-error):
      drop the candidate snapshot and end the record;
    * **improved** — branch the SEALED `candidate_sha` (never the live tree,
      which may have drifted since the park; the diff was scope-checked so it
      carries only in-scope changes), layer the ledger update on top, push, and
      open the PR. The mechanical moved-base merge the first pass does is
      deliberately NOT here (docs/design/research-loop.md): a stale PR is a
      re-wake, not an orchestrator auto-merge.
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
    config = ClimbConfig(target=record.target, benchmark=record.benchmark, agent_id=record.agent_id)
    eval_minutes = next(
        (b.eval_minutes for b in contract.benchmarks if b.name == record.benchmark), None
    )
    measurer = dispatch.measurer(
        run_dir, repo_root=workspace, eval_minutes=int(eval_minutes or 0), run_tag=run_id
    )
    # measured_paths from the COMMITTED base..candidate diff — the sealed
    # candidate, never `changed_paths()` on a live tree that may have drifted.
    # NUL-delimited (like Workspace.staged_paths) so a path with a space is one
    # entry, not two that could each slip past the scope check.
    measured_paths = tuple(
        p for p in ws.git("diff", "--name-only", "-z", base_sha, candidate_sha).split("\0") if p
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
            panel_revisions=panel_revisions,
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
        result = resume_climb(
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
    except ClimbParked as parked:
        # another measure this wake dispatched is not done — re-park on the new
        # afterany, keeping the SAME candidate snapshot the next wake reads.
        # PROGRESS only if this wake dispatched a NEW job set (e.g. the candidate
        # resolved and the suite pairs fanned out); a blind re-park (empty
        # afterany) or the same jobs still pending is NO progress, so the stuck
        # cap must keep counting.
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
            keep_wake_attempts=not made_progress,
            base_branch=base_branch,
            panel_reads=panel_reads,
        )
        return LiveClimbOutcome(run_id=run_id, outcome="parked")

    def _do_revise(verdict: PanelVerdict, reads: int) -> LiveClimbOutcome | None:
        """A blocking panel finding -> wake the agent to revise (the depth
        axis). Re-run the session with the findings, re-snapshot the revised
        tree, and re-measure — which re-parks (dispatched) so the NEXT wake
        runs the panel again. Returns the parked/terminal outcome, or None when
        the revision session could not run (the caller then DRAFTs the original
        candidate — the improvement is real, it just could not be revised)."""
        assert harness is not None and spec is not None
        wake_result = run_role(
            spec, harness, verdict.wake_text, workspace, resume_session_id=record.resume_session_id
        )
        if not wake_result.ok:
            log.warning("wake revision session failed for %s; drafting the original", run_id)
            return None
        # the revised tree is a NEW candidate; snapshot it (parented on base).
        # A snapshot failure (git write-tree, disk) must NOT escape and leave the
        # run WAITING to retry — that would re-spend a revision session on the
        # same finding. Fall back to DRAFTING the original (its improvement is
        # real and measured); the caller commits candidate_sha, not the revision.
        try:
            new_snap = snapshot_tree(ws, base_sha)
        except Exception as exc:
            log.warning(
                "wake revision snapshot failed for %s (%s); drafting the original",
                run_id,
                redact(f"{type(exc).__name__}: {exc}", secrets),
            )
            return None
        new_measured = tuple(
            p
            for p in ws.git("diff", "--name-only", "-z", base_sha, new_snap.commit).split("\0")
            if p
        )
        old_snap = Snapshot(commit=candidate_sha, tree="", ref=candidate_ref)
        try:
            revised = resume_climb(
                contract,
                bench,
                base_sha=base_sha,
                candidate_sha=new_snap.commit,
                seed=seed,
                suite_seed=suite_seed,
                measured_paths=new_measured,
                session=wake_result.session,
                measurer=measurer,
                min_relative_improvement=config.min_relative_improvement,
            )
        except ClimbParked as p:
            # re-park on the NEW candidate; carry the revision count so the next
            # wake honors the cap. Keep the new snapshot, drop the superseded old.
            _park_run(
                run_root,
                record,
                p,
                new_snap.ref,
                eval_minutes,
                now,
                secrets,
                base_branch=base_branch,
                panel_reads=reads,
            )
            drop_snapshot(ws, old_snap)
            return LiveClimbOutcome(run_id=run_id, outcome="parked")
        except Exception as exc:
            # the re-measure could not even dispatch (e.g. Slurm submit) — do NOT
            # discard the ORIGINAL's real, measured improvement; drop the new
            # snapshot and DRAFT the original (the caller keeps candidate_sha).
            log.warning(
                "wake revision re-measure failed for %s (%s); drafting the original",
                run_id,
                redact(f"{type(exc).__name__}: {exc}", secrets),
            )
            drop_snapshot(ws, new_snap)
            return None
        if revised.outcome == "improved":
            # the dispatched re-measure should have PARKED; an inline 'improved'
            # here (a cached result) is unexpected and unverified — draft the
            # original rather than ABORT with no PR.
            log.warning(
                "wake revision re-measure returned improved without parking for %s; drafting",
                run_id,
            )
            drop_snapshot(ws, new_snap)
            return None
        # the re-measure returned a NEGATIVE terminal (the revision went out of
        # scope, regressed, or eval-errored): end the run, drop BOTH snapshots.
        drop_snapshot(ws, old_snap)
        drop_snapshot(ws, new_snap)
        report_path = run_dir / "report.md"
        _best_effort(
            "run report",
            lambda: report_path.write_text(revised.report(config, redact_secrets=secrets)),
            secrets,
        )
        ending = _ENDINGS_BY_OUTCOME.get(revised.outcome, ABORTED)
        final = _clear_stage(
            RunRecord(
                **{
                    **record.__dict__,
                    "state": ENDED,
                    "ending": ending,
                    "ending_note": redact(revised.note, secrets),
                }
            )
        )
        _best_effort("final record", lambda: save_record(run_root, final, now), secrets)
        _post_issue_finished(
            github,
            config.target,
            issue_number,
            run_id,
            revised.outcome,
            "",
            redact(revised.report(config, redact_secrets=secrets), secrets)[:8000],
            secrets,
        )
        return LiveClimbOutcome(
            run_id=run_id, outcome=revised.outcome, report_path=str(report_path)
        )

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
            return LiveClimbOutcome(run_id=run_id, outcome="no-improvement")

        from datetime import UTC as _UTC
        from datetime import datetime as _dt

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
                _best_effort(
                    "auto-merge arming",
                    lambda: github.arm_auto_merge_when_review_required(
                        config.target, int(pr_number)
                    ),
                    secrets,
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
            return LiveClimbOutcome(
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
            # what was measured), over base_sha. Slice 1: a blocking or degraded
            # verdict opens a DRAFT PR carrying the findings and never arms
            # auto-merge; a clean verdict (or no panel) arms. Waking the agent
            # to REVISE on a blocking finding — the depth axis — is the next
            # slice; for now a human triages the draft.
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
                # DEPTH AXIS (docs/design/research-loop.md): a blocking finding
                # WAKES THE AGENT to revise — re-run the session with the
                # findings, re-snapshot, re-measure (-> re-park) — instead of
                # drafting, bounded by panel_revisions. Fall through to a DRAFT
                # when we cannot resume (no harness/session) or the cap is hit.
                can_revise = (
                    bool(verdict.blocking)
                    and harness is not None
                    # a no-resume backend (hermes) declares supports_resume=False
                    # -> draft, don't resume (claude and codex both resume)
                    and getattr(harness, "supports_resume", True)
                    and spec is not None
                    and bool(record.resume_session_id)
                    and reads <= panel_revisions
                )
                if can_revise:
                    revised = _do_revise(verdict, reads)
                    if revised is not None:
                        return revised
                    # the revision session could not run — DRAFT the original
                    result = dc_replace(
                        result,
                        panel_transcript=verdict.transcript,
                        panel_rounds=reads,
                        panel_blocking_open=True,
                    )
                else:
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
                _best_effort(
                    "auto-merge arming",
                    lambda: github.arm_auto_merge_when_review_required(
                        config.target, int(pr_number)
                    ),
                    secrets,
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
            return LiveClimbOutcome(run_id=run_id, outcome="publish-error")
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
        return LiveClimbOutcome(
            run_id=run_id, outcome="improved", pr_url=pr_url, report_path=str(report_path)
        )

    # a negative terminal: end the record, THEN release the snapshot. Save
    # BEFORE dropping (same ordering as the improved path): if the save fails,
    # the run stays WAITING with its snapshot intact, so a re-wake can still
    # reconstruct — never WAITING with the snapshot already gone.
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
    return LiveClimbOutcome(run_id=run_id, outcome=result.outcome, report_path=str(report_path))


def _measure_committed(
    ws: Workspace,
    evaluator: Evaluator,
    run_dir: Path,
    name: str,
    sha: str,
    bench: Any,
    extra_env: dict[str, str] | None = None,
) -> float:
    """Measure a COMMITTED tree in a throwaway worktree.

    Both sides of the post-merge comparison run in equivalent pristine
    environments — the long-lived workspace carries session-created caches
    and virtualenvs a fresh tree lacks, so measuring one side there would
    bias the accept/reject decision. Eval writes are discarded with the
    worktree, and the measured content is exactly the commit: the sha IS
    the drift fingerprint.
    """
    wt = run_dir / f"measure-{name}"
    ws.git("worktree", "add", "--detach", str(wt), sha)
    try:
        return float(evaluator.evaluate(wt, bench.command, bench.metric, extra_env=extra_env))
    finally:
        removed = _best_effort(
            "worktree cleanup", lambda: ws.git("worktree", "remove", "--force", str(wt))
        )
        if not removed:  # never silent: a leaked worktree is a disk leak —
            # and prune alone only drops the ADMIN entry, so delete the
            # directory itself first
            _best_effort("worktree dir removal", lambda: shutil.rmtree(wt, ignore_errors=True))
            _best_effort("worktree prune", lambda: ws.git("worktree", "prune"))


def _panel_lenses_from_args(args: Any) -> tuple[PanelLens, ...]:
    """Build the verification-panel lenses from the CLI args (empty `--panel`
    disables it). Shared by the fresh-climb and the `--resume` wake paths so a
    dispatched improvement runs the SAME panel as an inline one. Raises
    ValueError on a bad panel/backend config — a configured gate must never
    silently vanish."""
    import os

    if not args.panel.strip():
        return ()
    from autoresearch.panel import parse_lenses
    from autoresearch.roles import reviewer_spec

    panel_key = FileTokenProvider(Path(args.panel_key_file).expanduser()).token()
    lenses = []
    for kind, backend, model in parse_lenses(args.panel):
        hermes_repo_env = os.environ.get("REVIEW_HERMES_REPO", "").strip()
        try:
            judge = build_harness(
                panel_key,
                reviewer_spec(),
                backend=backend,
                binary=args.claude_bin if backend == "claude" else None,
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
    return tuple(lenses)


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
) -> Callable[[float, float, str], PanelVerdict]:
    """The git half of the pre-PR panel: prepare the two read-only checkouts
    and the synthetic claim, then hand off to `run_panel` (which owns no git).

    Each call snapshots the CURRENT workspace tree as a detached commit and
    checks it out as `pr-head/` (sanitized — the candidate is an untrusted
    tree), next to `base/` (the trusted pre-session commit: contract and
    ruler). Worktrees are removed after the read; a fresh pair is built per
    round because the tree changes with every revision."""
    from autoresearch.review_agent import sanitize_checkout

    reads = {"n": start_round}

    def runner(baseline: float, candidate: float, report: str) -> PanelVerdict:
        reads["n"] += 1
        panel_ws = run_dir / "panel"
        shutil.rmtree(panel_ws, ignore_errors=True)
        panel_ws.mkdir(parents=True, exist_ok=True)
        ws.git("add", "-A")
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
                body=(
                    f"Automated improvement claim (pre-PR): {benchmark} "
                    f"{baseline} -> {candidate}, measured by the orchestrator.\n\n"
                    f"## Research report\n\n{report[:MAX_CLAIM_CHARS]}"
                ),
                # base..snapshot, never base..worktree: the snapshot commit
                # includes newly ADDED files, which a working-tree diff omits
                diff=ws.git("diff", f"{base_sha}..{snapshot}"),
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


def live_climb(
    config: ClimbConfig,
    run_root: Path,
    run_id: str,
    harness: Harness,
    evaluator: Evaluator,
    github: GitHubClient,
    bot_auth: FileTokenProvider,
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
    panel_revisions: int = 1,
    dispatch: DispatchSettings | None = None,
) -> LiveClimbOutcome:
    """Run one climb against the real target repo. With `panel_lenses`, the
    pre-PR verification panel gates the claim before any PR exists
    (docs/design/orchestrator-verify.md); blocking findings still open at
    the cap open a DRAFT PR carrying them."""
    run_dir = run_root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace = run_dir / "ws"

    # The record exists before any network or clone work: every crash from
    # here on has a record to end. (A run once stranded in `implementing`
    # because the region between record creation and the contained call
    # could still raise.)
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
        climb_job_id=_os.environ.get("SLURM_JOB_ID", ""),
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
        return LiveClimbOutcome(run_id=run_id, outcome="climb-error")

    tree_hashes: list[str] = []
    try:
        ws = Workspace.clone(_target_clone_url(config.target), workspace, auth=bot_auth)
        # the BASE BRANCH tip, not HEAD: they differ if the PR base is ever
        # not the clone's default branch, and the freshness comparison below
        # must be against the branch the PR will actually land on
        base_sha = ws.git("rev-parse", f"origin/{base_branch}").strip()
        contract_text = (workspace / ".autoresearch.yaml").read_text()
        contract = load_contract(contract_text, config.target)
        # With author syscalls armed, the channel (`.autoresearch/`) never
        # enters diffs, scope, or drift fingerprints — repo-local exclude.
        # Gated on the SAME flag as the launcher: with the feature off, an
        # untracked `.autoresearch/` file must be staged and judged like any
        # other agent edit, not silently hidden by a magic dir name (terra,
        # #132 r2 — the off state stays byte-identical to today).
        author_syscalls = bool(os.environ.get("AUTORESEARCH_AUTHOR_SYSCALLS"))
        if author_syscalls and not AUTHOR_SLEEP_WAKE_READY:
            log.warning(
                "AUTORESEARCH_AUTHOR_SYSCALLS is set but the wake side is not "
                "built yet (Phase A part 2); author syscalls stay OFF this run"
            )
            author_syscalls = False
        # The `.autoresearch/` channel must be KERNEL-OWNED. In a fresh clone,
        # anything already at that path was committed by the TARGET — a symlink
        # (install would write through it to a host path with our permissions —
        # terra #133 r1), a tracked request (free cluster compute — #132 r3), or
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
        if author_syscalls:
            syscall_excluded(workspace)
            # the author's interface is the TOOL (`python .autoresearch/syscall
            # launch ... -- <cmd>`; `... sleep`), never the raw ABI file —
            # install it plus the informational budget its `status` shows.
            syscall_install_tool(workspace)
            _bench = next((b for b in contract.benchmarks if b.name == config.benchmark), None)
            if _bench is not None:
                syscall_write_budget(
                    workspace,
                    launches_remaining=_bench.depth_k,
                    sleeps_remaining=_bench.sleep_k,
                )

        def changed_paths() -> list[str]:
            ws.git("add", "-A")
            paths = ws.staged_paths()
            # Content fingerprint of the whole tree: the drift check must
            # catch a file REWRITTEN during eval (same path set, different
            # bytes), not only files created or deleted.
            tree_hashes.append(ws.git("write-tree").strip())
            ws.git("reset")
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

        # The baseline is measured in a throwaway worktree of the pre-session
        # commit — the session never sees the directory the baseline eval ran
        # in, so eval artifacts (even gitignored ones) cannot leak the run
        # seed or the sampled pool into the solver's view. The dispatched
        # backend checks out each sha in its own eval job, so it needs no such
        # worktree.
        baseline_wt = run_dir / "measure-baseline"
        if not dispatched:
            ws.git("worktree", "add", "--detach", str(baseline_wt), "HEAD")
        # the panel's base is the PRE-SESSION commit — the exact tree the
        # baseline was measured on — never origin/<base_branch>, which can
        # name a different branch than the clone's checkout (terra, #95 r3)
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
            )
            if panel_lenses
            else None
        )
        # DISPATCHED: each measure runs as its own eval job, checking out its
        # tree sha fresh from this workspace's `refs/dispatch/*` — so the
        # measurer needs only the repo, and a not-yet-done measure PARKS the
        # climb. INLINE: the eval runs in the caller's worktrees — the pristine
        # pre-session tree (baseline_wt, a CLEAN checkout, cache-safe) for
        # base_sha and the session's LIVE workspace for every candidate
        # snapshot (measured fresh — its content is not pinned by the sha) —
        # and never parks. Either way `snapshot` commits the workspace's
        # current content to a candidate sha and we own the ref lifecycle,
        # keeping the one candidate ref a park needs and dropping the rest when
        # the climb ends.
        measurer: Measurer
        if dispatched:
            assert dispatch is not None and eval_minutes is not None  # should_dispatch(None) False
            measurer = dispatch.measurer(
                run_dir, repo_root=workspace, eval_minutes=eval_minutes, run_tag=run_id
            )
        else:
            measurer = LocalMeasurer(evaluator, clean={pre_session_sha: baseline_wt})
        snapshots: list[Snapshot] = []

        def snapshot() -> str:
            snap = snapshot_tree(ws, pre_session_sha)
            # inline only: register the live workspace for this candidate sha.
            # dispatched jobs check the sha out fresh, so they need no map.
            if isinstance(measurer, LocalMeasurer):
                measurer.live[snap.commit] = workspace
            snapshots.append(snap)
            return snap.commit

        # Author syscalls (research-loop-buildout.md Phase A), dark by default:
        # offered only when the operator arms AUTORESEARCH_AUTHOR_SYSCALLS, the
        # dispatcher has cluster coordinates (the launches are Slurm jobs), and
        # the backend can resume (the wake resumes the SAME session).
        # `author_syscalls` already reflects the channel-ownership guard above
        # (a target-shipped `.autoresearch` — symlink, tracked request, or any
        # other pre-existing form — has disabled the feature for this run).
        launcher = None
        if author_syscalls and dispatch is not None and getattr(harness, "supports_resume", True):
            launcher = _make_launcher(dispatch, run_dir, workspace, run_id)

        parked: ClimbParked | None = None
        kept_ref = ""  # the ONE candidate snapshot ref that must outlive a park
        try:
            # the last-known score orients the brief only; the gate re-measures
            # both sides after the session, so None (a first run) is fine.
            prior_best = load_leader(workspace).get(config.benchmark)
            result = climb_once(
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
                spec=spec,
                panel_runner=panel_runner,
                panel_revisions=panel_revisions,
                brief_baseline=prior_best.best if prior_best else None,
                launcher=launcher,
            )
        except ClimbParked as p:
            # The climb dispatched its measures and hibernated. Persist the
            # re-entry stage as a WAITING record (not an error), keep the
            # candidate snapshot alive for the wake, and end. The wake re-enters
            # from the record (the wake path is a later PR). `parked` is set only
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
            return LiveClimbOutcome(run_id=run_id, outcome="parked")
        finally:
            for snap in snapshots:
                # a candidate park must OUTLIVE the wake — keep the ONE recorded
                # snapshot (matched by ref, not commit); drop every other one.
                if parked and kept_ref and snap.ref == kept_ref:
                    continue
                drop_snapshot(ws, snap)  # best-effort + self-logging; never raises
            # the baseline worktree exists only on the inline path (the
            # dispatched backend never created one).
            if not dispatched and not _best_effort(
                "baseline worktree cleanup",
                lambda: ws.git("worktree", "remove", "--force", str(baseline_wt)),
            ):
                import shutil

                shutil.rmtree(baseline_wt, ignore_errors=True)
                _best_effort("worktree prune", lambda: ws.git("worktree", "prune"))
    except Exception as exc:
        exc_name = type(exc).__name__
        note = redact(f"{exc_name}: {exc}", secrets)[:500]
        log.warning("climb failed for %s: %s", run_id, note)
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
                f"Outcome: **climb-error**\n"
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
                    f"Run `{run_id}` finished (climb-error): {exc_name}. "
                    f"Details are in the run's record and report on the orchestrator.",
                ),
                secrets,
            )
        return LiveClimbOutcome(
            run_id=run_id,
            outcome="climb-error",
            # an outcome must never point at a report that was not written
            report_path=str(report_path) if wrote else "",
        )

    report = result.report(config, redact_secrets=secrets)
    report_path = run_dir / "report.md"
    wrote_report = _best_effort("run report", lambda: report_path.write_text(report), secrets)

    pr_url = ""
    outcome_name = result.outcome
    branch = ""
    pushed = False
    if result.outcome == "improved":
        try:
            # The committed tree must be EXACTLY the measured tree: code the
            # agent's solver wrote during the candidate eval was neither
            # scope-checked nor measured, so its presence voids the claim.
            if not result.measured_paths:
                raise WorkspaceDrift("improved with zero code changes — metric noise, not progress")
            post_eval = set(changed_paths())
            if post_eval != set(result.measured_paths):
                drift = sorted(post_eval.symmetric_difference(result.measured_paths))
                raise WorkspaceDrift(f"workspace changed during eval: {drift[:10]}")
            # Fail CLOSED: two fingerprints must exist (pre-eval from
            # climb_once's scope check, post-eval from just above) — a
            # missing one means the drift protection did not run.
            if len(tree_hashes) < 2:
                raise WorkspaceDrift("content fingerprints missing; drift check did not run")
            if tree_hashes[-1] != tree_hashes[-2]:
                raise WorkspaceDrift(
                    "file contents changed during eval (same paths, different bytes)"
                )
            # unique branch per run: a fixed name collides on the second run
            branch = f"{config.branch_prefix}/{run_id}"
            ws.branch(branch)
            if result.baseline is None or result.candidate is None:
                raise WorkspaceDrift("improved result missing measurements")
            bench = next(b for b in contract.benchmarks if b.name == config.benchmark)
            baseline, candidate = result.baseline, result.candidate

            # Freshness: the base branch may have MOVED during the climb
            # (sessions run for many minutes; another PR can merge meanwhile).
            # Landing the change on the clone's snapshot would open a
            # conflicted PR — or worse, a clean-merging one whose claim was
            # never measured against what it actually lands on. So: merge the
            # moved base INTO the run branch (merge commit — never rebase)
            # and re-measure on the merged tree before anything is pushed.
            ws.git_network("fetch", str(ws.url or ws.remote_url()), base_branch)
            fresh_base = ws.git("rev-parse", "FETCH_HEAD").strip()
            base_moved = fresh_base != base_sha
            if base_moved:
                # the agent's work goes in its own commit first, so the merge
                # commit stays a pure merge
                ws.commit_all(
                    f"agent: improve {config.benchmark}\n\nAgent: {config.agent_id}",
                    author=config.bot_login,
                    forbidden=lambda p: bool(out_of_scope([p], contract)),
                )
                try:
                    ws.git(
                        "-c",
                        f"user.name={config.bot_login}",
                        "-c",
                        f"user.email={config.bot_login}@users.noreply.github.com",
                        "merge",
                        "--no-edit",
                        "FETCH_HEAD",
                    )
                except GitError as exc:
                    # a content conflict and an infrastructure failure need
                    # different triage — do not report one as the other
                    conflicted = False
                    with contextlib.suppress(GitError):
                        conflicted = bool(ws.git("ls-files", "-u").strip())
                    with contextlib.suppress(GitError):
                        ws.git("merge", "--abort")
                    if conflicted:
                        raise WorkspaceDrift(
                            f"base branch moved during the climb and the merge "
                            f"conflicted: {str(exc)[:300]}"
                        ) from exc
                    raise WorkspaceDrift(
                        f"base branch moved and the merge FAILED (not a content "
                        f"conflict): {str(exc)[:300]}"
                    ) from exc
                # The claim must hold on the tree that actually lands —
                # BOTH sides of it. Upstream may have changed the metric for
                # everyone, so comparing a merged-tree candidate against the
                # pre-merge baseline would describe a delta that never
                # existed on any single tree (and could push a regression
                # relative to the fresh base). Both sides are measured in
                # throwaway worktrees of COMMITS — equivalent pristine
                # environments, and no dirty-tree check needed: eval writes
                # are discarded with the worktree and the shas pin content.
                merged_sha = ws.git("rev-parse", "HEAD").strip()
                seed_env = (
                    {bench.seed_env: str(result.run_seed)}
                    if bench.seed_env and result.run_seed
                    else None
                )
                baseline = _measure_committed(
                    ws, evaluator, run_dir, "fresh-base", fresh_base, bench, seed_env
                )
                candidate = _measure_committed(
                    ws, evaluator, run_dir, "merged", merged_sha, bench, seed_env
                )
                if not orch_improved(
                    baseline, candidate, bench.direction, config.min_relative_improvement
                ):
                    raise WorkspaceDrift(
                        f"candidate {candidate} does not beat the fresh base's "
                        f"{baseline} after merging the moved base (upstream "
                        f"absorbed or invalidated the improvement)"
                    )
                suite = result.suite
                if suite:
                    # the suite gate must hold on the tree that actually lands,
                    # same as the claim itself — re-measure every sibling on
                    # the fresh pair under the recorded suite seed
                    rows = []
                    for i, row in enumerate(suite):
                        sib = next(b for b in contract.benchmarks if b.name == row.name)
                        env = (
                            {sib.seed_env: str(result.suite_seed)}
                            if sib.seed_env and result.suite_seed
                            else None
                        )
                        # worktree labels use the sibling INDEX: the name is
                        # contract text (untrusted) and must not shape a path
                        sib_base = _measure_committed(
                            ws, evaluator, run_dir, f"fresh-base-sib{i}", fresh_base, sib, env
                        )
                        sib_cand = _measure_committed(
                            ws, evaluator, run_dir, f"merged-sib{i}", merged_sha, sib, env
                        )
                        regressed = suite_regressed(
                            sib_base, sib_cand, sib.direction, sib.min_delta, sib.min_delta_rel
                        )
                        if regressed:
                            raise SuiteRegressed(
                                f"suite regression after merging the moved base: "
                                f"{sib.name} {sib_base} -> {sib_cand}"
                            )
                        rows.append(
                            SuiteMeasurement(
                                name=sib.name,
                                baseline=sib_base,
                                candidate=sib_cand,
                                regressed=False,
                                display_digits=sib.display_digits,
                            )
                        )
                    suite = tuple(rows)
                if panel_lenses:
                    # the merged tree may carry an UPDATED contract: the
                    # fresh panel judges by the rules it will land under
                    try:
                        fresh_contract = ws.git("show", f"{fresh_base}:.autoresearch.yaml")
                    except GitError:
                        fresh_contract = contract_text
                    # the panel's verdict must hold on the tree that actually
                    # lands, same as the claim and the suite gate (terra, #95
                    # round 5). No wake here — the session has concluded, so
                    # blocking or degraded goes straight to the draft path.
                    merged_runner = build_panel_runner(
                        ws,
                        run_dir,
                        fresh_base,
                        panel_lenses,
                        fresh_contract,
                        config.target,
                        config.benchmark,
                        config.bot_login,
                        created[:10],
                        start_round=result.panel_rounds,
                    )
                    verdict = merged_runner(
                        baseline,
                        candidate,
                        result.session.final_text if result.session else "",
                    )
                    joined = (
                        f"{result.panel_transcript}\n\n{verdict.transcript}"
                        if result.panel_transcript
                        else verdict.transcript
                    )
                    result = dc_replace(
                        result,
                        panel_transcript=joined,
                        panel_rounds=result.panel_rounds + 1,
                        panel_blocking_open=result.panel_blocking_open or bool(verdict.blocking),
                        panel_degraded=verdict.degraded,
                    )
                result = dc_replace(result, baseline=baseline, candidate=candidate, suite=suite)

            # the report was written from the PRE-freshness result: refresh
            # it so the merged-tree measurements and panel verdict are the
            # record (terra note, #95 round 7)
            _best_effort(
                "run report refresh",
                lambda: report_path.write_text(result.report(config, redact_secrets=secrets)),
                secrets,
            )

            # Progress record (BENCHMARKS.md + results/leader.json), written
            # by the orchestrator from ITS measurements after the drift check
            # — read from the (possibly merged) tree, so the leader check runs
            # against the FRESH ledger, not the clone's snapshot.
            # We credit BEATING YOUR OWN BASELINE, not only beating the recorded
            # best (docs/design/research-loop.md, "two kinds of win"): a clean
            # composable win — improving over base_sha by the gate's threshold —
            # is a valid PR even when it is not the new SOTA. So there is no
            # recorded-best hard-fail here; the gate (`measure_and_decide`) has
            # already required candidate to beat base_sha on paired seeds. The
            # ledger's `best` still only advances on a genuine improvement over
            # it (update_leader), so SOTA stays tracked — just not required.
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
            # The commit veto re-checks FULL scope (allowed + forbidden) as
            # defense in depth behind climb_once's pre-eval check. The two
            # orchestrator-written progress files are the only exemption.
            # (When the base moved, the agent's work is already committed and
            # only the progress files remain to stage.)
            ws.commit_all(
                f"agent: improve {config.benchmark} "
                f"({_title_pair(baseline, candidate)})"
                f"\n\nAgent: {config.agent_id}",
                author=config.bot_login,
                forbidden=lambda p: p not in PROGRESS_PATHS and bool(out_of_scope([p], contract)),
            )
            # Last-moment re-check: the re-measurement above can take
            # minutes, and the base can move AGAIN meanwhile. This narrows
            # the unverified window to seconds; it cannot eliminate it.
            ws.git_network("fetch", str(ws.url or ws.remote_url()), base_branch)
            if ws.git("rev-parse", "FETCH_HEAD").strip() != fresh_base:
                raise WorkspaceDrift(
                    "base branch moved again during re-measurement; "
                    "ending without pushing (a later run will retry)"
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
                # blocking findings open at the panel cap, or a degraded
                # final read: visible, plainly not merge-ready
                draft=result.panel_blocking_open or result.panel_degraded,
            )
            # Arm auto-merge, best-effort, and ONLY when branch protection
            # requires a human review — the guard keeps bot-never-merges
            # enforced in code, not in per-repo config. Repos without
            # auto-merge enabled just log the refusal.
            pr_number = pr_url.rstrip("/").rsplit("/", 1)[-1]
            # never arm a draft: open blocking findings or an uncertified
            # read mean a human must look; approving+arming would route
            # around the panel
            if pr_number.isdigit() and not (result.panel_blocking_open or result.panel_degraded):
                _best_effort(
                    "auto-merge arming",
                    lambda: github.arm_auto_merge_when_review_required(
                        config.target, int(pr_number)
                    ),
                    secrets,
                )
            final = RunRecord(
                **{
                    **record.__dict__,
                    "state": IN_REVIEW,
                    "pr_url": pr_url,
                    "resume_session_id": result.session.session_id if result.session else "",
                    "ending_note": pr_url,
                }
            )
        except SuiteRegressed as exc:
            # the gate's verdict is the same wherever it fires: an honest
            # negative, not an abort — the merged tree just answered later
            outcome_name = "suite-regression"
            result = dc_replace(result, outcome="suite-regression", note=str(exc))
            log.info("suite-regressed for %s: %s", run_id, exc)
            final = RunRecord(
                **{
                    **record.__dict__,
                    "state": ENDED,
                    "ending": NEGATIVE_RESULT,
                    "ending_note": redact(str(exc), secrets)[:480],
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
    log.info("run %s: %s %s", run_id, outcome_name, pr_url)
    return LiveClimbOutcome(
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
    (measured 2026-08-08: scancel and walltime timeout both signal the
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

    parser = argparse.ArgumentParser(description="One live climb on one benchmark.")
    # --target/--benchmark drive a fresh climb; they are read from the record
    # on a --resume wake instead, so they are optional (validated below).
    parser.add_argument("--target", default="")
    parser.add_argument("--benchmark", default="")
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
    parser.add_argument("--panel-revisions", type=int, default=1)
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
    # resume after a fleet flip (review: terra blocking).
    # each --codex-config KEY=VALUE becomes a `-c KEY=VALUE` pair for codex
    codex_extra = tuple(a for c in args.codex_config for a in ("-c", c))

    bot_auth = FileTokenProvider(Path(args.pat_file))

    # --resume WAKES a parked dispatched run: no session, no api key, no panel —
    # just rebuild the dispatched measurer and re-enter the decision. The wake
    # job the WakeDispatcher submits runs exactly this.
    if args.resume:
        if not (args.account and args.partition and args.image and Path(args.image).is_file()):
            parser.error(
                "--resume needs the cluster triple (--account/--partition/--image) "
                "to rebuild the dispatched measurer"
            )
        from autoresearch.compute import SlurmCompute
        from autoresearch.runstate import load_record, release_lease

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
            release_lease(args.run_root, args.resume)
            parser.error(f"parked run {args.resume}: {_err}")
        # the wake runs the SAME verification panel as a fresh climb, so a
        # dispatched improvement is not published unverified.
        try:
            wake_lenses = _panel_lenses_from_args(args)
        except ValueError as exc:
            parser.error(str(exc))
        wake_panel_key = ""
        wake_api_key = ""
        wake_harness = None
        wake_spec = None
        # The editor harness is built when the wake may RESUME the session: a
        # panel is configured (a blocking finding wakes the author to revise),
        # or the park is an AUTHOR-SLEEP (that wake always resumes the session
        # with its launches' results). A panel-less candidate wake stays a pure
        # read-decide-publish job and must not require the author key.
        _wake_stage = getattr(_wake_record, "stage", None) or {}
        if wake_lenses:
            # the panel's judges need the verifier key; an author-sleep wake
            # alone does NOT — a panel-less deployment must not be forced to
            # provision an unused credential (terra, #135 r1).
            wake_panel_key = FileTokenProvider(Path(args.panel_key_file).expanduser()).token()
        if wake_lenses or _wake_stage.get("phase") == "author-sleep":
            wake_api_key = FileTokenProvider(Path(wake_key_file)).token()
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
                dispatch=DispatchSettings(
                    compute=SlurmCompute(),
                    image=args.image,
                    account=args.account,
                    partition=args.partition,
                ),
                github=GitHubClient(auth=bot_auth),
                bot_auth=bot_auth,
                now=time.time(),
                secrets=tuple(k for k in (bot_auth.token(), wake_panel_key, wake_api_key) if k),
                base_branch=args.base_branch,
                panel_lenses=wake_lenses,
                harness=wake_harness,
                spec=wake_spec,
                panel_revisions=args.panel_revisions,
            )
        finally:
            # This wake job HOLDS the run's lease (the sweep transferred it on
            # dispatch); release it on every exit so a re-parked run is
            # immediately eligible for the next sweep instead of waiting out the
            # TTL reap. Idempotent (no-op if no lease file).
            release_lease(args.run_root, args.resume)
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
    # same 0600 discipline as the PAT: this key spends real money
    api_key = FileTokenProvider(Path(args.key_file)).token()
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_id = f"{args.benchmark}-{stamp}"

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
        panel_lenses = _panel_lenses_from_args(args)
    except ValueError as exc:
        parser.error(str(exc))
    # the panel key joins the redaction set: judge error text can echo request
    # material like any other model error.
    panel_key = (
        FileTokenProvider(Path(args.panel_key_file).expanduser()).token()
        if args.panel.strip()
        else ""
    )

    # Dispatched measurement needs the full cluster triple AND a real image
    # file to bind against; missing any, the climb measures inline (the tick
    # sets these on the climb job's env, a bare CLI run leaves them empty).
    dispatch: DispatchSettings | None = None
    if args.account and args.partition and args.image and Path(args.image).is_file():
        from autoresearch.compute import SlurmCompute

        dispatch = DispatchSettings(
            compute=SlurmCompute(),
            image=args.image,
            account=args.account,
            partition=args.partition,
        )
    try:
        try:
            outcome = live_climb(
                config=ClimbConfig(target=args.target, benchmark=args.benchmark),
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
                panel_revisions=args.panel_revisions,
                dispatch=dispatch,
                evaluator=SubprocessEvaluator(container_image=args.image),
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
                        panel_key,
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
            # Fired in live_climb's microseconds-wide pre-containment window:
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
