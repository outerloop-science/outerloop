"""Human-readable benchmark progress, written by the orchestrator.

Two files in the target repo, updated as part of each improvement PR so the
progress record and the change that caused it land atomically:

- ``results/leader.json`` — the machine ledger (the scaling sketch's
  "leader"): per benchmark, the original baseline, the current best, and
  which run set it.
- ``BENCHMARKS.md`` — the same data as a table for humans, rendered from the
  ledger (never parsed back).

Both are ORCHESTRATOR-written from orchestrator-measured numbers: the agent
editing either one is a scope violation that ends the run, and the publish
step overwrites them from trusted data only after the workspace-drift check
has passed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
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
    baseline: float  # first orchestrator-measured value; never changes
    best: float  # current best orchestrator-measured value
    best_run: str  # run id that set the best
    updated: str  # ISO date
    # seed the best was measured under (0 = none recorded / fixed pool):
    # with resampled pools, a bare scalar is not re-derivable — this plus
    # the eval's seed_env makes the ledger number reproducible
    run_seed: int = 0


def load_leader(workspace: Path) -> dict[str, LeaderEntry]:
    """The ledger from the target tree; tolerant of absence and corruption
    (a broken ledger must not block an improvement — it gets rewritten)."""
    path = workspace / LEADER_FILE
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.warning("unreadable %s; starting a fresh ledger", path)
        return {}
    entries: dict[str, LeaderEntry] = {}
    if isinstance(raw, dict):
        for name, item in raw.items():
            if not isinstance(item, dict):
                continue
            known = {k: v for k, v in item.items() if k in LeaderEntry.__dataclass_fields__}
            try:
                entries[name] = LeaderEntry(**known)
            except TypeError:
                log.warning("skipping malformed leader entry %r", name)
    return entries


def update_leader(
    entries: dict[str, LeaderEntry],
    benchmark: str,
    metric: str,
    direction: str,
    baseline: float,
    candidate: float,
    run_id: str,
    date: str,
    run_seed: int = 0,
) -> dict[str, LeaderEntry]:
    """A new ledger with this run's improvement folded in. The baseline is
    pinned by the FIRST entry and never moves; best follows improvements."""
    existing = entries.get(benchmark)
    pinned_baseline = existing.baseline if existing is not None else baseline
    # Direction-aware: a run improved vs ITS OWN baseline can still be worse
    # than the recorded best (stale clone, eval noise) — best never regresses.
    if existing is not None:
        beats = candidate > existing.best if direction == "max" else candidate < existing.best
        if not beats:
            return dict(entries)
    updated = dict(entries)
    updated[benchmark] = LeaderEntry(
        benchmark=benchmark,
        metric=metric,
        direction=direction,
        baseline=pinned_baseline,
        best=candidate,
        best_run=run_id,
        updated=date,
        run_seed=run_seed,
    )
    return updated


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
        "eval command — never taken from an agent's claim — and updated as",
        "part of the pull request that achieved it.",
        "",
        "| benchmark | metric | baseline | best | progress | last improved | by run |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name in sorted(entries):
        e = entries[name]
        arrow = "↓" if e.direction == "min" else "↑"
        d = (digits or {}).get(e.benchmark)
        lines.append(
            f"| {e.benchmark} | `{e.metric}` {arrow} | {fmt_metric(e.baseline, d)} | "
            f"{fmt_metric(e.best, d)} | {_delta(e)} | {e.updated} | `{e.best_run}` |"
        )
    lines += [
        "",
        "_Written by [autoresearch](https://github.com/agentic-learning-ai-lab/autoresearch);",
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
