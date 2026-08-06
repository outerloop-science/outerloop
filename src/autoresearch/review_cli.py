"""Entry point for the advisory-review workflow.

Reads the PR from the environment the workflow provides, runs one review, and
upserts a single comment (one thread per PR). Exits 0 even when it skips or
fails: an advisory reviewer must never turn a target repo's CI red.
"""

from __future__ import annotations

import logging
import os
import sys

from autoresearch.github import EnvTokenProvider, GitHubClient
from autoresearch.llm import AnthropicCompleter
from autoresearch.review import MARKER, PullRequest, format_comment, review

log = logging.getLogger(__name__)
# Configurable so anyone self-hosting this reviewer can name their own bot.
DEFAULT_BOT_LOGIN = "agentic-learning-bot"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    repo = os.environ["PR_REPO"]
    number = int(os.environ["PR_NUMBER"])
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
            author=str(pr_data.get("user", {}).get("login", "")),
            labels=tuple(label["name"] for label in pr_data.get("labels", [])),
        )
        completer = AnthropicCompleter(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            model=os.environ.get("REVIEW_MODEL") or "claude-opus-5",
            effort=os.environ.get("REVIEW_EFFORT") or "high",
        )
        bot_login = os.environ.get("REVIEW_BOT_LOGIN") or DEFAULT_BOT_LOGIN
        body = format_comment(review(pr, completer, bot_login))
        if body is None:
            log.info("nothing to post")
            return 0
        client.upsert_comment(repo, number, MARKER, body)
        log.info("posted advisory review on %s#%s", repo, number)
    except Exception as exc:  # advisory: never fail the target repo's CI
        log.warning("advisory review did not complete: %s: %s", type(exc).__name__, exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
