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

from outerloop.brief import MAX_TASK_CHARS, _cap, _fence
from outerloop.contract import Contract
from outerloop.followup import QUALIFYING_ASSOCIATIONS
from outerloop.github import is_own_login

log = logging.getLogger(__name__)

CLAIM_MARKER = "<!-- autoresearch:claimed -->"
# Posted (by the bot only) to undo a claim whose run never started — a failed
# submit must not strand the issue, since the claim scan skips claimed issues.
RELEASE_MARKER = "<!-- autoresearch:claim-released -->"
# Claim attempts per issue before intake gives up on it: a durable submit
# failure must not claim/release (and comment) forever. Same idea as the
# steward lane's MAX_STEWARD_ATTEMPTS.
MAX_INTAKE_ATTEMPTS = 3
# steward work orders carry this label; they are the STEWARD lane's,
# never the solver's (a solver climb cannot touch env paths anyway)
STEWARD_LABEL = "autoresearch:steward"


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


def qualifying_issue(issue: dict, bot_login: str) -> bool:
    author = str((issue.get("user") or {}).get("login", ""))
    if is_own_login(author, bot_login):
        return False  # the kernel's own issues (research log, alarms) are never orders
    if str(issue.get("author_association", "")) not in QUALIFYING_ASSOCIATIONS:
        return False
    return bool(str(issue.get("title") or "").strip())


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
        labels = {
            str(label.get("name", "")).casefold()
            for label in issue.get("labels", [])
            if isinstance(label, dict)
        }
        if STEWARD_LABEL in labels:
            continue  # the steward lane's, never the solver's
        if not qualifying_issue(issue, bot_login):
            continue
        number = int(issue["number"])
        claimed = False
        attempts = 0
        for c in github.list_comments(repo, number):
            author = str((c.get("user") or {}).get("login", ""))
            if not is_own_login(author, bot_login):
                continue  # only the bot's own markers count — no forged releases
            body = str(c.get("body", ""))
            if CLAIM_MARKER in body:
                claimed = True
                attempts += 1
            if RELEASE_MARKER in body:
                claimed = False
        if claimed:
            continue  # already claimed by a run
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
    quoted = _cap(f"{task.title}\n\n{task.body}".strip(), MAX_TASK_CHARS - 400)
    fence = _fence(quoted)
    return (
        f"A maintainer (@{task.author}) opened issue #{task.number} requesting "
        f"work on the `{task.benchmark}` benchmark. Their request:\n"
        f"{fence}\n{quoted}\n{fence}\n"
        "Address the request's substance within the contract's rules."
    )
