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

from autoresearch.github import EnvTokenProvider, GitHubClient, GitHubError
from autoresearch.llm import AnthropicCompleter, RefusalError
from autoresearch.review import MARKER, PullRequest, format_comment, review

log = logging.getLogger(__name__)

# Failures that mean "this run can't post" — logged, never fatal, because an
# advisory reviewer must not turn a target repo's CI red. Programming errors
# (AttributeError, KeyError, TypeError) deliberately propagate.
EXPECTED_FAILURES = (GitHubError, RefusalError, ValueError, OSError, json.JSONDecodeError)


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
            api_key=os.environ["ANTHROPIC_API_KEY"],
            model=os.environ.get("REVIEW_MODEL") or "claude-opus-5",
            effort=os.environ.get("REVIEW_EFFORT") or "high",
        )
        body = format_comment(review(pr, completer, bot_login))
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
