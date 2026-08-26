"""The role-runner: build a harness for a role, run one session, read its result.

One loop runs every role (docs/design/consolidation.md), and ONE
construction builds every harness: `build_harness`
maps a RoleSpec to any backend uniformly — backends are interchangeable, and
containment is the deployment's business (`container_image` where a jail
exists, the ephemeral runner where one doesn't), never a per-role tool posture.
Roles differ by prompt, verbs, and output handling.

`run_role` runs a RoleSpec on a Harness. A role WITH an `output_schema` (a
judge) records its verdict through the installed syscall tool (`finding` /
`conclude`) and the kernel reads it back authoritatively (`read_verdict`);
each call is validated in-session. It does NOT judge, gate, measure, or
post — the result-policy (kernel) acts on the RoleResult.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoresearch.harness import (
    ClaudeCodeHarness,
    CodexHarness,
    Harness,
    HermesHarness,
    SessionResult,
    vertex_from_env,
)
from autoresearch.rolespec import RoleSpec

log = logging.getLogger(__name__)

# Native Claude Code tool names a spec may grant. A spec's other tools
# (pr-context-read, retriever) are harness-provided MCP tools, wired
# separately — never passed as native CLI tools.
_NATIVE_TOOLS = frozenset({"Read", "Grep", "Glob", "Write", "Edit", "Bash"})

# hermes names capabilities as toolsets: `file` is the read/edit surface,
# `terminal` the shell. Everything else stays disabled for parity with the
# other backends — no spec grants web/browser/... tools there either.
_HERMES_TOOLSETS = (
    "file",
    "terminal",
    "web",
    "search",
    "browser",
    "computer_use",
    "code_execution",
    "delegation",
    "cronjob",
    "skills",
    "memory",
)

# hermes resolves credentials per provider (a registry); "openai" maps to its
# canonical `openai-api` provider id (api-key auth against api.openai.com —
# plain "openai" is a provider GROUP there, not an id).
_HERMES_PROVIDERS = {
    "openrouter": ("openrouter", "OPENROUTER_API_KEY"),
    "openai": ("openai-api", "OPENAI_API_KEY"),
}


def role_key(key_file: str | Path, backend: str = "claude") -> str:
    """Read a role's API key file — tolerating its ABSENCE exactly when the
    deployment's Vertex config covers the claude backend (an ADC-only
    deployment holds no Anthropic key at all; the harness then authenticates
    via ADC and ignores api_key). Every other backend, and claude without
    Vertex, still fails loudly on a missing/lax key file."""
    from autoresearch.harness import vertex_from_env

    path = Path(key_file).expanduser()
    if backend == "claude" and vertex_from_env() is not None and not path.is_file():
        return ""
    from autoresearch.github import FileTokenProvider

    return FileTokenProvider(path).token()


def build_harness(
    api_key: str,
    spec: RoleSpec,
    *,
    backend: str = "claude",
    binary: str | None = None,
    model: str | None = None,
    container_image: str = "",
    codex_extra_args: tuple[str, ...] = (),
    hermes_repo: Path | None = None,
    hermes_provider: str = "",
) -> Harness:
    """Construct the harness for any role on any backend — the ONE deployment
    wiring (`spec.tools` → native flags, `spec.budget` → turns/walltime,
    `spec.execution` → the backend's execution surface). Each branch below is
    the backend's irreducible calling convention, nothing more:

    - claude: `spec.tools` filtered to the native CLI tools; a judge's cwd is an
      untrusted checkout, so judges (specs with an `output_schema`) run `--bare`
      — never loading the tree's CLAUDE.md / hooks as instructions. Editors keep
      instruction discovery (the target repo's guidance is legitimate for them).
    - codex: `danger-full-access` uniformly — codex's own sandbox needs
      bubblewrap (absent in the image, unreliable nested in apptainer), so the
      boundary is the deployment's container or ephemeral runner, exactly as it
      is for every other backend.
    - hermes: toolsets from the spec's execution (`terminal` for a role that
      executes); provider/key seeded per the registry above.

    Containment is NOT decided here: pass `container_image` where the
    deployment has a jail (the cluster), pass none where the runner itself is
    the ephemeral boundary (CI). The tokenless split keeps credentials out of
    the session either way."""
    if backend == "codex":
        return CodexHarness(
            api_key=api_key,
            binary=binary or "codex",
            model=model or "",  # "" -> codex's configured default; pin a verified id
            sandbox="danger-full-access",
            timeout_s=spec.budget.walltime_s,
            container_image=container_image,
            extra_args=codex_extra_args,
        )
    if backend == "hermes":
        if hermes_repo is None:
            raise ValueError("hermes backend needs hermes_repo (the pinned clone)")
        if (hermes_provider or "openrouter") not in _HERMES_PROVIDERS:
            raise ValueError(f"unknown hermes provider: {hermes_provider!r}")
        seed, key_env = _HERMES_PROVIDERS[hermes_provider or "openrouter"]
        # `terminal` (the shell) is keyed on the SAME signal claude uses — the
        # spec granting the Bash tool — not on can_execute, so every backend
        # gives a role the same shell/no-shell whether or not those two ever
        # diverge for a future spec.
        enabled = ("file", "terminal") if "Bash" in spec.tools else ("file",)
        return HermesHarness(
            api_key=api_key,
            key_env=key_env,
            repo_dir=hermes_repo,
            provider=seed,
            model=model or "",
            max_turns=spec.budget.max_turns,
            timeout_s=spec.budget.walltime_s,
            enabled_toolsets=enabled,
            disabled_toolsets=tuple(t for t in _HERMES_TOOLSETS if t not in enabled),
        )
    if backend != "claude":
        raise ValueError(f"unknown backend: {backend!r}")
    return ClaudeCodeHarness(
        api_key=api_key,
        binary=binary or "claude",
        model=model or "claude-opus-5",
        max_turns=spec.budget.max_turns,
        timeout_s=spec.budget.walltime_s,
        allowed_tools=tuple(tool for tool in spec.tools if tool in _NATIVE_TOOLS),
        container_image=container_image,
        # a judge's cwd contains an untrusted checkout: never load its CLAUDE.md
        # / hooks / project settings as instructions (defence in depth beside
        # the caller's sanitize_checkout)
        bare=spec.output_schema is not None,
        # Vertex (ADC) billing when the deployment configures it; the env
        # contract has ONE owner (harness.vertex_from_env), so every claude
        # role on every CLI flips together and the API key stays the fallback
        vertex=vertex_from_env(),
    )


@dataclass(frozen=True)
class RoleResult:
    """The outcome of one role run: the session plus, for judge roles, the
    validated verdict. `ok` is False when the session errored or no valid
    verdict was committed; `data` is the validated object (None for an editing
    role, whose artifact is the workspace diff, or on failure)."""

    ok: bool
    session: SessionResult
    data: dict[str, Any] | None = None
    error: str = ""


def run_role(
    spec: RoleSpec,
    harness: Harness,
    brief_text: str,
    workspace: Path,
    resume_session_id: str | None = None,
) -> RoleResult:
    """Run one role session; for a judge (a spec with an `output_schema`), read
    the verdict it committed through the syscall tool.

    The `harness` is assumed already constructed for this role
    (`build_harness`). A judge records each finding as one validated tool call
    and commits with `conclude`; `read_verdict` is the authoritative kernel-side
    check, so there is no repair loop — a missing verdict (the judge never
    concluded) or a malformed one is a failure the caller surfaces (a skip
    stub), never a clean read (silence is never endorsement). Installing the
    tool BEFORE the session force-owns the `.autoresearch/` channel, so a
    pre-planted or stale ABI never survives into the read — a resumed (revise)
    session likewise starts from a clean channel and commits a fresh verdict.
    """
    is_judge = spec.output_schema is not None
    if is_judge:
        from autoresearch.syscall import install_tool

        install_tool(workspace)
    session = harness.run(brief_text, workspace, resume_session_id)
    if session.is_error:
        return RoleResult(
            ok=False, session=session, error=session.error_detail or session.stop_reason
        )
    if not is_judge:
        return RoleResult(ok=True, session=session)  # editing role: artifact is the diff
    from autoresearch.syscall import VerdictError, read_verdict

    try:
        data = read_verdict(workspace)
    except VerdictError as exc:
        return RoleResult(ok=False, session=session, error=f"invalid verdict: {exc}")
    if data is None:
        return RoleResult(ok=False, session=session, error="judge produced no verdict")
    return RoleResult(ok=True, session=session, data=data)
