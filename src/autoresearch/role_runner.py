"""The role-runner: run one role session and validate its output.

One loop replaces the per-role driver modules (docs/design/consolidation.md). It
runs a RoleSpec on a Harness, and for a role that declares an `output_schema`
(the judges) it parses and validates the session's final message into structured
data, repairing once if the model returned malformed output. It does NOT judge,
gate, measure, or post — the result-policy (kernel) acts on the RoleResult.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoresearch.harness import Harness, SessionResult
from autoresearch.rolespec import RoleSpec

log = logging.getLogger(__name__)

DEFAULT_MAX_REPAIRS = 1


@dataclass(frozen=True)
class RoleResult:
    """The outcome of one role run: the session plus, for judge roles, the
    validated structured payload. `ok` is False when the session errored or the
    output never validated; `data` is the validated object (None for an editing
    role, whose artifact is the workspace diff, or on failure)."""

    ok: bool
    session: SessionResult
    data: dict[str, Any] | None = None
    error: str = ""


def _extract_json(text: str) -> str:
    """The JSON object inside a final message, tolerating code fences and prose
    around it: the substring from the first `{` to the last `}`."""
    stripped = text.strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        return stripped[start : end + 1]
    return stripped


def _validate_output(text: str, schema: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Parse `text` as the schema's top-level object. Dep-free: a JSON object
    with the schema's `required` keys present. Deeper validation is the
    backend's native structured output or a later jsonschema pass."""
    try:
        data = json.loads(_extract_json(text))
    except json.JSONDecodeError as exc:
        return None, f"final message is not valid JSON: {exc}"
    if not isinstance(data, dict):
        return None, "final message is not a JSON object"
    missing = [key for key in schema.get("required", []) if key not in data]
    if missing:
        return None, f"missing required keys: {missing}"
    return data, ""


def _repair_prompt(error: str) -> str:
    return (
        f"Your last message was not the structured output this task requires: {error}. "
        "Reply with ONLY the JSON object — no prose, no code fences."
    )


def run_role(
    spec: RoleSpec,
    harness: Harness,
    brief_text: str,
    workspace: Path,
    resume_session_id: str | None = None,
    max_repairs: int = DEFAULT_MAX_REPAIRS,
) -> RoleResult:
    """Run one role session; validate structured output for judge roles.

    A judge whose first message is malformed is asked once (per `max_repairs`)
    to resend just the JSON, resuming the same session so it keeps its context.

    The `harness` is assumed already constructed for this role — its tools,
    execution sandbox, and budget set to match `spec`. That construction (map
    spec.tools to the backend's native tool flags vs harness-provided MCP
    tools, spec.execution to the sandbox, spec.budget to turns/walltime) is the
    deployment wiring, done where the harness is built for the role — the same
    way climb builds its harness from effective_limits. run_role does not
    reconcile a mismatched harness; it runs what it is given.
    """
    session = harness.run(brief_text, workspace, resume_session_id)
    if session.is_error:
        return RoleResult(
            ok=False, session=session, error=session.error_detail or session.stop_reason
        )
    if spec.output_schema is None:
        return RoleResult(ok=True, session=session)  # editing role: artifact is the diff
    data, error = _validate_output(session.final_text, spec.output_schema)
    repairs = 0
    while data is None and repairs < max_repairs:
        # A repair prompt only works if it resumes the session that holds the
        # investigation context. With no session to resume, a fresh session
        # would see only "resend the JSON" and produce nothing useful — fail
        # instead of burning a context-less turn.
        resume_target = session.session_id or resume_session_id
        if not resume_target:
            log.info("role %s: cannot repair structured output without a session", spec.name)
            break
        repairs += 1
        log.info("role %s: repairing structured output (%s)", spec.name, error)
        session = harness.run(_repair_prompt(error), workspace, resume_session_id=resume_target)
        if session.is_error:
            return RoleResult(
                ok=False, session=session, error=session.error_detail or session.stop_reason
            )
        data, error = _validate_output(session.final_text, spec.output_schema)
    if data is None:
        return RoleResult(ok=False, session=session, error=f"invalid structured output: {error}")
    return RoleResult(ok=True, session=session, data=data)
