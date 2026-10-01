"""Effective session/job limits: contract wishes clamped by our ceilings.

Contracts live in TARGET repos and are untrusted input (contract.py's
threat model). A target may therefore SHAPE the orchestrator's spend on it
— shorter sessions, tighter job walltimes — but must never be able to
raise it: every contract value is clamped into [floor, ceiling], and the
ceilings are code on the orchestrator side, not configuration a target
can reach. Validated operator author overrides may raise the session ceilings.
Absent values fall back to the defaults the pilot has run with all along.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
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
# floor = session floor + overhead + self-deadline margin: even at the
# floors, a session must fit inside its job with the ending's runway.
# Public: the tick's OUTERLOOP_MAX_JOB_MINUTES knob floors here too.
ATTEMPT_JOB_MINUTES_FLOOR = 40

# Public: the tick shrinks a capped job's session with the same floor the
# contract clamp uses.
SESSION_MINUTES_FLOOR = 10

_BOUNDS: dict[str, tuple[int, int, int]] = {
    "session_max_turns": (120, 10, 120),
    "session_minutes": (90, SESSION_MINUTES_FLOOR, 90),
    "attempt_job_minutes": (120, ATTEMPT_JOB_MINUTES_FLOOR, 120),
    "followup_job_minutes": (90, 20, 90),
}

# A climb job must outlive its session long enough for the orchestrator's
# own work around it (clone, two evals, publish, ending writes). Public:
# the tick's cap warning uses it as the no-runway threshold too.
ATTEMPT_OVERHEAD_MINUTES = 20

# Operator author overrides may raise sessions up to these, never further.
OVERRIDE_SESSION_MINUTES_CEILING = 240
OVERRIDE_SESSION_TURNS_CEILING = 300


@dataclass(frozen=True)
class EffectiveLimits:
    session_max_turns: int
    session_minutes: int
    attempt_job_minutes: int
    followup_job_minutes: int


def _clamp(name: str, value: int | None) -> int:
    default, floor, ceiling = _BOUNDS[name]
    if value is None:
        return default
    return max(floor, min(int(value), ceiling))


def effective_limits(
    budgets: Any = None, *, session_minutes: int | None = None, session_max_turns: int | None = None
) -> EffectiveLimits:
    """Resolve a contract's optional budget knobs into enforceable limits.

    `budgets` is the contract's Budgets model (or None for pure defaults);
    unknown/absent attributes read as None. Optional session ceilings come only
    from validated operator overrides (or their persisted binding), never the
    contract. The session is finally shrunk
    to fit inside the climb job with room for the orchestrator's overhead —
    a session that outlives its job ends as a kill, not a report.
    """
    values = {
        name: _clamp(name, getattr(budgets, name, None) if budgets is not None else None)
        for name in _BOUNDS
    }
    for name, override in (
        ("session_minutes", session_minutes),
        ("session_max_turns", session_max_turns),
    ):
        if override is not None:
            requested = getattr(budgets, name, None)
            values[name] = max(
                _BOUNDS[name][1], min(requested, override) if requested is not None else override
            )
    if session_minutes is not None:
        wanted = values["session_minutes"] + ATTEMPT_OVERHEAD_MINUTES
        requested_job = getattr(budgets, "attempt_job_minutes", None)
        values["attempt_job_minutes"] = (
            min(wanted, max(ATTEMPT_JOB_MINUTES_FLOOR, requested_job))
            if requested_job is not None
            else wanted
        )
    max_session = values["attempt_job_minutes"] - ATTEMPT_OVERHEAD_MINUTES
    if values["session_minutes"] > max_session:
        floor = _BOUNDS["session_minutes"][1]
        values["session_minutes"] = max(floor, max_session)
    return EffectiveLimits(**values)


def capped_limits(limits: EffectiveLimits, job_minutes: int) -> EffectiveLimits:
    """Shrink the author session to leave overhead inside the actual job."""
    return replace(
        limits,
        session_minutes=max(
            SESSION_MINUTES_FLOOR,
            min(limits.session_minutes, job_minutes - ATTEMPT_OVERHEAD_MINUTES),
        ),
    )


def clamp_bound_limits(limits: EffectiveLimits, budgets: Any) -> EffectiveLimits:
    """Apply a newly loaded trusted contract without raising a bound budget."""
    values = asdict(limits)
    for name in values:
        requested = getattr(budgets, name, None)
        if requested is not None:
            values[name] = min(values[name], max(_BOUNDS[name][1], requested))
    if values["session_minutes"] < limits.session_minutes:
        values["attempt_job_minutes"] = min(
            values["attempt_job_minutes"], values["session_minutes"] + ATTEMPT_OVERHEAD_MINUTES
        )
    return capped_limits(EffectiveLimits(**values), values["attempt_job_minutes"])


def bound_limits(value: Any) -> EffectiveLimits | None:
    """A run's persisted limits, or None (today's defaults) when absent or unreadable."""
    if not isinstance(value, dict):
        return None
    values: dict[str, int] = {}
    for name in _BOUNDS:
        stored = value.get(name)
        if not isinstance(stored, int) or isinstance(stored, bool):
            return None  # missing, bool, float or string: never coerced
        values[name] = stored
    ceilings = {
        **{name: ceiling for name, (_, _, ceiling) in _BOUNDS.items()},
        "session_minutes": OVERRIDE_SESSION_MINUTES_CEILING,
        "session_max_turns": OVERRIDE_SESSION_TURNS_CEILING,
        "attempt_job_minutes": OVERRIDE_SESSION_MINUTES_CEILING + ATTEMPT_OVERHEAD_MINUTES,
    }
    for name, (_, floor, _) in _BOUNDS.items():
        values[name] = max(floor, min(values[name], ceilings[name]))
    return EffectiveLimits(**values)
