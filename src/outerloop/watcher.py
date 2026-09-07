"""The session watcher: a thread beside a live author session that answers its
questions through the syscall channel in seconds — `queue` (the kernel's jobs
in the cluster queue, every agent's) and `history` (this run's launches) —
where the tick would take a cadence. It changes no lifecycle state: it writes
answer files into the channel and nothing else; submitting, cancelling,
sealing and parking stay at the sleep boundary (docs/design/session-watcher.md).

Failure never reaches the session: an exception is logged and the request is
left standing, the tool times out and says so. A watcher that dies leaves the
session exactly as it is without one."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from outerloop.climbboard import queue_rows
from outerloop.launchlog import history, why_by_job
from outerloop.runstate import run_dir
from outerloop.syscall import MAX_WHY_CHARS, mark_done, marker_requested, write_channel_json

log = logging.getLogger(__name__)

VERBS = ("queue", "history")
POLL_S = 2.0
# one scheduler query per this many seconds however often the marker is
# rewritten: a session cannot turn the watcher into a squeue loop
QUERY_GAP_S = 5.0
MAX_QUEUE_ROWS = 500
# the lane's node states move slowly; one sinfo a minute is plenty
LANE_GAP_S = 60.0
# a stopped watcher may still be inside a scheduler query (its own timeout,
# a minute at most); the attempt waits this long for it, then moves on — the
# thread is a daemon and writes nothing once stopped
JOIN_S = 15.0


@dataclass
class WatcherContext:
    """What the watcher needs to answer: where the channel is, which run it
    serves, and the compute backend to ask (None = no queue, local mode)."""

    workspace: Path
    run_root: Path
    run_id: str
    target: str
    agent_id: str
    compute: Any = None
    gpu_partition: str = ""
    poll_s: float = POLL_S
    query_gap_s: float = QUERY_GAP_S
    lane_gap_s: float = LANE_GAP_S
    clock: Callable[[], float] = field(default=time.time)


def _kind(name: str) -> str:
    """A short label for a kernel job that is not a launch."""
    for key in ("wake", "followup", "eval", "resident", "tick"):
        if key in name:
            return key
    return name


class SessionWatcher:
    """`with SessionWatcher(ctx):` around the harness subprocess. `service()`
    is one pass, public so the loop is testable without the thread."""

    def __init__(self, ctx: WatcherContext) -> None:
        self.ctx = ctx
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._queue_cache: dict[str, Any] | None = None
        self._queue_at = 0.0
        self._lane_cache: dict[str, Any] | None = None
        self._lane_at = 0.0

    def __enter__(self) -> SessionWatcher:
        self._thread = threading.Thread(target=self._loop, name="session-watcher", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=JOIN_S)
            if self._thread.is_alive():
                log.warning("session watcher still inside a scheduler query at session end")

    def _loop(self) -> None:
        # wait() is False on timeout: poll; True once stopped
        while not self._stop.wait(self.ctx.poll_s):
            self.service()

    def service(self) -> None:
        """Answer every verb whose request marker is newer than its done marker."""
        for verb in VERBS:
            if self._stop.is_set():
                return
            try:
                requested = marker_requested(self.ctx.workspace, f"{verb}-request", f"{verb}-done")
                if requested is None:
                    continue
                payload = self.queue_view() if verb == "queue" else self.history_view()
                if self._stop.is_set():
                    return  # the session ended meanwhile: nothing is written after it
                write_channel_json(self.ctx.workspace, f"{verb}.json", payload)
                mark_done(self.ctx.workspace, f"{verb}-done", requested)
            except Exception as exc:  # the request stands; the tool times out
                log.warning("session watcher: %s failed: %s", verb, exc)

    def history_view(self) -> dict[str, Any]:
        ctx = self.ctx
        return {
            "at": ctx.clock(),
            "run_id": ctx.run_id,
            "history": history(run_dir(ctx.run_root, ctx.run_id)),
        }

    def queue_view(self) -> dict[str, Any]:
        """The kernel's jobs as the board projects them (attributed to agents),
        each launch labelled with its author's `why`, plus the GPU lane's load.
        Cached for `query_gap_s` — the rate limit on scheduler queries."""
        ctx = self.ctx
        now = ctx.clock()
        if self._queue_cache is not None and now - self._queue_at < ctx.query_gap_s:
            return {**self._queue_cache, "at": now, "cached": True}
        error = ""
        rows: list[dict[str, Any]] = []
        try:
            snapshot = ctx.compute.queue_snapshot() if ctx.compute is not None else []
            rows = [
                dict(r) for r in queue_rows(ctx.run_root, ctx.target, snapshot)[:MAX_QUEUE_ROWS]
            ]
        except Exception as exc:
            error = str(exc)[:200]
        labels: dict[str, dict[str, Any]] = {}
        for rid in {str(r.get("run_id") or "") for r in rows} - {""}:
            labels.update(why_by_job(run_dir(ctx.run_root, rid)))
        for r in rows:
            rid = str(r.get("run_id") or "")
            name = str(r.get("name") or "")
            prefix = f"{rid}-launch-" if rid else ""
            r["mine"] = bool(rid) and rid == ctx.run_id
            if prefix and name.startswith(prefix):
                r["experiment"] = name[len(prefix) :]
                r["why"] = str(labels.get(str(r.get("id") or ""), {}).get("why", ""))[
                    :MAX_WHY_CHARS
                ]
                r["kind"] = "launch"
            else:
                r["experiment"] = ""
                r["why"] = ""
                r["kind"] = _kind(name)
        view = {
            "at": now,
            "run_id": ctx.run_id,
            "agent": ctx.agent_id,
            "jobs": rows,
            "lane": self._lane_load(),
            "error": error,
        }
        self._queue_cache, self._queue_at = view, now
        return view

    def _lane_load(self) -> dict[str, Any]:
        """The GPU lane's node states, refreshed once per `lane_gap_s` (a
        second scheduler query per request would defeat the rate limit). A
        failed sinfo is reported in the answer, never passed off as a lane
        with no state."""
        ctx = self.ctx
        load = getattr(ctx.compute, "lane_load", None)
        if load is None or not ctx.gpu_partition:
            return {}
        now = ctx.clock()
        if self._lane_cache is not None and now - self._lane_at < ctx.lane_gap_s:
            return self._lane_cache
        try:
            lane: dict[str, Any] = {
                "partition": ctx.gpu_partition,
                "nodes": load(ctx.gpu_partition),
            }
        except Exception as exc:
            lane = {"partition": ctx.gpu_partition, "nodes": {}, "error": str(exc)[:200]}
        self._lane_cache, self._lane_at = lane, now
        return lane
