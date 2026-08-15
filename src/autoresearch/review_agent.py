"""Agent-session reviewer: runs the reviewer as an agent over a read-only
PR-head checkout.

`run_agent_review` is the orchestration core, testable with a fake harness and
client. It builds on the shared vocabulary in `review`: the same skip rules,
the same rubric (via `build_agent_brief`), the same result policy
(`review_result_from_role`), and the same inline-posting path (`format_review`
-> `post_round_review`).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from autoresearch.github import GitHubClient
from autoresearch.harness import (
    ClaudeCodeHarness,
    CodexHarness,
    Harness,
    HermesHarness,
    backend_id,
    budget_exhausted,
    outage,
)
from autoresearch.posting import (
    EXPECTED_FAILURES,
    post_round_review,
    post_skip_stub,
)
from autoresearch.review import (
    MARKER,
    PullRequest,
    build_agent_brief,
    format_comment,
    format_review,
    skip_reason,
)
from autoresearch.role_runner import run_role
from autoresearch.roles import review_result_from_role, reviewer_spec
from autoresearch.rolespec import RoleSpec

log = logging.getLogger(__name__)

# Native Claude Code tools a judge session uses. The RoleSpec's other tools
# (pr-context-read, retriever) are harness-provided MCP tools, wired separately;
# they are not passed as native CLI tools here.
_NATIVE_READ_TOOLS = ("Read", "Grep", "Glob")


def build_reviewer_harness(
    api_key: str,
    spec: RoleSpec | None = None,
    *,
    backend: str = "claude",
    binary: str | None = None,
    model: str | None = None,
    container_image: str = "",
    hermes_repo: Path | None = None,
    provider: str = "",
    sandbox_extra: tuple[str, ...] = (),
) -> Harness:
    """Construct a read-only harness for the reviewer from its RoleSpec, for the
    chosen backend — the deployment wiring the role-runner assumes (docs: "the
    harness is assumed already constructed for the role").

    How the read-only boundary is enforced differs by backend, and that
    difference is a trust difference, not a detail:
    - Claude: a native read-only tool set (no Write/Edit/Bash). A tool-set
      boundary — the strongest, and the trusted default.
    - Codex: reads by executing shell, so it needs an OS jail (`--sandbox
      read-only`, plus `sandbox_extra` for host-specific config such as
      `-c use_legacy_landlock=true` on GitHub-hosted runners). Even jailed it
      can read a parent's env via /proc, so it is not run next to a token.
    - Hermes: no read-only tool set at all — `file` bundles write, and
      `approvals.deny` gates only shell. Its safety as a judge is ENVIRONMENTAL,
      not tool-set: `terminal` is disabled (no shell, no /proc reach) and its
      writes are inert on an ephemeral runner (nothing to push, scrubbed env).
      Experimental only, and valid ONLY where writes cannot persist.

    Budget: Claude gets max_turns and walltime; Codex is bounded by walltime
    only (no per-turn cap yet) and does not use container_image."""
    spec = spec or reviewer_spec()
    if spec.execution.can_execute:
        raise ValueError("build_reviewer_harness is for read-only judge roles")
    if backend == "codex":
        return CodexHarness(
            api_key=api_key,
            binary=binary or "codex",
            model=model or "",  # codex's configured default
            sandbox="read-only",
            timeout_s=spec.budget.walltime_s,
            extra_args=sandbox_extra,
        )
    if backend == "hermes":
        if hermes_repo is None:
            raise ValueError("hermes reviewer backend needs hermes_repo (the pinned clone)")
        # "openai" maps to hermes's canonical openai-api provider (api-key
        # auth against api.openai.com, key read from OPENAI_API_KEY; plain
        # "openai" is a provider GROUP in hermes, not an id). openai-direct
        # skips the OpenRouter platform fee at the same token rate, and the
        # org's tax exemption applies. Model ids are provider-native:
        # openai/gpt-5.6-terra on openrouter, gpt-5.6-terra on openai-api.
        hermes_providers = {
            "openrouter": ("openrouter", "OPENROUTER_API_KEY"),
            "openai": ("openai-api", "OPENAI_API_KEY"),
        }
        if (provider or "openrouter") not in hermes_providers:
            raise ValueError(f"unknown hermes provider: {provider!r}")
        seed, key_env = hermes_providers[provider or "openrouter"]
        # Defaults already encode the judge shape: file toolset only, terminal
        # and every other executing/egress toolset disabled. Read-only rests on
        # that config plus the ephemeral runner (see the docstring above).
        return HermesHarness(
            api_key=api_key,
            key_env=key_env,
            repo_dir=hermes_repo,
            provider=seed,
            model=model or "",
            max_turns=spec.budget.max_turns,
            timeout_s=spec.budget.walltime_s,
        )
    if backend != "claude":
        raise ValueError(f"unknown reviewer backend: {backend!r}")
    allowed = tuple(tool for tool in spec.tools if tool in _NATIVE_READ_TOOLS)
    return ClaudeCodeHarness(
        api_key=api_key,
        binary=binary or "claude",
        model=model or "claude-opus-5",
        max_turns=spec.budget.max_turns,
        timeout_s=spec.budget.walltime_s,
        allowed_tools=allowed,
        container_image=container_image,
        # A judge's cwd contains an untrusted checkout: never load its
        # CLAUDE.md / hooks / project settings as instructions.
        bare=True,
    )


# Files an agent harness may auto-load as INSTRUCTIONS from a checkout. In an
# untrusted tree they are attack surface (a PR-authored CLAUDE.md, or
# .claude/settings.json hooks that execute commands), so the CLIs rename them
# before any session starts. Renamed — not deleted — so a judge can still read
# them as data. Backend-agnostic defense in depth behind claude's --bare.
INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md", ".claude", ".mcp.json")
SANITIZED_SUFFIX = ".pr-data"


def sanitize_checkout(tree: Path) -> tuple[int, int]:
    """Rename instruction-bearing files/dirs anywhere under `tree` so no agent
    backend auto-loads untrusted content as instructions. Returns
    (renamed, failed). A non-zero `failed` means an instruction file is still
    live (e.g. a crafted name collision blocking the rename) — callers must
    FAIL CLOSED and skip the session rather than judge an unsanitized tree.
    Never raises."""
    renamed = failed = 0
    if not tree.is_dir():
        return 0, 0
    # bottom-up so a renamed directory doesn't orphan paths found beneath it
    for path in sorted(tree.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.name in INSTRUCTION_FILES:
            try:
                path.rename(path.with_name(path.name + SANITIZED_SUFFIX))
                renamed += 1
            except OSError as exc:
                log.warning("could not sanitize %s: %s", path, exc)
                failed += 1
    return renamed, failed


def _pull_request(client: GitHubClient, repo: str, number: int) -> tuple[PullRequest, dict]:
    pr_data = client.get_pull_request(repo, number)
    diff = client.get_pull_request_diff(repo, number)
    pr = PullRequest(
        repo=repo,
        number=number,
        title=str(pr_data.get("title", "")),
        body=str(pr_data.get("body") or ""),
        diff=diff,
        author=str((pr_data.get("user") or {}).get("login", "")),
        # `or []`: the labels field may be null in the payload, not just absent
        labels=tuple(
            str(label.get("name", ""))
            for label in (pr_data.get("labels") or [])
            if isinstance(label, dict)
        ),
    )
    return pr, pr_data


def _emit(
    path: Path,
    repo: str,
    number: int,
    *,
    kind: str,
    data: dict[str, Any] | None = None,
    detail: str = "",
    reviewed_by: str = "",
) -> None:
    """Write the posting step's input. repo/number ride along so the poster
    can refuse an envelope that does not match its own PR reference;
    reviewed_by (backend/model) rides along for the round stamp."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "repo": repo,
                "number": number,
                "kind": kind,
                "data": data,
                "detail": detail,
                "reviewed_by": reviewed_by,
            }
        )
    )


def run_agent_review(
    client: GitHubClient,
    repo: str,
    number: int,
    harness: Harness,
    workspace: Path,
    *,
    bot_login: str,
    spec: RoleSpec | None = None,
    today: str | None = None,
    emit_path: Path | None = None,
) -> str | None:
    """Review PR #`number` as an agent session over `workspace` (a read-only
    PR-head checkout the caller prepared). Post the findings inline via the
    Reviews API. Returns the round label, or None when it skipped or could not
    produce a verdict. Advisory: never raises the expected failures, so it can
    never turn a target repo's CI red.

    With `emit_path`, nothing is posted: the raw findings (or a skip-stub
    marker) are written there for a separate posting step — the least-token
    split (docs/design/reviewer-infra.md). The session job runs with read-only
    permissions; the posting job holds the write token but runs no session.
    A clean skip (bot PR, opt-out, empty diff) writes nothing.
    """
    spec = spec or reviewer_spec()
    today = today or datetime.now(UTC).date().isoformat()
    try:
        pr, pr_data = _pull_request(client, repo, number)
        skip = skip_reason(pr, bot_login)
        if skip is not None:
            log.info("skipping agent review of %s#%s: %s", repo, number, skip)
            return None

        role_result = run_role(spec, harness, build_agent_brief(pr, today), workspace)
        review = review_result_from_role(role_result)
        if review is None:
            # No verdict: an errored or refused session, not a clean read. An
            # API outage or a budget-exhausted session (walltime/turns) says so
            # on the thread; other failures are logged, advisory-silent.
            detail = role_result.error or role_result.session.stop_reason
            log.warning("agent review produced no verdict on %s#%s: %s", repo, number, detail)
            if outage(role_result.session) or budget_exhausted(role_result.session):
                # `detail` is already api-key-redacted by the harness (it owns
                # its own secret), so no secrets are passed here.
                if emit_path is not None:
                    _emit(
                        emit_path,
                        repo,
                        number,
                        kind="skip-stub",
                        detail=detail,
                        reviewed_by=backend_id(harness),
                    )
                else:
                    post_skip_stub(client, repo, number, "advisory review", RuntimeError(detail))
            return None

        if emit_path is not None:
            # raw data, not rendered text: the posting step re-validates and
            # sanitizes at the render boundary, so the artifact crossing the
            # job boundary carries no pre-trusted markup
            _emit(
                emit_path,
                repo,
                number,
                kind="findings",
                data=role_result.data,
                reviewed_by=backend_id(harness),
            )
            cost = role_result.session.cost_usd
            log.info(
                "emitted findings for %s#%s (cost=%s turns=%d)",
                repo,
                number,
                f"${cost:.2f}" if cost else "unreported",
                role_result.session.num_turns,
            )
            return "emitted"

        rendered = format_review(review, pr.diff)
        full = format_comment(review)
        if rendered is None or full is None:
            log.info("nothing to post")
            return None
        body, inline = rendered
        round_label = post_round_review(
            client,
            repo,
            number,
            MARKER,
            body,
            inline,
            pr_data,
            fallback_body=full,
            reviewed_by=backend_id(harness),
        )
        cost = role_result.session.cost_usd
        log.info(
            "posted agent review (%s) on %s#%s (cost=%s turns=%d)",
            round_label,
            repo,
            number,
            f"${cost:.2f}" if cost else "unreported",
            role_result.session.num_turns,
        )
        return round_label
    except EXPECTED_FAILURES as exc:  # advisory: never fail the target repo's CI
        log.warning("agent review did not complete: %s: %s", type(exc).__name__, exc)
        return None
