"""Agent-session reviewer: the deployment path that runs the reviewer as an
agent over a read-only PR-head checkout, instead of the one-shot completer.

`run_agent_review` is the orchestration core, testable with a fake harness and
client. It reuses everything the completer path already has: the same skip
rules, the same rubric (via `build_agent_brief`), the same result-policy
(`review_result_from_role`), and the same inline-posting path (`format_review`
-> `post_round_review`). The only new thing is *how the verdict is produced* —
an agent session with read tools — not how it is judged or posted.

This lives beside `review_cli` (the completer path), which stays the live
default until this path is validated on a real runner. When the workflow swaps
to this path, the SAME change sunsets the completer path — the completer branch
in `review_cli`, `llm.AnthropicCompleter` and `review.review`/`build_prompt` if
then unused, and their tests — so `main` never carries two reviewer
implementations. The workflow swap and its CHANGELOG entry come with that
validation.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from autoresearch.github import GitHubClient
from autoresearch.harness import Harness, outage
from autoresearch.review import (
    MARKER,
    PullRequest,
    build_agent_brief,
    format_comment,
    format_review,
    skip_reason,
)
from autoresearch.review_cli import EXPECTED_FAILURES, post_round_review, post_skip_stub
from autoresearch.role_runner import run_role
from autoresearch.roles import review_result_from_role, reviewer_spec
from autoresearch.rolespec import RoleSpec

log = logging.getLogger(__name__)


def _pull_request(client: GitHubClient, repo: str, number: int) -> tuple[PullRequest, dict]:
    pr_data = client.get_pull_request(repo, number)
    diff = client.get_pull_request_diff(repo, number)
    pr = PullRequest(
        repo=repo,
        number=number,
        title=str(pr_data.get("title", "")),
        body=str(pr_data.get("body") or ""),
        diff=diff,
        author=str((pr_data.get("user") or {}).get("login", "")),
        labels=tuple(
            str(label.get("name", ""))
            for label in pr_data.get("labels", [])
            if isinstance(label, dict)
        ),
    )
    return pr, pr_data


def run_agent_review(
    client: GitHubClient,
    repo: str,
    number: int,
    harness: Harness,
    workspace: Path,
    *,
    bot_login: str,
    spec: RoleSpec | None = None,
    explicit: bool = False,
    today: str | None = None,
) -> str | None:
    """Review PR #`number` as an agent session over `workspace` (a read-only
    PR-head checkout the caller prepared). Post the findings inline via the
    Reviews API. Returns the round label, or None when it skipped or could not
    produce a verdict. Advisory: never raises the expected failures, so it can
    never turn a target repo's CI red.
    """
    spec = spec or reviewer_spec()
    today = today or datetime.now(UTC).date().isoformat()
    try:
        pr, pr_data = _pull_request(client, repo, number)
        skip = skip_reason(pr, bot_login, explicit)
        if skip is not None:
            log.info("skipping agent review of %s#%s: %s", repo, number, skip)
            return None

        role_result = run_role(spec, harness, build_agent_brief(pr, today), workspace)
        review = review_result_from_role(role_result)
        if review is None:
            # No verdict: an errored or refused session, not a clean read.
            # Say so on the thread only for an outage (API refused us); other
            # failures are logged, advisory-silent.
            detail = role_result.error or role_result.session.stop_reason
            log.warning("agent review produced no verdict on %s#%s: %s", repo, number, detail)
            if outage(role_result.session):
                post_skip_stub(client, repo, number, "advisory review", RuntimeError(detail))
            return None

        rendered = format_review(review, pr.diff)
        full = format_comment(review)
        if rendered is None or full is None:
            log.info("nothing to post")
            return None
        body, inline = rendered
        round_label = post_round_review(
            client, repo, number, MARKER, body, inline, pr_data, fallback_body=full
        )
        log.info("posted agent review (%s) on %s#%s", round_label, repo, number)
        return round_label
    except EXPECTED_FAILURES as exc:  # advisory: never fail the target repo's CI
        log.warning("agent review did not complete: %s: %s", type(exc).__name__, exc)
        return None
