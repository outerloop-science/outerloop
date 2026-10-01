"""Contract budget wishes clamped by orchestrator ceilings."""

from __future__ import annotations

from outerloop.contract import load_contract
from outerloop.limits import EffectiveLimits, effective_limits

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
        session_max_turns=120,
        session_minutes=90,
        attempt_job_minutes=120,
        followup_job_minutes=90,
    )
    assert effective_limits(None) == _limits()  # no contract at all


def test_contract_shapes_spend_downward() -> None:
    lim = _limits(", session_max_turns: 20, session_minutes: 15, attempt_job_minutes: 40")
    assert lim.session_max_turns == 20
    assert lim.session_minutes == 15
    assert lim.attempt_job_minutes == 40


def test_ceilings_cannot_be_raised_by_a_contract() -> None:
    """The security property: contracts are merged by TARGET maintainers,
    so shaping is strictly downward — asking for more yields the default,
    on every knob."""
    lim = _limits(
        ", session_max_turns: 100000, session_minutes: 100000"
        ", attempt_job_minutes: 100000, followup_job_minutes: 100000"
    )
    assert lim == effective_limits(None)  # identical to no knobs at all


def test_floors_defeat_starvation() -> None:
    lim = _limits(", session_max_turns: 1, session_minutes: 1, attempt_job_minutes: 1")
    assert lim.session_max_turns == 10
    assert lim.session_minutes == 10
    # floor 40 = session floor (10) + orchestrator overhead (20) + runway:
    # even the floor combination keeps the session inside the job
    assert lim.attempt_job_minutes == 40
    assert lim.session_minutes <= lim.attempt_job_minutes - 20


def test_session_is_shrunk_to_fit_inside_the_job() -> None:
    """A session that outlives its job ends as a kill, not a report."""
    lim = _limits(", session_minutes: 60, attempt_job_minutes: 60")
    assert lim.attempt_job_minutes == 60
    assert lim.session_minutes == 40  # 60 - 20 overhead


def test_contract_rejects_nonpositive_knobs() -> None:
    import pytest

    # pydantic surfaces schema violations as ValueError subclasses
    with pytest.raises(ValueError):
        load_contract(BASE % ", session_minutes: 0", "org/pilot")


def test_operator_session_limits_and_contract_clamp():
    from types import SimpleNamespace

    assert effective_limits(session_minutes=240, session_max_turns=300) == EffectiveLimits(
        300, 240, 260, 90
    )
    for requested, minutes, turns in [
        (None, 180, 250),
        (150, 150, 150),
        (999, 180, 250),
        (1, 10, 10),
    ]:
        limits = effective_limits(
            SimpleNamespace(session_minutes=requested, session_max_turns=requested),
            session_minutes=180,
            session_max_turns=250,
        )
        assert limits == EffectiveLimits(turns, minutes, minutes + 20, 90)
    assert effective_limits(session_max_turns=300) == EffectiveLimits(300, 90, 120, 90)
    assert effective_limits(session_minutes=180) == EffectiveLimits(120, 180, 200, 90)


def test_contract_job_budget_still_lowers_an_overridden_session():
    from types import SimpleNamespace

    limits = effective_limits(SimpleNamespace(attempt_job_minutes=60), session_minutes=180)
    assert limits.session_minutes == 40
    assert limits.attempt_job_minutes == 60
    for requested, expected in [(150, 150), (999, 200)]:
        limits = effective_limits(
            SimpleNamespace(attempt_job_minutes=requested), session_minutes=180
        )
        assert limits.attempt_job_minutes == expected
        assert limits.session_minutes == expected - 20


def test_contract_discovered_after_binding_cannot_raise_limits():
    from types import SimpleNamespace

    from outerloop.limits import clamp_bound_limits

    for bound in [effective_limits(session_max_turns=300), effective_limits(session_minutes=180)]:
        assert clamp_bound_limits(bound, None) == bound
        assert (
            clamp_bound_limits(bound, SimpleNamespace(session_minutes=999, session_max_turns=999))
            == bound
        )
    bound = effective_limits(session_minutes=180, session_max_turns=250)
    assert clamp_bound_limits(
        bound, SimpleNamespace(session_minutes=60, session_max_turns=30)
    ) == EffectiveLimits(30, 60, 80, 90)
    assert clamp_bound_limits(bound, SimpleNamespace(attempt_job_minutes=60)) == EffectiveLimits(
        250, 40, 60, 90
    )


def test_bound_limits_tolerates_damaged_records():
    from outerloop.limits import bound_limits, effective_limits

    assert bound_limits(None) is None
    assert bound_limits({"session_minutes": 180}) is None
    assert bound_limits("180") is None
    base = effective_limits().__dict__
    for bad in ("250", 180.9, 1e10000, True):
        assert bound_limits({**base, "session_minutes": bad}) is None
    full = {**effective_limits().__dict__, "session_minutes": 180, "future_knob": 1}
    kept, floored = bound_limits(full), bound_limits({**full, "session_max_turns": 0})
    assert kept is not None and kept.session_minutes == 180
    assert floored is not None and floored.session_max_turns == 10


def test_bound_limits_caps_at_the_override_ceilings():
    from outerloop.limits import bound_limits, effective_limits

    huge = {**effective_limits().__dict__, "session_minutes": 10**9, "session_max_turns": 10**9}
    huge["attempt_job_minutes"] = 10**9
    capped = bound_limits(huge)
    assert capped is not None
    assert (capped.session_minutes, capped.session_max_turns, capped.attempt_job_minutes) == (
        240,
        300,
        260,
    )
