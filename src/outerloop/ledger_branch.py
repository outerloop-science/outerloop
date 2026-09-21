"""Pinned reads and compare-and-swap writes for the shared research ledger."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict

from outerloop.github import GitHubClient, GitHubError
from outerloop.progress import (
    LEADER_FILE,
    PROGRESS_FILE,
    LeaderEntry,
    LedgerReadError,
    PendingSubmission,
    parse_leader,
    parse_pending,
    render_markdown,
)

RESEARCH_LOG_BRANCH = "research-log"
Ledger = dict[str, LeaderEntry]
Pendings = dict[str, PendingSubmission]
LedgerEdit = Callable[[Ledger, Pendings], dict[str, str]]


class LedgerWriteError(RuntimeError):
    """A ledger write could not finish; retain the operation for the next tick."""


def ensure_ledger_branch(github: GitHubClient, target: str, pinned_commit: str) -> None:
    """Create an absent branch from the caller's pin, preserving an existing branch."""
    if not pinned_commit:
        raise ValueError("branch creation requires a pinned commit")
    head = github.branch_head(target, RESEARCH_LOG_BRANCH)
    if head is None:
        raise LedgerReadError("ledger branch head unavailable")
    if head or github.dry_run:
        return
    try:
        github.create_ref(target, f"refs/heads/{RESEARCH_LOG_BRANCH}", pinned_commit)
    except GitHubError as exc:
        if not github.branch_head(target, RESEARCH_LOG_BRANCH):
            raise LedgerWriteError("could not create ledger branch") from exc


def _read_at(github: GitHubClient, target: str, head: str) -> tuple[Ledger, Pendings]:
    if not head:
        raise LedgerReadError("ledger branch does not exist; seed it from a pinned commit")
    try:
        # A recursive tree avoids the contents API's silent 1,000-entry cap.
        tree = github.get_tree(target, head)
        if not isinstance(tree, dict) or tree.get("truncated") is not False:
            raise LedgerReadError("incomplete ledger tree")
        paths = tree["tree"]
        if not isinstance(paths, list):
            raise LedgerReadError("malformed ledger tree")
        leader: Ledger = {}
        pendings: Pendings = {}
        for item in paths:
            path = item["path"]
            if path == LEADER_FILE:
                leader = parse_leader(github.get_file(target, path, head))
            elif path.startswith("results/submissions/") and path.endswith(".json"):
                pending = parse_pending(github.get_file(target, path, head))
                if pending is not None:
                    if pending.path != path:
                        raise LedgerReadError("submission identity does not match its path")
                    pendings[path] = pending
        return leader, pendings
    except (GitHubError, KeyError, TypeError, ValueError) as exc:
        raise LedgerReadError(f"cannot read ledger at {head}") from exc


def read_ledger(github: GitHubClient, target: str) -> tuple[str, Ledger, Pendings]:
    """Read authoritative leader and live submissions at one captured branch head."""
    head = github.branch_head(target, RESEARCH_LOG_BRANCH)
    if head is None:
        raise LedgerReadError("ledger branch head unavailable")
    leader, pendings = _read_at(github, target, head)
    return head, leader, pendings


def write_ledger(
    github: GitHubClient,
    target: str,
    expected_head: str,
    files: LedgerEdit,
    digits: dict[str, int] | None = None,
) -> None:
    """Apply a pure file-patch callback, recomputing on conflicts up to three times.

    The callback receives the current leader and pendings. Include leader.json
    to change the leader; its Markdown is always rendered in the same commit.
    Use record_pending/reject patches to add or remove submissions.
    """
    head = expected_head
    for attempt in range(3):
        leader, pendings = _read_at(github, target, head)
        patch = dict(files(leader, pendings))
        for path, content in patch.items():
            if path in {LEADER_FILE, PROGRESS_FILE}:
                continue
            if not path.startswith("results/submissions/") or not path.endswith(".json"):
                raise ValueError("ledger patch contains an unrelated path")
            pending = parse_pending(content)
            if pending is not None and pending.path != path:
                raise ValueError("submission identity does not match patch path")
            parts = path.split("/")
            if len(parts) != 4 or any(part in {"", ".", ".."} for part in parts):
                raise ValueError("invalid submission path")
        if LEADER_FILE in patch:
            leader = parse_leader(patch[LEADER_FILE])
        patch[LEADER_FILE] = (
            json.dumps({name: asdict(entry) for name, entry in sorted(leader.items())}, indent=2)
            + "\n"
        )
        patch[PROGRESS_FILE] = render_markdown(leader, target, digits)
        if github.put_files(
            target, patch, RESEARCH_LOG_BRANCH, "Update benchmark ledger", expected_head=head
        ):
            return
        new_head = github.branch_head(target, RESEARCH_LOG_BRANCH)
        if not new_head or new_head == head:
            raise LedgerWriteError("ledger write failed without a confirmed head change")
        head = new_head
        if attempt == 2:
            raise LedgerWriteError("ledger head moved during all three write attempts")
