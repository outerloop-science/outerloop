"""The harness seam: one agent session in, one result out.

`Harness.run` takes rendered brief text and a workspace directory and returns
a :class:`SessionResult`. Provider quirks (auth, CLI flags, output parsing)
live inside adapters; context policy lives in `brief` and is shared by every
backend — that separation is what makes backend comparisons honest
(docs/design/architecture.md, "The backend seam").

Sessions run in a scrubbed environment: an explicit allowlist plus the one
API key the backend needs. The bot PAT and anything else in the orchestrator's
environment never reach a session (threat model: credential theft).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

# What a session's environment contains — nothing else survives from the
# parent. HOME is deliberately NOT inherited: it is redirected to a per-run
# directory so a session cannot read key files under the real home or poison
# other runs via ~/.claude state. (Residual risk: the filesystem itself is
# not sandboxed — same-user absolute paths remain readable. See the threat
# model; the bot PAT must not live on the account that runs sessions until
# OS-level sandboxing lands.)
SESSION_ENV_ALLOWLIST = ("PATH", "TERM", "LANG", "LC_ALL", "TMPDIR")

# Fallbacks only — every orchestrated path threads effective_limits in
# explicitly (climb/steward CLIs pass turns and minutes; the follow-up CLI
# derives its timeout from the job walltime). Kept at the session ceilings
# so a site that forgets still grants the intended budget rather than
# silently undercutting it.
DEFAULT_TIMEOUT_S = 5400
DEFAULT_MAX_TURNS = 120


@dataclass(frozen=True)
class SessionResult:
    """What happened in one session — everything the orchestrator needs to
    judge, bill, and report it. The workspace diff is captured by the caller
    (it owns the git clone); the harness owns only the session.

    On the "timeout" path, cost and session id are unknown (the CLI is killed
    before it reports); budget accounting must treat a timeout as worst-case
    spend, not zero.
    """

    stop_reason: str  # backend's stop reason, or "timeout" / "spawn-error"
    is_error: bool
    cost_usd: float
    num_turns: int
    session_id: str
    final_text: str  # the agent's closing message (the research report draft)
    transcript_path: str  # raw backend output, api-key-redacted, on disk
    # human-readable cause when is_error: the backend's error subtype and
    # messages (e.g. "error_max_turns: Reached maximum number of turns
    # (60)"), or our own explanation on the timeout path. This is what
    # reports and issue comments show — stop_reason alone reads as noise
    # ("tool_use") when a session dies mid-tool-call.
    error_detail: str = ""


class Harness(Protocol):
    """One coding session over a workspace. Implementations are adapters.

    A *run* (one hypothesis) may span many sessions: a session that launches a
    long experiment ends, and when results arrive the orchestrator wakes the
    agent with `resume_session_id` — restoring its full working context — and
    a wake prompt carrying the results. Session state lives in the per-run
    HOME next to the workspace, so wakes survive orchestrator restarts and can
    land on a different cluster node (shared filesystem)."""

    def run(
        self, brief_text: str, workspace: Path, resume_session_id: str | None = None
    ) -> SessionResult: ...


def session_env(api_key: str, key_variable: str, home: Path) -> dict[str, str]:
    """The scrubbed environment a session runs with."""
    env = {name: os.environ[name] for name in SESSION_ENV_ALLOWLIST if name in os.environ}
    env["HOME"] = str(home)
    env[key_variable] = api_key
    return env


def redact(text: str, secrets: tuple[str, ...]) -> str:
    """Strip known secrets from text before it is stored anywhere."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _error_result(stop_reason: str, transcript_path: str = "", detail: str = "") -> SessionResult:
    return SessionResult(
        stop_reason=stop_reason,
        is_error=True,
        error_detail=(detail or stop_reason)[:500],
        cost_usd=0.0,
        num_turns=0,
        session_id="",
        final_text="",
        transcript_path=transcript_path,
    )


# Substrings that mean "the API itself is unavailable to us" — credit,
# limit, auth, throttling. Matched only against error surfaces of an
# is_error result (backend error text, never agent prose).
OUTAGE_PATTERNS = (
    "credit balance",
    "usage limit",
    "spending limit",
    "billing",
    "authentication_error",
    "invalid x-api-key",
    "rate_limit_error",
    "overloaded_error",
)


def outage(result: SessionResult) -> bool:
    """True when the session failed because the API refused us — dead
    credits, spend cap, bad key, throttling — not because of anything in
    the run. Callers treat this as an infrastructure outage: pause the
    lanes, and never bill the failure against a run's retry caps or a
    work order's attempts."""
    if not result.is_error:
        return False
    # error_detail is backend error text and always wins; final_text is
    # consulted ONLY when no detail exists AND it carries the legacy CLI
    # error shape ("API Error ..."), never agent prose — a failed session's
    # report that merely MENTIONS billing or limits must not trip a latch
    # that pauses every lane (review findings, rounds 1 and 2).
    surface = result.error_detail.casefold()
    if not surface:
        text = result.final_text.strip().casefold()
        if not text.startswith("api error"):
            return False
        surface = text
    return any(pattern in surface for pattern in OUTAGE_PATTERNS)


def budget_exhausted(result: SessionResult) -> bool:
    """True when the session stopped because OUR limits ran out — turns or
    session walltime — rather than because anything failed. Callers report
    this as the budget-exhausted ending, never as an error: "caps hit
    mid-run" is one of the six honest deaths, not a malfunction."""
    return result.stop_reason == "timeout" or result.error_detail.startswith("error_max_turns")


def _write_private(directory: Path, stem: str, suffix: str, text: str) -> str:
    """Atomically create a fresh owner-only file and write `text` to it.

    O_EXCL closes the TOCTOU between name choice and creation, and it also
    refuses symlinks (a session could plant a dangling link where its own
    transcript will land, redirecting the write to an arbitrary same-user
    file). Returns the path written, or "" — storage failures must not crash
    the adapter."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for n in range(1, 1000):
        path = directory / (f"{stem}{suffix}" if n == 1 else f"{stem}-{n}{suffix}")
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            continue
        except OSError as exc:
            log.warning("could not store transcript at %s: %s", path, exc)
            return ""
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        return str(path)
    log.warning("could not find a free transcript name in %s", directory)
    return ""


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


@dataclass
class ClaudeCodeHarness:
    """Headless Claude Code (`claude -p`), as validated in the Torch spike:
    the JSON output carries cost, usage, session id, and stop reason.

    `run` never raises: every failure comes back as an error SessionResult.
    """

    api_key: str
    binary: str = "claude"
    model: str = "claude-opus-5"
    max_turns: int = DEFAULT_MAX_TURNS
    timeout_s: int = DEFAULT_TIMEOUT_S
    # The working set for code + running the repo's own tests. Note the env
    # DOES hold the session API key (the CLI needs it), so Bash here is a
    # trusted-ish surface; the brief is the only untrusted-ish input and it
    # passes the task-source gate upstream.
    allowed_tools: tuple[str, ...] = ("Write", "Edit", "Read", "Glob", "Grep", "Bash")
    extra_args: tuple[str, ...] = field(default_factory=tuple)
    # Apptainer image for session containment (decided 2026-08-06). When set,
    # the session runs under `apptainer exec --containall --cleanenv`: no host
    # $HOME, no host env, no same-user absolute paths — the session sees only
    # the workspace, its per-run HOME, and the read-only claude binary. This
    # closes the threat model's shared-filesystem residual risk. Images stay
    # generic (python + uv + git); the binary is bind-mounted in.
    container_image: str = ""
    apptainer_binary: str = "apptainer"

    CONTAINER_CLAUDE = "/opt/agent/claude"

    def run(
        self, brief_text: str, workspace: Path, resume_session_id: str | None = None
    ) -> SessionResult:
        # Both live OUTSIDE the git clone: the transcript must never enter the
        # diff that gets committed/pushed, and the per-RUN home (0700; reused
        # across this run's sessions, never across runs — the orchestrator
        # gives every run a fresh workspace path) is what lets a later wake
        # restore the agent's working context.
        transcript_stem = f"{workspace.name}-session"
        session_home = workspace.parent / f"{workspace.name}-home"
        try:
            session_home.mkdir(parents=True, exist_ok=True, mode=0o700)
            # mkdir's mode is umask-masked and ignored entirely on reuse;
            # enforce it either way.
            os.chmod(session_home, 0o700)
        except OSError as exc:
            log.warning("could not create session home %s: %s", session_home, exc)
            return _error_result("workspace-error")
        claude_argv = [
            self.CONTAINER_CLAUDE if self.container_image else self.binary,
            "-p",
            # The brief travels on stdin: argv is world-readable via /proc on
            # shared nodes, and briefs carry private research text.
            "Follow the brief provided on stdin.",
            "--model",
            self.model,
            "--output-format",
            "json",
            "--max-turns",
            str(self.max_turns),
            "--allowedTools",
            ",".join(self.allowed_tools),
            "--permission-mode",
            "acceptEdits",
            *self.extra_args,
        ]
        if resume_session_id:
            claude_argv += ["--resume", resume_session_id]
        if self.container_image:
            # bind sources must be absolute or apptainer fails at mount time
            workspace = workspace.resolve()
            session_home = session_home.resolve()
            if not os.path.isabs(self.binary):
                # a relative bind source fails at mount time deep inside
                # apptainer; catch the misconfiguration here instead
                log.warning("container sessions need an absolute claude path")
                return _error_result("config-error")
            command = [
                self.apptainer_binary,
                "exec",
                "--containall",
                "--cleanenv",
                "--bind",
                f"{workspace}:{workspace}",
                # --home (NOT --env HOME=..., which apptainer silently
                # refuses): mounts the per-run home at the same path inside
                # and sets $HOME to it — native resume state survives
                # contained/uncontained flips and lands on the shared FS,
                # not a tmpfs that evaporates at session end.
                "--home",
                f"{session_home}:{session_home}",
                "--bind",
                f"{self.binary}:{self.CONTAINER_CLAUDE}:ro",
                "--pwd",
                str(workspace),
                self.container_image,
                *claude_argv,
            ]
        else:
            command = claude_argv
        try:
            # start_new_session puts the CLI and every descendant (Bash-tool
            # children included) in one process group we can kill as a unit —
            # a timed-out session must not leave orphans holding the API key
            # and writing into the clone.
            env = session_env(self.api_key, "ANTHROPIC_API_KEY", session_home)
            if self.container_image:
                # --cleanenv drops the host environment inside the container
                # EXCEPT APPTAINERENV_* variables, which apptainer injects
                # with the prefix stripped — the key travels via the
                # environment, never argv (argv is world-readable in /proc).
                env["APPTAINERENV_ANTHROPIC_API_KEY"] = self.api_key
            process = subprocess.Popen(
                command,
                cwd=workspace,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            log.warning("could not spawn %s: %s", self.binary, exc)
            return _error_result("spawn-error")

        try:
            stdout, stderr = process.communicate(input=brief_text, timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            # Bounded drain: a descendant that left the process group (setsid)
            # can hold the pipe open past the kill; run() must still return.
            try:
                stdout, _ = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout = ""
                with contextlib.suppress(subprocess.TimeoutExpired):
                    stdout, _ = process.communicate(timeout=5)
            path = _write_private(
                workspace.parent, transcript_stem, ".json", redact(stdout or "", (self.api_key,))
            )
            log.warning("session timed out after %ss in %s", self.timeout_s, workspace)
            return _error_result(
                "timeout",
                path,
                detail=f"session hit its {self.timeout_s}s walltime and was killed",
            )

        stdout = redact(stdout, (self.api_key,))
        transcript_path = _write_private(workspace.parent, transcript_stem, ".json", stdout)
        data = _parse_result(stdout)
        if data is None:
            stderr_tail = redact(stderr, (self.api_key,))[-500:]
            log.warning("unparseable session output (exit %s): %s", process.returncode, stderr_tail)
            return _error_result("unparseable-output", transcript_path)
        # Field-level salvage: a quirky cost value must not cost us the
        # session id (which resume depends on) or vice versa.
        is_error = bool(data.get("is_error", process.returncode != 0))
        subtype = str(data.get("subtype") or "")
        errors = data.get("errors")
        messages = "; ".join(str(e) for e in errors if e) if isinstance(errors, list) else ""
        detail = f"{subtype}: {messages}" if subtype and messages else (subtype or messages)
        # bounded here so every downstream note/report/comment inherits it
        detail = detail[:500]
        return SessionResult(
            stop_reason=str(data.get("stop_reason") or data.get("subtype") or "unknown"),
            is_error=is_error,
            error_detail=detail if is_error else "",
            cost_usd=_float(data.get("total_cost_usd")),
            num_turns=_int(data.get("num_turns")),
            session_id=str(data.get("session_id") or ""),
            final_text=str(data.get("result") or ""),
            transcript_path=transcript_path,
        )


def _parse_result(stdout: str) -> dict[str, Any] | None:
    """The result object from the CLI's JSON output, or None.

    When stdout is not one clean JSON object, prefer the FIRST candidate that
    looks like the CLI's result (carries session_id/total_cost_usd): the CLI
    prints its result before any stray output that follows it, so forward
    order keeps a trailing look-alike from substituting its fields into
    billing and resume.
    """
    text = stdout.strip()
    if not text:
        return None
    with contextlib.suppress(json.JSONDecodeError):
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    candidates: list[dict[str, Any]] = []
    for chunk in (*text.splitlines(), text[text.find("{") : text.rfind("}") + 1]):
        with contextlib.suppress(json.JSONDecodeError):
            data = json.loads(chunk)
            if isinstance(data, dict):
                candidates.append(data)
    for candidate in candidates:
        if "session_id" in candidate or "total_cost_usd" in candidate:
            return candidate
    return candidates[0] if candidates else None


def _codex_command(
    binary: str,
    model: str,
    sandbox: str,
    workspace: Path,
    last_message_path: Path,
    resume_session_id: str | None,
    extra_args: tuple[str, ...],
) -> list[str]:
    """Argv for one headless `codex exec` run.

    The prompt is NOT an argument: `codex exec` reads it from stdin when no
    positional prompt is given, keeping the brief out of world-readable /proc
    argv (the same rule as the Claude adapter). Flags follow the Codex docs and
    must be checked against `codex exec --help` on the cluster.
    """
    head = [binary, "exec", "resume", resume_session_id] if resume_session_id else [binary, "exec"]
    return [
        *head,
        "--json",  # JSONL events on stdout (session id, usage)
        "--model",
        model,
        "--sandbox",
        sandbox,  # "read-only" for judge roles; "workspace-write" for authors
        "--cd",
        str(workspace),
        "--output-last-message",  # final message -> file (reliable final_text)
        str(last_message_path),
        "--skip-git-repo-check",
        *extra_args,
    ]


def _parse_codex_result(
    stdout: str, last_message: str, returncode: int, transcript_path: str = ""
) -> SessionResult:
    """Best-effort SessionResult from `codex exec --json` output.

    `final_text` comes from the --output-last-message file, which is reliable.
    `session_id` is pulled from the JSONL events defensively; the exact event
    schema must be confirmed against a live `--json` run, so until then resume
    may be unavailable. Cost is left at 0 (these backends are subscription or
    token metered; the budget layer meters them by a session/token proxy).
    Never raises.
    """
    session_id = ""
    saw_error = False
    errors: list[str] = []
    for line in stdout.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            continue  # human-readable lines can interleave the JSONL; skip them
        if not isinstance(event, dict):
            continue
        for key in ("session_id", "conversation_id"):
            value = event.get(key)
            if not session_id and isinstance(value, str):
                session_id = value
        # A truthy `error` value or an error-typed event, not the mere presence
        # of an "error" key — a benign event may carry `"error": null`.
        if str(event.get("type", "")).endswith("error") or event.get("error"):
            saw_error = True
            message = event.get("message") or event.get("error")
            if isinstance(message, str):
                errors.append(message)
    is_error = returncode != 0 or saw_error
    detail = "; ".join(errors)[:500]
    return SessionResult(
        stop_reason="error" if is_error else "completed",
        is_error=is_error,
        error_detail=detail if is_error else "",
        cost_usd=0.0,
        num_turns=0,
        session_id=session_id,
        final_text=last_message.strip(),
        transcript_path=transcript_path,
    )


@dataclass
class CodexHarness:
    """Headless OpenAI Codex CLI (`codex exec`) — a second Harness backend.

    Stage 1's swappability proof, first used for the read-only reviewer
    (docs/design/consolidation.md): Codex's own `--sandbox read-only` plus a
    judge RoleSpec's tool set. Flags and the JSONL event schema follow the Codex
    docs and MUST be verified against `codex exec --help` and a live `--json`
    run on the cluster before production; `session_id` and cost parsing are
    best-effort until then. No apptainer wrapper yet (the reviewer runs
    read-only); the author-on-Codex path adds one later.

    `run` never raises: every failure comes back as an error SessionResult.
    """

    api_key: str
    binary: str = "codex"
    model: str = "gpt-5-codex"  # verify the available model id on the cluster
    sandbox: str = "read-only"  # judge default; authors pass "workspace-write"
    timeout_s: int = DEFAULT_TIMEOUT_S
    extra_args: tuple[str, ...] = field(default_factory=tuple)

    def run(
        self, brief_text: str, workspace: Path, resume_session_id: str | None = None
    ) -> SessionResult:
        transcript_stem = f"{workspace.name}-codex"
        session_home = workspace.parent / f"{workspace.name}-home"
        try:
            session_home.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(session_home, 0o700)
        except OSError as exc:
            log.warning("could not create session home %s: %s", session_home, exc)
            return _error_result("workspace-error")
        # --output-last-message target lives inside the per-run home (0700),
        # not the shared parent, so model output is not exposed there while
        # codex is writing it; cleared first so a stale file can never be read
        # as this run's result, and deleted again after reading.
        last_message_path = session_home / "codex-last-message.txt"
        with contextlib.suppress(OSError):
            last_message_path.unlink()
        command = _codex_command(
            self.binary,
            self.model,
            self.sandbox,
            workspace,
            last_message_path,
            resume_session_id,
            self.extra_args,
        )
        try:
            env = session_env(self.api_key, "OPENAI_API_KEY", session_home)
            process = subprocess.Popen(
                command,
                cwd=workspace,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            log.warning("could not spawn %s: %s", self.binary, exc)
            return _error_result("spawn-error")
        try:
            stdout, _ = process.communicate(input=brief_text, timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            try:
                stdout, _ = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout = ""
                with contextlib.suppress(subprocess.TimeoutExpired):
                    stdout, _ = process.communicate(timeout=5)
            path = _write_private(
                workspace.parent, transcript_stem, ".jsonl", redact(stdout or "", (self.api_key,))
            )
            with contextlib.suppress(OSError):
                last_message_path.unlink()  # clean up on the timeout path too
            log.warning("codex session timed out after %ss in %s", self.timeout_s, workspace)
            return _error_result(
                "timeout",
                path,
                detail=f"session hit its {self.timeout_s}s walltime and was killed",
            )
        stdout = redact(stdout, (self.api_key,))
        transcript_path = _write_private(workspace.parent, transcript_stem, ".jsonl", stdout)
        last_message = ""
        # errors="replace": a non-UTF-8 last-message file must not raise
        # UnicodeDecodeError (not an OSError) and break the never-raises contract.
        with contextlib.suppress(OSError):
            last_message = redact(last_message_path.read_text(errors="replace"), (self.api_key,))
        # The final message is preserved in the 0600 transcript; drop the raw
        # file so model output is not left behind.
        with contextlib.suppress(OSError):
            last_message_path.unlink()
        return _parse_codex_result(stdout, last_message, process.returncode, transcript_path)


@dataclass
class FakeHarness:
    """Deterministic in-process harness for tests and dry runs."""

    result: SessionResult
    script: Any = None  # optional callable(brief_text, workspace) for side effects
    calls: list[tuple[str, str, str | None]] = field(default_factory=list)

    def run(
        self, brief_text: str, workspace: Path, resume_session_id: str | None = None
    ) -> SessionResult:
        self.calls.append((brief_text, str(workspace), resume_session_id))
        if self.script is not None:
            self.script(brief_text, workspace)
        return self.result
