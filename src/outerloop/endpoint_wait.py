"""Durable per-run waits and shared endpoint outage log latches."""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

from outerloop.endpoints import EndpointProfile, EndpointUnavailable
from outerloop.runstate import _save_record, load_record, run_dir

log = logging.getLogger(__name__)


class EndpointWaitReason(str):
    """Transient intake deferral, not a configuration error to log every tick."""


@contextlib.contextmanager
def journal(root: Path, name: str) -> Iterator[dict[str, Any]]:
    directory = root / "endpoint-waits"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    with (directory / f"{name}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text())
        except FileNotFoundError:
            state = {}
        yield state
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state))
        os.replace(temporary, path)


def waiting(root: Path, run_id: str) -> dict[str, Any]:
    value = load_record(root, run_id).stage.get("endpoint_wait")
    return dict(value) if isinstance(value, dict) else {}


def _set_wait(root: Path, run_id: str, name: str, now: float, *, clear: bool = False) -> None:
    """Merge only our stage key under the existing record writer lock."""
    with (run_dir(root, run_id) / ".record-lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = load_record(root, run_id)
        if record.ended():
            return
        stage = dict(record.stage)
        previous = stage.get("endpoint_wait")
        if clear:
            if not isinstance(previous, dict) or previous.get("endpoint") != name:
                return
            stage.pop("endpoint_wait")
        elif isinstance(previous, dict) and previous.get("endpoint") == name:
            return
        else:
            stage["endpoint_wait"] = {"endpoint": name, "since": now}
        _save_record(root, replace(record, stage=stage), now)


def unavailable(root: Path | None, run_id: str, exc: EndpointUnavailable, now: float) -> None:
    """Every deferral path uses this helper; intake/dry runs have no record to mark."""
    if root is None or not run_id or not exc.endpoint:
        return
    name = exc.endpoint
    with journal(root, name) as state:
        record = load_record(root, run_id)
        if record.ended():
            return
        previous = record.stage.get("endpoint_wait")
        started = (
            previous["since"]
            if isinstance(previous, dict) and previous.get("endpoint") == name
            else now
        )
        first = "since" not in state
        state.setdefault("since", now)
        state.setdefault("runs", {}).setdefault(run_id, started)
        _set_wait(root, run_id, name, state["runs"][run_id])
    if first:
        log.warning("endpoint %s unavailable; waiting runs: %s", name, run_id)


def recovered(root: Path, run_id: str, name: str, now: float) -> None:
    """A successful session probe ends the shared outage; only this run resumes."""
    name = name.lower()
    path = root / "endpoint-waits" / f"{name}.json"
    if not path.parent.exists():
        # No outage was ever journaled here. A crash can still persist the run
        # stage before the journal rename; once the folder exists, recovery
        # always takes the journal lock so a concurrent outage write is not lost.
        if waiting(root, run_id).get("endpoint") == name:
            _set_wait(root, run_id, name, now, clear=True)
        return  # Healthy/legacy runs need no journal or record writes.
    with journal(root, name) as state:
        since = state.pop("since", None)
        runs = state.pop("runs", {})
        _set_wait(root, run_id, name, now, clear=True)
    if since is not None:
        log.warning(
            "endpoint %s recovered after %.1fs; waited runs: %s",
            name,
            max(0, now - since),
            ", ".join(sorted(runs)),
        )


def run_context(workspace: Path) -> tuple[Path | None, str]:
    directory = next(
        (
            p
            for p in (workspace, *workspace.parents)
            if p.parent.name == "runs" and (p / "state.json").is_file()
        ),
        None,
    )
    return (directory.parent.parent, directory.name) if directory is not None else (None, "")


def session_url(profile: EndpointProfile, workspace: Path) -> str:
    url = profile.session_url()
    root, run_id = run_context(workspace)
    if root is not None:
        recovered(root, run_id, profile.name, time.time())
    return url
