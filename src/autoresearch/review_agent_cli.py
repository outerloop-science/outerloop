"""Entry point for the agent-session advisory reviewer.

Runs the reviewer as a read-only agent over a PR-head checkout the workflow
prepared (REVIEW_CHECKOUT), and posts the findings inline. Exits 0 even on skip
or failure — an advisory reviewer must never turn a target repo's CI red.

Lives beside review_cli (the completer entry point) during validation; the
workflow swap that makes this the default sunsets the completer path.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from autoresearch.github import EnvTokenProvider, GitHubClient
from autoresearch.review_agent import build_reviewer_harness, run_agent_review

log = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    repo = os.environ["PR_REPO"]
    number = int(os.environ["PR_NUMBER"])
    # Fail closed: without the bot login we cannot honor "never review
    # bot-authored PRs", so we do not review at all.
    bot_login = os.environ.get("REVIEW_BOT_LOGIN", "").strip()
    if not bot_login:
        log.warning("REVIEW_BOT_LOGIN is unset; skipping (cannot identify bot-authored PRs)")
        return 0
    api_key = os.environ.get("ANTHROPIC_REVIEWER_KEY", "").strip()
    if not api_key:
        log.warning("ANTHROPIC_REVIEWER_KEY is unset or empty; skipping review")
        return 0
    # The workflow checks out the PR head read-only into REVIEW_CHECKOUT; the
    # agent reads it but never executes it (read-only tool set). Fail closed:
    # defaulting to cwd would silently review the wrong tree (the reviewer's
    # own repo) if the checkout step were misconfigured.
    checkout = os.environ.get("REVIEW_CHECKOUT", "").strip()
    if not checkout:
        log.warning("REVIEW_CHECKOUT is unset; skipping (won't review the wrong tree)")
        return 0
    workspace = Path(checkout).resolve()
    explicit = os.environ.get("REVIEW_EXPLICIT_REQUEST", "").strip().lower() == "true"

    client = GitHubClient(auth=EnvTokenProvider("GITHUB_TOKEN"))
    harness = build_reviewer_harness(
        api_key,
        binary=os.environ.get("CLAUDE_BINARY", "claude"),
        model=os.environ.get("REVIEW_MODEL") or None,
    )
    run_agent_review(
        client,
        repo,
        number,
        harness,
        workspace,
        bot_login=bot_login,
        explicit=explicit,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
