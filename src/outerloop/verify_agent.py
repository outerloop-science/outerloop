"""Agent-session verifier: runs the verifier as an agent over TWO
checkouts — the PR head (the change under review) and the base branch (the
trusted contract and ruler).

`run_agent_verify` is the orchestration core, testable with a fake harness and
client. It builds on the shared pieces in `verifier`: the skip rule (bot PRs
only), the rubric (via `build_verify_agent_brief`), the result-policy sanitizer
(`verify_result_from_role`), and the posting (`post_round` with the verify
marker — always an issue comment, so rounds ride into follow-up wakes). The
agent reads the ruler from base/ and follows the change through pr-head/.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from outerloop.contract import find_contract
from outerloop.github import GitHubClient
from outerloop.harness import Harness, backend_id, budget_exhausted, outage
from outerloop.posting import EXPECTED_FAILURES, post_round, post_skip_stub
from outerloop.review_agent import _emit, _pull_request
from outerloop.role_runner import run_role
from outerloop.roles import verifier_spec, verify_result_from_role
from outerloop.rolespec import RoleSpec
from outerloop.verifier import (
    VERIFY_MARKER,
    build_verify_agent_brief,
    format_verify_comment,
    gather_thread,
    verify_skip_reason,
)

log = logging.getLogger(__name__)


def _base_contract(client: GitHubClient, repo: str, pr_data: dict) -> str:
    """The contract from the BASE branch (never the PR — the solver cannot have
    shaped it). Best-effort: a transient fetch failure degrades the round (the
    model notes the gap) rather than losing it."""
    base = pr_data.get("base")
    base_ref = str(base.get("ref", "")) if isinstance(base, dict) else ""
    try:
        found = find_contract(lambda n: client.get_file_content(repo, n, base_ref or "HEAD"))
        return found[1] if found else ""
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
    emit_path: Path | None = None,
) -> str | None:
    """Verify bot PR #`number` as an agent session over `workspace`, a
    directory holding the two checkouts the workflow prepared:
    `pr-head/` (the change) and `base/` (trusted contract + ruler). Posts the
    findings as one issue comment per round. Returns the round label, or None
    when it skipped or could not produce a verdict. Advisory: never raises the
    expected failures.

    With `emit_path` (the tokenless split, mirroring the reviewer): nothing is
    posted — the raw verdict, or a skip-stub, is written there for a separate
    write-token job (`verify_post_cli`). The session job then needs only a
    read-scoped token, so a shell judge has no write credential to lift.
    """
    spec = spec or verifier_spec()
    today = today or datetime.now(UTC).date().isoformat()
    reviewed_by = backend_id(harness)

    def _skip_stub(detail: str) -> None:
        # `detail` is already api-key-redacted by the harness (it owns its own
        # secret), so no secret is passed here.
        if emit_path is not None:
            _emit(emit_path, repo, number, kind="skip-stub", detail=detail, reviewed_by=reviewed_by)
        else:
            post_skip_stub(client, repo, number, "verification", RuntimeError(detail))

    try:
        pr, pr_data = _pull_request(client, repo, number)
        skip = verify_skip_reason(pr, bot_login)
        if skip is not None:
            log.info("skipping verification of %s#%s: %s", repo, number, skip)
            # a clean skip still leaves an envelope so the post job can REQUIRE
            # an artifact — a missing one then always means a broken session
            if emit_path is not None:
                _emit(emit_path, repo, number, kind="skip-clean", detail=skip)
            return None

        contract_text = _base_contract(client, repo, pr_data)
        thread: tuple[tuple[str, str], ...] = ()
        try:
            thread = gather_thread(client, repo, number, bot_login)
        except EXPECTED_FAILURES as exc:
            log.warning("verifying without the discussion thread: %s", exc)

        from outerloop.syscall import tool_command

        brief = build_verify_agent_brief(
            pr, contract_text, today=today, thread=thread, syscall_cmd=tool_command(workspace)
        )
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
                _skip_stub(detail)
            elif emit_path is not None:
                # nothing worth posting, but the post job still needs an
                # artifact so a MISSING one always means a broken session
                _emit(emit_path, repo, number, kind="skip-clean", detail=detail)
            return None

        if emit_path is not None:
            _emit(
                emit_path,
                repo,
                number,
                kind="findings",
                data=role_result.data,
                reviewed_by=reviewed_by,
            )
            return "emitted"
        body = format_verify_comment(result)
        if body is None:
            log.info("nothing to post")
            return None
        round_label = post_round(
            client, repo, number, VERIFY_MARKER, body, pr_data, reviewed_by=reviewed_by
        )
        log.info("posted verification (%s) on %s#%s", round_label, repo, number)
        return round_label
    except EXPECTED_FAILURES as exc:  # advisory role: never fail the target's CI
        log.warning("agent verification did not complete: %s: %s", type(exc).__name__, exc)
        return None
