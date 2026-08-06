"""Entry point for the advisory-review workflow.

Reads the PR from the environment the workflow provides, runs one review, and
upserts a single comment (one thread per PR). Exits 0 even when it skips or
fails: an advisory reviewer must never turn a target repo's CI red.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime

from autoresearch.github import EnvTokenProvider, GitHubClient, GitHubError
from autoresearch.llm import AnthropicCompleter, CompleterError, RefusalError, TruncatedError
from autoresearch.review import (
    MARKER,
    MAX_CONTEXT_FILES,
    PullRequest,
    format_comment,
    pick_context_files,
    review,
    skip_reason,
)

log = logging.getLogger(__name__)

# Failures that mean "this run can't post" — logged, never fatal, because an
# advisory reviewer must not turn a target repo's CI red. Programming errors
# (AttributeError, KeyError, TypeError) deliberately propagate.
EXPECTED_FAILURES = (
    GitHubError,
    RefusalError,
    TruncatedError,
    CompleterError,
    ValueError,
    OSError,
    json.JSONDecodeError,
)


def _gather_context(
    client: GitHubClient, repo: str, number: int, pr_data: dict
) -> tuple[tuple[str, str], ...]:
    """Head-revision contents of changed files, bounded. Best-effort: a
    degraded review beats no review, so failures here return empty context."""
    try:
        head_sha = str((pr_data.get("head") or {}).get("sha", ""))
        if not head_sha:
            return ()
        # Cap the fetch fan-out BEFORE hitting the contents API: a 400-file PR
        # must not turn into 400 sequential requests against the workflow
        # token's rate budget. Lazy generator so pick_context_files stops the
        # fetching as soon as its caps are met.
        files = client.get_pull_request_files(repo, number)[: MAX_CONTEXT_FILES * 3]

        def candidates() -> Iterator[tuple[str, str]]:
            for item in files:
                if item.get("status") == "removed":
                    continue
                path = str(item.get("filename", ""))
                if not path:
                    continue
                content = client.get_file_content(repo, path, head_sha)
                if content is not None:
                    yield (path, content)

        return pick_context_files(candidates())
    except (GitHubError, ValueError) as exc:  # ValueError covers JSONDecodeError
        log.warning("reviewing without file context: %s", exc)
        return ()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    repo = os.environ["PR_REPO"]
    number = int(os.environ["PR_NUMBER"])
    # Fail closed: without knowing the bot's login we cannot honor "never
    # review bot-authored PRs", so we don't review at all.
    bot_login = os.environ.get("REVIEW_BOT_LOGIN", "").strip()
    if not bot_login:
        log.warning("REVIEW_BOT_LOGIN is unset; skipping (cannot identify bot-authored PRs)")
        return 0
    # An unset secret arrives as an empty string; skip cleanly instead of
    # crashing inside the API client.
    if not os.environ.get("ANTHROPIC_REVIEWER_KEY", "").strip():
        log.warning("ANTHROPIC_REVIEWER_KEY is unset or empty; skipping review")
        return 0
    client = GitHubClient(auth=EnvTokenProvider("GITHUB_TOKEN"))

    try:
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
        completer = AnthropicCompleter(
            api_key=os.environ["ANTHROPIC_REVIEWER_KEY"],
            model=os.environ.get("REVIEW_MODEL") or "claude-opus-5",
            effort=os.environ.get("REVIEW_EFFORT") or "high",
        )
        # Context is fetched only for PRs that will actually be reviewed —
        # bot-authored and opted-out PRs must not pay the API fan-out.
        if skip_reason(pr, bot_login) is None:
            pr = replace(pr, context_files=_gather_context(client, repo, number, pr_data))
        today = datetime.now(UTC).date().isoformat()
        body = format_comment(review(pr, completer, bot_login, today=today))
        if body is None:
            log.info("nothing to post")
            return 0
        client.upsert_comment(repo, number, MARKER, body)
        log.info("posted advisory review on %s#%s", repo, number)
    except EXPECTED_FAILURES as exc:  # advisory: never fail the target repo's CI
        log.warning("advisory review did not complete: %s: %s", type(exc).__name__, exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
