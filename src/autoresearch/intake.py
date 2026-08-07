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

from autoresearch.brief import MAX_TASK_CHARS, _cap, _fence
from autoresearch.contract import Contract
from autoresearch.followup import QUALIFYING_ASSOCIATIONS

log = logging.getLogger(__name__)

CLAIM_MARKER = "<!-- autoresearch:claimed -->"


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
    if author.casefold() == bot_login.casefold():
        return False
    if str(issue.get("author_association", "")) not in QUALIFYING_ASSOCIATIONS:
        return False
    return bool(str(issue.get("title") or "").strip())


def pick_issue(github, repo: str, contract: Contract, bot_login: str) -> IssueTask | None:
    """The oldest qualifying, unclaimed issue that names exactly one
    benchmark. At most one — intake is deliberately slow."""
    issues = sorted(github.list_open_issues(repo), key=lambda i: i.get("number", 0))
    for issue in issues:
        if not qualifying_issue(issue, bot_login):
            continue
        number = int(issue["number"])
        if any(CLAIM_MARKER in str(c.get("body", "")) for c in github.list_comments(repo, number)):
            continue  # already claimed by a run
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
