"""Entry point for the agent-session verifier.

Runs the verifier as an agent over the two checkouts the workflow
prepared under VERIFY_CHECKOUT (`pr-head/` and `base/`), and posts the findings
as an issue comment. Exits 0 even on skip or failure — the verifier is
advisory and must never turn a target repo's CI red.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from outerloop.github import EnvTokenProvider, GitHubClient
from outerloop.review_agent import sanitize_checkout
from outerloop.role_runner import build_harness
from outerloop.roles import verifier_spec
from outerloop.verify_agent import run_agent_verify

log = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # Fail closed on every misconfiguration, and always exit 0: an advisory
    # role skips cleanly rather than redding the caller's CI.
    repo = os.environ.get("PR_REPO", "").strip()
    number_raw = os.environ.get("PR_NUMBER", "").strip()
    if not repo or not number_raw.isdigit():
        log.warning("PR_REPO/PR_NUMBER unset or invalid; skipping")
        return 0
    bot_login = os.environ.get("REVIEW_BOT_LOGIN", "").strip()
    if not bot_login:
        log.warning("REVIEW_BOT_LOGIN is unset; skipping (cannot identify bot-authored PRs)")
        return 0
    from outerloop.harness import vertex_from_env

    api_key = os.environ.get("ANTHROPIC_VERIFIER_KEY", "").strip()
    # ADC-only deployments: Vertex covering the (claude) verifier stands in
    # for the key, same tolerance as role_key
    if not api_key and vertex_from_env() is None:
        log.warning("ANTHROPIC_VERIFIER_KEY is unset or empty; skipping verification")
        return 0
    # The directory holding the workflow's two checkouts: pr-head/
    # (the change) and base/ (trusted contract + ruler). Fail closed: a cwd
    # default would let a misconfigured workflow verify the wrong trees.
    checkout = os.environ.get("VERIFY_CHECKOUT", "").strip()
    if not checkout:
        log.warning("VERIFY_CHECKOUT is unset; skipping (won't verify the wrong trees)")
        return 0
    workspace = Path(checkout).resolve()
    # Both trees must actually be there — a session over a wrong layout would
    # read nothing and post a hollow "no findings" that reads as a clean pass.
    if not (workspace / "pr-head").is_dir() or not (workspace / "base").is_dir():
        log.warning("VERIFY_CHECKOUT lacks pr-head/ and base/; skipping")
        return 0
    # Only pr-head is untrusted: rename instruction files (CLAUDE.md, .claude/
    # hooks, ...) so no backend auto-loads PR content as instructions. A rename
    # failure means an instruction file is still live — fail closed.
    renamed, failed = sanitize_checkout(workspace / "pr-head")
    if failed:
        log.warning("pr-head could not be fully sanitized (%d left); skipping", failed)
        return 0
    if renamed:
        log.info("sanitized %d instruction file(s) in pr-head", renamed)

    spec = verifier_spec()
    client = GitHubClient(auth=EnvTokenProvider("GITHUB_TOKEN"))
    harness = build_harness(
        api_key,
        spec,
        binary=os.environ.get("REVIEW_BINARY") or None,
        model=os.environ.get("VERIFY_MODEL") or None,
    )
    # Tokenless split: with VERIFY_EMIT_FILE set, the verdict is written there
    # instead of posted — this job then needs only a read token, and a separate
    # write-token job (verify_post_cli) posts with no session next to it.
    emit_file = os.environ.get("VERIFY_EMIT_FILE", "").strip()
    run_agent_verify(
        client,
        repo,
        int(number_raw),
        harness,
        workspace,
        bot_login=bot_login,
        spec=spec,
        emit_path=Path(emit_file).resolve() if emit_file else None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
