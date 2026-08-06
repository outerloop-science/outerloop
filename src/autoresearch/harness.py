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

DEFAULT_TIMEOUT_S = 3600
DEFAULT_MAX_TURNS = 80


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


def _error_result(stop_reason: str, transcript_path: str = "") -> SessionResult:
    return SessionResult(
        stop_reason=stop_reason,
        is_error=True,
        cost_usd=0.0,
        num_turns=0,
        session_id="",
        final_text="",
        transcript_path=transcript_path,
    )


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
            command = [
                self.apptainer_binary,
                "exec",
                "--containall",
                "--cleanenv",
                "--bind",
                f"{workspace}:{workspace}",
                "--bind",
                f"{session_home}:{session_home}",
                "--bind",
                f"{self.binary}:{self.CONTAINER_CLAUDE}:ro",
                # HOME inside the container is the same per-run path, so
                # native resume state survives contained/uncontained flips.
                "--env",
                f"HOME={session_home}",
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
            return _error_result("timeout", path)

        stdout = redact(stdout, (self.api_key,))
        transcript_path = _write_private(workspace.parent, transcript_stem, ".json", stdout)
        data = _parse_result(stdout)
        if data is None:
            stderr_tail = redact(stderr, (self.api_key,))[-500:]
            log.warning("unparseable session output (exit %s): %s", process.returncode, stderr_tail)
            return _error_result("unparseable-output", transcript_path)
        # Field-level salvage: a quirky cost value must not cost us the
        # session id (which resume depends on) or vice versa.
        return SessionResult(
            stop_reason=str(data.get("stop_reason") or data.get("subtype") or "unknown"),
            is_error=bool(data.get("is_error", process.returncode != 0)),
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
