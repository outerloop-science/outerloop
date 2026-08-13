"""Agent-session verifier: runs the verifier as an agent over TWO read-only
checkouts — the PR head (the change under review) and the base branch (the
trusted contract and ruler) — instead of the one-shot completer.

`run_agent_verify` is the orchestration core, testable with a fake harness and
client. It reuses the completer path's pieces: the same skip rule (bot PRs
only), the same rubric (via `build_verify_agent_brief`), the same result-policy
sanitizer (`verify_result_from_role`), and the same posting (`post_round` with
the verify marker — always an issue comment, so rounds ride into follow-up
wakes). What changes is how the verdict is produced: an agent that reads the
ruler from base/ and follows the change through pr-head/, rather than a single
call over fenced excerpts.

Lives beside `verifier_cli` (the completer path) until the migration completes;
the sunset then removes the whole one-shot seam at once.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from autoresearch.github import GitHubClient
from autoresearch.harness import Harness, budget_exhausted, outage
from autoresearch.review_agent import _pull_request
from autoresearch.review_cli import EXPECTED_FAILURES, post_round, post_skip_stub
from autoresearch.role_runner import run_role
from autoresearch.roles import verifier_spec, verify_result_from_role
from autoresearch.rolespec import RoleSpec
from autoresearch.verifier import (
    VERIFY_MARKER,
    build_verify_agent_brief,
    format_verify_comment,
    verify_skip_reason,
)
from autoresearch.verifier_cli import gather_thread

log = logging.getLogger(__name__)


def _base_contract(client: GitHubClient, repo: str, pr_data: dict) -> str:
    """The contract from the BASE branch (never the PR — the solver cannot have
    shaped it). Best-effort: a transient fetch failure degrades the round (the
    model notes the gap) rather than losing it."""
    base = pr_data.get("base")
    base_ref = str(base.get("ref", "")) if isinstance(base, dict) else ""
    try:
        return client.get_file_content(repo, ".autoresearch.yaml", base_ref or "HEAD") or ""
    except EXPECTED_FAILURES as exc:
        log.warning("verifying without the contract: %s", exc)
        return ""


def run_agent_verify(
    client: GitHubClient,
    repo: str,
    number: int,
    harness: Harness,
    workspace: Path,
    *,
    bot_login: str,
    spec: RoleSpec | None = None,
    today: str | None = None,
) -> str | None:
    """Verify bot PR #`number` as an agent session over `workspace`, a
    directory holding the two read-only checkouts the workflow prepared:
    `pr-head/` (the change) and `base/` (trusted contract + ruler). Posts the
    findings as one issue comment per round. Returns the round label, or None
    when it skipped or could not produce a verdict. Advisory: never raises the
    expected failures.
    """
    spec = spec or verifier_spec()
    today = today or datetime.now(UTC).date().isoformat()
    try:
        pr, pr_data = _pull_request(client, repo, number)
        skip = verify_skip_reason(pr, bot_login)
        if skip is not None:
            log.info("skipping verification of %s#%s: %s", repo, number, skip)
            return None

        contract_text = _base_contract(client, repo, pr_data)
        thread: tuple[tuple[str, str], ...] = ()
        try:
            thread = gather_thread(client, repo, number, bot_login)
        except EXPECTED_FAILURES as exc:
            log.warning("verifying without the discussion thread: %s", exc)

        brief = build_verify_agent_brief(pr, contract_text, today=today, thread=thread)
        role_result = run_role(spec, harness, brief, workspace)
        result = verify_result_from_role(role_result)
        if result is None:
            # No verdict is never a clean read: the verifier's silence must not
            # look like an endorsement. An API outage OR a session that ran out
            # of budget (walltime/turns) says so on the thread — otherwise a
            # timed-out verification is indistinguishable from "no issues".
            detail = role_result.error or role_result.session.stop_reason
            log.warning("verification produced no verdict on %s#%s: %s", repo, number, detail)
            if outage(role_result.session) or budget_exhausted(role_result.session):
                post_skip_stub(client, repo, number, "verification", RuntimeError(detail))
            return None

        body = format_verify_comment(result)
        if body is None:
            log.info("nothing to post")
            return None
        round_label = post_round(client, repo, number, VERIFY_MARKER, body, pr_data)
        log.info("posted verification (%s) on %s#%s", round_label, repo, number)
        return round_label
    except EXPECTED_FAILURES as exc:  # advisory role: never fail the target's CI
        log.warning("agent verification did not complete: %s: %s", type(exc).__name__, exc)
        return None
