"""Posting half of the verifier's tokenless split (mirrors review_post_cli).

Reads the verdict envelope the session job emitted (`VERIFY_EMIT_FILE`),
re-validates it, and posts through the normal verification path. This job holds
the write token; NO model session runs next to it, so a shell judge in the
session job has no write credential to lift. The artifact crosses a job
boundary, so nothing in it is trusted: the envelope must name this PR, the
bot-only skip rule is re-checked here, and every string passes the same
sanitizing render as the single-job path. Exits 0 on every failure — the
verifier never fails the target repo's CI.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from outerloop.github import EnvTokenProvider, GitHubClient
from outerloop.posting import EXPECTED_FAILURES, post_round, post_skip_stub
from outerloop.review_agent import _pull_request
from outerloop.verifier import (
    VERIFY_MARKER,
    format_verify_comment,
    verify_result_from_data,
    verify_skip_reason,
)

log = logging.getLogger(__name__)


def post_from_file(
    client: GitHubClient, repo: str, number: int, bot_login: str, path: Path
) -> str | None:
    """Post the emitted verdict (or skip stub). Returns the round label, or
    None when there was nothing to post or the envelope was refused."""
    try:
        envelope = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("verdict file unreadable (%s); nothing posted", exc)
        return None
    if not isinstance(envelope, dict):
        log.warning("verdict file is not an object; nothing posted")
        return None
    if envelope.get("repo") != repo or envelope.get("number") != number:
        log.warning("verdict envelope names a different PR; refused")
        return None
    kind = envelope.get("kind")
    if kind == "skip-clean":
        log.info("session skipped cleanly (%s); nothing to post", envelope.get("detail", ""))
        return None
    if kind not in ("skip-stub", "findings"):
        log.warning("unknown envelope kind %r; nothing posted", kind)
        return None
    try:
        # the write authority re-checks the bot-only rule for EVERY kind: this
        # side of the artifact boundary is the one that must never post on a
        # non-bot PR — a forged envelope is still a post
        pr, pr_data = _pull_request(client, repo, number)
        skip = verify_skip_reason(pr, bot_login)
        if skip is not None:
            log.info("skipping post on %s#%s: %s", repo, number, skip)
            return None
        if kind == "skip-stub":
            post_skip_stub(client, repo, number, "verification", RuntimeError("no verdict"))
            return "skip-stub"
        data = envelope.get("data")
        result = verify_result_from_data(data if isinstance(data, dict) else {})
        body = format_verify_comment(result)
        if body is None:
            log.info("nothing to post")
            return None
        round_label = post_round(
            client,
            repo,
            number,
            VERIFY_MARKER,
            body,
            pr_data,
            reviewed_by=str(envelope.get("reviewed_by", "")),
        )
        log.info("posted verification (%s) on %s#%s", round_label, repo, number)
        return round_label
    except EXPECTED_FAILURES as exc:  # advisory: never fail the target repo's CI
        log.warning("posting did not complete: %s: %s", type(exc).__name__, exc)
        return None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    repo = os.environ.get("PR_REPO", "").strip()
    number_raw = os.environ.get("PR_NUMBER", "").strip()
    if not repo or not number_raw.isdigit():
        log.warning("PR_REPO/PR_NUMBER unset or invalid; skipping")
        return 0
    bot_login = os.environ.get("REVIEW_BOT_LOGIN", "").strip()
    if not bot_login:
        log.warning("REVIEW_BOT_LOGIN is unset; skipping (cannot re-check the bot rule)")
        return 0
    emit_file = os.environ.get("VERIFY_EMIT_FILE", "").strip()
    if not emit_file:
        log.warning("VERIFY_EMIT_FILE is unset; skipping")
        return 0
    path = Path(emit_file).resolve()
    if not path.is_file():
        log.info("no verdict file at %s (clean skip upstream); nothing to post", path)
        return 0
    client = GitHubClient(auth=EnvTokenProvider("GITHUB_TOKEN"))
    post_from_file(client, repo, int(number_raw), bot_login, path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
