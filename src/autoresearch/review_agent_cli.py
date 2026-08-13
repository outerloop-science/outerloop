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
from autoresearch.review_agent import build_reviewer_harness, run_agent_review, sanitize_checkout

log = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # Fail closed on a missing/invalid PR reference too, so a misconfigured
    # workflow skips cleanly rather than exiting nonzero (never red the CI).
    repo = os.environ.get("PR_REPO", "").strip()
    number_raw = os.environ.get("PR_NUMBER", "").strip()
    if not repo or not number_raw.isdigit():
        log.warning("PR_REPO/PR_NUMBER unset or invalid; skipping")
        return 0
    number = int(number_raw)
    # Fail closed: without the bot login we cannot honor "never review
    # bot-authored PRs", so we do not review at all.
    bot_login = os.environ.get("REVIEW_BOT_LOGIN", "").strip()
    if not bot_login:
        log.warning("REVIEW_BOT_LOGIN is unset; skipping (cannot identify bot-authored PRs)")
        return 0
    # Backend is a deployment choice, not baked in: pick the harness and its
    # key by REVIEW_BACKEND (claude | codex), consistent with the Harness seam.
    backend = os.environ.get("REVIEW_BACKEND", "claude").strip().lower()
    key_var = {"claude": "ANTHROPIC_REVIEWER_KEY", "codex": "OPENAI_REVIEWER_KEY"}.get(backend)
    if key_var is None:
        log.warning("unknown REVIEW_BACKEND %r; skipping review", backend)
        return 0
    api_key = os.environ.get(key_var, "").strip()
    if not api_key:
        log.warning("%s is unset or empty; skipping review", key_var)
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
    # The checkout is untrusted: rename instruction files (CLAUDE.md, .claude/
    # hooks, ...) so no backend auto-loads PR content as instructions. A rename
    # failure means an instruction file is still live — fail closed.
    renamed, failed = sanitize_checkout(workspace)
    if failed:
        log.warning("checkout could not be fully sanitized (%d left); skipping", failed)
        return 0
    if renamed:
        log.info("sanitized %d instruction file(s) in the checkout", renamed)
    explicit = os.environ.get("REVIEW_EXPLICIT_REQUEST", "").strip().lower() == "true"

    client = GitHubClient(auth=EnvTokenProvider("GITHUB_TOKEN"))
    harness = build_reviewer_harness(
        api_key,
        backend=backend,
        binary=os.environ.get("REVIEW_BINARY") or None,  # else the backend default on PATH
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
