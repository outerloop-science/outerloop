"""Private, best-effort evidence for each harness invocation (including wakes)."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import math
import os
import stat
import time
import uuid
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from outerloop.harness import SessionResult

log = logging.getLogger(__name__)
active: ContextVar[dict[str, Any] | None] = ContextVar("session_evidence", default=None)
DEFAULT_CAP = 32 * 1024 * 1024
NATIVE_MAX_DEPTH = 8
NATIVE_MAX_ENTRIES = 10_000
NATIVE_MAX_SECONDS = 0.25


class NativeLookupLimit(OSError):
    """Discovery could not establish a unique source within its budget."""


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) and n >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def usage(data: Any, backend: str) -> dict[str, int]:
    """Normalize reported counters; absent counters stay absent, never estimated."""
    if not isinstance(data, dict):
        return {}
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "cached_input_tokens": ("cached_input_tokens", "cache_read_input_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
        "cache_creation_input_tokens": ("cache_creation_input_tokens",),
    }
    out = {}
    for key, names in aliases.items():
        for name in names:
            n = number(data.get(name))
            if n is not None and n.is_integer():
                out[key] = int(n)
                break
    details = data.get("prompt_tokens_details", {})
    if isinstance(details, dict) and "cached_tokens" in details:
        n = number(details["cached_tokens"])
        if n is not None and n.is_integer():
            out["cached_input_tokens"] = int(n)
    # Claude's input excludes cache reads/writes; the common input counter includes them.
    if backend == "claude" and "input_tokens" in out:
        out["input_tokens"] += out.get("cached_input_tokens", 0) + out.get(
            "cache_creation_input_tokens", 0
        )
    return out


def price(model: str, tokens: dict[str, int]) -> float | None:
    """Operator JSON table, USD per million tokens. No default prices."""
    try:
        table = json.loads(os.environ.get("OUTERLOOP_TOKEN_PRICES", "{}"))
        rates = table[model]
        if not {"input_tokens", "output_tokens"} <= tokens.keys():
            return None
        cached = tokens.get("cached_input_tokens", 0)
        created = tokens.get("cache_creation_input_tokens", 0)
        counts = {**tokens, "input_tokens": tokens["input_tokens"] - cached - created}
        total = 0.0
        for key, count in counts.items():
            if count < 0:
                return None
            if count == 0:
                continue
            rate = number(rates.get(key))
            if rate is None:
                return None
            total += count * rate / 1_000_000
        return total if math.isfinite(total) else None
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def read_bounded(path: Path, cap: int) -> bytes:
    """Refuse symlinks at every component, and special files (including FIFOs)."""
    path = path.absolute()
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError("not a regular file")
            return stream.read(cap)
    finally:
        os.close(fd)


def native_path(home: Path, backend: str, session_id: str) -> Path | None:
    if not session_id or any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        for c in session_id
    ):
        return None
    if backend == "hermes":
        return home / f"evidence-{session_id}.json"
    root = home / (".claude/projects" if backend == "claude" else ".codex/sessions")
    pattern = f"{session_id}.jsonl" if backend == "claude" else f"rollout-*-{session_id}.jsonl"
    deadline = time.monotonic() + NATIVE_MAX_SECONDS
    entries = 0
    matches: list[Path] = []

    def check_budget() -> None:
        if entries > NATIVE_MAX_ENTRIES or time.monotonic() >= deadline:
            raise NativeLookupLimit("native discovery entry/time limit")

    def walk(fd: int, path: Path, depth: int) -> None:
        nonlocal entries
        check_budget()
        with os.scandir(fd) as children:
            for child in children:
                entries += 1
                check_budget()
                if child.is_dir(follow_symlinks=False):
                    if depth >= NATIVE_MAX_DEPTH:
                        raise NativeLookupLimit("native discovery depth limit")
                    child_fd = os.open(
                        child.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                    )
                    try:
                        walk(child_fd, path / child.name, depth + 1)
                    finally:
                        os.close(child_fd)
                elif child.is_file(follow_symlinks=False) and fnmatch.fnmatchcase(
                    child.name, pattern
                ):
                    matches.append(path / child.name)
        check_budget()

    # Open every ancestor without following links, including the session HOME.
    fd = os.open(root.absolute().anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in root.absolute().parts[1:]:
            check_budget()
            child_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child_fd
        walk(fd, root, 0)
    except NativeLookupLimit:
        raise
    except OSError:
        return None
    finally:
        os.close(fd)
    return matches[0] if len(matches) == 1 else None


def capture_session(
    backend: str,
) -> Callable[[Callable[..., SessionResult]], Callable[..., SessionResult]]:
    def decorate(run: Callable[..., SessionResult]) -> Callable[..., SessionResult]:
        @wraps(run)
        def wrapped(
            self: Any, brief_text: str, workspace: Path, resume_session_id: str | None = None
        ) -> SessionResult:
            state: dict[str, Any] = {
                "prompt": brief_text,
                "started_at": datetime.now(UTC).isoformat(),
            }
            token = active.set(state)
            try:
                result = run(self, brief_text, workspace, resume_session_id)
                state["ended_at"] = datetime.now(UTC).isoformat()
                if (
                    backend == "codex"
                    and result.stop_reason == "timeout"
                    and result.transcript_path
                ):
                    from outerloop.harness import _parse_codex_result

                    try:
                        partial = _parse_codex_result(
                            Path(result.transcript_path).read_text(), "", 1
                        )
                        result = replace(
                            result, tokens=partial.tokens, session_id=partial.session_id
                        )
                    except OSError:
                        pass
                if backend != "claude" and result.stop_reason != "timeout":
                    result = replace(result, cost_usd=price(self.model, result.tokens))
                try:
                    result = save(
                        self, backend, workspace, brief_text, resume_session_id, result, state
                    )
                except Exception:
                    log.warning("could not retain session evidence", exc_info=True)
                return result
            finally:
                active.reset(token)

        return wrapped

    return decorate


def save(
    harness: Any,
    backend: str,
    workspace: Path,
    brief: str,
    resume_id: str | None,
    result: SessionResult,
    state: dict[str, Any],
) -> SessionResult:
    from outerloop.appauth import issued_tokens
    from outerloop.harness import _write_private, redact

    invocation = uuid.uuid4().hex
    directory = workspace.parent
    stem = f"{workspace.name}-{backend}-{invocation}"
    secrets = (harness.api_key, *issued_tokens())
    artifacts: dict[str, Any] = {}

    def store(name: str, content: str, suffix: str, *, redacted: bool = False) -> str:
        text = content if redacted else redact(content, secrets)
        path = _write_private(directory, stem + "-" + name, suffix, text)
        artifacts[name] = {
            "path": path,
            "sha256": hashlib.sha256(text.encode()).hexdigest() if path else None,
            "status": "present" if path else "write-error",
        }
        return path

    prompt_path = store("prompt", state["prompt"], ".md")
    if "query" in state:
        store("query", state["query"], ".txt")
    capture = result.transcript_path
    artifacts["capture"] = {"path": capture, "sha256": None, "status": "missing"}
    if capture:
        # The existing capture has already been redacted by the adapter.
        try:
            with open(capture, "rb") as stream:
                artifacts["capture"].update(
                    sha256=hashlib.file_digest(stream, "sha256").hexdigest(), status="present"
                )
        except OSError:
            artifacts["capture"]["status"] = "read-error"
    try:
        cap = max(0, int(os.environ.get("OUTERLOOP_NATIVE_LOG_MAX_BYTES", str(DEFAULT_CAP))))
    except ValueError:
        cap = DEFAULT_CAP
    artifacts["native"] = {
        "path": "",
        "sha256": None,
        "status": "missing",
        "truncated": False,
        "cap_bytes": cap,
    }
    try:
        source = native_path(
            directory / f"{workspace.name}-home",
            backend,
            (state.get("native_id") if backend == "hermes" else result.session_id)
            or state.get("native_id")
            or resume_id
            or "",
        )
    except NativeLookupLimit:
        source = None
        artifacts["native"]["status"] = "unavailable"
        artifacts["native"]["reason"] = "discovery-limit"
    if source:
        try:
            # Look ahead across the cutoff so a secret straddling it cannot leak.
            lookahead = max((len(s.encode()) for s in secrets), default=0) + 1
            raw = read_bounded(source, cap + lookahead)
            text = redact(raw.decode("utf-8", errors="ignore"), secrets)
            if len(raw) == cap + lookahead:
                # Earlier replacements can shrink an incomplete trailing secret
                # into the output cap. Withhold any unresolved suffix.
                unresolved = max(
                    (
                        size
                        for secret in secrets
                        for size in range(1, len(secret))
                        if text.endswith(secret[:size])
                    ),
                    default=0,
                )
                if unresolved:
                    text = text[:-unresolved]
            cleaned = text.encode()
            content = cleaned[:cap].decode("utf-8", errors="ignore")
            store("native", content, source.suffix, redacted=True)
            artifacts["native"].update(
                truncated=len(raw) > cap or len(cleaned) > cap, cap_bytes=cap
            )
        except OSError:
            pass
    record = {
        "schema_version": 1,
        "invocation_id": invocation,
        "backend": backend,
        "model": redact(harness.model, secrets),
        "session_id": redact(
            result.session_id
            or state.get("session_id")
            or resume_id
            or state.get("native_id")
            or "",
            secrets,
        ),
        "native_capture_id": state.get("native_id"),
        "resume_session_id": redact(resume_id, secrets) if resume_id else None,
        "started_at": state["started_at"],
        "ended_at": state["ended_at"],
        "stop_reason": redact(result.stop_reason, secrets),
        "num_turns": result.num_turns or None,
        "tokens": result.tokens,
        "cost_usd": result.cost_usd,
        "brief_sha256": hashlib.sha256(brief.encode()).hexdigest(),
        "artifacts": artifacts,
    }
    record_path = _write_private(directory, stem, ".session.json", json.dumps(record, indent=2))
    return replace(result, prompt_path=prompt_path, session_record_path=record_path)


def session_totals(directory: Path) -> dict[str, Any]:
    """Tolerant reads: legacy runs without sidecars have unknown coverage."""
    known = 0.0
    unknown = 0
    count = 0
    for path in directory.glob("*.session.json"):
        count += 1
        try:
            data = json.loads(path.read_text())
            cost = number(data.get("cost_usd"))
            if cost is None:
                unknown += 1
            else:
                known += cost
        except (OSError, ValueError, AttributeError):
            unknown += 1
    return {
        "session_cost_usd": known if count and not unknown else None,
        "known_session_cost_usd": known,
        "unpriced_sessions": unknown,
        "captured_sessions": count,
    }


def restore_session(stage: Mapping[str, Any], session_id: str) -> SessionResult:
    """Read parked-session fields, tolerating the pre-evidence record shape."""
    from outerloop.harness import SessionResult

    return SessionResult(
        stop_reason="resumed",
        is_error=False,
        cost_usd=number(stage.get("session_cost_usd")),
        tokens=usage(stage.get("session_tokens"), "codex"),
        session_record_path=str(stage.get("session_record_path") or ""),
        num_turns=int(number(stage.get("session_turns")) or 0),
        session_id=session_id,
        final_text=str(stage.get("report", "")),
        transcript_path="",
    )
