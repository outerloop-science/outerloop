"""The requested lane: maintainer issues become runs.

An open issue on the target repo qualifies when its author carries repo
standing (same association gate as review comments). The tick claims at most
one per cycle by commenting a claim marker, then submits a climb job whose
task carries the issue text data-fenced; the resulting PR references the
issue, and the run's report lands back on the issue thread — the loop closes
with whoever asked (docs/design/architecture.md, "The life of a run").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from outerloop.brief import MAX_TASK_CHARS, cap, code_fence
from outerloop.contract import Contract
from outerloop.followup import QUALIFYING_ASSOCIATIONS
from outerloop.github import is_own_login
from outerloop.markers import has_label, has_marker, marker

log = logging.getLogger(__name__)

CLAIM_MARKER = marker("claimed")
# Posted (by the bot only) to undo a claim whose run never started — a failed
# submit must not strand the issue, since the claim scan skips claimed issues.
RELEASE_MARKER = marker("claim-released")
# Claim attempts per issue before intake gives up on it: a durable submit
# failure must not claim/release (and comment) forever. Same idea as the
# steward lane's MAX_STEWARD_ATTEMPTS.
MAX_INTAKE_ATTEMPTS = 3


@dataclass(frozen=True)
class IssueTask:
    number: int
    title: str
    body: str
    author: str
    benchmark: str  # inferred from the issue text against the contract


def infer_benchmark(text: str, contract: Contract) -> str:
    """The single contract benchmark the issue names, or "" if not exactly
    one — ambiguity is a human problem, not a guess."""
    lowered = text.casefold()
    named = [b.name for b in contract.benchmarks if b.name.casefold() in lowered]
    return named[0] if len(named) == 1 else ""


def issue_labels(issue: dict) -> set[str]:
    """The issue's label names, casefolded."""
    return {
        str(label.get("name", "")).casefold()
        for label in issue.get("labels", [])
        if isinstance(label, dict)
    }


def disqualification(issue: dict, bot_login: str, *, vouching_label: str | None = None) -> str:
    """Why this issue is not a work order, or "" when it is. A maintainer
    vouches for an issue by writing it (an OWNER, MEMBER or COLLABORATOR
    author) or, on a lane that accepts one, by setting the lane's label (only
    triage rights can). The label matters because the kernel lists issues with
    the App's token, and an App without the members permission sees a private
    org member as CONTRIBUTOR. The reason is what the skip log says."""
    author = str((issue.get("user") or {}).get("login", ""))
    if is_own_login(author, bot_login):
        return "the kernel's own issue"  # research log, alarms: never orders
    if not str(issue.get("title") or "").strip():
        return "no title"
    association = str(issue.get("author_association", ""))
    if association in QUALIFYING_ASSOCIATIONS:
        return ""
    if vouching_label and has_label(issue_labels(issue), vouching_label):
        return ""
    wanted = "/".join(QUALIFYING_ASSOCIATIONS)
    if vouching_label:
        wanted += f", or the {vouching_label} label"
    return f"by {author} as {association or 'unknown'} (needs {wanted})"


def qualifying_issue(issue: dict, bot_login: str, *, vouching_label: str | None = None) -> bool:
    return not disqualification(issue, bot_login, vouching_label=vouching_label)


def pick_issue(github, repo: str, contract: Contract, bot_login: str) -> IssueTask | None:
    """The oldest qualifying, unclaimed issue that names exactly one
    benchmark. At most one — intake is deliberately slow."""
    if not bot_login.strip():
        # fail closed like the steward picker: with no identity the claim
        # scan below would see NO claims and re-claim every tick — an
        # unbounded paid loop
        log.warning("pick_issue: bot_login is blank; intake lane sits out")
        return None
    issues = sorted(github.list_open_issues(repo), key=lambda i: i.get("number", 0))
    for issue in issues:
        number = int(issue["number"])
        # every skip says why: a silent one cost a day of "why is my issue
        # not picked up" on a private org member's issue
        if has_label(issue_labels(issue), "steward"):
            log.info("issue #%s skipped: a steward work order", number)
            continue
        if reason := disqualification(issue, bot_login, vouching_label="task"):
            log.info("issue #%s skipped: %s", number, reason)
            continue
        claimed = False
        attempts = 0
        for c in github.list_comments(repo, number):
            author = str((c.get("user") or {}).get("login", ""))
            if not is_own_login(author, bot_login):
                continue  # only the bot's own markers count — no forged releases
            body = str(c.get("body", ""))
            if has_marker(body, "claimed"):
                claimed = True
                attempts += 1
            if has_marker(body, "claim-released"):
                claimed = False
        if claimed:
            log.info("issue #%s skipped: claimed by a run", number)
            continue
        if attempts >= MAX_INTAKE_ATTEMPTS:
            log.info("issue #%s burned %d claim attempts; needs a human look", number, attempts)
            continue
        text = f"{issue.get('title', '')}\n{issue.get('body') or ''}"
        benchmark = infer_benchmark(text, contract)
        if not benchmark:
            log.info("issue #%s names zero or several benchmarks; skipping", number)
            continue
        return IssueTask(
            number=number,
            title=str(issue.get("title") or ""),
            body=str(issue.get("body") or ""),
            author=str((issue.get("user") or {}).get("login", "")),
            benchmark=benchmark,
        )
    return None


def issue_hypothesis(task: IssueTask) -> str:
    """The task text for the brief: the maintainer's ask, data-fenced.

    The author passed the standing gate, so the REQUEST is legitimate; the
    fence marks where quoted text ends and the harness's authority resumes.
    """
    quoted = cap(f"{task.title}\n\n{task.body}".strip(), MAX_TASK_CHARS - 400)
    fence = code_fence(quoted)
    return (
        f"A maintainer (@{task.author}) opened issue #{task.number} requesting "
        f"work on the `{task.benchmark}` benchmark. Their request:\n"
        f"{fence}\n{quoted}\n{fence}\n"
        "Address the request's substance within the contract's rules."
    )
