"""Entry point for the advisory-review workflow.

Reads the PR from the environment the workflow provides, runs one review, and
posts one NEW comment per round — numbered, stamped with the reviewed head —
so every round notifies and stays visible (edits do neither). Exits 0 even
when it skips or fails: an advisory reviewer must never turn a target repo's
CI red.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime

from autoresearch.github import EnvTokenProvider, GitHubClient, GitHubError
from autoresearch.llm import AnthropicCompleter, CompleterError, RefusalError, TruncatedError
from autoresearch.posting import (
    EXPECTED_FAILURES as _POSTING_FAILURES,
)
from autoresearch.posting import (
    post_round,
    post_round_review,
    post_skip_stub,
)
from autoresearch.review import (
    MARKER,
    MAX_CONTEXT_FILES,
    PullRequest,
    format_comment,
    format_review,
    pick_context_files,
    review,
    skip_reason,
)

log = logging.getLogger(__name__)

# The completer path catches the shared posting/transport failures PLUS the
# model-call errors an AnthropicCompleter can raise — logged, never fatal,
# because an advisory reviewer must not turn a target repo's CI red. Programming
# errors (AttributeError, KeyError, TypeError) deliberately propagate.
EXPECTED_FAILURES = (*_POSTING_FAILURES, RefusalError, TruncatedError, CompleterError)


def _gather_context(
    client: GitHubClient, repo: str, number: int, pr_data: dict
) -> tuple[tuple[str, str], ...]:
    """Head-revision contents of changed files, bounded. Best-effort: a
    degraded review beats no review, so failures here return empty context."""
    try:
        head = pr_data.get("head")
        if not isinstance(head, dict):
            return ()
        head_sha = str(head.get("sha", ""))
        if not head_sha:
            return ()
        # Fork PRs: the head commit lives in the head repo, not the base. (Our
        # workflow only reviews same-repo PRs, but self-hosters can run the
        # CLI directly.)
        head_repo = head.get("repo")
        content_repo = (
            str(head_repo.get("full_name") or "") if isinstance(head_repo, dict) else ""
        ) or repo
        # Filter first, THEN cap the fetch fan-out — a PR whose first entries
        # are all deletions must not blind the reviewer to later files — and a
        # 400-file PR can't turn into 400 sequential requests against the
        # workflow token's rate budget. Lazy generator: pick_context_files
        # stops the fetching at its caps.
        files = [
            item
            for item in client.get_pull_request_files(repo, number)
            if item.get("status") != "removed" and item.get("filename")
        ][: MAX_CONTEXT_FILES * 3]

        def candidates() -> Iterator[tuple[str, str]]:
            for item in files:
                path = str(item["filename"])
                content = client.get_file_content(content_repo, path, head_sha)
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
    # crashing inside the API client. The completer is Anthropic by construction
    # (the multi-backend path is the agent reviewer); read its key once.
    reviewer_key = os.environ.get("ANTHROPIC_REVIEWER_KEY", "").strip()
    if not reviewer_key:
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
            api_key=reviewer_key,
            model=os.environ.get("REVIEW_MODEL") or "claude-opus-5",
            effort=os.environ.get("REVIEW_EFFORT") or "high",
        )
        # A maintainer-added re-request label is an explicit ask: it
        # overrides the automatic bot-author skip (see review.skip_reason).
        explicit = os.environ.get("REVIEW_EXPLICIT_REQUEST", "").strip().lower() == "true"
        # Context is fetched only for PRs that will actually be reviewed —
        # bot-authored and opted-out PRs must not pay the API fan-out.
        if skip_reason(pr, bot_login, explicit) is None:
            pr = replace(pr, context_files=_gather_context(client, repo, number, pr_data))
        today = datetime.now(UTC).date().isoformat()
        result = review(pr, completer, bot_login, today=today, explicit_request=explicit)
        # Human PRs get inline findings (resolvable threads, auto-outdated
        # on push). Explicit rounds on BOT PRs stay issue comments: those
        # ride into follow-up wakes as context, and the wake plumbing
        # reads the issue-comment collection.
        bot_authored = pr.author.strip().casefold() == bot_login.strip().casefold()
        if bot_authored or os.environ.get("REVIEW_INLINE", "").strip().lower() == "false":
            body = format_comment(result)
            if body is None:
                log.info("nothing to post")
                return 0
            round_label = post_round(client, repo, number, MARKER, body, pr_data)
        else:
            rendered = format_review(result, diff)
            full = format_comment(result)
            if rendered is None or full is None:
                log.info("nothing to post")
                return 0
            body, inline = rendered
            round_label = post_round_review(
                client, repo, number, MARKER, body, inline, pr_data, fallback_body=full
            )
        log.info("posted advisory review (%s) on %s#%s", round_label, repo, number)
    except EXPECTED_FAILURES as exc:  # advisory: never fail the target repo's CI
        log.warning("advisory review did not complete: %s: %s", type(exc).__name__, exc)
        if isinstance(exc, CompleterError):
            post_skip_stub(client, repo, number, "advisory review", exc, secrets=(reviewer_key,))
    return 0


if __name__ == "__main__":
    sys.exit(main())
