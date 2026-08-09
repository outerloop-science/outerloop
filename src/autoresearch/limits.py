"""Effective session/job limits: contract wishes clamped by our ceilings.

Contracts live in TARGET repos and are untrusted input (contract.py's
threat model). A target may therefore SHAPE the orchestrator's spend on it
— shorter sessions, tighter job walltimes — but must never be able to
raise it: every contract value is clamped into [floor, ceiling], and the
ceilings are code on the orchestrator side, not configuration a target
can reach. Absent values fall back to the defaults the pilot has run with
all along.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# (default, floor, ceiling) per knob. Floors keep a hostile-or-typo'd
# contract from starving runs into uselessness (a 1-turn session still
# spends money and reports nothing). CEILING == DEFAULT, deliberately:
# contracts are merged by TARGET-repo maintainers, not by us, so any
# ceiling above the default would let them raise our spend — the knobs
# shape strictly downward. Raising a target's budget is an
# orchestrator-side decision (config we control), not a contract edit.
# Raised from 60/60/90/60 on 2026-08-09 (maintainer decision): the first
# steward work order to BUILD an env burned its full 60-turn budget mid-
# work — session budgets sized for solver tweaks starve construction work.
_BOUNDS: dict[str, tuple[int, int, int]] = {
    "session_max_turns": (120, 10, 120),
    "session_minutes": (90, 10, 90),
    # floor = session floor + overhead + self-deadline margin: even at the
    # floors, a session must fit inside its job with the ending's runway
    "climb_job_minutes": (120, 40, 120),
    "followup_job_minutes": (90, 20, 90),
}

# A climb job must outlive its session long enough for the orchestrator's
# own work around it (clone, two evals, publish, ending writes).
_CLIMB_OVERHEAD_MINUTES = 20


@dataclass(frozen=True)
class EffectiveLimits:
    session_max_turns: int
    session_minutes: int
    climb_job_minutes: int
    followup_job_minutes: int


def _clamp(name: str, value: int | None) -> int:
    default, floor, ceiling = _BOUNDS[name]
    if value is None:
        return default
    return max(floor, min(int(value), ceiling))


def effective_limits(budgets: Any = None) -> EffectiveLimits:
    """Resolve a contract's optional budget knobs into enforceable limits.

    `budgets` is the contract's Budgets model (or None for pure defaults);
    unknown/absent attributes read as None. The session is finally shrunk
    to fit inside the climb job with room for the orchestrator's overhead —
    a session that outlives its job ends as a kill, not a report.
    """
    values = {
        name: _clamp(name, getattr(budgets, name, None) if budgets is not None else None)
        for name in _BOUNDS
    }
    max_session = values["climb_job_minutes"] - _CLIMB_OVERHEAD_MINUTES
    if values["session_minutes"] > max_session:
        floor = _BOUNDS["session_minutes"][1]
        values["session_minutes"] = max(floor, max_session)
    return EffectiveLimits(**values)
