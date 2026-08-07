"""One live climb, end to end: clone → climb_once → commit/push/PR → report.

This is the glue `orchestrator.climb_once` deliberately does not own: the git
side (bot-auth clone, veto-checked commit, push, PR) and the run's durable
record. One invocation = one run = at most one PR.

Credential separation holds throughout: the bot PAT is read orchestrator-side
and used only by Workspace network calls and the PR client, after the session
has ended; the session sees only its own capped API key inside its container.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from autoresearch.contract import load_contract
from autoresearch.github import (
    FileTokenProvider,
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
    record = RunRecord(
        run_id=run_id,
        target=config.target,
        task_title=f"improve {config.benchmark}",
        benchmark=config.benchmark,
        state="implementing",
        agent_id=config.agent_id,
        deadline=now + 24 * 3600,
        issue_number=issue_number,
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
            # Progress record (BENCHMARKS.md + results/leader.json), written
            # by the orchestrator from ITS measurements after the drift check
            # — the improvement and its human-readable record land in one PR.
            if result.baseline is None or result.candidate is None:
                raise WorkspaceDrift("improved result missing measurements")
            bench = next(b for b in contract.benchmarks if b.name == config.benchmark)
            prior = load_leader(workspace).get(config.benchmark)
            if prior is not None and not orch_improved(
                prior.best, result.candidate, bench.direction, config.min_relative_improvement
            ):
                raise WorkspaceDrift(
                    f"candidate {result.candidate} does not beat the recorded "
                    f"best {prior.best} by the noise floor (stale clone or eval noise)"
                )
            entries = update_leader(
                load_leader(workspace),
                benchmark=bench.name,
                metric=bench.metric,
                direction=bench.direction,
                baseline=result.baseline,
                candidate=result.candidate,
                run_id=run_id,
                date=created[:10],
            )
            write_progress(workspace, entries, config.target)
            # The commit veto re-checks FULL scope (allowed + forbidden) as
            # defense in depth behind climb_once's pre-eval check. The two
            # orchestrator-written progress files are the only exemption.
            ws.commit_all(
                f"agent: improve {config.benchmark} "
                f"({_title_pair(result.baseline, result.candidate)})"
                f"\n\nAgent: {config.agent_id}",
                author=config.bot_login,
                forbidden=lambda p: p not in PROGRESS_PATHS and bool(out_of_scope([p], contract)),
            )
            ws.push(branch)
            pushed = True
            body = pr_body(result, config, redact_secrets=secrets)
            if issue_number:
                body = f"Addresses #{issue_number}.\n\n{body}"
            pr_url = github.create_pull(
                config.target,
                # short precision in the title; full precision lives in the
                # PR body table and the ledger
                title=f"[agent] {config.benchmark}: "
                f"{_title_pair(result.baseline, result.candidate)}",
                head=branch,
                base=base_branch,
                body=body,
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


def main() -> int:
    import argparse
    import os
    import time
    from datetime import UTC, datetime

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

    outcome = live_climb(
        config=ClimbConfig(target=args.target, benchmark=args.benchmark),
        run_root=args.run_root,
        run_id=run_id,
        harness=ClaudeCodeHarness(
            api_key=api_key,
            binary=args.claude_bin,
            model=args.model,
            max_turns=args.max_turns,
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
    print(f"outcome={outcome.outcome} pr={outcome.pr_url or '-'} report={outcome.report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
