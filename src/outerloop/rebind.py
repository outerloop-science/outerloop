"""Explicit operator requests, applied only by the next wake lease holder."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from outerloop.runstate import RunRecord, _save_record, load_record, run_dir

log = logging.getLogger(__name__)
REQUEST = "rebind.json"


def request_status(root: Path, run_id: str) -> dict[str, Any] | None:
    try:
        return json.loads((run_dir(root, run_id) / REQUEST).read_text())
    except FileNotFoundError:
        return None


def requested(root: Path, run_id: str) -> bool:
    pending = request_status(root, run_id)
    return pending is not None and pending.get("status", "pending") == "pending"


def _publish(path: Path, pending: dict[str, Any]) -> None:
    temporary = path.with_name(f".rebind.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(pending))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _failed_application(path: Path, pending: dict[str, Any], error: Exception) -> None:
    pending["failures"] = pending.get("failures", 0) + 1
    pending["status"] = "failed" if pending["failures"] >= 3 else "pending"
    pending["last_error"] = str(error)
    _publish(path, pending)
    log.warning("run %s: rebind %s: %s", path.parent.name, pending["status"], error)


def failed_application(root: Path, run_id: str, request_id: str, error: Exception) -> None:
    """Record unexpected apply errors without charging a replacement request."""
    directory = run_dir(root, run_id)
    with (directory / ".record-lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pending = request_status(root, run_id)
        if pending and pending["id"] == request_id and pending.get("status") != "failed":
            _failed_application(directory / REQUEST, pending, error)


def cancel(root: Path, run_id: str) -> None:
    if not run_id or Path(run_id).name != run_id or run_id in (".", ".."):
        raise ValueError("invalid run id")
    directory = run_dir(root, run_id)
    if not directory.is_dir():
        raise ValueError(f"no pending rebind for run {run_id}")
    with (directory / ".record-lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not requested(root, run_id):
            raise ValueError(f"no pending rebind for run {run_id}")
        (directory / REQUEST).unlink()


def request(root: Path, run_id: str, note: str = "") -> bool:
    """Publish once under the record lock; never mutate a running session."""
    if not run_id or Path(run_id).name != run_id or run_id in (".", ".."):
        raise ValueError("invalid run id")
    directory = run_dir(root, run_id)
    try:
        load_record(root, run_id)
    except FileNotFoundError:
        raise ValueError(f"unknown run {run_id}") from None
    with (directory / ".record-lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if load_record(root, run_id).ended():
            raise ValueError(f"run {run_id} has ended")
        path = directory / REQUEST
        if requested(root, run_id):
            return False
        _publish(
            path,
            {
                "id": uuid.uuid4().hex,
                "time": time.time(),
                "note": note,
                "status": "pending",
                "failures": 0,
                "last_error": "",
            },
        )
    return True


def apply(root: Path, record: RunRecord, image: str) -> RunRecord:
    """Validate before replacing any binding. Caller owns the wake lease."""
    from outerloop.attempt import (
        author_config_error,
        effective_author_credential,
        fleet_author_model,
    )
    from outerloop.contract import load_contract
    from outerloop.endpoints import EndpointUnavailable, resolve_endpoint
    from outerloop.github import GitError, Workspace, contract_at
    from outerloop.harness import ClaudeModelUnset
    from outerloop.limits import effective_limits
    from outerloop.tick import _claim_overrides

    directory = run_dir(root, record.run_id)
    if not requested(root, record.run_id):
        return record
    with (directory / ".record-lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = load_record(root, record.run_id)
        path = directory / REQUEST
        if record.ended() or not path.exists():
            return record
        pending = json.loads(path.read_text())
        try:
            if pending.get("status") == "failed":
                return record
            if record.author_rebind_id == pending["id"]:
                path.unlink()  # Recover a crash after saving the binding.
                return record
            selected = next(
                (
                    o
                    for o in _claim_overrides(record.target).get(record.target, ())
                    if o.matches(record.agent_id)
                ),
                None,
            )
            backend = (
                selected.backend
                if selected
                else os.environ.get("OUTERLOOP_AUTHOR_BACKEND", "claude") or "claude"
            )
            model = selected.resolved_model() if selected else fleet_author_model(backend)
            error = author_config_error(backend, model, image)
            if error:
                raise ValueError(error)
            _, profile = resolve_endpoint(model, backend)
            if profile:
                _ = profile.url
            credential = effective_author_credential(backend, model)
            credential.key()
            budgets = None
            if record.stage.get("base_sha"):
                budgets = load_contract(
                    contract_at(Workspace(directory / "ws"), str(record.stage["base_sha"])),
                    record.target,
                ).budgets
            limits = effective_limits(
                budgets,
                session_minutes=selected.session_minutes if selected else None,
                session_max_turns=selected.session_max_turns if selected else None,
            )
        except (
            ValueError,
            OSError,
            KeyError,
            EndpointUnavailable,
            GitError,
            ClaudeModelUnset,
        ) as exc:
            _failed_application(path, pending, exc)
            return record
        now = time.time()
        history = record.author_history or [
            {
                "backend": record.author_backend or "claude",
                "model": record.author_model,
                "since": record.created,
                "note": "",
            }
        ]
        stage = dict(record.stage)
        stage.pop("endpoint_wait", None)
        from outerloop.provenance import candidate_authors

        stage["candidate_authors"] = candidate_authors(record)
        changed_backend = backend != (record.author_backend or "claude")
        if changed_backend:
            stage.pop("hermes_resume_required_chars", None)
        rebound = replace(
            record,
            author_backend=backend,
            author_model=model,
            author_key_file=str(credential.key_file),
            # Only an override with session fields binds limits; otherwise defaults apply.
            author_limits=(
                asdict(limits)
                if selected and (selected.session_minutes or selected.session_max_turns)
                else None
            ),
            author_overridden=selected is not None,
            resume_session_id="" if changed_backend else record.resume_session_id,
            stage=stage,
            wake_attempts=0,
            author_rebind_id=pending["id"],
            author_history=[
                *history,
                {
                    "backend": backend,
                    "model": model,
                    "since": now,
                    "note": pending["note"],
                },
            ],
        )
        _save_record(root, rebound, now)
        path.unlink()
        log.info(
            "run %s: rebind %s/%s -> %s/%s",
            record.run_id,
            record.author_backend or "claude",
            record.author_model,
            backend,
            model,
        )
        return load_record(root, record.run_id)
