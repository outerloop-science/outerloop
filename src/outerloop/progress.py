"""Benchmark measurements and confirmed progress on the research-log branch."""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path

log = logging.getLogger(__name__)

LEADER_FILE = "results/leader.json"
PROGRESS_FILE = "BENCHMARKS.md"
PROGRESS_PATHS = (LEADER_FILE, PROGRESS_FILE)


@dataclass(frozen=True)
class LeaderEntry:
    benchmark: str
    metric: str
    direction: str  # "min" | "max"
    baseline: float  # pinned until a ruler reset
    best: float  # current best orchestrator-measured value
    best_run: str  # run id that set the best
    updated: str  # ISO date
    # seed the best was measured under (0 = none recorded / fixed pool):
    # with resampled pools, a bare scalar is not re-derivable — this plus
    # the eval's seed_env makes the ledger number reproducible
    run_seed: int = 0
    main_commit: str = ""
    measured_sha: str = ""
    measurement_signature: str = ""
    reset_commit: str = ""
    ruler: str = ""


class LedgerReadError(ValueError):
    """The authoritative ledger could not be read safely."""


def parse_leader(content: str) -> dict[str, LeaderEntry]:
    """Decode a complete ledger, refusing malformed rows and nonfinite values."""
    try:
        raw = json.loads(content)
        if not isinstance(raw, dict):
            raise ValueError("ledger must be an object")
        entries = {}
        for name, item in raw.items():
            entry = LeaderEntry(**item)
            for field, value in asdict(entry).items():
                if field in {"baseline", "best"}:
                    if type(value) not in (int, float) or not math.isfinite(value):
                        raise ValueError("invalid measurement")
                elif field == "run_seed":
                    if type(value) is not int:
                        raise ValueError("invalid seed")
                elif not isinstance(value, str):
                    raise ValueError("invalid text field")
            if entry.benchmark != name or entry.direction not in {"min", "max"}:
                raise ValueError("invalid benchmark identity or direction")
            entries[name] = entry
        return entries
    except (TypeError, ValueError) as exc:
        raise LedgerReadError("malformed leader ledger") from exc


def load_leader_strict(workspace: Path) -> dict[str, LeaderEntry]:
    """Read for mutation; absence is empty, corruption or unreadability raises."""
    try:
        return parse_leader((workspace / LEADER_FILE).read_text())
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise LedgerReadError("unreadable leader ledger") from exc


def load_leader(workspace: Path) -> dict[str, LeaderEntry]:
    """Best-effort display read; never use this to authorize a branch write."""
    try:
        return load_leader_strict(workspace)
    except LedgerReadError:
        log.warning("unreadable %s; displaying an empty ledger", workspace / LEADER_FILE)
        return {}


@dataclass(frozen=True)
class PendingSubmission:
    benchmark: str
    metric: str
    direction: str
    baseline: float
    candidate: float
    run_id: str
    run_seed: int
    ruler: str
    measurement_signature: str  # canonical JSON of Benchmark.measurement_signature()
    measured_sha: str
    pr_number: int
    published_head: str
    timestamp: str
    kind: str = "SOLVER"  # SOLVER | RESET
    status: str = "PENDING"
    min_delta: float = 0.0
    min_delta_rel: float = 0.0

    @property
    def path(self) -> str:
        return f"results/submissions/{self.run_id}/{self.published_head}.json"


def parse_pending(content: str) -> PendingSubmission | None:
    """Read a pending record or its removal tombstone, rejecting malformed data."""
    try:
        raw = json.loads(content)
        if raw is None:
            return None
        pending = PendingSubmission(**raw)
        for field, value in asdict(pending).items():
            if field in {"baseline", "candidate", "min_delta", "min_delta_rel"}:
                if type(value) not in (int, float) or not math.isfinite(value):
                    raise ValueError("invalid measurement")
            elif field in {"run_seed", "pr_number"}:
                if type(value) is not int:
                    raise ValueError("invalid integer")
            elif not isinstance(value, str) or not value:
                raise ValueError("missing identity")
        if pending.direction not in {"min", "max"} or pending.kind not in {"SOLVER", "RESET"}:
            raise ValueError("invalid direction or kind")
        for part in (pending.run_id, pending.published_head):
            if part in {".", ".."} or any(
                c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
                for c in part
            ):
                raise ValueError("invalid submission path")
        if pending.status != "PENDING":
            raise ValueError("invalid submission status")
        if pending.pr_number <= 0:
            raise ValueError("invalid PR number")
        return pending
    except (TypeError, ValueError) as exc:
        raise LedgerReadError("malformed pending submission") from exc


def record_pending(pending: PendingSubmission) -> dict[str, str]:
    """Return the file patch for a validated submission; does not advance a leader."""
    content = json.dumps(asdict(pending), indent=2, allow_nan=False) + "\n"
    parse_pending(content)
    return {pending.path: content}


def reject(pending: PendingSubmission) -> dict[str, str]:
    """Drop a submission using a tombstone; never change the leader."""
    record_pending(pending)
    return {pending.path: "null\n"}


def confirm(
    entries: dict[str, LeaderEntry],
    pending: PendingSubmission,
    main_commit: str,
    *,
    is_ancestor: Callable[[str, str], bool],
) -> dict[str, LeaderEntry]:
    """Fold a measured merge into the leader using verified base-branch ancestry.

    The caller verifies the merge corresponds to the measured tree and processes
    ruler changes before their solvers in base-branch ancestry order. Ancestry
    checks must raise on unavailable history, never guess from timestamps.
    """
    record_pending(pending)
    if not main_commit:
        raise ValueError("confirmation requires a merge commit")
    old = entries.get(pending.benchmark)
    if old and old.reset_commit and is_ancestor(main_commit, old.reset_commit):
        return dict(entries)
    if old and old.reset_commit and not is_ancestor(old.reset_commit, main_commit):
        raise ValueError("merge is not on the reset ancestry")
    if old and old.main_commit == main_commit:
        return dict(entries)
    result = dict(entries)
    if pending.kind == "RESET":
        if old and old.main_commit and is_ancestor(main_commit, old.main_commit):
            beats_reset = (
                old.best > pending.candidate
                if pending.direction == "max"
                else old.best < pending.candidate
            )
            if (
                old.measurement_signature == pending.measurement_signature
                and old.ruler == pending.ruler
                and beats_reset
            ):
                result[pending.benchmark] = replace(
                    old, baseline=pending.candidate, reset_commit=main_commit
                )
                return result
        baseline = pending.candidate
        reset_commit = main_commit
    else:
        if old:
            if old.measurement_signature and (
                old.measurement_signature != pending.measurement_signature
                or old.ruler != pending.ruler
            ):
                return result
            if old.metric != pending.metric or old.direction != pending.direction:
                raise ValueError("solver changed the ruler without a reset")
            beats = (
                pending.candidate > old.best
                if pending.direction == "max"
                else pending.candidate < old.best
            )
            from outerloop.orchestrator import clears_min_delta

            if not beats or not clears_min_delta(
                old.best,
                pending.candidate,
                pending.direction,
                pending.min_delta,
                pending.min_delta_rel,
            ):
                return result
        baseline = old.baseline if old else pending.baseline
        reset_commit = old.reset_commit if old else ""
    result[pending.benchmark] = LeaderEntry(
        benchmark=pending.benchmark,
        metric=pending.metric,
        direction=pending.direction,
        baseline=baseline,
        best=pending.candidate,
        best_run=pending.run_id,
        updated=pending.timestamp,
        run_seed=pending.run_seed,
        main_commit=main_commit,
        measured_sha=pending.measured_sha,
        measurement_signature=pending.measurement_signature,
        reset_commit=reset_commit,
        ruler=pending.ruler,
    )
    return result


def _delta(entry: LeaderEntry) -> str:
    if entry.baseline == 0 or entry.best == entry.baseline:
        return "—"
    rel = (entry.best - entry.baseline) / abs(entry.baseline) * 100
    good = rel >= 0 if entry.direction == "max" else rel <= 0
    return f"{'▲' if good else '▼'} {rel:+.1f}%"


DEFAULT_DISPLAY_DIGITS = 6


def fmt_metric(value: float, digits: int | None = None) -> str:
    """Render a measurement for a HUMAN surface at the benchmark's
    conventional precision. The ledger keeps the full float; every
    comparison (improvement thresholds, leader monotonicity) runs on full
    floats — this is presentation only."""
    return f"{value:.{digits or DEFAULT_DISPLAY_DIGITS}g}"


def render_markdown(
    entries: dict[str, LeaderEntry],
    target: str,
    digits: dict[str, int] | None = None,
) -> str:
    lines = [
        "# Benchmark progress",
        "",
        f"Autonomous improvement record for `{target}`. Every number in this",
        "table was measured by the orchestrator re-running the contract's",
        "eval command. Published results are pending until their PR merges.",
        "",
        "| benchmark | metric | baseline | best | progress | last improved | by run | "
        "main commit |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name in sorted(entries):
        e = entries[name]
        arrow = "↓" if e.direction == "min" else "↑"
        d = (digits or {}).get(e.benchmark)
        provenance = (
            f"[{e.main_commit[:9]}](https://github.com/{target}/commit/{e.main_commit})"
            if e.main_commit
            else "provenance unknown"
        )
        lines.append(
            f"| {e.benchmark} | `{e.metric}` {arrow} | {fmt_metric(e.baseline, d)} | "
            f"{fmt_metric(e.best, d)} | {_delta(e)} | {e.updated} | `{e.best_run}` | {provenance} |"
        )
    lines += [
        "",
        "_Written by [outerloop](https://github.com/outerloop-science/outerloop);",
        "do not edit by hand — agent edits to this file end the run._",
        "",
    ]
    return "\n".join(lines)


def write_progress(
    workspace: Path,
    entries: dict[str, LeaderEntry],
    target: str,
    digits: dict[str, int] | None = None,
) -> None:
    leader_path = workspace / LEADER_FILE
    leader_path.parent.mkdir(parents=True, exist_ok=True)
    # the ledger keeps FULL precision — it feeds comparisons, never eyes
    leader_path.write_text(
        json.dumps({name: asdict(e) for name, e in sorted(entries.items())}, indent=2) + "\n"
    )
    (workspace / PROGRESS_FILE).write_text(render_markdown(entries, target, digits))
