"""In-review follow-up: humans steer the run through its PR.

One `respond_once` call services one in-review run (docs/design/architecture.md,
"The life of a run"): PR merged or closed ends the run; new qualifying
comments wake the SAME agent session that wrote the code — native resume in
the retained workspace — and its answer goes back to the thread as the bot,
with any code changes scope-checked, re-measured, and pushed to the PR branch.

Comment gating mirrors the intake gate without extra API scopes: GitHub's
`author_association` field marks OWNER/MEMBER/COLLABORATOR, which is exactly
"people with standing in this repo". Everything else — including the bot's
own comments and the advisory marker — is ignored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path

from autoresearch.brief import render_review_wake
from autoresearch.contract import load_contract
from autoresearch.github import GitHubClient, Workspace
from autoresearch.harness import Harness, redact
from autoresearch.orchestrator import Evaluator, out_of_scope
from autoresearch.orchestrator import improved as orch_improved
from autoresearch.progress import PROGRESS_PATHS, load_leader, update_leader, write_progress
from autoresearch.review import APPROVAL_PATTERN, REDACTED
from autoresearch.runstate import (
    ENDED,
    IN_REVIEW,
    MERGED,
    REJECTED,
    RunRecord,
    acquire_lease,
    load_record,
    release_lease,
    run_dir,
    save_record,
)

log = logging.getLogger(__name__)

QUALIFYING_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
MAX_COMMENTS_PER_WAKE = 5
MAX_REPLY_CHARS = 20_000

REPLY_MARKER = "<!-- autoresearch:followup -->"


@dataclass(frozen=True)
class FollowupOutcome:
    run_id: str
    action: str  # "ended-merged" | "ended-rejected" | "no-op" | "replied" | "error"
    note: str = ""


def _pr_number(pr_url: str) -> int:
    tail = pr_url.rstrip("/").rsplit("/", 1)[-1]
    if not tail.isdigit():
        raise ValueError(f"cannot parse PR number from {pr_url!r}")
    return int(tail)


def qualifying_comments(
    comments: list[dict], bot_login: str, since_id: int
) -> list[tuple[int, str, str]]:
    """(id, author, body) for comments that may steer the run."""
    picked = []
    for comment in comments:
        cid = comment.get("id")
        if not isinstance(cid, int) or cid <= since_id:
            continue
        author = str((comment.get("user") or {}).get("login", ""))
        if author.casefold() == bot_login.casefold():
            continue
        body = str(comment.get("body") or "")
        if not body.strip():
            continue  # e.g. a review submission with no text
        if REPLY_MARKER in body or "autoresearch:advisory-review" in body:
            continue
        if str(comment.get("author_association", "")) not in QUALIFYING_ASSOCIATIONS:
            continue
        picked.append((cid, author, body))
    return picked


def respond_once(
    run_root: Path,
    run_id: str,
    harness: Harness,
    evaluator: Evaluator,
    github: GitHubClient,
    bot_login: str,
    now: float,
    secrets: tuple[str, ...] = (),
    created: str = "",
) -> FollowupOutcome:
    record = load_record(run_root, run_id)
    if record.state != IN_REVIEW:
        return FollowupOutcome(run_id, "no-op", f"state is {record.state}, not in-review")
    if not record.pr_url:
        return FollowupOutcome(run_id, "error", "in-review run has no pr_url")
    number = _pr_number(record.pr_url)
    # The same lease that serializes experiment wakes serializes follow-ups:
    # two concurrent responders would double-spend a session and double-reply.
    if not acquire_lease(run_root, run_id, holder=f"followup:{now}", holder_job_id="", now=now):
        return FollowupOutcome(run_id, "no-op", "lease held; another responder is active")
    try:
        return _respond(
            run_root,
            run_id,
            record,
            number,
            harness,
            evaluator,
            github,
            bot_login,
            now,
            secrets,
            created,
        )
    finally:
        release_lease(run_root, run_id)


def _respond(
    run_root: Path,
    run_id: str,
    record: RunRecord,
    number: int,
    harness: Harness,
    evaluator: Evaluator,
    github: GitHubClient,
    bot_login: str,
    now: float,
    secrets: tuple[str, ...],
    created: str,
) -> FollowupOutcome:

    pr = github.get_pull_request(record.target, number)
    if pr.get("merged") or pr.get("merged_at"):
        save_record(run_root, replace(record, state=ENDED, ending=MERGED), now)
        return FollowupOutcome(run_id, "ended-merged")
    if pr.get("state") == "closed":
        save_record(
            run_root,
            replace(record, state=ENDED, ending=REJECTED, ending_note="PR closed unmerged"),
            now,
        )
        return FollowupOutcome(run_id, "ended-rejected")

    # All three places a maintainer can write: the conversation tab, a
    # top-level review, and inline Files-changed comments. GitHub ids share
    # one global space, so a single cursor covers them.
    sources = list(github.list_comments(record.target, number))
    sources += list(github.list_pr_reviews(record.target, number))
    sources += list(github.list_pr_review_comments(record.target, number))
    comments = qualifying_comments(sources, bot_login, record.last_comment_id)
    if not comments:
        return FollowupOutcome(run_id, "no-op", "no new qualifying comments")
    comments = comments[:MAX_COMMENTS_PER_WAKE]
    newest_id = max(cid for cid, _, _ in comments)

    workspace = run_dir(run_root, run_id) / "ws"
    if not workspace.is_dir():
        return FollowupOutcome(run_id, "error", "workspace no longer exists (GC'd?)")
    ws = Workspace(root=workspace, auth=github.auth, url=None)
    contract_text = (workspace / ".autoresearch.yaml").read_text()
    contract = load_contract(contract_text, record.target)
    bench = next((b for b in contract.benchmarks if b.name == record.benchmark), None)
    if bench is None:
        return FollowupOutcome(
            run_id, "error", f"benchmark {record.benchmark!r} not in the contract"
        )

    prompt = render_review_wake([(author, body) for _, author, body in comments])
    session = harness.run(prompt, workspace, resume_session_id=record.resume_session_id or None)
    if session.is_error:
        # cursor NOT advanced: the next attempt sees the same comments
        return FollowupOutcome(run_id, "error", f"session: {session.stop_reason}")

    # Same self-approval scrub as the reviewer: the pipeline must never nudge
    # humans toward merging its own work, even in the author's voice.
    reply_body = APPROVAL_PATTERN.sub(REDACTED, redact(session.final_text, secrets))[
        :MAX_REPLY_CHARS
    ]
    measured_note = ""

    changed = _changed_paths(ws)
    if changed:
        violations = out_of_scope(changed, contract)
        if violations:
            # revert the out-of-scope response; reply honestly, keep the PR
            ws.git("checkout", "--", ".")
            ws.git("clean", "-fdq")
            measured_note = (
                "\n\n_(A code change was attempted but touched paths outside "
                "the contract's scope and was not applied.)_"
            )
        else:
            pre_eval_tree = _tree_hash(ws)
            try:
                candidate = evaluator.evaluate(workspace, bench.command, bench.metric)
            except Exception as exc:
                ws.git("checkout", "--", ".")
                ws.git("clean", "-fdq")
                measured_note = (
                    "\n\n_(A code change was attempted but the eval failed on "
                    f"it, so it was not applied: {redact(str(exc), secrets)[:200]})_"
                )
            else:
                if _tree_hash(ws) != pre_eval_tree:
                    # same drift rule as the climb: the pushed tree must be
                    # exactly the measured tree
                    ws.git("checkout", "--", ".")
                    ws.git("clean", "-fdq")
                    measured_note = (
                        "\n\n_(A code change was attempted but the tree "
                        "changed during measurement, so it was not applied.)_"
                    )
                else:
                    prior = load_leader(workspace).get(bench.name)
                    entries = update_leader(
                        load_leader(workspace),
                        benchmark=bench.name,
                        metric=bench.metric,
                        direction=bench.direction,
                        baseline=candidate,  # pinned by existing entry if present
                        candidate=candidate,
                        run_id=run_id,
                        date=created[:10],
                    )
                    write_progress(workspace, entries, record.target)
                    branch = _current_branch(ws)
                    ws.commit_all(
                        f"agent: address review feedback ({bench.metric}={candidate:.6g})",
                        author=record.agent_id,
                        forbidden=lambda p: (
                            p not in PROGRESS_PATHS and bool(out_of_scope([p], contract))
                        ),
                    )
                    ws.push(branch)
                    worse = prior is not None and not orch_improved(
                        prior.best, candidate, bench.direction, 0.0
                    )
                    measured_note = (
                        f"\n\n**Re-measured after this change: `{bench.metric}` = "
                        f"{candidate:.6g}**"
                        + (
                            " — worse than the PR's previous number, stated plainly."
                            if worse
                            else ""
                        )
                    )

    github.comment(record.target, number, f"{REPLY_MARKER}\n{reply_body}{measured_note}")
    save_record(
        run_root,
        replace(
            record,
            last_comment_id=newest_id,
            resume_session_id=session.session_id or record.resume_session_id,
        ),
        now,
    )
    return FollowupOutcome(run_id, "replied", f"processed {len(comments)} comment(s)")


def _changed_paths(ws: Workspace) -> list[str]:
    ws.git("add", "-A")
    paths = ws.staged_paths()
    ws.git("reset")
    return paths


def _tree_hash(ws: Workspace) -> str:
    ws.git("add", "-A")
    tree = ws.git("write-tree").strip()
    ws.git("reset")
    return tree


def _current_branch(ws: Workspace) -> str:
    return ws.git("rev-parse", "--abbrev-ref", "HEAD").strip()


def main() -> int:
    import argparse
    import os
    import time

    from autoresearch.github import FileTokenProvider
    from autoresearch.harness import ClaudeCodeHarness
    from autoresearch.orchestrator import SubprocessEvaluator

    parser = argparse.ArgumentParser(description="Service one in-review run.")
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--image", default="")
    parser.add_argument("--uncontained", action="store_true")
    parser.add_argument("--claude-bin", default=os.path.expanduser("~/.local/bin/claude"))
    parser.add_argument("--model", default="claude-opus-5")
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument("--bot-login", default="agentic-learning-bot")
    parser.add_argument("--pat-file", default=os.path.expanduser("~/.config/autoresearch/bot_pat"))
    parser.add_argument(
        "--key-file", default=os.path.expanduser("~/.config/autoresearch/harness_key")
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if not args.image and not args.uncontained:
        parser.error("--image is required (or pass --uncontained explicitly, dev only)")

    from datetime import UTC, datetime

    api_key = FileTokenProvider(Path(args.key_file)).token()
    bot_auth = FileTokenProvider(Path(args.pat_file))
    outcome = respond_once(
        args.run_root,
        args.run_id,
        harness=ClaudeCodeHarness(
            api_key=api_key,
            binary=args.claude_bin,
            model=args.model,
            max_turns=args.max_turns,
            container_image=args.image,
        ),
        evaluator=SubprocessEvaluator(container_image=args.image),
        github=GitHubClient(auth=bot_auth),
        bot_login=args.bot_login,
        now=time.time(),
        secrets=(api_key, bot_auth.token()),
        created=datetime.now(UTC).isoformat(),
    )
    print(f"action={outcome.action} note={outcome.note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
