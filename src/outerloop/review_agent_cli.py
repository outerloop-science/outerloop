"""Entry point for the agent-session advisory reviewer.

Runs the reviewer as an agent over a PR-head checkout the workflow prepared
(REVIEW_CHECKOUT), and posts the findings inline. Exits 0 even on skip
or failure — an advisory reviewer must never turn a target repo's CI red.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from outerloop.github import EnvTokenProvider, GitHubClient
from outerloop.harness import Harness
from outerloop.review_agent import (
    _emit,
    run_agent_review,
    sanitize_checkout,
)
from outerloop.role_runner import build_harness
from outerloop.roles import reviewer_spec
from outerloop.rolespec import RoleSpec

log = logging.getLogger(__name__)


def _skip_stub(emit_env: str, repo: str, number: int, detail: str, reviewed_by: str) -> None:
    """A fail-closed skip still leaves an envelope when emitting: the post job
    REQUIRES an artifact, and a standing reviewer that cannot run must say so
    on the PR rather than fail into silence."""
    log.warning("%s; skipping review", detail)
    if emit_env:
        _emit(
            Path(emit_env).resolve(),
            repo,
            number,
            kind="skip-stub",
            detail=detail,
            reviewed_by=reviewed_by,
        )


def resolve_reviewer_harness(spec: RoleSpec) -> tuple[Harness | None, str, str]:
    """Resolve the reviewer-env backend contract into a built harness:
    `(harness, "", backend_label)` on success, `(None, why, backend_label)`
    on a config that must skip. The ONE owner of the REVIEW_BACKEND /
    REVIEW_MODEL / REVIEW_HERMES_* / key-var contract, shared by the
    reviewer and the summarizer CLIs — free-form caller inputs are compared
    with EXACTLY GitHub's expression semantics (case-insensitive, never
    trimmed) so this code and the workflow's key-injection expressions can
    never disagree about a value."""
    backend = os.environ.get("REVIEW_BACKEND", "claude").lower()
    review_model = os.environ.get("REVIEW_MODEL", "").strip()
    hermes_provider = os.environ.get("REVIEW_HERMES_PROVIDER", "").lower() or "openrouter"
    key_var = {
        "claude": "ANTHROPIC_REVIEWER_KEY",
        "codex": "OPENAI_REVIEWER_KEY",
        "hermes": {"openrouter": "OPENROUTER_API_KEY", "openai": "OPENAI_REVIEWER_KEY"}.get(
            hermes_provider
        ),
    }.get(backend)
    if key_var is None:
        what = (
            f"unknown REVIEW_HERMES_PROVIDER {hermes_provider!r}"
            if backend == "hermes"
            else f"unknown REVIEW_BACKEND {backend!r}"
        )
        return None, what, backend
    from outerloop.harness import vertex_from_env

    api_key = os.environ.get(key_var, "").strip()
    # an ADC-only deployment holds no Anthropic key: Vertex covering the
    # claude backend stands in for it (same tolerance as role_key)
    vertex_covers = backend == "claude" and vertex_from_env() is not None
    if not api_key and not vertex_covers:
        return None, f"{key_var} is unset or empty", backend
    hermes_repo: Path | None = None
    provider = ""
    if backend == "hermes":
        hermes_repo_env = os.environ.get("REVIEW_HERMES_REPO", "").strip()
        if not hermes_repo_env:
            return None, "REVIEW_HERMES_REPO is unset (hermes needs its pinned clone)", backend
        hermes_repo = Path(hermes_repo_env).resolve()
        provider = hermes_provider
        if provider == "openrouter" and review_model and "/" not in review_model:
            return (
                None,
                "hermes+openrouter requires an OpenRouter-shaped REVIEW_MODEL "
                "(openai/gpt-5.6-terra, with the provider prefix)",
                backend,
            )
        if provider == "openai" and (not review_model or "/" in review_model):
            return (
                None,
                "hermes+openai requires a provider-native REVIEW_MODEL "
                "(gpt-5.6-terra, no openai/ prefix)",
                backend,
            )
    harness = build_harness(
        api_key,
        spec,
        backend=backend,
        binary=os.environ.get("REVIEW_BINARY") or None,  # else the backend default on PATH
        model=review_model or None,
        hermes_repo=hermes_repo,
        hermes_provider=provider,
    )
    return harness, "", backend


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
    emit_env = os.environ.get("REVIEW_EMIT_FILE", "").strip()
    # Fail closed: without the bot login we cannot honor "never review
    # bot-authored PRs", so we do not review at all.
    bot_login = os.environ.get("REVIEW_BOT_LOGIN", "").strip()
    if not bot_login:
        _skip_stub(emit_env, repo, number, "REVIEW_BOT_LOGIN is unset", "")
        return 0
    # Backend is a deployment choice, not baked in: the shared resolver owns
    # the REVIEW_BACKEND/REVIEW_MODEL/REVIEW_HERMES_*/key-var contract (also
    # used by the summarizer CLI); any config that must skip becomes a
    # PR-visible stub, never a silent log line or a traceback.
    # the backend label for pre-harness stubs (attribution only; the resolver
    # below re-derives it authoritatively)
    backend = os.environ.get("REVIEW_BACKEND", "claude").lower()
    # The workflow checks out the PR head into REVIEW_CHECKOUT for the agent
    # to investigate (it records its verdict via the syscall tool). Fail closed:
    # defaulting to cwd would silently review the wrong tree (the reviewer's
    # own repo) if the checkout step were misconfigured.
    checkout = os.environ.get("REVIEW_CHECKOUT", "").strip()
    if not checkout:
        _skip_stub(
            emit_env,
            repo,
            number,
            "REVIEW_CHECKOUT is unset (won't review the wrong tree)",
            backend,
        )
        return 0
    workspace = Path(checkout).resolve()
    # The checkout is untrusted: rename instruction files (CLAUDE.md, .claude/
    # hooks, ...) so no backend auto-loads PR content as instructions. A rename
    # failure means an instruction file is still live — fail closed.
    renamed, failed = sanitize_checkout(workspace)
    if failed:
        _skip_stub(
            emit_env,
            repo,
            number,
            f"checkout could not be fully sanitized ({failed} instruction files left)",
            backend,
        )
        return 0
    if renamed:
        log.info("sanitized %d instruction file(s) in the checkout", renamed)

    lens = os.environ.get("REVIEW_LENS", "").strip()
    if lens:
        from outerloop.review import REVIEW_LENSES

        if lens != "general" and lens not in REVIEW_LENSES:
            # a typo'd lens must be a PR-visible stub, never a silent
            # default-review (a configured lens must never quietly vanish)
            _skip_stub(
                emit_env,
                repo,
                number,
                f"unknown REVIEW_LENS {lens!r} (have: {sorted(REVIEW_LENSES)})",
                backend,
            )
            return 0

    harness, why, backend = resolve_reviewer_harness(reviewer_spec())
    if harness is None:
        _skip_stub(emit_env, repo, number, why, backend)
        return 0

    client = GitHubClient(auth=EnvTokenProvider("GITHUB_TOKEN"))
    # Least-token split: with REVIEW_EMIT_FILE set, findings are written there
    # instead of posted — this job then needs only READ permissions, and a
    # separate posting job (review_post_cli) holds the write token with no
    # session next to it. This is what makes non-Claude backends safe on the
    # auto path (docs/design/reviewer-infra.md).
    run_agent_review(
        client,
        repo,
        number,
        harness,
        workspace,
        bot_login=bot_login,
        emit_path=Path(emit_env).resolve() if emit_env else None,
        lens=lens,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
