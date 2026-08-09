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
from pathlib import Path
from typing import Any

from autoresearch.contract import load_contract
from autoresearch.github import (
    FileTokenProvider,
    GitError,
    GitHubClient,
    Workspace,
)
from autoresearch.harness import ClaudeCodeHarness, Harness, redact
from autoresearch.orchestrator import (
    ClimbConfig,
    Evaluator,
    SubprocessEvaluator,
    climb_once,
    out_of_scope,
    pr_body,
)
from autoresearch.orchestrator import improved as orch_improved
from autoresearch.progress import (
    PROGRESS_PATHS,
    load_leader,
    update_leader,
    write_progress,
)
from autoresearch.runstate import (
    ABORTED,
    ENDED,
    IN_REVIEW,
    NEGATIVE_RESULT,
    RunRecord,
    save_record,
)

log = logging.getLogger(__name__)


class WorkspaceDrift(RuntimeError):
    """The tree changed between measurement and commit."""


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
    "session-error": ABORTED,
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


def _measure_committed(
    ws: Workspace, evaluator: Evaluator, run_dir: Path, name: str, sha: str, bench: Any
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
        return float(evaluator.evaluate(wt, bench.command, bench.metric))
    finally:
        removed = _best_effort(
            "worktree cleanup", lambda: ws.git("worktree", "remove", "--force", str(wt))
        )
        if not removed:  # never silent: a leaked worktree is a disk leak —
            # and prune alone only drops the ADMIN entry, so delete the
            # directory itself first
            _best_effort("worktree dir removal", lambda: shutil.rmtree(wt, ignore_errors=True))
            _best_effort("worktree prune", lambda: ws.git("worktree", "prune"))


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
) -> LiveClimbOutcome:
    """Run one climb against the real target repo."""
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
        ws = Workspace.clone(f"https://github.com/{config.target}.git", workspace, auth=bot_auth)
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

        result = climb_once(
            config,
            contract_text,
            workspace,
            harness,
            evaluator,
            ruler=RULER,
            changed_paths=changed_paths,
            created=created,
            task_hypothesis=task_hypothesis,
        )
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
                baseline = _measure_committed(
                    ws, evaluator, run_dir, "fresh-base", fresh_base, bench
                )
                candidate = _measure_committed(ws, evaluator, run_dir, "merged", merged_sha, bench)
                if not orch_improved(
                    baseline, candidate, bench.direction, config.min_relative_improvement
                ):
                    raise WorkspaceDrift(
                        f"candidate {candidate} does not beat the fresh base's "
                        f"{baseline} after merging the moved base (upstream "
                        f"absorbed or invalidated the improvement)"
                    )
                result = dc_replace(result, baseline=baseline, candidate=candidate)

            # Progress record (BENCHMARKS.md + results/leader.json), written
            # by the orchestrator from ITS measurements after the drift check
            # — read from the (possibly merged) tree, so the leader check runs
            # against the FRESH ledger, not the clone's snapshot.
            prior = load_leader(workspace).get(config.benchmark)
            if prior is not None and not orch_improved(
                prior.best, candidate, bench.direction, config.min_relative_improvement
            ):
                raise WorkspaceDrift(
                    f"candidate {candidate} does not beat the recorded "
                    f"best {prior.best} by the noise floor (stale clone or eval noise)"
                )
            entries = update_leader(
                load_leader(workspace),
                benchmark=bench.name,
                metric=bench.metric,
                direction=bench.direction,
                baseline=baseline,
                candidate=candidate,
                run_id=run_id,
                date=created[:10],
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
            )
            # Arm auto-merge, best-effort, and ONLY when branch protection
            # requires a human review — the guard keeps bot-never-merges
            # enforced in code, not in per-repo config. Repos without
            # auto-merge enabled just log the refusal.
            pr_number = pr_url.rstrip("/").rsplit("/", 1)[-1]
            if pr_number.isdigit():
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
        summary = redact(result.report(config, redact_secrets=secrets), secrets)[:8000]
        link = f"\n\nPull request: {pr_url}" if pr_url else ""
        _best_effort(
            "issue report",
            lambda: github.comment(
                config.target,
                issue_number,
                f"Run `{run_id}` finished ({outcome_name}).{link}\n\n{summary}",
            ),
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
    parser.add_argument("--target", required=True)
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--image", default="", help="apptainer image for session+eval")
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
        "--key-file", default=os.path.expanduser("~/.config/autoresearch/harness_key")
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

    # same 0600 discipline as the PAT: this key spends real money
    api_key = FileTokenProvider(Path(args.key_file)).token()
    bot_auth = FileTokenProvider(Path(args.pat_file))
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

    try:
        try:
            outcome = live_climb(
                config=ClimbConfig(target=args.target, benchmark=args.benchmark),
                run_root=args.run_root,
                run_id=run_id,
                harness=ClaudeCodeHarness(
                    api_key=api_key,
                    binary=args.claude_bin,
                    model=args.model,
                    max_turns=args.max_turns,
                    timeout_s=args.session_minutes * 60,
                    container_image=args.image,
                ),
                evaluator=SubprocessEvaluator(container_image=args.image),
                github=GitHubClient(auth=bot_auth),
                bot_auth=bot_auth,
                now=time.time(),
                created=datetime.now(UTC).isoformat(),
                secrets=(api_key, bot_auth.token()),
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
