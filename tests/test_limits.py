"""Contract budget wishes clamped by orchestrator ceilings."""

from __future__ import annotations

from autoresearch.contract import load_contract
from autoresearch.limits import EffectiveLimits, effective_limits

BASE = """
benchmarks:
  - {name: tsp, command: c, metric: m, direction: min}
budgets: {gpu_hours_per_run: 1, runs_per_week: 3%s}
scope: {allowed: [src/]}
roadmap: docs/roadmap.md
"""


def _limits(budget_extra: str = "") -> EffectiveLimits:
    return effective_limits(load_contract(BASE % budget_extra, "org/pilot").budgets)


def test_absent_knobs_yield_the_standing_defaults() -> None:
    assert _limits() == EffectiveLimits(
        session_max_turns=60,
        session_minutes=60,
        climb_job_minutes=90,
        followup_job_minutes=60,
    )
    assert effective_limits(None) == _limits()  # no contract at all


def test_contract_shapes_spend_downward() -> None:
    lim = _limits(", session_max_turns: 20, session_minutes: 15, climb_job_minutes: 40")
    assert lim.session_max_turns == 20
    assert lim.session_minutes == 15
    assert lim.climb_job_minutes == 40


def test_ceilings_cannot_be_raised_by_a_contract() -> None:
    """The security property: contracts are merged by TARGET maintainers,
    so shaping is strictly downward — asking for more yields the default,
    on every knob."""
    lim = _limits(
        ", session_max_turns: 100000, session_minutes: 100000"
        ", climb_job_minutes: 100000, followup_job_minutes: 100000"
    )
    assert lim == effective_limits(None)  # identical to no knobs at all


def test_floors_defeat_starvation() -> None:
    lim = _limits(", session_max_turns: 1, session_minutes: 1, climb_job_minutes: 1")
    assert lim.session_max_turns == 10
    assert lim.session_minutes == 10
    # floor 40 = session floor (10) + orchestrator overhead (20) + runway:
    # even the floor combination keeps the session inside the job
    assert lim.climb_job_minutes == 40
    assert lim.session_minutes <= lim.climb_job_minutes - 20


def test_session_is_shrunk_to_fit_inside_the_job() -> None:
    """A session that outlives its job ends as a kill, not a report."""
    lim = _limits(", session_minutes: 60, climb_job_minutes: 60")
    assert lim.climb_job_minutes == 60
    assert lim.session_minutes == 40  # 60 - 20 overhead


def test_contract_rejects_nonpositive_knobs() -> None:
    import pytest

    # pydantic surfaces schema violations as ValueError subclasses
    with pytest.raises(ValueError):
        load_contract(BASE % ", session_minutes: 0", "org/pilot")
