"""Entry point for the advisory-review workflow.

Reads the PR from the environment the workflow provides, runs one review, and
posts one NEW comment per round — numbered, stamped with the reviewed head —
so every round notifies and stays visible (edits do neither). Exits 0 even
when it skips or fails: an advisory reviewer must never turn a target repo's
CI red.
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


def post_round(
    client: GitHubClient, repo: str, number: int, marker: str, body: str, pr_data: dict
) -> str:
    """Post one NEW comment per round — numbered, stamped with the reviewed
    head — so every round notifies and stays visible (edits do neither).
    Shared by the advisory reviewer and the verifier; each counts rounds by
    ITS OWN marker. The old one-thread upsert guarded against
    synchronize-triggered spam; runs are now only PR-open or an explicit
    label request, so volume is human-bounded.
    """
    head = pr_data.get("head")
    head_sha = str(head.get("sha", ""))[:8] if isinstance(head, dict) else ""
    # The round number is cosmetic: an EXPECTED failure counting prior
    # rounds must never cost the round itself. Programming errors still
    # propagate, per this module's policy.
    try:
        prior = [
            str(c.get("body", ""))
            for c in client.list_comments(repo, number)
            # STARTS WITH the marker: a quote-reply prefixes every line
            # with "> ", so it cannot match — and this stays true for
            # any posting identity (Actions token, GitHub App, or a
            # self-hoster's machine-user PAT, which posts as type User)
            if str(c.get("body", "")).lstrip().startswith(marker)
        ]
        round_label = f"**Round {len(prior) + 1}**"
        if head_sha and any(f"reviewed head `{head_sha}`" in b for b in prior):
            round_label += " (re-run on the same head)"
    except EXPECTED_FAILURES as exc:
        log.warning("could not count prior rounds: %s", exc)
        round_label = "**New round** (prior count unavailable)"
    stamp = f"{round_label} — reviewed head `{head_sha or 'unknown'}`.\n\n"
    client.comment(repo, number, body.replace(marker, f"{marker}\n{stamp}", 1))
    return round_label


SKIP_MARKER = "<!-- autoresearch:round-skipped -->"


def post_skip_stub(client: GitHubClient, repo: str, number: int, role: str, exc: Exception) -> None:
    """Silence is invisible: when the model API refuses a round (dead
    credits, spend cap, auth), say so on the thread instead of leaving a
    gap only the Actions tab can see. A DIFFERENT marker than a real
    round, deliberately — a stub never counts toward round numbering and
    never rides as follow-up wake context (both match on their own
    markers)."""
    note = str(exc)[:200]
    try:
        client.comment(
            repo,
            number,
            f"{SKIP_MARKER}\n*The {role} round could not run — the model API "
            f"refused the request ({type(exc).__name__}: {note}). Treat this "
            f"as an outage, not a clean read; re-add the review label to "
            f"re-request once the API recovers.*",
        )
    except EXPECTED_FAILURES as post_exc:
        log.warning("could not post the skip stub: %s", post_exc)


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
        # A maintainer-added re-request label is an explicit ask: it
        # overrides the automatic bot-author skip (see review.skip_reason).
        explicit = os.environ.get("REVIEW_EXPLICIT_REQUEST", "").strip().lower() == "true"
        # Context is fetched only for PRs that will actually be reviewed —
        # bot-authored and opted-out PRs must not pay the API fan-out.
        if skip_reason(pr, bot_login, explicit) is None:
            pr = replace(pr, context_files=_gather_context(client, repo, number, pr_data))
        today = datetime.now(UTC).date().isoformat()
        body = format_comment(
            review(pr, completer, bot_login, today=today, explicit_request=explicit)
        )
        if body is None:
            log.info("nothing to post")
            return 0
        round_label = post_round(client, repo, number, MARKER, body, pr_data)
        log.info("posted advisory review (%s) on %s#%s", round_label, repo, number)
    except EXPECTED_FAILURES as exc:  # advisory: never fail the target repo's CI
        log.warning("advisory review did not complete: %s: %s", type(exc).__name__, exc)
        if isinstance(exc, CompleterError):
            post_skip_stub(client, repo, number, "advisory review", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
