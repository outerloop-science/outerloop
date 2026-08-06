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

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

# What a session's environment contains — nothing else survives from the
# parent. HOME is deliberately NOT inherited: it is redirected to a fresh
# per-session directory so a session cannot read key files under the real
# home or poison future sessions via ~/.claude state. (Residual risk: the
# filesystem itself is not sandboxed — same-user absolute paths remain
# readable. See the threat model; the bot PAT must not live on the account
# that runs sessions until OS-level sandboxing lands.)
SESSION_ENV_ALLOWLIST = ("PATH", "TERM", "LANG", "LC_ALL", "TMPDIR")

DEFAULT_TIMEOUT_S = 3600
DEFAULT_MAX_TURNS = 80


@dataclass(frozen=True)
class SessionResult:
    """What happened in one session — everything the orchestrator needs to
    judge, bill, and report it. The workspace diff is captured by the caller
    (it owns the git clone); the harness owns only the session."""

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


@dataclass
class ClaudeCodeHarness:
    """Headless Claude Code (`claude -p`), as validated in the Torch spike:
    the JSON output carries cost, usage, session id, and stop reason."""

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

    def run(
        self, brief_text: str, workspace: Path, resume_session_id: str | None = None
    ) -> SessionResult:
        # Both live OUTSIDE the git clone: the transcript must never enter the
        # diff that gets committed/pushed, and the per-run HOME keeps CLI
        # state (and anything a session writes "home") out of the real home.
        # The HOME is per-RUN, not per-session: it is what lets a later wake
        # (`resume_session_id`) restore the agent's working context.
        transcript = _fresh_path(workspace.parent, f"{workspace.name}-session", ".json")
        session_home = workspace.parent / f"{workspace.name}-home"
        session_home.mkdir(parents=True, exist_ok=True)
        command = [
            self.binary,
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
            command += ["--resume", resume_session_id]
        try:
            completed = subprocess.run(
                command,
                cwd=workspace,
                env=session_env(self.api_key, "ANTHROPIC_API_KEY", session_home),
                input=brief_text,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            # TimeoutExpired carries bytes even under text=True.
            raw = exc.stdout or b""
            partial = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
            transcript.write_text(redact(partial, (self.api_key,)))
            log.warning("session timed out after %ss in %s", self.timeout_s, workspace)
            return SessionResult(
                stop_reason="timeout",
                is_error=True,
                cost_usd=0.0,
                num_turns=0,
                session_id="",
                final_text="",
                transcript_path=str(transcript),
            )
        except OSError as exc:
            log.warning("could not spawn %s: %s", self.binary, exc)
            return SessionResult(
                stop_reason="spawn-error",
                is_error=True,
                cost_usd=0.0,
                num_turns=0,
                session_id="",
                final_text="",
                transcript_path="",
            )

        stdout = redact(completed.stdout, (self.api_key,))
        transcript.write_text(stdout)
        data = _parse_result(stdout)
        if data is None:
            stderr_tail = redact(completed.stderr, (self.api_key,))[-500:]
            log.warning(
                "unparseable session output (exit %s): %s", completed.returncode, stderr_tail
            )
            return SessionResult(
                stop_reason="unparseable-output",
                is_error=True,
                cost_usd=0.0,
                num_turns=0,
                session_id="",
                final_text="",
                transcript_path=str(transcript),
            )
        try:
            return SessionResult(
                stop_reason=str(data.get("stop_reason") or data.get("subtype") or "unknown"),
                is_error=bool(data.get("is_error", completed.returncode != 0)),
                cost_usd=float(data.get("total_cost_usd") or 0.0),
                num_turns=int(data.get("num_turns") or 0),
                session_id=str(data.get("session_id") or ""),
                final_text=str(data.get("result") or ""),
                transcript_path=str(transcript),
            )
        except (ValueError, TypeError):
            # Quirky field types must degrade like any other bad output —
            # this adapter never raises.
            log.warning("session output had malformed fields")
            return SessionResult(
                stop_reason="unparseable-output",
                is_error=True,
                cost_usd=0.0,
                num_turns=0,
                session_id="",
                final_text="",
                transcript_path=str(transcript),
            )


def _fresh_path(directory: Path, stem: str, suffix: str) -> Path:
    """A path that does not clobber earlier sessions of the same run."""
    path = directory / f"{stem}{suffix}"
    n = 2
    while path.exists():
        path = directory / f"{stem}-{n}{suffix}"
        n += 1
    return path


def _parse_result(stdout: str) -> dict[str, Any] | None:
    """The result object from the CLI's JSON output, or None."""
    text = stdout.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Defensive: the CLI should emit exactly one JSON object, but logs
        # around it must not cost us a paid-for session. Try the last line,
        # then the outermost brace span.
        for candidate in (text.splitlines()[-1], text[text.find("{") : text.rfind("}") + 1]):
            try:
                data = json.loads(candidate)
                break
            except json.JSONDecodeError:
                continue
        else:
            return None
    return data if isinstance(data, dict) else None


@dataclass
class FakeHarness:
    """Deterministic in-process harness for tests and dry runs."""

    result: SessionResult
    script: Any = None  # optional callable(brief_text, workspace) for side effects
    calls: list[tuple[str, str]] = field(default_factory=list)

    def run(
        self, brief_text: str, workspace: Path, resume_session_id: str | None = None
    ) -> SessionResult:
        self.calls.append((brief_text, str(workspace)))
        if self.script is not None:
            self.script(brief_text, workspace)
        return self.result
