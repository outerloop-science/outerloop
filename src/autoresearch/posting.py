"""Shared GitHub posting helpers for the reviewer and verifier.

Round-numbered comments, inline reviews, and skip stubs — the machinery for
getting a judge's findings onto a PR thread. Backend-agnostic on purpose: no
model dependency, so any judge backend posts through here.
"""

from __future__ import annotations

import json
import logging

from autoresearch.github import GitHubClient, GitHubError
from autoresearch.harness import redact

log = logging.getLogger(__name__)

# Posting/transport failures an advisory role tolerates — logged, never fatal,
# because an advisory reviewer or verifier must not turn a target repo's CI red.
# Programming errors (AttributeError, KeyError, TypeError) deliberately
# propagate. Model-call errors are NOT here: they arise inside the agent
# session, which handles them itself — posting has nothing to do with the model.
EXPECTED_FAILURES = (
    GitHubError,
    ValueError,
    OSError,
    json.JSONDecodeError,
)


def post_round(
    client: GitHubClient,
    repo: str,
    number: int,
    marker: str,
    body: str,
    pr_data: dict,
    reviewed_by: str = "",
) -> str:
    """Post one NEW comment per round — numbered, stamped with the reviewed
    head — so every round notifies and stays visible (edits do neither).
    Shared by the advisory reviewer and the verifier; each counts rounds by
    ITS OWN marker. The old one-thread upsert guarded against
    synchronize-triggered spam; runs are now only PR-open or an explicit
    label request, so volume is human-bounded.
    """
    stamp, round_label = _round_stamp(client, repo, number, marker, pr_data, reviewed_by)
    client.comment(repo, number, body.replace(marker, f"{marker}\n{stamp}", 1))
    return round_label


def _round_stamp(
    client: GitHubClient,
    repo: str,
    number: int,
    marker: str,
    pr_data: dict,
    reviewed_by: str = "",
) -> tuple[str, str]:
    """(stamp line, round label): prior rounds are counted across BOTH
    issue comments and review bodies, so switching a role between posting
    styles never resets its numbering."""
    head = pr_data.get("head")
    head_sha = str(head.get("sha", ""))[:8] if isinstance(head, dict) else ""
    # attribution is render-side data (it can cross a job boundary in the
    # least-token split): strip backticks/newlines, cap, never trust
    by = " ".join(str(reviewed_by).split()).replace("`", "")[:60]
    # The round number is cosmetic: an EXPECTED failure counting prior
    # rounds must never cost the round itself. Programming errors still
    # propagate, per this module's policy.
    try:
        bodies = [str(c.get("body", "")) for c in client.list_comments(repo, number)]
        bodies += [str(r.get("body", "")) for r in client.list_pr_reviews(repo, number)]
        # STARTS WITH the marker: a quote-reply prefixes every line with
        # "> ", so it cannot match — and this stays true for any posting
        # identity (Actions token, GitHub App, or a self-hoster's
        # machine-user PAT, which posts as type User)
        prior = [b for b in bodies if b.lstrip().startswith(marker)]
        # Rounds count PER REVIEWER: with several standing opinions on one
        # PR, a shared counter reads as re-reviews that never happened
        # (terra "Round 1", claude "Round 2"). Unattributed rounds keep the
        # shared count.
        if by:
            prior = [b for b in prior if f"reviewer `{by}`" in b]
        round_label = f"**Round {len(prior) + 1}**"
        if head_sha and any(f"reviewed head `{head_sha}`" in b for b in prior):
            round_label += " (re-run on the same head)"
    except EXPECTED_FAILURES as exc:
        log.warning("could not count prior rounds: %s", exc)
        round_label = "**New round** (prior count unavailable)"
    by_clause = f" — reviewer `{by}`" if by else ""
    return f"{round_label} — reviewed head `{head_sha or 'unknown'}`{by_clause}.\n\n", round_label


def post_round_review(
    client: GitHubClient,
    repo: str,
    number: int,
    marker: str,
    body: str,
    inline: list[dict],
    pr_data: dict,
    fallback_body: str,
    reviewed_by: str = "",
) -> str:
    """The Reviews-API sibling of post_round: body summary plus anchored
    inline comments, event COMMENT always (the client hard-codes it). A
    posting failure falls back to a plain issue comment carrying
    fallback_body — the FULL single-comment rendering, because the review
    body alone may say no more than "findings are attached" while the
    findings live in the rejected inline payload."""
    stamp, round_label = _round_stamp(client, repo, number, marker, pr_data, reviewed_by)
    try:
        client.create_pr_review(repo, number, body.replace(marker, f"{marker}\n{stamp}", 1), inline)
    except EXPECTED_FAILURES as exc:
        log.warning("inline review failed (%s); falling back to a comment", exc)
        client.comment(repo, number, fallback_body.replace(marker, f"{marker}\n{stamp}", 1))
    return round_label


SKIP_MARKER = "<!-- autoresearch:round-skipped -->"


def post_skip_stub(
    client: GitHubClient,
    repo: str,
    number: int,
    role: str,
    exc: Exception,
    secrets: tuple[str, ...] = (),
) -> None:
    """Silence is invisible: when the model API refuses a round (dead
    credits, spend cap, auth), say so on the thread instead of leaving a
    gap only the Actions tab can see. A DIFFERENT marker than a real
    round, deliberately — a stub never counts toward round numbering and
    never rides as follow-up wake context (both match on their own
    markers).

    `secrets` are the model API key(s) the caller holds — an auth error is
    exactly the class that can echo request material, so we scrub them from the
    posted text. The caller supplies them (the harness owns its own key) so
    posting stays backend-agnostic — the key env var is provider-specific, this
    module is not.
    """
    note = redact(str(exc), tuple(s for s in secrets if s))[:200]
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
