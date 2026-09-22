"""Durable publish handoff and merge observation for measured submissions."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, replace
from functools import cmp_to_key
from pathlib import Path
from typing import Any, cast

from outerloop.contract import Benchmark, Contract
from outerloop.github import GitHubClient, Workspace
from outerloop.ledger_branch import (
    RESEARCH_LOG_BRANCH,
    ensure_ledger_branch,
    read_ledger,
    write_ledger,
)
from outerloop.orchestrator import steward_out_of_scope
from outerloop.progress import (
    LEADER_FILE,
    LeaderEntry,
    PendingSubmission,
    confirm,
    parse_leader,
    parse_pending,
    record_pending,
    reject,
)
from outerloop.runstate import LEDGER_RETRY as LEDGER_RETRY
from outerloop.runstate import RunRecord, acknowledge_ledger_pending, save_record

log = logging.getLogger(__name__)


def display_leader(github: GitHubClient, target: str) -> dict[str, LeaderEntry]:
    """A failed display/decision read means no prior, never checkout fallback."""
    try:
        head = github.branch_head(target, RESEARCH_LOG_BRANCH)
        if not head:
            return {}
        return parse_leader(github.get_file(target, LEADER_FILE, head))
    except Exception:
        # API exception strings can contain credentials; no remote detail is needed.
        log.warning("branch ledger unavailable; using no prior measurement")
        return {}


def measurement_pending(
    ws: Workspace,
    contract: Contract,
    bench: Benchmark,
    record: RunRecord,
    baseline: float,
    candidate: float,
    seed: int,
    measured_sha: str,
    pr_number: int,
    published_head: str,
    timestamp: str,
    *,
    kind: str,
) -> PendingSubmission:
    signature = json.dumps(bench.measurement_signature(), separators=(",", ":"))
    # Ruler identity includes harness contents, not just its command. Solver
    # edits do not change it; stewardship edits do, even with the same contract.
    ruler_files = []
    for entry in ws.git("ls-tree", "-rz", measured_sha).split("\0"):
        if entry:
            _, path = entry.split("\t", 1)
            if not steward_out_of_scope([path], contract):
                ruler_files.append(entry)
    ruler = hashlib.sha256((signature + "\0".join(sorted(ruler_files))).encode()).hexdigest()
    return PendingSubmission(
        bench.name,
        bench.metric,
        bench.direction,
        baseline,
        candidate,
        record.run_id,
        seed,
        ruler,
        signature,
        measured_sha,
        pr_number,
        published_head,
        timestamp,
        kind=kind,
        min_delta=bench.min_delta or 0.0,
        min_delta_rel=bench.min_delta_rel or 0.0,
    )


def queue_pending(
    root: Path,
    record: RunRecord,
    github: GitHubClient,
    pending: PendingSubmission,
    contract: Contract,
    now: float,
) -> RunRecord:
    """Save intent before the API write. Keep failed intents through park cleanup."""
    queue = dict(cast(dict[str, Any], record.stage.get(LEDGER_RETRY) or {}))
    queue[pending.path] = {
        "pending": asdict(pending),
        "digits": {b.name: b.display_digits for b in contract.benchmarks if b.display_digits},
    }
    record = replace(
        record,
        stage={**record.stage, LEDGER_RETRY: queue, "ledger_digits": queue[pending.path]["digits"]},
    )
    save_record(root, record, now)
    return retry_pending(root, record, github, now)


def retry_pending(root: Path, record: RunRecord, github: GitHubClient, now: float) -> RunRecord:
    queue = dict(cast(dict[str, Any], record.stage.get(LEDGER_RETRY) or {}))
    for path, work in list(queue.items()):
        try:
            pending = parse_pending(json.dumps(work["pending"]))
            if pending is None:
                raise ValueError("missing pending payload")
            pin = github.branch_sha(record.target, github.default_branch(record.target))
            ensure_ledger_branch(github, record.target, pin)
            head, _, _ = read_ledger(github, record.target)

            def edit(leader, pendings, submission: PendingSubmission = pending):
                return record_pending(submission)

            write_ledger(
                github,
                record.target,
                head,
                edit,
                digits=work["digits"],
            )
        except Exception:
            log.warning("branch ledger publish deferred; durable retry retained")
            break
        del queue[path]
        record = acknowledge_ledger_pending(root, record.run_id, path, now)
    return record


def observe_target(github: GitHubClient, target: str, digits: dict[str, int]) -> set[int]:
    """Reconcile all branch pendings, resets first and each kind in ancestry order.

    Returns PRs whose merge tree is unmeasured. An API/ancestry failure raises
    before run cleanup; the immutable pending files are the retry journal.
    """
    unmeasured: set[int] = set()
    head, _, pendings = read_ledger(github, target, unmeasured=unmeasured)
    merged: list[tuple[PendingSubmission, str]] = []
    closed: list[PendingSubmission] = []
    terminal: list[PendingSubmission] = []
    for pending in pendings.values():
        pr = github.get_pull_request(target, pending.pr_number)
        if pr.get("merged") or pr.get("merged_at"):
            sha = pr.get("merge_commit_sha")
            if not isinstance(sha, str) or not sha:
                raise ValueError("merged PR has no merge commit")
            # Only the measurement for the final published head can confirm.
            if (pr.get("head") or {}).get("sha") != pending.published_head:
                final_head = (pr.get("head") or {}).get("sha")
                if any(
                    p.pr_number == pending.pr_number and p.published_head == final_head
                    for p in pendings.values()
                ):
                    closed.append(pending)
                else:
                    log.warning("merged PR head was not measured; leaderboard unchanged")
                    terminal.append(pending)
                    unmeasured.add(pending.pr_number)
                continue
            merge_tree = github.commit_tree(target, sha)
            # Publish carries the measured tree; the sealed commit stays local.
            measured_tree = github.commit_tree(target, pending.published_head)
            if merge_tree != measured_tree:
                log.warning("merged PR has an unmeasured tree; leaderboard unchanged")
                terminal.append(pending)
                unmeasured.add(pending.pr_number)
                continue
            merged.append((pending, sha))
        elif pr.get("state") == "closed":
            closed.append(pending)

    def ancestor(a: str, b: str) -> bool:
        return github.head_contains(target, a, b)

    def order(a, b):
        if a[0].kind != b[0].kind:
            return -1 if a[0].kind == "RESET" else 1
        if a[1] == b[1]:
            return 0
        if ancestor(a[1], b[1]):
            return -1
        if ancestor(b[1], a[1]):
            return 1
        raise ValueError("merged submissions have unrelated ancestry")

    merged.sort(key=cmp_to_key(order))
    if not merged and not closed and not terminal:
        return unmeasured

    def edit(leader, current):
        patch = {}
        for pending in terminal:
            if pending.path in current:
                patch.update(record_pending(replace(pending, status="UNMEASURED")))
        for pending in closed:
            if pending.path in current:
                patch.update(reject(pending))
        for pending, sha in merged:
            if pending.path in current:
                leader = confirm(leader, pending, sha, is_ancestor=ancestor)
                patch.update(reject(pending))
        patch[LEADER_FILE] = json.dumps({k: asdict(v) for k, v in leader.items()})
        return patch

    write_ledger(github, target, head, edit, digits=digits)
    return unmeasured
