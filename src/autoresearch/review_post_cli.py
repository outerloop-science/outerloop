"""Posting half of the least-token split.

Reads the findings file the session job emitted (`REVIEW_EMIT_FILE`),
re-validates it, and posts through the normal advisory path. This job holds
the write token; no model session runs next to it. The artifact crosses a
job boundary, so nothing in it is trusted: the envelope must name this PR,
the skip rules are re-checked, and every string passes the same sanitizing
render as the single-job path. Exits 0 on every failure — advisory means
advisory.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from autoresearch.github import EnvTokenProvider, GitHubClient
from autoresearch.posting import EXPECTED_FAILURES, post_round_review, post_skip_stub
from autoresearch.review import (
    MARKER,
    format_comment,
    format_review,
    result_from_data,
    sanitize,
    skip_reason,
)
from autoresearch.review_agent import _pull_request

log = logging.getLogger(__name__)

# An opinion label rides into the round body so a human can tell the second
# opinion from the primary reviewer at a glance. Caller-configured, but
# rendered into a comment: length-capped and newline-stripped.
MAX_OPINION_LABEL = 80


def post_from_file(
    client: GitHubClient,
    repo: str,
    number: int,
    bot_login: str,
    path: Path,
    opinion_label: str = "",
) -> str | None:
    """Post the emitted findings (or skip stub). Returns the round label, or
    None when there was nothing to post or the envelope was refused."""
    try:
        envelope = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("findings file unreadable (%s); nothing posted", exc)
        return None
    if not isinstance(envelope, dict):
        log.warning("findings file is not an object; nothing posted")
        return None
    if envelope.get("repo") != repo or envelope.get("number") != number:
        log.warning("findings envelope names a different PR; refused")
        return None
    kind = envelope.get("kind")
    if kind not in ("skip-stub", "findings"):
        log.warning("unknown envelope kind %r; nothing posted", kind)
        return None
    try:
        # The write authority re-checks the skip rules for EVERY kind: the
        # session job decided them once, but this side of the artifact
        # boundary is the one that must never post on a bot PR — a forged
        # stub envelope is still a post.
        pr, pr_data = _pull_request(client, repo, number)
        skip = skip_reason(pr, bot_login)
        if skip is not None:
            log.info("skipping post on %s#%s: %s", repo, number, skip)
            return None
        if kind == "skip-stub":
            # sanitize: the detail crossed the job boundary and lands in a
            # comment — collapse newlines, neutralize markdown, cap length
            detail = sanitize(str(envelope.get("detail", "")), 300)
            # name WHOSE round failed: with two standing reviewers, an
            # unattributed stub is ambiguous exactly when a key dies
            who = " ".join(opinion_label.split())[:MAX_OPINION_LABEL] or str(
                envelope.get("reviewed_by", "")
            )
            role = f"advisory review ({who})" if who else "advisory review"
            post_skip_stub(client, repo, number, role, RuntimeError(detail))
            return "skip-stub"
        data = envelope.get("data")
        review = result_from_data(data if isinstance(data, dict) else {})
        rendered = format_review(review, pr.diff)
        full = format_comment(review)
        if rendered is None or full is None:
            log.info("nothing to post")
            return None
        body, inline = rendered
        if opinion_label:
            # AFTER the marker, never before it: round counting and the
            # quote-reply defense both match marker-FIRST bodies
            label = " ".join(opinion_label.split())[:MAX_OPINION_LABEL]
            body = body.replace(MARKER, f"{MARKER}\n*{label}*", 1)
            full = full.replace(MARKER, f"{MARKER}\n*{label}*", 1)
        round_label = post_round_review(
            client,
            repo,
            number,
            MARKER,
            body,
            inline,
            pr_data,
            fallback_body=full,
            reviewed_by=str(envelope.get("reviewed_by", "")),
        )
        log.info("posted second-opinion review (%s) on %s#%s", round_label, repo, number)
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
        log.warning("REVIEW_BOT_LOGIN is unset; skipping (cannot re-check the bot skip)")
        return 0
    emit_file = os.environ.get("REVIEW_EMIT_FILE", "").strip()
    if not emit_file:
        log.warning("REVIEW_EMIT_FILE is unset; skipping")
        return 0
    path = Path(emit_file).resolve()
    if not path.is_file():
        log.info("no findings file at %s (clean skip upstream); nothing to post", path)
        return 0
    client = GitHubClient(auth=EnvTokenProvider("GITHUB_TOKEN"))
    post_from_file(
        client,
        repo,
        int(number_raw),
        bot_login,
        path,
        opinion_label=os.environ.get("REVIEW_OPINION_LABEL", "").strip(),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
