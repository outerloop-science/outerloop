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

from autoresearch.github import EnvTokenProvider, GitHubClient
from autoresearch.review_agent import (
    _emit,
    run_agent_review,
    sanitize_checkout,
)
from autoresearch.role_runner import build_harness
from autoresearch.roles import reviewer_spec

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
    # Backend is a deployment choice, not baked in: pick the harness and its
    # key by REVIEW_BACKEND (claude | codex | hermes), per the Harness seam.
    # Free-form caller inputs are compared with EXACTLY GitHub's expression
    # semantics — case-insensitive, never trimmed — so this CLI and the
    # workflow's key-injection expressions can never disagree about a value:
    # a padded input misses both layers and lands in the unknown-value stub.
    # explicit-empty mirrors the workflow too: '' matches no key-injection
    # expression there, so here it must land in the unknown-backend stub
    # rather than silently meaning claude
    backend = os.environ.get("REVIEW_BACKEND", "claude").lower()
    # a model id is never compared against workflow expressions, so trimming
    # is safe here — and the same trimmed value must reach gate and harness
    review_model = os.environ.get("REVIEW_MODEL", "").strip()
    # hermes's key SOURCE follows its provider: openai-direct uses the same
    # org-registered OpenAI key as codex (no OpenRouter platform fee).
    # Explicitly-empty input means the default, same as an omitted one.
    hermes_provider = os.environ.get("REVIEW_HERMES_PROVIDER", "").lower() or "openrouter"
    key_var = {
        "claude": "ANTHROPIC_REVIEWER_KEY",
        "codex": "OPENAI_REVIEWER_KEY",
        "hermes": {"openrouter": "OPENROUTER_API_KEY", "openai": "OPENAI_REVIEWER_KEY"}.get(
            hermes_provider
        ),
    }.get(backend)
    if key_var is None:
        # a typo in a caller's free-form backend/provider input must be a
        # PR-visible stub, not a silent log line or a traceback
        what = (
            f"unknown REVIEW_HERMES_PROVIDER {hermes_provider!r}"
            if backend == "hermes"
            else f"unknown REVIEW_BACKEND {backend!r}"
        )
        log.warning("%s; skipping review", what)
        if emit_env:
            _emit(
                Path(emit_env).resolve(),
                repo,
                number,
                kind="skip-stub",
                detail=what,
                reviewed_by=backend,
            )
        return 0
    from autoresearch.harness import vertex_from_env

    api_key = os.environ.get(key_var, "").strip()
    # an ADC-only deployment holds no Anthropic key: Vertex covering the
    # claude backend stands in for it (same tolerance as role_key), so the
    # skip-stub fires only when NEITHER credential source exists
    vertex_covers = backend == "claude" and vertex_from_env() is not None
    if not api_key and not vertex_covers:
        log.warning("%s is unset or empty; skipping review", key_var)
        if emit_env:
            # a standing second-opinion service must not die silently when
            # its key is missing or rotates out (keys expire): the stub the
            # post job publishes is the PR-visible warning
            _emit(
                Path(emit_env).resolve(),
                repo,
                number,
                kind="skip-stub",
                detail=f"{key_var} is unset or empty",
                # no harness exists yet; the backend name still attributes the stub
                reviewed_by=backend,
            )
        return 0
    # Backend-specific deployment config, resolved from env so the workflow
    # (which knows the host) supplies it, never the pure builder:
    #   hermes needs its pinned clone and a provider to seed ~/.hermes/config.
    hermes_repo: Path | None = None
    provider = ""
    if backend == "hermes":
        hermes_repo_env = os.environ.get("REVIEW_HERMES_REPO", "").strip()
        if not hermes_repo_env:
            _skip_stub(
                emit_env,
                repo,
                number,
                "REVIEW_HERMES_REPO is unset (hermes needs its pinned clone)",
                backend,
            )
            return 0
        hermes_repo = Path(hermes_repo_env).resolve()
        provider = hermes_provider
        if provider == "openrouter" and review_model and "/" not in review_model:
            # the mirror mistake: OpenRouter ids are provider/model-shaped, so
            # a native id would burn a session on an unresolvable model
            _skip_stub(
                emit_env,
                repo,
                number,
                "hermes+openrouter requires an OpenRouter-shaped REVIEW_MODEL "
                "(openai/gpt-5.6-terra, with the provider prefix)",
                backend,
            )
            return 0
        if provider == "openai" and (not review_model or "/" in review_model):
            # an empty model falls back to hermes's default id and a slash
            # marks an OpenRouter-shaped one — api.openai.com serves neither,
            # so the session would burn its budget on an unservable id
            detail = (
                "hermes+openai requires a provider-native REVIEW_MODEL "
                "(gpt-5.6-terra, no openai/ prefix)"
            )
            log.warning("%s; skipping review", detail)
            if emit_env:
                _emit(
                    Path(emit_env).resolve(),
                    repo,
                    number,
                    kind="skip-stub",
                    detail=detail,
                    reviewed_by=backend,
                )
            return 0
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

    client = GitHubClient(auth=EnvTokenProvider("GITHUB_TOKEN"))
    harness = build_harness(
        api_key,
        reviewer_spec(),
        backend=backend,
        binary=os.environ.get("REVIEW_BINARY") or None,  # else the backend default on PATH
        model=review_model or None,
        hermes_repo=hermes_repo,
        hermes_provider=provider,
    )
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
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
