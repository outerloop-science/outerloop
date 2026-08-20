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

from autoresearch.contract import load_contract
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
from autoresearch.harness import ClaudeCodeHarness, Harness, SessionResult, redact
from autoresearch.measure import DispatchSettings, LocalMeasurer
from autoresearch.orchestrator import (
    ClimbConfig,
    ClimbParked,
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
from autoresearch.verifier import MAX_CLAIM_CHARS

log = logging.getLogger(__name__)

# Where a climb job reads its keys unless the CLI flags say otherwise. The
# tick preflights the panel key (and compares it against the author key —
# role separation) before claiming/submitting.
PANEL_KEY_DEFAULT = "~/.config/autoresearch/verifier_key"
HARNESS_KEY_DEFAULT = "~/.config/autoresearch/harness_key"


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
    deadline = now + (effective_eval_minutes(eval_minutes) + PARK_QUEUE_SLACK_MIN) * 60
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

    # Every dispatched park is a CANDIDATE park (the baseline is measured by the
    # gate after the session, not before it), so a candidate sha must be
    # present. Guard rather than crash on `git diff base ""` if a stray
    # baseline-shaped record ever reached here.
    if stage.get("phase") != "candidate" or not stage.get("candidate_sha"):
        raise EvalError(f"resume_run: run {run_id} is not a candidate park (stage={stage!r})")

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
            seed=int(stage["seed"]),  # type: ignore[call-overload]
            suite_seed=int(stage["suite_seed"]),  # type: ignore[call-overload]
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
        )
        return LiveClimbOutcome(run_id=run_id, outcome="parked")

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
        try:
            # FORCE-checkout the sealed candidate: at wake the workspace still
            # holds the session's dirty tree (HEAD is pre_session_sha), so a
            # plain checkout could be blocked; the sha already captured exactly
            # the measured content.
            ws.git("checkout", "-f", "-B", branch, candidate_sha)

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
                result = dc_replace(
                    result,
                    panel_transcript=verdict.transcript,
                    panel_rounds=1,
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
    from autoresearch.review_agent import build_reviewer_harness

    panel_key = FileTokenProvider(Path(args.panel_key_file).expanduser()).token()
    lenses = []
    for kind, backend, model in parse_lenses(args.panel):
        hermes_repo_env = os.environ.get("REVIEW_HERMES_REPO", "").strip()
        try:
            judge = build_reviewer_harness(
                panel_key,
                backend=backend,
                binary=args.claude_bin if backend == "claude" else None,
                model=model or None,
                container_image=args.image if backend == "claude" else "",
                hermes_repo=Path(hermes_repo_env) if hermes_repo_env else None,
                provider=os.environ.get("REVIEW_HERMES_PROVIDER", "openrouter"),
            )
        except ValueError as exc:
            raise ValueError(f"panel entry {kind}:{backend}: {exc}") from exc
        lenses.append(PanelLens(kind=kind, harness=judge))
    return tuple(lenses)


def build_editor_harness(
    api_key: str,
    spec: RoleSpec | None = None,
    *,
    binary: str | None = None,
    model: str | None = None,
    container_image: str = "",
) -> ClaudeCodeHarness:
    """Construct an editing role's harness from its RoleSpec — the same
    deployment wiring the judges use (`spec.budget` drives turns and walltime,
    `spec.tools` the tool set, so manifest and harness cannot disagree). The
    session runs contained (apptainer) and KEEPS instruction-file discovery —
    the target repo's CLAUDE.md is legitimate guidance for an editing role,
    unlike a judge's untrusted checkout.

    Claude-only by validation status, not by design: the seam (`run_role`,
    `climb_once`) takes any Harness. A codex or hermes editor needs its
    resume and write+execute containment story bench-validated first; then
    it is a new branch here, zero kernel change (the reviewer's rollout)."""
    spec = spec or author_spec()
    if not spec.execution.can_execute:
        raise ValueError("build_editor_harness is for editing roles")
    return ClaudeCodeHarness(
        api_key=api_key,
        binary=binary or "claude",
        model=model or "claude-opus-5",
        max_turns=spec.budget.max_turns,
        timeout_s=spec.budget.walltime_s,
        # the manifest drives the tool set, same as the budget (all author
        # tools are native Claude tools; no MCP tools to filter out)
        allowed_tools=spec.tools,
        container_image=container_image,
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
            )
        except ClimbParked as p:
            # The climb dispatched its measures and hibernated. Persist the
            # re-entry stage as a WAITING record (not an error), keep the
            # candidate snapshot alive for the wake, and end. The wake re-enters
            # from the record (the wake path is a later PR). `parked` is set only
            # AFTER a successful write: if _park_run raises, it stays None so the
            # finally drops every snapshot (no leak) and the outer handler ends
            # the run as an error rather than a half-written hibernation.
            if p.phase == "candidate":
                # keep exactly ONE snapshot for that sha (two can share a
                # commit); record and keep that same ref, drop the rest.
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
    parser.add_argument("--model", default="claude-opus-5")
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
    parser.add_argument("--key-file", default=os.path.expanduser(HARNESS_KEY_DEFAULT))
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
        from autoresearch.runstate import release_lease

        # the wake runs the SAME verification panel as a fresh climb, so a
        # dispatched improvement is not published unverified.
        try:
            wake_lenses = _panel_lenses_from_args(args)
        except ValueError as exc:
            parser.error(str(exc))
        wake_panel_key = (
            FileTokenProvider(Path(args.panel_key_file).expanduser()).token()
            if args.panel.strip()
            else ""
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
                secrets=tuple(k for k in (bot_auth.token(), wake_panel_key) if k),
                base_branch=args.base_branch,
                panel_lenses=wake_lenses,
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

    # same 0600 discipline as the PAT: this key spends real money
    api_key = FileTokenProvider(Path(args.key_file).expanduser()).token()
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
                harness=build_editor_harness(
                    api_key,
                    spec,
                    binary=args.claude_bin,
                    model=args.model,
                    container_image=args.image,
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
