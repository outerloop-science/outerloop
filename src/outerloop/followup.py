"""In-review follow-up: humans steer the run through its PR.

One `respond_once` call services one in-review run (docs/design/architecture.md,
"The life of a run"): PR merged or closed ends the run; new qualifying
comments wake the SAME agent session that wrote the code — native resume in
the retained workspace — and its answer goes back to the thread as the bot,
with submitted changes measured and published through the shared author engine.

Comment gating mirrors the intake gate without extra API scopes: GitHub's
`author_association` field marks OWNER/MEMBER/COLLABORATOR, which is exactly
"people with standing in this repo". Everything else — including the bot's
own comments and the advisory marker — is ignored.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from outerloop.measure import DispatchSettings

from outerloop.contract import load_contract
from outerloop.dispatch import image_file_arg
from outerloop.github import (
    GitHubClient,
    GitHubError,
    Workspace,
    bot_login_from_env,
    contract_at,
    is_own_login,
)
from outerloop.harness import Harness, SessionResult, default_binary, outage, redact
from outerloop.inbox import (
    Message,
    append,
    delivered_seq,
    pending,
    thread_for,
)
from outerloop.markers import has_marker, marker
from outerloop.orchestrator import (
    Evaluator,
    out_of_scope,
    steward_out_of_scope,
)
from outerloop.review import APPROVAL_PATTERN, REDACTED
from outerloop.role_runner import role_key
from outerloop.roles import followup_spec
from outerloop.rolespec import RoleSpec
from outerloop.runstate import (
    ENDED,
    IN_REVIEW,
    MERGED,
    REJECTED,
    WAITING,
    RunRecord,
    acquire_lease,
    load_record,
    release_lease,
    run_dir,
    save_record,
    stamp_outage,
)
from outerloop.verifier import VERIFY_MARKER

log = logging.getLogger(__name__)

QUALIFYING_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
MAX_COMMENTS_PER_WAKE = 5
MAX_REPLY_CHARS = 20_000

REPLY_MARKER = marker("followup")


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


MAX_CONTEXT_COMMENTS = 3
MAX_CONTEXT_COMMENT_CHARS = 4_000


# The verifier posts its rounds via the Actions workflow token — an identity
# no ordinary account can assume. Marker text alone is public and forgeable;
# identity + marker together are not. The marker is the renderer's own
# constant, and marker-first is its tested shape: the body starts with the
# marker (asserted in the render test) and publishes through posting.post_round,
# which inserts the round stamp AFTER the marker — always as an ISSUE comment.
# That is why this reads one collection and matches at the start of the body;
# a quote-reply prefixes every line with "> ", so quoted rounds can never
# re-qualify. (The advisory reviewer posts inline reviews on human PRs, which
# never ride into a bot-PR wake, so its marker is not here.)
ACTIONS_BOT_LOGIN = "github-actions[bot]"
MACHINE_ROUND_MARKERS = (VERIFY_MARKER,)


def context_comments(comments: list[dict], since_id: int) -> list[tuple[str, str]]:
    """(author, body) for NEW machine review rounds — the verifier's,
    identified by POSTING IDENTITY plus marker. They never trigger a wake
    and never steer; they ride along as data-fenced CONTEXT so a woken
    agent can see what a maintainer's one-line 'address the findings'
    refers to, without a human relaying the text by hand.

    Deliberately NOTHING else qualifies: on a public repo, arbitrary
    commenters would otherwise get their text injected into a session with
    push access, guarded only by advisory fencing. A drive-by comment
    worth the agent's attention is a
    maintainer's to quote — quoting is the human act that grants standing.
    """
    picked: list[tuple[str, str]] = []
    for comment in comments:
        cid = comment.get("id")
        if not isinstance(cid, int) or cid <= since_id:
            continue
        author = str((comment.get("user") or {}).get("login", ""))
        if author.casefold() != ACTIONS_BOT_LOGIN.casefold():
            continue
        body = str(comment.get("body") or "")
        if not any(body.lstrip().startswith(m) for m in MACHINE_ROUND_MARKERS):
            continue
        if len(body) > MAX_CONTEXT_COMMENT_CHARS:
            body = body[:MAX_CONTEXT_COMMENT_CHARS] + "\n…[truncated]"
        picked.append((author, body))
    return picked[-MAX_CONTEXT_COMMENTS:]


def dirty_pr_head(pr: dict) -> str:
    """The head sha when an OPEN PR conflicts with its base, else "".
    GitHub computes mergeability lazily: mergeable None means unknown (not
    dirty), so a fresh PR never false-positives — the next tick re-asks."""
    if pr.get("state") != "open" or pr.get("merged"):
        return ""
    if pr.get("mergeable") is False or pr.get("mergeable_state") == "dirty":
        return str((pr.get("head") or {}).get("sha", ""))
    return ""


def stale_pr_head(pr: dict) -> str:
    """The head sha when an OPEN PR is cleanly mergeable but BEHIND its base,
    else "". A moved base staled the measured claim (publish deliberately
    declined to arm auto-merge), so the author is woken to merge the base in
    and the result is re-measured — same machinery as a conflict, minus the
    resolving."""
    if pr.get("state") != "open" or pr.get("merged"):
        return ""
    if pr.get("mergeable") is True and pr.get("mergeable_state") == "behind":
        return str((pr.get("head") or {}).get("sha", ""))
    return ""


def base_sync_head(pr: dict) -> str:
    """The head needing a base sync — conflicted or merely behind."""
    return dirty_pr_head(pr) or stale_pr_head(pr)


def conflict_wake_action(record: RunRecord, pr: dict) -> str:
    """Tick-side gate (cheap, read-only, PURE — the caller fetched the PR):
    "wake" for an in-review PR whose base moved out from under it —
    conflicted OR cleanly behind — and that has not been woken for THIS
    head yet; "clear" when a previously-woken PR is current again (the base
    can move and stale the SAME head a second time, so the cursor must
    re-arm); "" otherwise."""
    head = base_sync_head(pr)
    if head and head != record.dirty_wake_head:
        return "wake"
    if not head and record.dirty_wake_head and pr.get("mergeable") is True:
        return "clear"
    return ""


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
        if is_own_login(author, bot_login):
            continue
        body = str(comment.get("body") or "")
        if not body.strip():
            continue  # e.g. a review submission with no text
        if has_marker(body, "followup") or has_marker(body, "advisory-review"):
            continue
        if str(comment.get("author_association", "")) not in QUALIFYING_ASSOCIATIONS:
            continue
        picked.append((cid, author, body))
    return picked


def _ending_comment(record: RunRecord, ending: str) -> str:
    """What the requesting issue is told when its run's PR merges or closes.

    Claims are what make an open issue inert: intake never re-picks a
    claimed issue, and the steward lane re-claims only after a release
    marker. So a merge says "close when satisfied — fresh work needs a
    fresh issue", and a human-closed steward PR posts its OWN release
    (honest wording; otherwise reconciliation would release it later
    as "killed or crashed").
    """
    from outerloop.steward import MAX_STEWARD_ATTEMPTS, RELEASE_MARKER

    if ending == MERGED:
        return (
            f"Pull request {record.pr_url} was merged; run `{record.run_id}` is "
            "complete. Close this issue when the request is satisfied. Leaving "
            "it open queues nothing — a claimed issue is never picked up again, "
            "so further work needs a fresh issue."
        )
    if record.agent_id.startswith("steward"):
        return (
            f"{RELEASE_MARKER}\nPull request {record.pr_url} was closed without "
            f"merging; run `{record.run_id}` ended. Claim released — the lane "
            f"retries up to {MAX_STEWARD_ATTEMPTS} total attempts, then waits "
            "for a human."
        )
    return (
        f"Pull request {record.pr_url} was closed without merging; run "
        f"`{record.run_id}` ended. This issue stays claimed — file a fresh "
        "issue to request another attempt."
    )


def _release_parked_snapshot(run_root: Path, record: RunRecord) -> None:
    """A run that ends while a dispatched re-measure is parked must not leave
    the sealed commit's retaining ref behind (best-effort: the ending is
    load-bearing, the ref release is hygiene — a failure logs)."""
    from outerloop.dispatch import Snapshot, drop_snapshot

    ws = Workspace(root=run_dir(run_root, record.run_id) / "ws")
    for stage in (record.stage,):
        ref = str(stage.get("candidate_ref", "") or "")
        if not ref:
            continue
        try:
            drop_snapshot(
                ws, Snapshot(commit=str(stage.get("candidate_sha", "")), tree="", ref=ref)
            )
        except Exception as exc:
            log.warning("parked snapshot release failed for %s: %s", record.run_id, exc)


def _end_run(
    run_root: Path, record: RunRecord, github: GitHubClient, ending: str, note: str, now: float
) -> None:
    """Flip the record to ended, then tell the requesting issue (best effort:
    the state transition is load-bearing, the comment is a courtesy — a
    comment failure logs and is never retried)."""
    _release_parked_snapshot(run_root, record)
    save_record(
        run_root,
        replace(record, state=ENDED, ending=ending, ending_note=note),
        now,
    )
    if not record.issue_number:
        return
    try:
        github.comment(record.target, record.issue_number, _ending_comment(record, ending))
    except Exception as exc:
        log.warning(
            "ending comment on %s#%s failed for %s: %s",
            record.target,
            record.issue_number,
            record.run_id,
            type(exc).__name__,
        )


def close_if_done(run_root: Path, record: RunRecord, github: GitHubClient, now: float) -> str:
    """End the run if its PR is merged/closed. Returns the ending or ""."""
    if not acquire_lease(run_root, record.run_id, holder=f"close:{now}", holder_job_id="", now=now):
        return ""
    try:
        record = load_record(run_root, record.run_id)
        number = _pr_number(record.pr_url)
        try:
            pr = github.get_pull_request(record.target, number)
        except GitHubError as exc:
            if exc.status == 404:
                # The PR was deleted out from under us — nothing left to review or
                # merge. End the run (state transition + a courtesy note on the
                # issue, not the gone PR) rather than re-fetching a 404 every tick.
                _end_run(run_root, record, github, REJECTED, "PR no longer exists", now)
                return REJECTED
            raise
        if pr.get("merged") or pr.get("merged_at"):
            _end_run(run_root, record, github, MERGED, "", now)
            return MERGED
        if pr.get("state") == "closed":
            _end_run(run_root, record, github, REJECTED, "PR closed unmerged", now)
            return REJECTED
        return ""
    finally:
        release_lease(run_root, record.run_id)


def inbox_wake_pending(run_root: Path, record: RunRecord) -> bool:
    """Undelivered messages trigger the next review leg."""
    return bool(
        record.panel_wake_text or pending(run_dir(run_root, record.run_id), record.inbox_seq)
    )


def has_new_comments(record: RunRecord, github: GitHubClient, bot_login: str) -> bool:
    """Cheap read-only check the tick can afford every cycle."""
    number = _pr_number(record.pr_url)
    return bool(
        qualifying_comments(
            github.list_comments(record.target, number), bot_login, record.last_comment_id
        )
        or qualifying_comments(
            github.list_pr_reviews(record.target, number), bot_login, record.last_review_id
        )
        or qualifying_comments(
            github.list_pr_review_comments(record.target, number),
            bot_login,
            record.last_review_comment_id,
        )
    )


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
    spec: RoleSpec | None = None,
    panel_lenses: tuple[Any, ...] = (),
    panel_builder: Callable[..., Callable[[float, float, str], Any]] | None = None,
    panel_skip: str = "",
    dispatch: DispatchSettings | None = None,
) -> FollowupOutcome:
    """Resume the author on review messages; only submit measures and publishes."""
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
            spec,
            panel_lenses,
            panel_builder,
            panel_skip,
            dispatch,
        )
    except Exception as exc:
        log.warning("followup failed for %s: %s", run_id, redact(str(exc), secrets))
        return FollowupOutcome(
            run_id, "error", redact(f"{type(exc).__name__}: {exc}", secrets)[:300]
        )
    finally:
        release_lease(run_root, run_id)


def build_review_messages(
    run_root: Path,
    record: RunRecord,
    number: int,
    github: GitHubClient,
    bot_login: str,
    now: float,
    pr: dict,
    base_sha_at_fetch: str = "",
) -> tuple[RunRecord, dict[str, int], list[tuple[int, str, str]]]:
    """Append GitHub data before advancing any collection position."""
    base_ref = str((pr.get("base") or {}).get("ref", "")) or "main"
    is_steward = record.agent_id.startswith("steward")
    # All three places a maintainer can write — three REST collections with
    # INDEPENDENT id sequences, so each keeps its own cursor.
    collections = {
        "comment": github.list_comments(record.target, number),
        "review": github.list_pr_reviews(record.target, number),
        "review_comment": github.list_pr_review_comments(record.target, number),
    }
    per_source = {
        "comment": (
            qualifying_comments(
                collections["comment"],
                bot_login,
                record.last_comment_id,
            ),
            record.last_comment_id,
        ),
        "review": (
            qualifying_comments(
                collections["review"],
                bot_login,
                record.last_review_id,
            ),
            record.last_review_id,
        ),
        "review_comment": (
            qualifying_comments(
                collections["review_comment"],
                bot_login,
                record.last_review_comment_id,
            ),
            record.last_review_comment_id,
        ),
    }
    merged = [
        (source, cid, author, body)
        for source, (items, _) in per_source.items()
        for cid, author, body in items
    ]
    is_conflict = bool(dirty_pr_head(pr))
    conflict_head = base_sync_head(pr)
    conflict_wake = bool(conflict_head) and conflict_head != record.dirty_wake_head
    # oldest first WITHIN each source (ids are monotonic per source); cap the
    # wake, and advance each cursor only to the max id actually processed
    merged.sort(key=lambda item: item[1])
    merged = merged[:MAX_COMMENTS_PER_WAKE]
    cursors = {
        "comment": record.last_comment_id,
        "review": record.last_review_id,
        "review_comment": record.last_review_comment_id,
    }
    for source, cid, _, _ in merged:
        cursors[source] = max(cursors[source], cid)
    comments = [(cid, author, body) for _, cid, author, body in merged]

    directory = run_dir(run_root, record.run_id)
    thread = f"pr:{number}"
    for source, cid, author, body in merged:
        original = next(c for c in collections[source] if c.get("id") == cid)
        append(
            directory,
            Message(
                0,
                "comment",
                "human",
                thread,
                now,
                f"{source}:{cid}",
                {
                    "author": author,
                    "body": body,
                    "association": original.get("author_association", ""),
                },
            ),
        )
    if conflict_wake:
        fact = (
            (
                "Your PR conflicts with its base. "
                f"{base_ref} moved and this PR no longer merges cleanly."
            )
            if is_conflict
            else (
                f"Your PR is behind its base. {base_ref} moved since the claim was measured; "
                "the measurement is stale and auto-merge was not armed; no conflicts were detected."
            )
        )
        append(
            directory,
            Message(
                0,
                "base-moved",
                "git",
                thread,
                now,
                f"base:{conflict_head}:{base_sha_at_fetch}",
                {
                    "text": fact
                    + f" At delivery, `origin/{base_ref}` has been fetched into your workspace. "
                    "Only a submit measures and publishes a change; a human merges the updated PR.",
                    "base_sha": base_sha_at_fetch,
                },
            ),
        )
    context = [
        (original, entry)
        for original in collections["comment"]
        for entry in context_comments([original], record.last_comment_id)
    ][-MAX_CONTEXT_COMMENTS:]
    for original, (author, body) in context:
        append(
            directory,
            Message(
                0,
                "comment",
                "human",
                thread,
                now,
                f"comment:{original['id']}",
                {
                    "author": author,
                    "body": body,
                    "association": original.get("author_association", ""),
                    "context_only": True,
                },
            ),
        )
    if is_steward:
        append(
            directory,
            Message(
                0,
                "note",
                "kernel",
                thread,
                now,
                f"role:{record.resume_session_id}:{record.inbox_seq}",
                {
                    "text": "Role: BENCHMARK STEWARD. Scope: env/eval/tests territory only; "
                    "solver directories and the record ledger remain forbidden. "
                    "The orchestrator re-validates and re-bases records after changes."
                },
            ),
        )
    if record.panel_wake_text:
        # an older kernel left the findings on the record: into the inbox
        # once (the key dedupes a retried wake); the field is cleared by this
        # wake's own record save, under its lease
        append(
            directory,
            Message(
                0,
                "panel-verdict",
                "panel",
                thread,
                now,
                f"panel:legacy:{(pr.get('head') or {}).get('sha', '')!s}",
                {
                    "head": str((pr.get("head") or {}).get("sha", "")),
                    "findings": [
                        {
                            "blocking": True,
                            "summary": "Pending panel findings",
                            "detail": record.panel_wake_text,
                        }
                    ],
                },
            ),
        )
        record = replace(record, panel_wake_text="")
    return record, cursors, comments


def retire_followup_stage(run_root: Path, record: RunRecord, now: float) -> bool:
    """Retire a previous kernel's re-measure on its next wake."""
    import json

    from outerloop.dispatch import Snapshot, drop_snapshot
    from outerloop.runstate import RECORD_NAME

    path = run_dir(run_root, record.run_id) / RECORD_NAME
    raw = json.loads(path.read_text())
    stage = raw.get("followup_stage")
    if not stage:
        return False
    ws = Workspace(root=run_dir(run_root, record.run_id) / "ws")
    ref = str(stage.get("candidate_ref") or "")
    if ref:
        drop_snapshot(ws, Snapshot(commit="", tree="", ref=ref))
    log.info("run %s: retired legacy followup_stage; left in review", record.run_id)
    raw.pop("followup_stage", None)
    tmp = path.with_suffix(".migration")
    tmp.write_text(json.dumps(raw))
    tmp.replace(path)
    save_record(run_root, replace(record, state=IN_REVIEW), now)
    return True


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
    spec: RoleSpec | None = None,
    panel_lenses: tuple[Any, ...] = (),
    panel_builder: Callable[..., Callable[[float, float, str], Any]] | None = None,
    panel_skip: str = "",
    dispatch: DispatchSettings | None = None,
) -> FollowupOutcome:
    # a deployment bug is refused before any GitHub read or contract load —
    # the contained error outcome retries next tick either way, so fail as
    # cheaply as possible
    spec = spec or followup_spec()
    if not spec.execution.can_execute:
        raise ValueError(
            "the follow-up responder is an editing role; the spec must allow execution"
        )

    pr = github.get_pull_request(record.target, number)
    if pr.get("merged") or pr.get("merged_at"):
        _end_run(run_root, record, github, MERGED, "", now)
        return FollowupOutcome(run_id, "ended-merged")
    if pr.get("state") == "closed":
        _end_run(run_root, record, github, REJECTED, "PR closed unmerged", now)
        return FollowupOutcome(run_id, "ended-rejected")
    if retire_followup_stage(run_root, record, now):
        return FollowupOutcome(run_id, "no-op", "retired legacy re-measure; left in review")

    workspace = run_dir(run_root, run_id) / "ws"
    if not workspace.is_dir():
        return FollowupOutcome(run_id, "error", "workspace no longer exists (GC'd?)")
    from outerloop.attempt import target_clone_url

    ws = Workspace(root=workspace, auth=github.auth, url=target_clone_url(record.target))
    base_ref = str((pr.get("base") or {}).get("ref") or "main")
    try:
        ws.fetch_origin()
        base_sha = ws.git("rev-parse", f"origin/{base_ref}").strip()
    except Exception as exc:
        return FollowupOutcome(run_id, "error", f"base fetch failed: {exc}")
    record = replace(record, stage={**record.stage, "base_sha": base_sha, "base_branch": base_ref})
    save_record(run_root, record, now)
    contract_text = contract_at(ws, base_sha)
    contract = load_contract(contract_text, record.target)
    bench = next((b for b in contract.benchmarks if b.name == record.benchmark), None)
    if bench is None:
        return FollowupOutcome(
            run_id, "error", f"benchmark {record.benchmark!r} not in the contract"
        )

    is_steward = record.agent_id.startswith("steward")
    scope_check = steward_out_of_scope if is_steward else out_of_scope

    # Fill the manifest's key family and scope from the record and contract
    # so the spec run_role receives is TRUE (roles.md: the follow-up runs
    # under the resuming role's own key and scope). run_role does not consume
    # these fields — like instructions/skills, they are manifest data ahead
    # of the loader — enforcement stays scope_check below and the CLI's
    # key-file.
    owned = (
        (contract.steward.allowed if contract.steward else [])
        if is_steward
        else contract.scope.allowed
    )
    spec = replace(spec, key="steward" if is_steward else "author", scope=tuple(owned))

    base_sha_at_fetch = base_sha
    record, cursors, comments = build_review_messages(
        run_root, record, number, github, bot_login, now, pr, base_sha_at_fetch
    )
    directory = run_dir(run_root, run_id)
    conflict_head = base_sync_head(pr)
    conflict_wake = bool(conflict_head) and conflict_head != record.dirty_wake_head
    inbox_wake = inbox_wake_pending(run_root, record)
    messages = pending(directory, delivered_seq(record))
    if not comments and not conflict_wake and not inbox_wake and record.state != WAITING:
        return FollowupOutcome(run_id, "no-op", "no new qualifying comments")
    delivery_seq = max((m.seq for m in messages), default=record.inbox_seq)
    from outerloop.attempt import LINE_MEMORY_PATHS, _park_run, run_author_leg, submission_paths
    from outerloop.dispatch import drop_snapshot, snapshot_tree
    from outerloop.orchestrator import AttemptResult, RunConfig, RunParked

    snapshots = []

    def snapshot() -> str:
        snap = snapshot_tree(
            ws,
            ws.git("rev-parse", "HEAD").strip(),
            exclude=LINE_MEMORY_PATHS if bench.lines else (),
            author=bot_login,
        )
        snapshots.append(snap)
        return snap.commit

    def acknowledge(seq: int) -> None:
        nonlocal record
        record = replace(record, inbox_seq=seq)
        save_record(run_root, record, now)

    tip = str((pr.get("head") or {}).get("sha") or ws.git("rev-parse", "HEAD").strip())

    def launch_changes() -> list[str]:
        return submission_paths(ws, tip, bool(bench.lines))

    record = replace(record, stage={**record.stage, "panel_skip": panel_skip})
    if panel_skip:
        append(
            directory,
            Message(
                0,
                "note",
                "kernel",
                thread_for(record),
                now,
                f"panel-skip:{record.inbox_seq}:{panel_skip}",
                {"text": f"panel read skipped: {panel_skip}"},
            ),
        )

    from outerloop.attempt import _line_ref_for, build_panel_runner, publish
    from outerloop.compute import LocalCompute
    from outerloop.measure import DispatchedMeasurer

    config = RunConfig(
        target=record.target,
        benchmark=record.benchmark,
        agent_id=record.agent_id,
        bot_login=bot_login,
    )
    measurer = (
        dispatch.measurer(
            directory, repo_root=workspace, eval_minutes=bench.eval_minutes or 0, run_tag=run_id
        )
        if dispatch
        else DispatchedMeasurer(
            compute=LocalCompute(),
            run_dir=directory,
            repo_root=workspace,
            image="",
            account="",
            partition="",
            eval_minutes=bench.eval_minutes or 0,
            run_tag=run_id,
        )
    )
    panel_runner = (
        (panel_builder or build_panel_runner)(
            ws,
            directory,
            base_sha,
            panel_lenses,
            contract_text,
            record.target,
            record.benchmark,
            bot_login,
            created,
            secrets=secrets,
            exclude=LINE_MEMORY_PATHS if bench.lines else (),
        )
        if panel_lenses
        else None
    )

    kept_ref = ""
    try:
        result = run_author_leg(
            RunConfig(
                target=record.target,
                benchmark=record.benchmark,
                agent_id=record.agent_id,
                bot_login=bot_login,
            ),
            contract_text,
            workspace,
            harness,
            measurer,
            base_sha,
            snapshot,
            run_root=run_root,
            record=record,
            ws=ws,
            dispatch=dispatch,
            github=github,
            secrets=secrets,
            spec=spec,
            changed_paths=launch_changes,
            scope_validator=scope_check,
            on_inbox_delivered=acknowledge,
            panel_runner=panel_runner,
            on_stop=lambda session: AttemptResult(outcome="review", session=session),
        )
    except RunParked as parked:
        kept_ref = next(s.ref for s in snapshots if s.commit == parked.candidate_sha)
        record = replace(
            record,
            last_comment_id=cursors["comment"],
            last_review_id=cursors["review"],
            last_review_comment_id=cursors["review_comment"],
        )
        try:
            _park_run(
                run_root,
                record,
                parked,
                kept_ref,
                bench.eval_minutes,
                now,
                secrets,
                dispatch=dispatch,
                base_branch=base_ref,
            )
        except Exception:
            from outerloop.attempt import afterany_ids

            if dispatch is not None:
                for job_id in afterany_ids(parked.afterany):
                    dispatch.compute.cancel(job_id)
            kept_ref = ""
            raise
        return FollowupOutcome(run_id, "parked")
    finally:
        for snap in snapshots:
            if snap.ref != kept_ref:
                drop_snapshot(ws, snap)
    record = replace(
        record,
        last_comment_id=cursors["comment"],
        last_review_id=cursors["review"],
        last_review_comment_id=cursors["review_comment"],
    )
    if result.outcome == "improved":
        published = publish(
            result=result,
            ws=ws,
            workspace=workspace,
            run_root=run_root,
            run_dir=directory,
            run_id=run_id,
            record=record,
            config=config,
            contract=contract,
            github=github,
            now=now,
            secrets=secrets,
            base_branch=base_ref,
            base_sha=base_sha,
            issue_number=record.issue_number,
            line_ref=_line_ref_for(bench, record.agent_id),
            date=created[:10],
        )
        return FollowupOutcome(run_id, "replied", published.outcome)
    session = result.session
    if session is None:
        return FollowupOutcome(run_id, "error", result.note)
    if result.outcome != "review":
        # cursor NOT advanced: the next attempt sees the same comments
        # Deliberately NOT a budget-exhausted ending: follow-ups never end
        # the run, and "error" is what keeps cursors un-advanced so the next
        # tick retries the reply (wake_attempts caps the spend). The detail
        # string still names the real cause for the log reader.
        if outage(session):
            # The API refused us — refund the wake attempt the tick billed
            # at submit (this responder holds the lease) and stamp the
            # latch so the lanes pause instead of burning the retry cap
            # on a dead key every half hour. Best-effort: a full state
            # disk must degrade to a plain error outcome, not lose the
            # honest note to an escaping exception.
            role = "steward" if is_steward else "solver"
            try:
                stamp_outage(run_root, session.error_detail[:300], now, role=role)
                latest = load_record(run_root, run_id)
                save_record(
                    run_root, replace(latest, wake_attempts=max(0, latest.wake_attempts - 1)), now
                )
            except (OSError, ValueError) as exc:
                log.warning("outage bookkeeping failed for %s: %s", run_id, exc)
            return FollowupOutcome(
                run_id, "error", f"api outage: {session.error_detail or session.stop_reason}"
            )
        return FollowupOutcome(
            run_id,
            "error",
            f"session: {result.note or session.error_detail or session.stop_reason}",
        )

    from outerloop.attempt import _clear_stage

    latest = load_record(run_root, run_id)
    record = replace(record, stage=latest.stage)
    record = replace(_clear_stage(record), state=IN_REVIEW, wake_attempts=record.wake_attempts)
    delivery_seq = max(delivery_seq, record.inbox_seq)
    return finish_review_leg(
        cursors=cursors,
        delivery_seq=delivery_seq,
        conflict_head=conflict_head,
        conflict_wake=conflict_wake,
        github=github,
        now=now,
        number=number,
        record=record,
        run_root=run_root,
        secrets=secrets,
        session=session,
        replies_posted=result.replies_posted,
    )


def finish_review_leg(
    *,
    cursors: dict[str, int],
    delivery_seq: int,
    conflict_head: str,
    conflict_wake: bool,
    github: GitHubClient,
    now: float,
    number: int,
    record: RunRecord,
    run_root: Path,
    secrets: tuple[str, ...],
    session: SessionResult,
    replies_posted: int = 0,
) -> FollowupOutcome:
    from outerloop.attempt import post_replies

    directory = run_dir(run_root, record.run_id)
    replies_posted += post_replies(record, github, (), secrets, directory)
    reply = APPROVAL_PATTERN.sub(REDACTED, redact(session.final_text, secrets))[:MAX_REPLY_CHARS]
    if reply and not replies_posted:
        github.comment(record.target, number, f"{REPLY_MARKER}\n{reply}")
    save_record(
        run_root,
        replace(
            record,
            state=IN_REVIEW,
            last_comment_id=cursors["comment"],
            last_review_id=cursors["review"],
            last_review_comment_id=cursors["review_comment"],
            dirty_wake_head=conflict_head if conflict_wake else record.dirty_wake_head,
            resume_session_id=session.session_id or record.resume_session_id,
            inbox_seq=delivery_seq,
            wake_attempts=0,
        ),
        now,
    )
    return FollowupOutcome(record.run_id, "replied", "review leg completed")


def main() -> int:
    import argparse
    import os
    import time

    from outerloop.appauth import add_credential_args, resolve_bot_auth
    from outerloop.harness import DEFAULT_MAX_TURNS
    from outerloop.orchestrator import SubprocessEvaluator

    parser = argparse.ArgumentParser(description="Service one in-review run.")
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--image", default="", type=image_file_arg)
    parser.add_argument("--uncontained", action="store_true")
    parser.add_argument("--claude-bin", default=default_binary("claude"))
    parser.add_argument(
        "--codex-bin",
        default=default_binary("codex"),
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("OUTERLOOP_AUTHOR_MODEL") or "claude-opus-5",
        help="fallback model only; a run's OWN (backend, model) from its record wins",
    )
    # No --author-backend: a follow-up services ONE run, whose backend+model are
    # persisted on its record (legacy records are claude). It never uses a fleet
    # default that could mismatch the run.
    parser.add_argument(
        "--codex-config",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="codex `-c KEY=VALUE` config for the codex author (repeatable)",
    )
    # the tick passes the effective limit explicitly; this fallback follows
    # the harness ceiling so a bare CLI run is never silently starved
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    parser.add_argument(
        "--bot-login",
        default=bot_login_from_env(),
        help="the login the kernel posts as (OUTERLOOP_BOT_LOGIN); required",
    )
    parser.add_argument(
        "--job-minutes",
        type=int,
        default=0,
        help="this job's Slurm walltime; arms the self-deadline (0 = off)",
    )
    add_credential_args(parser)
    parser.add_argument(
        "--key-file",
        default="",
        help="author key file; default resolves per backend (config-driven): "
        "OUTERLOOP_CLAUDE_KEY_FILE for claude, OUTERLOOP_CODEX_KEY_FILE for codex",
    )
    parser.add_argument(
        "--panel",
        default="",
        help="verification lenses (kind[:backend[:model]], comma-separated) that "
        "read a submitted change; '' = no panel",
    )
    parser.add_argument("--panel-key-file", default="", help="the claude panel lenses' key file")
    parser.add_argument("--account", default=os.environ.get("OUTERLOOP_ACCOUNT", ""))
    parser.add_argument("--partition", default=os.environ.get("OUTERLOOP_PARTITION", ""))
    parser.add_argument("--gpu-partition", default=os.environ.get("OUTERLOOP_GPU_PARTITION", ""))
    parser.add_argument("--gpu-account", default=os.environ.get("OUTERLOOP_GPU_ACCOUNT", ""))
    parser.add_argument(
        "--panel-minutes",
        type=int,
        default=0,
        help="walltime the tick added to this job for the panel's read (0 = none fit: "
        "the read is skipped and said so; the author's budget is never the panel's)",
    )
    parser.add_argument("--panel-skip", default="", help="why the tick skipped the panel")
    args = parser.parse_args()
    if not args.bot_login.strip():
        parser.error("--bot-login / OUTERLOOP_BOT_LOGIN is required (no default identity, #298)")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if not args.image and not args.uncontained:
        parser.error("--image is required (or pass --uncontained explicitly, dev only)")

    from datetime import UTC, datetime

    from outerloop.attempt import (
        PANEL_KEY_DEFAULT,
        _dispatch_settings,
        _panel_lenses_from_args,
        codex_author_config_error,
        resolve_author_key_file,
        resume_author,
    )
    from outerloop.panel import panel_read_minutes

    # the climb's own resolver, the climb's own rule: the compute backend
    # always exists, so a revision's evals dispatch and meter the same way a
    # climb's do. Account and partition are optional (#300).
    dispatch = _dispatch_settings(args)

    # a follow-up services ONE run: reproduce THAT run's author (the persisted
    # (backend, model) PAIR), not the current fleet default, so a codex-authored
    # PR is revised by codex (native resume of its session), with the codex model
    # and codex key. Legacy/unreadable records are treated as claude; respond_once
    # re-reads the record and handles a truly missing one.
    try:
        _rec: object | None = load_record(args.run_root, args.run_id)
    except Exception:
        # never crash on an unreadable/odd record — fall back to the claude
        # author (resume_author); respond_once re-reads and handles a missing one
        _rec = None
    author_backend, author_model, author_key = resume_author(_rec, args.model)
    _err = codex_author_config_error(author_backend, author_model, args.image)
    if _err:
        parser.error(f"run {args.run_id}: {_err}")
    # an explicit --key-file still overrides; otherwise the run's recorded key
    args.key_file = (
        resolve_author_key_file(author_backend, args.key_file) if args.key_file else author_key
    )
    codex_extra = tuple(a for c in args.codex_config for a in ("-c", c))
    api_key = role_key(args.key_file, author_backend)
    bot_auth = resolve_bot_auth(args.pat_file, args.github_app_file)

    # The panel AFTER the author is resolved: this run's author key is the
    # RECORDED one (not the fleet default the tick preflights against), so
    # role separation is checked here on the credentials themselves — one
    # key never plays author and judge, whatever paths it was read from
    # (terra #229 r1). Lens rules and judge keys are the climb's own
    # (_panel_lenses_from_args); every judge key joins the redaction set.
    args.panel_key_file = args.panel_key_file or PANEL_KEY_DEFAULT
    try:
        panel_lenses, panel_secrets = _panel_lenses_from_args(args)
    except ValueError as exc:
        parser.error(str(exc))
    # A panel that cannot run in THIS job never costs the reply: the
    # follow-up runs panel-free and the skip is posted on the thread (the
    # PR stays human-merged). Two such cases: a judge key that is this run's
    # author key — the tick preflights against the FLEET key, a run started
    # under another key is only known here (terra #229 r2) — and a read the
    # partition cap left no walltime for (--panel-minutes).
    panel_skip = args.panel_skip
    if panel_skip:
        panel_lenses = ()
    if api_key and api_key in panel_secrets:
        panel_skip = "a panel judge key is this run's author key (role separation)"
        log.warning("run %s: %s; the follow-up runs without the panel", args.run_id, panel_skip)
        panel_lenses = ()
    if panel_lenses and args.panel_minutes < panel_read_minutes(args.panel):
        panel_skip = (
            f"the job's walltime cap left {args.panel_minutes} min for a read "
            f"that needs {panel_read_minutes(args.panel)}"
        )
        panel_lenses = ()

    # Same self-deadline as the climb: Slurm never signals this process,
    # so walltime deaths must be our own clock's job. respond_once contains
    # exceptions per-lane, and its lease/cursor rules keep a Terminated
    # ending honest (cursors un-advanced on failure -> the next tick retries).
    import signal as _signal

    from outerloop.attempt import arm_self_deadline
    from outerloop.role_runner import build_harness

    armed = arm_self_deadline(args.job_minutes)
    if armed:
        log.info("self-deadline armed: Terminated in %ds", armed)
    # the manifest first, the harness from it (budget has one source: the
    # args). The session must end before its job does, so the walltime is
    # bounded by the job minus the self-deadline margin when one is known —
    # and minus the panel's minutes, which the tick ADDED for a read that
    # runs after the session on the same clock: the author keeps exactly the
    # budget it had without a panel.
    session_minutes = max(0, args.job_minutes - (args.panel_minutes if panel_lenses else 0))
    spec = followup_spec(
        max_turns=args.max_turns,
        walltime_s=(
            min(3600, max(300, session_minutes * 60 - 300)) if args.job_minutes > 0 else 3600
        ),
    )
    try:
        outcome = respond_once(
            args.run_root,
            args.run_id,
            harness=build_harness(
                api_key,
                spec,
                backend=author_backend,
                binary=args.claude_bin if author_backend == "claude" else args.codex_bin,
                model=author_model,
                container_image=args.image,
                codex_extra_args=codex_extra,
            ),
            spec=spec,
            evaluator=SubprocessEvaluator(container_image=args.image),
            github=GitHubClient(auth=bot_auth),
            bot_login=args.bot_login,
            now=time.time(),
            secrets=(api_key, bot_auth.token(), *panel_secrets),
            created=datetime.now(UTC).isoformat(),
            panel_lenses=panel_lenses,
            panel_skip=panel_skip,
            dispatch=dispatch,
        )
    finally:
        _signal.alarm(0)
    print(f"action={outcome.action} note={outcome.note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
