"""`measure_and_decide`: the post-session decision as a pure function of
committed shas and a Measurer. Exercised here with a fake measurer — no
session, no git, no cluster."""

from __future__ import annotations

from pathlib import Path

import pytest

from outerloop.contract import load_contract
from outerloop.harness import SessionResult
from outerloop.measure import Measure, MeasurementPending
from outerloop.orchestrator import (
    EvalError,
    MeasureOK,
    RunParked,
    _benchmark,
    measure_and_decide,
    resume_attempt,
)

# main benchmark + one sibling; a `shared` scope path drives the suite gate.
CONTRACT = """
benchmarks:
  - name: main
    command: run-main
    metric: r2
    direction: max
    seed_env: MAIN_SEED
  - name: sib
    command: run-sib
    metric: succ
    direction: max
    seed_env: SIB_SEED
    min_delta: 0.02
suite:
  metric: mean
  direction: max
budgets:
  gpu_hours_per_run: 0
  runs_per_week: 5
scope:
  allowed: [src/]
  shared: [src/shared/]
roadmap: README.md
"""

BASE = "a" * 40
CAND = "b" * 40


class FakeMeasurer:
    """Returns canned values keyed by measure name; can instead raise. `seen`
    accumulates every measure across ALL calls (the decision measures in two
    phases: baseline+candidate, then — only if credited — siblings)."""

    def __init__(
        self,
        values: dict[str, float] | None = None,
        raise_exc: Exception | None = None,
        raise_on_call: int | None = None,
    ):
        self.values = values or {}
        self.raise_exc = raise_exc
        self.raise_on_call = raise_on_call  # 1-indexed; None = raise on every call
        self.seen: list[Measure] = []
        self.per_call: list[list[str]] = []  # measure names, one entry per call
        self.calls = 0
        # the cached-baseline seam measure_and_decide reads off a measurer
        self.baseline_cache: Path | None = None
        self.run_tag = ""

    def results(self, measures: list[Measure]) -> dict[str, float]:
        self.calls += 1
        self.seen.extend(measures)
        self.per_call.append([m.name for m in measures])
        if self.raise_exc is not None and self.raise_on_call in (None, self.calls):
            raise self.raise_exc
        return {m.name: self.values[m.name] for m in measures}


# same contract with NO shared scope: the suite gate can never trigger.
CONTRACT_NO_SHARED = CONTRACT.replace("  shared: [src/shared/]\n", "")


def _decide(measurer, measured_paths=("src/model.py",), seed=7, suite_seed=99, text=CONTRACT):
    contract = load_contract(text, "x/y")
    return measure_and_decide(
        contract,
        _benchmark(contract, "main"),
        base_sha=BASE,
        candidate_sha=CAND,
        seed=seed,
        suite_seed=suite_seed,
        measured_paths=measured_paths,
        measurer=measurer,
        min_relative_improvement=0.005,
    )


def test_improved_no_shared_touch_returns_measure_ok():
    out = _decide(FakeMeasurer({"baseline": 0.50, "candidate": 0.60}))
    assert isinstance(out, MeasureOK)
    assert out.baseline == 0.50 and out.candidate == 0.60
    assert out.suite == () and out.suite_seed == 0  # sibling not touched -> no gate


def test_no_improvement_is_terminal():
    out = _decide(FakeMeasurer({"baseline": 0.50, "candidate": 0.50}))
    assert not isinstance(out, MeasureOK)
    assert out.outcome == "no-improvement"
    assert out.baseline == 0.50 and out.candidate == 0.50 and out.run_seed == 7


def test_non_improving_shared_path_candidate_never_measures_siblings():
    # the lazy-order guarantee: a candidate that fails the main threshold must
    # NOT burn any sibling eval, even when its diff touched shared code.
    m = FakeMeasurer({"baseline": 0.50, "candidate": 0.50})
    out = _decide(m, measured_paths=("src/shared/util.py",))
    assert not isinstance(out, MeasureOK) and out.outcome == "no-improvement"
    assert [mm.name for mm in m.seen] == ["baseline", "candidate"]  # no sib-* measured
    assert m.calls == 1  # phase 2 never ran


def test_out_of_scope_is_terminal_before_any_measurement():
    m = FakeMeasurer({"baseline": 0.50, "candidate": 0.60})
    out = _decide(m, measured_paths=("docs/secret.md",))
    assert not isinstance(out, MeasureOK)
    assert out.outcome == "scope-violation"
    assert m.seen == []  # never measured an out-of-scope tree


def test_eval_error_becomes_terminal_eval_error():
    out = _decide(FakeMeasurer(raise_exc=EvalError("candidate: no readable r2")))
    assert not isinstance(out, MeasureOK)
    assert out.outcome == "eval-error"
    assert "no readable r2" in out.note


def test_measurement_pending_propagates_for_the_caller_to_park():
    pending = MeasurementPending(("101", "102"))
    with pytest.raises(MeasurementPending) as exc:
        _decide(FakeMeasurer(raise_exc=pending))
    assert exc.value.afterany() == "afterany:101:102"


def test_shared_touch_runs_suite_gate_clean_pass():
    m = FakeMeasurer(
        {
            "baseline": 0.50,
            "candidate": 0.60,
            "sib-sib-base": 0.80,
            "sib-sib-cand": 0.80,  # unchanged sibling -> no regression
        }
    )
    out = _decide(m, measured_paths=("src/shared/util.py",))
    assert isinstance(out, MeasureOK)
    assert out.suite_seed == 99
    assert len(out.suite) == 1 and out.suite[0].name == "sib" and not out.suite[0].regressed


def test_shared_touch_sibling_regression_is_terminal():
    m = FakeMeasurer(
        {
            "baseline": 0.50,
            "candidate": 0.60,
            "sib-sib-base": 0.80,
            "sib-sib-cand": 0.70,  # dropped beyond min_delta 0.02 -> regressed
        }
    )
    out = _decide(m, measured_paths=("src/shared/util.py",))
    assert not isinstance(out, MeasureOK)
    assert out.outcome == "suite-regression"
    assert "sib" in out.note and out.suite_seed == 99


def test_suite_uses_one_suite_seed_via_each_siblings_own_var():
    m = FakeMeasurer(
        {
            "baseline": 0.50,
            "candidate": 0.60,
            "sib-sib-base": 0.80,
            "sib-sib-cand": 0.81,
        }
    )
    _decide(m, measured_paths=("src/shared/util.py",), seed=7, suite_seed=99)
    assert m.calls == 2  # phase 1 (baseline+candidate), then phase 2 (siblings)
    by_name = {mm.name: mm for mm in m.seen}
    # main pair carries the climb seed on the main var; sibling pair carries
    # the ONE suite_seed on the sibling's OWN var (never the main var).
    assert by_name["baseline"].env() == {"MAIN_SEED": "7"}
    assert by_name["candidate"].env() == {"MAIN_SEED": "7"}
    assert by_name["sib-sib-base"].env() == {"SIB_SEED": "99"}
    assert by_name["sib-sib-cand"].env() == {"SIB_SEED": "99"}


def test_no_shared_touch_does_not_dispatch_siblings():
    m = FakeMeasurer({"baseline": 0.50, "candidate": 0.60})
    _decide(m, measured_paths=("src/model.py",))
    assert [mm.name for mm in m.seen] == ["baseline", "candidate"]  # no sib-* measures
    assert m.calls == 1  # env-specific diff: phase 2 never ran


def test_phase_two_eval_error_is_terminal():
    # a sibling EvalError (raised only in the phase-2 call) must still surface
    # as eval-error — the phase-2 try/except is load-bearing.
    m = FakeMeasurer(
        {"baseline": 0.50, "candidate": 0.60},
        raise_exc=EvalError("sib-sib-base: no readable succ"),
        raise_on_call=2,
    )
    out = _decide(m, measured_paths=("src/shared/util.py",))
    assert m.calls == 2 and not isinstance(out, MeasureOK)
    assert out.outcome == "eval-error" and "succ" in out.note
    # the credited main pair (measured in phase 1) survives into the report
    assert out.baseline == 0.50 and out.candidate == 0.60


def test_phase_two_measures_only_siblings_never_the_main_pair():
    # pins the phase-2 filter: the second call must carry only sib-* measures,
    # never a re-measure of baseline/candidate.
    m = FakeMeasurer(
        {"baseline": 0.50, "candidate": 0.60, "sib-sib-base": 0.8, "sib-sib-cand": 0.81}
    )
    _decide(m, measured_paths=("src/shared/util.py",))
    assert m.per_call[0] == ["baseline", "candidate"]
    assert m.per_call[1] == ["sib-sib-base", "sib-sib-cand"]


def test_phase_two_measurement_pending_propagates():
    # the siblings' own park (a second afterany set) must reach the caller.
    m = FakeMeasurer(
        {"baseline": 0.50, "candidate": 0.60},
        raise_exc=MeasurementPending(("201", "202")),
        raise_on_call=2,
    )
    with pytest.raises(MeasurementPending) as exc:
        _decide(m, measured_paths=("src/shared/util.py",))
    assert exc.value.afterany() == "afterany:201:202"


def test_zero_seed_for_seeded_benchmark_fails_loud():
    # `main` declares seed_env MAIN_SEED; a 0 seed would run it unpaired.
    with pytest.raises(ValueError, match="seed"):
        _decide(FakeMeasurer({"baseline": 0.5, "candidate": 0.6}), seed=0)


def test_zero_suite_seed_for_seeded_sibling_fails_loud():
    # `sib` declares SIB_SEED, so suite_seed 0 is a caller-contract violation —
    # caught UP FRONT, before any measurement, regardless of the diff.
    m = FakeMeasurer({"baseline": 0.50, "candidate": 0.60})
    with pytest.raises(ValueError, match="suite_seed"):
        _decide(m, measured_paths=("src/shared/util.py",), suite_seed=0)
    assert m.calls == 0  # never measured


def test_empty_shared_scope_does_not_require_suite_seed():
    # a seeded sibling with NO shared scope can never trigger the gate, so a
    # 0 suite_seed is not a misconfiguration — measure, don't over-reject.
    m = FakeMeasurer({"baseline": 0.50, "candidate": 0.60})
    out = _decide(m, measured_paths=("src/model.py",), suite_seed=0, text=CONTRACT_NO_SHARED)
    assert isinstance(out, MeasureOK)


# --- resume_attempt: the wake side, re-entering the decision without a session ---

REPORT = "swapped the construction heuristic; tours shortened."


def _woke_session():
    return SessionResult(
        stop_reason="resumed",
        is_error=False,
        cost_usd=0.0,
        num_turns=0,
        session_id="s-woke",
        final_text=REPORT,
        transcript_path="",
    )


def _resume(measurer, measured_paths=("src/model.py",), seed=7, suite_seed=99, text=CONTRACT):
    contract = load_contract(text, "x/y")
    return resume_attempt(
        contract,
        _benchmark(contract, "main"),
        base_sha=BASE,
        candidate_sha=CAND,
        seed=seed,
        suite_seed=suite_seed,
        measured_paths=measured_paths,
        session=_woke_session(),
        measurer=measurer,
        min_relative_improvement=0.005,
    )


def test_resume_improved_carries_the_saved_report():
    out = _resume(FakeMeasurer({"baseline": 0.50, "candidate": 0.60}))
    assert out.outcome == "improved"
    assert out.baseline == 0.50 and out.candidate == 0.60
    # the session never re-runs: it is rebuilt from the saved write-up + its id
    assert out.session is not None
    assert out.session.final_text == REPORT
    assert out.session.session_id == "s-woke"
    assert out.session.num_turns == 0  # a wake ran no turns
    assert out.measured_paths == ("src/model.py",) and out.run_seed == 7
    # the wake path carries the sealed candidate sha too (so best-of-k can select
    # and publish it), matching the in-job improved return
    assert out.candidate_sha == CAND


def test_resume_no_improvement_is_terminal_with_the_report():
    out = _resume(FakeMeasurer({"baseline": 0.50, "candidate": 0.50}))
    assert out.outcome == "no-improvement"
    assert out.session is not None and out.session.final_text == REPORT
    # the wake gives a bare negative the same framing the in-job path sets,
    # so a resumed negative never ends note-less
    assert out.note == "a negative result reported clearly is a success"


def test_resume_reparks_when_a_measure_is_not_done():
    # a measure this wake needs isn't finished (its eval still queued) -> re-park
    # on the new afterany set, same shape as the first candidate park, carrying
    # the reconstructed session so a later wake still has the write-up.
    m = FakeMeasurer(raise_exc=MeasurementPending(("501", "502")))
    with pytest.raises(RunParked) as excinfo:
        _resume(m)
    parked = excinfo.value
    assert parked.phase == "candidate"
    assert parked.candidate_sha == CAND
    assert parked.afterany == "afterany:501:502"
    assert parked.seed == 7 and parked.suite_seed == 99
    assert parked.session is not None and parked.session.final_text == REPORT


def test_resume_eval_error_is_terminal():
    out = _resume(FakeMeasurer(raise_exc=EvalError("candidate: no readable r2")))
    assert out.outcome == "eval-error"
    assert "no readable r2" in out.note


CACHED_CONTRACT = """
benchmarks:
  - name: main
    command: run main
    metric: score
    direction: max
    baseline: cached
    min_delta: 0.02
budgets: {gpu_hours_per_run: 1, runs_per_week: 5}
scope:
  allowed: [src/]
roadmap: docs/roadmap.md
"""


def test_cached_baseline_measures_the_base_once_then_only_candidates(tmp_path):
    """`baseline: cached`: the first attempt on a base measures both and
    records the baseline; the next attempt on the same base measures ONLY its
    candidate and compares against the cache, saying so on the credited
    result. A different base misses the cache."""
    from outerloop.measure import read_baseline_cache

    contract = load_contract(CACHED_CONTRACT, "x/y")
    bench = _benchmark(contract, "main")

    def decide(m, base=BASE, seed=7):
        return measure_and_decide(
            contract,
            bench,
            base_sha=base,
            candidate_sha=CAND,
            seed=seed,
            suite_seed=0,
            measured_paths=("src/model.py",),
            measurer=m,
            min_relative_improvement=0.005,
        )

    first = FakeMeasurer({"baseline": 0.50, "candidate": 0.60})
    first.baseline_cache = tmp_path / "baselines"
    first.run_tag = "run-1"
    out = decide(first)
    assert isinstance(out, MeasureOK) and out.baseline == 0.50 and out.baseline_note == ""
    assert first.per_call == [["baseline", "candidate"]]
    entry = read_baseline_cache(
        tmp_path / "baselines", "main", BASE, command="run main", metric="score"
    )
    assert entry and entry["value"] == 0.50 and entry["seed"] == 7 and entry["run"] == "run-1"

    second = FakeMeasurer({"candidate": 0.61})  # no baseline value: it must not be asked for
    second.baseline_cache = tmp_path / "baselines"
    second.run_tag = "run-2"
    out2 = decide(second, seed=8)
    assert isinstance(out2, MeasureOK) and out2.baseline == 0.50 and out2.candidate == 0.61
    assert second.per_call == [["candidate"]]
    assert "reused from the cache" in out2.baseline_note and "seed 7" in out2.baseline_note

    other = FakeMeasurer({"baseline": 0.55, "candidate": 0.58})
    other.baseline_cache = tmp_path / "baselines"
    other.run_tag = "run-3"
    decide(other, base="c" * 40)
    assert other.per_call == [["baseline", "candidate"]]  # a new base: measured again


def test_paired_default_never_touches_the_cache(tmp_path):
    m = FakeMeasurer({"baseline": 0.50, "candidate": 0.60})
    m.baseline_cache = tmp_path / "baselines"
    out = _decide(m)
    assert isinstance(out, MeasureOK) and out.baseline_note == ""
    assert not (tmp_path / "baselines").exists()


def test_cached_baseline_requires_a_floor():
    with pytest.raises(ValueError, match="floor"):
        load_contract(CACHED_CONTRACT.replace("    min_delta: 0.02\n", ""), "x/y")


def test_cached_baseline_is_keyed_by_image_and_command(tmp_path):
    """A cache entry measured under another eval image or contract command
    is stale, not a baseline (terra #178): the gate misses and re-measures."""
    from outerloop.measure import read_baseline_cache, write_baseline_cache

    d = tmp_path / "baselines"
    write_baseline_cache(
        d, "main", BASE, value=0.5, seed=3, run_tag="r", image="/a.sif", command="run main"
    )
    assert read_baseline_cache(d, "main", BASE, image="/a.sif", command="run main")
    assert read_baseline_cache(d, "main", BASE, image="/b.sif", command="run main") is None
    assert read_baseline_cache(d, "main", BASE, image="/a.sif", command="run other") is None
    assert (
        read_baseline_cache(d, "main", BASE, image="/a.sif", command="run main", metric="x") is None
    )
    # two concurrent writers of the same entry: unique tmp files, last wins, no crash
    write_baseline_cache(
        d, "main", BASE, value=0.6, seed=4, run_tag="r", image="/a.sif", command="run main"
    )
    got = read_baseline_cache(d, "main", BASE, image="/a.sif", command="run main")
    assert got and got["value"] == 0.6 and not list(d.glob("*.tmp"))
