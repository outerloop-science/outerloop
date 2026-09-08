"""Post a maintenance digest: read the envelope the scan (or the summarizer)
emitted, render it, and upsert the one rolling digest issue. The write token
lives only here — the session jobs are read-only — the same split as the
reviewer. Exits 0 on every outcome.

Env: GITHUB_TOKEN, MAINTAIN_REPO, MAINTAIN_REF, REVIEW_EMIT_FILE,
REVIEW_OPINION_LABEL (optional attribution shown in the header).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from outerloop.github import EnvTokenProvider, GitHubClient
from outerloop.maintain import DIGEST_TITLE, MARKER, render_digest, render_stub
from outerloop.posting import EXPECTED_FAILURES
from outerloop.review import result_from_data

log = logging.getLogger(__name__)


def find_digest_issue(client: GitHubClient, repo: str) -> int | None:
    """The open, bot-authored issue carrying the digest marker, or None. Only
    a bot's own issue is ever rewritten: a person who pastes the marker into
    their issue keeps their text."""
    for issue in client.list_open_issues(repo):
        author_type = str((issue.get("user") or {}).get("type", ""))
        if author_type.casefold() != "bot":
            continue
        if MARKER in str(issue.get("body") or ""):
            return int(issue["number"])
    return None


def post_digest(
    client: GitHubClient,
    repo: str,
    ref: str,
    path: Path,
    *,
    today: str | None = None,
    opinion_label: str = "",
) -> str | None:
    """Post the emitted digest (or the could-not-run stub). Returns
    "created", "updated" or "skip-stub", or None when nothing was posted."""
    today = today or datetime.now(UTC).date().isoformat()
    try:
        envelope = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("findings file unreadable (%s); nothing posted", exc)
        return None
    if not isinstance(envelope, dict):
        log.warning("findings file is not an object; nothing posted")
        return None
    # a scan envelope names the repository and carries number 0: anything
    # else is a review envelope in the wrong pipeline
    if envelope.get("repo") != repo or envelope.get("number") != 0:
        log.warning("envelope names a different repository or a pull request; refused")
        return None
    kind = envelope.get("kind")
    if kind == "skip-clean":
        log.info("scan skipped cleanly (%s); nothing to post", envelope.get("detail", ""))
        return None
    if kind not in ("skip-stub", "findings"):
        log.warning("unknown envelope kind %r; nothing posted", kind)
        return None
    who = " ".join(opinion_label.split())[:60] or str(envelope.get("reviewed_by", ""))
    try:
        number = find_digest_issue(client, repo)
        if kind == "skip-stub":
            body = render_stub(
                str(envelope.get("detail", "")), repo=repo, ref=ref, today=today, who=who
            )
            if number is None:
                client.create_issue(repo, DIGEST_TITLE, body)
            else:
                client.comment(repo, number, body)
            return "skip-stub"
        data = envelope.get("data")
        result = result_from_data(data if isinstance(data, dict) else {})
        body = render_digest(result, repo=repo, ref=ref, today=today, reviewed_by=who)
        if number is None:
            number = client.create_issue(repo, DIGEST_TITLE, body)
            log.info("opened the digest issue %s#%s", repo, number)
            return "created"
        client.update_issue(repo, number, body)
        # a body edit notifies nobody; the comment does
        client.comment(
            repo,
            number,
            f"Digest updated for `{ref[:8]}` on {today}: {len(result.findings)} items.",
        )
        log.info("updated the digest issue %s#%s", repo, number)
        return "updated"
    except EXPECTED_FAILURES as exc:  # advisory: never red the repository's Actions
        log.warning("posting did not complete: %s: %s", type(exc).__name__, exc)
        return None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    repo = os.environ.get("MAINTAIN_REPO", "").strip()
    ref = os.environ.get("MAINTAIN_REF", "").strip()
    if not repo or not ref:
        log.warning("MAINTAIN_REPO/MAINTAIN_REF unset; skipping")
        return 0
    emit_file = os.environ.get("REVIEW_EMIT_FILE", "").strip()
    if not emit_file:
        log.warning("REVIEW_EMIT_FILE is unset; skipping")
        return 0
    path = Path(emit_file).resolve()
    if not path.is_file():
        log.info("no findings file at %s; nothing to post", path)
        return 0
    client = GitHubClient(auth=EnvTokenProvider("GITHUB_TOKEN"))
    post_digest(
        client,
        repo,
        ref,
        path,
        opinion_label=os.environ.get("REVIEW_OPINION_LABEL", "").strip(),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
