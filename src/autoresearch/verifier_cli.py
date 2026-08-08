"""Entry point for the verification workflow (bot-PR integrity reads).

Mirrors review_cli's constitution: reads the PR from the environment, runs
one verification, posts one new comment per round, and exits 0 even when it
skips or fails — an advisory role must never turn a target repo's CI red.
The extra context it gathers is the point: the contract and the frozen
ruler's source come from the BASE branch (never the PR), so the verifier
judges the claim against the rules and the eval as they actually are.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import replace
from datetime import UTC, datetime

from autoresearch.github import EnvTokenProvider, GitHubClient
from autoresearch.llm import AnthropicCompleter
from autoresearch.review import PullRequest
from autoresearch.review_cli import EXPECTED_FAILURES, _gather_context, post_round
from autoresearch.verifier import (
    MAX_RULER_FILES,
    VERIFY_MARKER,
    format_verify_comment,
    verify,
    verify_skip_reason,
)

log = logging.getLogger(__name__)

_MODULE_FLAG = re.compile(r"-m\s+([A-Za-z_][\w.]*)")


# Fan-out is bounded by ATTEMPTS, not successes — and the two sources get
# SEPARATE budgets: a contract full of 404ing module guesses (`-m pytest`)
# must neither train requests against the token's rate budget nor starve
# the tests/ tripwires of theirs.
MAX_MODULE_FETCH_ATTEMPTS = MAX_RULER_FILES
MAX_TEST_FETCH_ATTEMPTS = MAX_RULER_FILES


def _ruler_paths(contract_text: str) -> list[str]:
    """Best-effort paths of the frozen ruler's source, derived from the
    contract's own eval commands (`python -m pkg.mod` → src/pkg/mod.py) —
    no repo-layout assumptions beyond src-layout-or-flat. Bounded."""
    paths: list[str] = []
    for module in _MODULE_FLAG.findall(contract_text):
        rel = module.replace(".", "/") + ".py"
        for candidate in (f"src/{rel}", rel):
            if candidate not in paths:
                paths.append(candidate)
        if len(paths) >= MAX_MODULE_FETCH_ATTEMPTS:
            break
    return paths[:MAX_MODULE_FETCH_ATTEMPTS]


def gather_ruler(
    client: GitHubClient, repo: str, base_ref: str, contract_text: str
) -> tuple[tuple[str, str], ...]:
    """Fetch ruler source from the BASE branch: resolved eval modules first,
    then test files (the tripwires). Best-effort and bounded — a degraded
    verification beats none. Layout assumptions, stated honestly: eval
    modules resolve src-layout-or-flat; tripwires come from a TOP-LEVEL
    tests/ directory, non-recursive. Repos with nested or renamed test
    trees get module context only — the system prompt tells the model to
    note missing ruler source rather than guess."""
    out: list[tuple[str, str]] = []
    try:
        for path in _ruler_paths(contract_text):  # already capped at module budget
            if len(out) >= MAX_RULER_FILES:
                return tuple(out)
            content = client.get_file_content(repo, path, base_ref)
            if content is not None:
                out.append((path, content))
        test_attempts = 0
        for item in client.list_directory(repo, "tests", base_ref):
            if len(out) >= MAX_RULER_FILES or test_attempts >= MAX_TEST_FETCH_ATTEMPTS:
                break
            name = str(item.get("name", ""))
            path = str(item.get("path", ""))
            if item.get("type") == "file" and name.endswith(".py") and path:
                test_attempts += 1
                content = client.get_file_content(repo, path, base_ref)
                if content is not None:
                    out.append((path, content))
    except EXPECTED_FAILURES as exc:
        log.warning("verifying with partial ruler context: %s", exc)
    return tuple(out)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    repo = os.environ["PR_REPO"]
    number = int(os.environ["PR_NUMBER"])
    # Fail closed: without the bot's login we cannot identify bot-authored
    # PRs, and verifying a HUMAN PR is outside this role's constitution.
    bot_login = os.environ.get("REVIEW_BOT_LOGIN", "").strip()
    if not bot_login:
        log.warning("REVIEW_BOT_LOGIN is unset; skipping (cannot identify bot-authored PRs)")
        return 0
    if not os.environ.get("ANTHROPIC_VERIFIER_KEY", "").strip():
        log.warning("ANTHROPIC_VERIFIER_KEY is unset or empty; skipping verification")
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
        contract_text = ""
        ruler: tuple[tuple[str, str], ...] = ()
        # Context is fetched only for PRs that will actually be verified —
        # human-authored and opted-out PRs must not pay the API fan-out.
        if verify_skip_reason(pr, bot_login) is None:
            base = pr_data.get("base")
            base_ref = str(base.get("ref", "")) if isinstance(base, dict) else ""
            # Best-effort like the ruler: a transient failure here must
            # degrade the round (empty contract, model notes the gap), not
            # silently lose it to the outer handler.
            try:
                contract_text = (
                    client.get_file_content(repo, ".autoresearch.yaml", base_ref or "HEAD") or ""
                )
            except EXPECTED_FAILURES as exc:
                log.warning("verifying without the contract: %s", exc)
                contract_text = ""
            ruler = gather_ruler(client, repo, base_ref or "HEAD", contract_text)
            pr = replace(pr, context_files=_gather_context(client, repo, number, pr_data))
        completer = AnthropicCompleter(
            api_key=os.environ["ANTHROPIC_VERIFIER_KEY"],
            model=os.environ.get("VERIFY_MODEL") or "claude-opus-5",
            effort=os.environ.get("VERIFY_EFFORT") or "high",
        )
        today = datetime.now(UTC).date().isoformat()
        body = format_verify_comment(
            verify(pr, completer, bot_login, contract_text, ruler, today=today)
        )
        if body is None:
            log.info("nothing to post")
            return 0
        round_label = post_round(client, repo, number, VERIFY_MARKER, body, pr_data)
        log.info("posted verification (%s) on %s#%s", round_label, repo, number)
    except EXPECTED_FAILURES as exc:  # advisory role: never fail the target's CI
        log.warning("verification did not complete: %s: %s", type(exc).__name__, exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
