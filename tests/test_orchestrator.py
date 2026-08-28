"""The single-benchmark climb, against fakes for every seam."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from autoresearch.harness import FakeHarness, SessionResult
from autoresearch.orchestrator import (
    EvalError,
    RunConfig,
    _metric_from_output,
    attempt_once,
    improved,
    pr_body,
)

CONTRACT = """
benchmarks:
  - name: tsp
    command: uv run python -m pilot.eval --benchmark tsp --json
    metric: mean_tour_length
    direction: min
  - name: sokoban
    command: uv run python -m pilot.eval --benchmark sokoban --json
    metric: solve_rate
    direction: max
budgets: {gpu_hours_per_run: 1, runs_per_week: 10}
scope: {allowed: [src/pilot/solvers/]}
roadmap: docs/roadmap.md
"""

CONFIG = RunConfig(target="org/pilot", benchmark="tsp")


def ok_session(text: str = "Report: replaced NN with NN+2-opt.") -> SessionResult:
    return SessionResult(
        stop_reason="end_turn",
        is_error=False,
        cost_usd=1.25,
        num_turns=20,
        session_id="s1",
        final_text=text,
        transcript_path="",
    )


@dataclass
class FakeEvaluator:
    """Returns queued metric values; records calls."""

    values: list[float] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    seen_env: list = field(default_factory=list)
    seen_ws: list = field(default_factory=list)

    def evaluate(self, workspace: Path, command: str, metric: str, extra_env=None) -> float:
        self.calls.append((command, metric))
        self.seen_env.append(dict(extra_env) if extra_env else None)
        self.seen_ws.append(workspace)
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _wire(evaluator, workspace, base_ws=None):
    """An inline measurer over the fake evaluator + a fake snapshot, mirroring
    the real wiring's semantics minus git: each measure is evaluated once per
    identity (sha + contract facts) and cached — like the one measurer's slot
    cache — so call-count assertions see one eval per distinct measure."""

    class _InlineMeasurer:
        def __init__(self) -> None:
            self._cache: dict = {}

        def results(self, measures):
            from autoresearch.orchestrator import EvalError as _EvalError

            out = {}
            for m in measures:
                key = (m.name, m.tree_sha, m.command, m.metric, m.extra_env)
                if key not in self._cache:
                    ws = base_ws if (base_ws is not None and m.tree_sha == "base") else workspace
                    try:
                        self._cache[key] = evaluator.evaluate(
                            ws, m.command, m.metric, extra_env=m.env() or None
                        )
                    except _EvalError as exc:
                        # both real backends name the failing measure
                        raise _EvalError(f"{m.name}: {exc}") from exc
                out[m.name] = self._cache[key]
            return out

    counter = {"n": 0}

    def snapshot() -> str:
        counter["n"] += 1
        return f"cand{counter['n']}"

    return _InlineMeasurer(), snapshot


def run_climb(
    tmp_path, values, session=None, config=CONFIG, changed=None, harness=None, contract=None, **kw
):
    harness = harness or FakeHarness(result=session or ok_session())
    evaluator = FakeEvaluator(values=list(values))
    measurer, snapshot = _wire(evaluator, tmp_path)
    # `created` is a brief-only input, so a resume rejects it; a fresh run gets the
    # usual stamp. (Callers can still override via kw.)
    if "created" not in kw and not kw.get("resume_session_id"):
        kw["created"] = "2026-08-06T00:00:00Z"
    result = attempt_once(
        config,
        contract or CONTRACT,
        tmp_path,
        harness,
        measurer,
        "base",
        snapshot,
        ruler="mean tour length over the frozen pool",
        changed_paths=lambda: changed if changed is not None else ["src/pilot/solvers/tsp.py"],
        **kw,
    )
    return result, harness, evaluator


def test_improvement_end_to_end(tmp_path: Path) -> None:
    # brief_baseline is the ledger's last-known score, for the brief; the gate
    # re-measures both sides ([13.876, 13.10]) after the session.
    result, harness, evaluator = run_climb(tmp_path, [13.876, 13.10], brief_baseline=13.876)
    assert result.outcome == "improved"
    assert result.baseline == 13.876  # measured by the gate
    assert result.candidate == 13.10
    assert result.branch == "feat/auto/agent-01/tsp"
    # the brief carried the ledger baseline and the contract verbatim
    brief_text = harness.calls[0][0]
    assert "13.876" in brief_text
    # the score is stated as a FACT (currently X), never as a target to hit,
    # and the finish is the author's call — the de-prescriptified brief
    assert "currently 13.876" in brief_text
    assert "lower is better" in brief_text  # metric direction as context
    assert "You decide when your result is worth publishing" in brief_text
    assert "src/pilot/solvers/" in brief_text
    # eval ran twice (baseline + candidate) with the contract's command
    assert (
        evaluator.calls
        == [("uv run python -m pilot.eval --benchmark tsp --json", "mean_tour_length")] * 2
    )


def test_improved_result_exposes_the_candidate_sha(tmp_path: Path) -> None:
    # Phase 2a primitive: an improved result carries the sealed candidate sha the
    # caller publishes (and a depth loop selects the best across passes by).
    result, _, _ = run_climb(tmp_path, [13.876, 13.10])
    assert result.outcome == "improved" and result.candidate_sha == "cand1"


def test_attempt_once_resume_entry_skips_the_brief(tmp_path: Path) -> None:
    # Phase 2a primitive: a cumulative depth pass resumes the prior session with
    # the improve prompt instead of a fresh brief (the depth loop, part 2, drives
    # this; here we verify the entry point alone).
    result, harness, _ = run_climb(
        tmp_path, [13.876, 13.10], resume_session_id="prev-sess", improve_prompt="beat 13.876"
    )
    assert result.outcome == "improved"
    brief_text, _ws, resumed = harness.calls[0]  # the FIRST call is the resume
    # the exact-equality proves no fresh brief was rendered: a brief would carry
    # the task/contract preamble, never equal the bare improve prompt.
    assert brief_text == "beat 13.876" and resumed == "prev-sess"


def test_resume_entry_couples_session_and_prompt(tmp_path: Path) -> None:
    # resume-entry is a coupled pair (both or neither): a lone session id burns a
    # promptless turn; a lone improve_prompt would be silently discarded by the
    # fresh-brief branch (a depth pass becoming a fresh attempt). Reject both.
    with pytest.raises(ValueError, match="together"):
        run_climb(tmp_path, [13.876, 13.10], resume_session_id="prev-sess")
    with pytest.raises(ValueError, match="together"):
        run_climb(tmp_path, [13.876, 13.10], improve_prompt="beat it")


def test_resume_entry_requires_a_resuming_backend(tmp_path: Path) -> None:
    # resuming on a no-resume backend would end the climb as session-error; the
    # primitive rejects it up front (the depth loop picks resume-capable backends).
    # match the message unique to THIS check (not just "resume", which the coupling
    # error also contains) so a mis-firing precondition can't false-pass.
    with pytest.raises(ValueError, match="does not support resume"):
        run_climb(
            tmp_path,
            [13.876, 13.10],
            resume_session_id="prev-sess",
            improve_prompt="beat it",
            harness=FakeHarness(result=ok_session(), supports_resume=False),
        )


def test_resume_entry_rejects_brief_only_inputs(tmp_path: Path) -> None:
    # a resume skips build_brief, so brief-only inputs would be silently dropped;
    # the primitive rejects them loudly (the same anti-silent-discard stance).
    for brief_only in (
        {"task_hypothesis": "try 3-opt"},
        {"lessons": "prior climbs overfit the pool"},
        {"recent_reports": ("last week's report",)},
        {"created": "2026-08-06T00:00:00Z"},
        {"brief_baseline": 13.876},
    ):  # every one of the five guarded brief-only inputs, so dropping any regresses
        with pytest.raises(ValueError, match="no effect on a resume"):
            run_climb(
                tmp_path,
                [13.876, 13.10],
                resume_session_id="prev-sess",
                improve_prompt="beat it",
                **brief_only,
            )


def test_first_run_brief_has_no_baseline_number(tmp_path: Path) -> None:
    # a benchmark's first climb has no ledger entry -> the brief drops the
    # reference number (never the gate, which still measures both sides).
    result, harness, _ = run_climb(tmp_path, [13.876, 13.10])  # brief_baseline defaults None
    assert result.outcome == "improved" and result.baseline == 13.876
    brief_text = harness.calls[0][0]
    assert "no score recorded yet" in brief_text  # no reference number
    assert "currently" not in brief_text  # and no fabricated baseline
    assert "lower is better" in brief_text  # the metric context still orients


def test_direction_min_regression_is_no_improvement(tmp_path: Path) -> None:
    result, _, _ = run_climb(tmp_path, [13.876, 14.5])
    assert result.outcome == "no-improvement"
    assert "negative result" in result.note


# --- author syscalls (research-loop-buildout.md Phase A): the sleep side ---


def _write_syscall(tmp_path: Path, payload: dict) -> None:
    import json

    d = tmp_path / ".autoresearch"
    d.mkdir(exist_ok=True)
    # the tool tags every sleep with its syscall type; mirror that here
    (d / "syscall.json").write_text(json.dumps({"type": "sleep", **payload}))


def _fake_launcher(log: list):
    def launcher(sha: str, request) -> str:
        log.append((sha, request))
        ids = [f"90{i}" for i in range(len(request.launches))]
        return "afterany:" + ":".join(ids) if ids else ""  # same contract as the real one

    return launcher


DEEP_CONTRACT = CONTRACT.replace(
    "metric: mean_tour_length", "metric: mean_tour_length\n    depth_k: 3"
)


def test_author_sleep_parks_on_a_sealed_snapshot(tmp_path: Path) -> None:
    # the author launched work and slept: the tree is sealed, the launcher is
    # handed the request, and the park carries request + budget counts.
    from autoresearch.orchestrator import RunParked

    _write_syscall(
        tmp_path,
        {"launches": [{"name": "probe", "command": "uv run probe.py"}], "note": "check tails"},
    )
    launched: list = []
    with pytest.raises(RunParked) as exc:
        run_climb(tmp_path, [], contract=DEEP_CONTRACT, launcher=_fake_launcher(launched))
    p = exc.value
    assert p.phase == "author-sleep"
    assert p.afterany == "afterany:900"
    assert p.candidate_sha == "cand1"  # sealed BEFORE the jobs were submitted
    assert p.syscall is not None and p.syscall.note == "check tails"
    assert p.launches_used == 1 and p.sleeps_used == 1
    sha, request = launched[0]
    assert sha == "cand1" and request.launches[0].name == "probe"


def test_checkpoint_sleep_parks_with_no_dependency(tmp_path: Path) -> None:
    from autoresearch.orchestrator import RunParked

    _write_syscall(tmp_path, {"launches": []})
    with pytest.raises(RunParked) as exc:
        run_climb(tmp_path, [], contract=DEEP_CONTRACT, launcher=_fake_launcher([]))
    assert exc.value.afterany == "" and exc.value.sleeps_used == 1
    assert exc.value.launches_used == 0  # a checkpoint burns only the sleep


def test_submit_parks_the_dispatched_gate_with_the_submitted_marker(tmp_path: Path) -> None:
    # `submit` is a launch whose job is the GATE (buildout Phase B): the tree
    # is sealed, the dispatched gate parks the run as a candidate carrying the
    # submitted marker + budget counts, and a sibling launch rides the same
    # afterany, submitted on the sealed sha.
    from autoresearch.orchestrator import RunParked

    _write_syscall(
        tmp_path,
        {"launches": [{"name": "probe", "command": "uv run probe.py"}], "submit": True},
    )
    launched: list = []
    harness = FakeHarness(result=ok_session())
    m = ParkingMeasurer(park_on_call=1)
    with pytest.raises(RunParked) as exc:
        attempt_once(
            CONFIG,
            DEEP_CONTRACT,
            tmp_path,
            harness,
            m,
            "base",
            _bare_snapshot(),
            ruler="r",
            changed_paths=lambda: ["src/pilot/solvers/tsp.py"],
            created="t",
            launcher=_fake_launcher(launched),
        )
    p = exc.value
    assert p.phase == "candidate" and p.submitted
    assert p.syscall is not None and p.syscall.submit
    assert p.launches_used == 1 and p.sleeps_used == 1  # the submit rode one sleep
    assert p.afterany.endswith(":900")  # gate evals + the sibling launch, one afterany
    sha, request = launched[0]
    assert sha == "cand1" and request.launches[0].name == "probe"


def test_failed_gate_submit_feeds_back_to_the_author(tmp_path: Path) -> None:
    # an inline gate that rejects a submitted candidate resumes the AUTHOR with
    # the result (it decides what happens next) — never a silent terminal; the
    # author's plain finish then ends the run honestly.
    harness = _SeqHarness(["the claim", "conceded"], submit_on=(1,))
    evaluator = FakeEvaluator(values=[13.9, 13.9, 13.9])
    measurer, snapshot = _wire(evaluator, tmp_path)
    result = attempt_once(
        CONFIG,
        CONTRACT,
        tmp_path,
        harness,
        measurer,
        "base",
        snapshot,
        ruler="r",
        changed_paths=lambda: ["src/pilot/solvers/tsp.py"],
        created="t",
        launcher=lambda sha, req: "",
    )
    assert result.outcome == "no-improvement"
    assert harness.resumes == [None, "s1"]  # the author heard the gate result
    assert len(evaluator.calls) == 3  # the concluding tree differed, so it was measured


def _gate_negative_attempt(tmp_path: Path, harness, evaluator, contract: str = CONTRACT):
    measurer, snapshot = _wire(evaluator, tmp_path)
    return attempt_once(
        CONFIG,
        contract,
        tmp_path,
        harness,
        measurer,
        "base",
        snapshot,
        ruler="r",
        changed_paths=lambda: ["src/pilot/solvers/tsp.py"],
        created="t",
        launcher=lambda sha, req: "",
        tree_of=lambda sha: "same-tree",  # every seal has the same content
    )


def test_unchanged_tree_after_a_gate_negative_is_not_measured_again(tmp_path: Path) -> None:
    """The author hears the gate's negative and concludes without touching the
    tree: that verdict stands, and the identical tree is not sealed and
    measured a second time (the speedrun fleet paid a second 8 GPU-hour gate
    for an identical tree, 2026-08-28)."""
    harness = _SeqHarness(["the claim", "conceded"], submit_on=(1,))
    evaluator = FakeEvaluator(values=[13.9, 13.9])  # a third eval would empty the queue
    result = _gate_negative_attempt(tmp_path, harness, evaluator)
    assert result.outcome == "no-improvement"
    assert harness.resumes == [None, "s1"]
    assert len(evaluator.calls) == 2


def test_identical_resubmit_is_told_so_without_a_second_gate(tmp_path: Path) -> None:
    """A resubmit of the tree the gate already judged runs nothing: the author
    is told the verdict stands and decides again (bounded by sleep_k)."""
    harness = _SeqHarness(["the claim", "again", "conceded"], submit_on=(1, 2))
    evaluator = FakeEvaluator(values=[13.9, 13.9])
    result = _gate_negative_attempt(tmp_path, harness, evaluator)
    assert result.outcome == "no-improvement"
    assert harness.resumes == [None, "s1", "s2"]
    assert len(evaluator.calls) == 2
    assert "identical to the candidate the gate already measured" in harness.prompts[2]


def test_identical_resubmit_past_the_sleep_budget_ends_on_the_verdict(tmp_path: Path) -> None:
    """The unchanged-tree reuse runs BEFORE the budget check, so an author out
    of sleeps (or GPU-hours) is not refused a gate it would never run; past
    the sleep budget the attempt simply ends on the standing verdict."""
    one_sleep = CONTRACT.replace("    direction: min\n", "    direction: min\n    sleep_k: 1\n", 1)
    harness = _SeqHarness(["the claim", "again", "never reached"], submit_on=(1, 2))
    evaluator = FakeEvaluator(values=[13.9, 13.9])
    result = _gate_negative_attempt(tmp_path, harness, evaluator, contract=one_sleep)
    assert result.outcome == "no-improvement"
    assert harness.resumes == [None, "s1"]  # the second resubmit was the last word
    assert len(evaluator.calls) == 2


def test_inline_gate_never_dispatches_a_submits_sibling_launches(tmp_path: Path) -> None:
    # terra #144 r2: siblings dispatch only once the gate has PROVEN dispatched
    # (inside the MeasurementPending handler) — an inline gate would orphan
    # launch jobs no wake ever gathers. The author is told they did not run
    # and their budget stays unspent.
    _write_syscall(
        tmp_path,
        {"launches": [{"name": "probe", "command": "x"}], "submit": True},
    )
    launched: list = []
    harness = _SeqHarness(["the claim", "conceded"])
    evaluator = FakeEvaluator(values=[13.9, 13.9, 13.9])
    measurer, snapshot = _wire(evaluator, tmp_path)
    result = attempt_once(
        CONFIG,
        CONTRACT,
        tmp_path,
        harness,
        measurer,
        "base",
        snapshot,
        ruler="r",
        changed_paths=lambda: ["src/pilot/solvers/tsp.py"],
        created="t",
        launcher=_fake_launcher(launched),
    )
    assert result.outcome == "no-improvement"
    assert launched == []  # nothing dispatched, nothing orphaned


def test_syscalls_are_ignored_without_a_launcher(tmp_path: Path) -> None:
    # feature off (no launcher wired): a stray request file is inert and the
    # climb behaves exactly as today.
    _write_syscall(tmp_path, {"launches": [{"name": "probe", "command": "x"}]})
    result, _, _ = run_climb(tmp_path, [13.876, 13.10])
    assert result.outcome == "improved"
    assert (tmp_path / ".autoresearch" / "syscall.json").exists()  # not even consumed


def test_over_budget_request_wakes_one_refusal_then_measures(tmp_path: Path) -> None:
    # depth_k=1 here: a two-launch ask exceeds the budget. The author is
    # woken ONCE with the refusal (same session), then the climb measures the
    # tree as it stands. Nothing was launched.
    _write_syscall(
        tmp_path,
        {"launches": [{"name": "a", "command": "x"}, {"name": "b", "command": "y"}]},
    )
    launched: list = []
    tight = CONTRACT.replace("    direction: min\n", "    direction: min\n    depth_k: 1\n", 1)
    result, harness, _ = run_climb(
        tmp_path, [13.876, 13.10], contract=tight, launcher=_fake_launcher(launched)
    )
    assert result.outcome == "improved" and launched == []
    refusal_text, _ws, resumed = harness.calls[1]  # second call = the refusal wake
    assert "REFUSED" in refusal_text and "launch budget" in refusal_text
    assert resumed == "s1"  # the SAME session was woken


def test_author_sleep_refuses_an_out_of_scope_tree_before_launching(tmp_path: Path) -> None:
    # same invariant as the candidate path: an out-of-scope tree is never
    # snapshotted OR executed — a launch would run code from it in an external
    # job (the edit could be to the ruler itself). No snapshot, no jobs.
    _write_syscall(tmp_path, {"launches": [{"name": "probe", "command": "x"}]})
    launched: list = []
    result, _, _ = run_climb(
        tmp_path,
        [],
        contract=DEEP_CONTRACT,
        launcher=_fake_launcher(launched),
        changed=["docs/ruler-tamper.md"],
    )
    assert result.outcome == "scope-violation" and launched == []


def test_malformed_syscall_request_is_a_loud_error(tmp_path: Path) -> None:
    (tmp_path / ".autoresearch").mkdir()
    (tmp_path / ".autoresearch" / "syscall.json").write_text("{broken")
    result, _, _ = run_climb(tmp_path, [], launcher=_fake_launcher([]))
    assert result.outcome == "session-error"
    assert "unhonorable syscall request" in result.note


def test_noise_below_threshold_is_no_improvement(tmp_path: Path) -> None:
    result, _, _ = run_climb(tmp_path, [100.0, 99.9])  # 0.1% < 0.5% floor
    assert result.outcome == "no-improvement"


SEEDED_CONTRACT = CONTRACT.replace(
    "    direction: min\n",
    "    direction: min\n    seed_env: PILOT_TSP_SEED\n    min_delta: 0.5\n",
    1,
)


def test_seeded_benchmark_measures_both_sides_under_one_fresh_seed(tmp_path: Path) -> None:
    """seed_env: the orchestrator draws ONE seed per climb, pins baseline
    and candidate to it (paired), and reports it on the result for the
    ledger — nothing about the pool was knowable when the solver wrote."""
    harness = FakeHarness(result=ok_session())
    evaluator = FakeEvaluator(values=[13.876, 13.10])
    measurer, snapshot = _wire(evaluator, tmp_path)
    result = attempt_once(
        CONFIG,
        SEEDED_CONTRACT,
        tmp_path,
        harness,
        measurer,
        "base",
        snapshot,
        ruler="r",
        changed_paths=lambda: ["src/pilot/solvers/tsp.py"],
        created="2026-08-09T00:00:00Z",
    )
    assert result.outcome == "improved"
    first, second = evaluator.seen_env
    assert first is not None and set(first) == {"PILOT_TSP_SEED"}
    assert first == second  # paired: common random numbers
    assert result.run_seed == int(first["PILOT_TSP_SEED"]) > 0


def test_unseeded_benchmark_injects_nothing(tmp_path: Path) -> None:
    result, _, evaluator = run_climb(tmp_path, [13.876, 13.10])
    assert evaluator.seen_env == [None, None]
    assert result.run_seed == 0


def test_relative_floor_scales_with_the_level() -> None:
    from autoresearch.orchestrator import benchmark_floor, clears_min_delta

    # 13% relative floor: at level 500 the floor is 65, at 550 it is 71.5
    assert benchmark_floor(500.0, None, 0.13) == 65.0
    assert benchmark_floor(550.0, None, 0.13) == 71.5
    # a max benchmark must beat the recorded best by more than the scaled floor
    assert clears_min_delta(500.0, 566.0, "max", None, 0.13)  # +66 > 65
    assert not clears_min_delta(500.0, 564.0, "max", None, 0.13)  # +64 < 65
    # both set: the more conservative (max) applies
    assert benchmark_floor(500.0, 100.0, 0.13) == 100.0  # abs 100 > rel 65
    assert benchmark_floor(500.0, 40.0, 0.13) == 65.0  # rel 65 > abs 40
    # neither: no floor
    assert benchmark_floor(500.0, None, None) == 0.0
    assert clears_min_delta(500.0, 500.001, "max", None, None)
    # a declared floor with a non-finite recorded best fails closed, not open
    assert not clears_min_delta(float("nan"), 600.0, "max", None, 0.13)
    assert not clears_min_delta(float("inf"), 600.0, "max", 5.0, None)


def test_clears_min_delta_is_direction_aware_and_absolute() -> None:
    from autoresearch.orchestrator import clears_min_delta

    assert clears_min_delta(12.0, 11.4, "min", 0.5)  # 0.6 > 0.5
    assert not clears_min_delta(12.0, 11.5, "min", 0.5)  # exactly at the floor
    assert clears_min_delta(0.54, 0.65, "max", 0.10)
    assert not clears_min_delta(0.54, 0.60, "max", 0.10)  # inside pool luck
    assert clears_min_delta(12.0, 11.9, "min", None)  # no floor declared
    assert clears_min_delta(12.0, 11.9, "min", 0)  # zero floor = no floor
    assert not clears_min_delta(float("nan"), 11.9, "min", 0.5)


class ParkingMeasurer:
    """A Measurer that PARKS (raises MeasurementPending) on a chosen call —
    stands in for the dispatched backend without a cluster."""

    def __init__(self, park_on_call: int, values: dict[str, float] | None = None):
        self.park_on = park_on_call
        self.values = values or {}
        self.calls = 0

    def results(self, measures):
        from autoresearch.measure import MeasurementPending

        self.calls += 1
        if self.calls == self.park_on:
            raise MeasurementPending(("101", "102"))
        return {m.name: self.values[m.name] for m in measures}


def _bare_snapshot():
    n = {"i": 0}

    def snapshot() -> str:
        n["i"] += 1
        return f"cand{n['i']}"

    return snapshot


def test_candidate_measure_parks_after_the_session(tmp_path: Path) -> None:
    # The ONLY park: the session runs (no pre-session baseline), then the gate
    # dispatches baseline+candidate together and hibernates. A dispatched climb
    # never has a baseline park.
    from autoresearch.orchestrator import RunParked

    harness = FakeHarness(result=ok_session())
    m = ParkingMeasurer(park_on_call=1)  # the gate's first (baseline+candidate) call parks
    with pytest.raises(RunParked) as exc:
        attempt_once(
            CONFIG,
            CONTRACT,
            tmp_path,
            harness,
            m,
            "base",
            _bare_snapshot(),
            ruler="r",
            changed_paths=lambda: ["src/pilot/solvers/tsp.py"],
            created="t",
        )
    assert exc.value.phase == "candidate"
    assert exc.value.candidate_sha == "cand1"
    assert exc.value.session is not None  # the session ran before this park
    assert harness.calls  # the session did start


def test_session_error_runs_no_eval(tmp_path: Path) -> None:
    # nothing is measured before the session, so a session error measures
    # nothing at all (the gate, which measures baseline+candidate, never runs).
    bad = SessionResult(
        stop_reason="spawn-error",
        is_error=True,
        cost_usd=0.0,
        num_turns=0,
        session_id="",
        final_text="",
        transcript_path="",
    )
    result, _, evaluator = run_climb(tmp_path, [13.876, 999.0], session=bad)
    assert result.outcome == "session-error"
    assert evaluator.calls == []  # no measurement without a candidate
    assert result.baseline is None


def test_exhausted_session_is_a_budget_ending_not_an_error(tmp_path: Path) -> None:
    """Turns or walltime running out is 'caps hit mid-run' — the outcome
    names it, and the note carries the real cause, not a stop reason."""
    for stop, detail in (
        ("timeout", "session hit its 90-minute walltime and was killed"),
        ("tool_use", "error_max_turns: Reached maximum number of turns (120)"),
    ):
        dry = SessionResult(
            stop_reason=stop,
            is_error=True,
            cost_usd=1.0,
            num_turns=120,
            session_id="",
            final_text="",
            transcript_path="",
            error_detail=detail,
        )
        result, _, _evaluator = run_climb(tmp_path, [13.876, 999.0], session=dry)
        assert result.outcome == "session-budget"
        assert result.note == detail


def test_candidate_eval_failure_is_eval_error_with_session(tmp_path: Path) -> None:
    """The agent broke the eval — that's a failed run, not an improvement."""
    result, _, _ = run_climb(tmp_path, [13.876, EvalError("crashed")])
    assert result.outcome == "eval-error"
    assert result.session is not None


def test_snapshot_failure_is_eval_error_not_a_crash(tmp_path: Path) -> None:
    """A snapshot failure (the session ran, the tree could not be captured) is
    an eval-error result with the session — never an escaping exception."""
    harness = FakeHarness(result=ok_session())
    evaluator = FakeEvaluator(values=[13.876])  # baseline only; no candidate reached
    measurer, _ = _wire(evaluator, tmp_path)

    def snapshot() -> str:
        raise EvalError("git write-tree failed")

    result = attempt_once(
        CONFIG,
        CONTRACT,
        tmp_path,
        harness,
        measurer,
        "base",
        snapshot,
        ruler="r",
        changed_paths=lambda: ["src/pilot/solvers/tsp.py"],
        created="t",
    )
    assert result.outcome == "eval-error"
    assert result.session is not None and "snapshot" in result.note


def test_unknown_benchmark_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not in contract"):
        run_climb(tmp_path, [1.0], config=RunConfig(target="org/pilot", benchmark="chess"))


def test_improved_direction_semantics() -> None:
    assert improved(0.25, 0.31, "max", 0.005)
    assert not improved(0.25, 0.2501, "max", 0.005)
    assert improved(13.876, 13.0, "min", 0.005)
    assert not improved(13.876, 14.0, "min", 0.005)
    assert not improved(0.25, 0.24, "max", 0.005)


def test_metric_parsing_accepts_pilot_shape() -> None:
    """The pilot's eval prints the metric NAME as a value: caught pre-live by
    the review agent — this exact line would have aborted the first climb."""
    line = (
        '{"benchmark": "tsp", "metric": "mean_tour_length",'
        ' "value": 13.875696168157484, "direction": "min"}'
    )
    assert _metric_from_output(line, "mean_tour_length") == 13.875696168157484
    assert _metric_from_output(line, "solve_rate") is None


def test_metric_parsing_json_only_no_fuzzy_fallback() -> None:
    assert _metric_from_output('{"mean_tour_length": 13.1}', "mean_tour_length") == 13.1
    noisy = 'log line\n{"other": 1}\n{"solve_rate": 0.31, "n": 40}'
    assert _metric_from_output(noisy, "solve_rate") == 0.31
    assert _metric_from_output("nothing here", "solve_rate") is None
    assert _metric_from_output('{"solve_rate": "high"}', "solve_rate") is None
    # NO regex fallback: a fuzzy match that reads a progress line or a
    # prefixed metric name is worse than a clean failure
    assert _metric_from_output("solve_rate: 0.28", "solve_rate") is None
    assert _metric_from_output('{"mean_solve_rate": 0.99}', "solve_rate") is None


def test_scope_violation_blocks_before_candidate_eval(tmp_path: Path) -> None:
    """An out-of-scope edit could be to the ruler itself — the tree is never
    measured."""
    result, _, evaluator = run_climb(
        tmp_path, [13.876, 1.0], changed=["src/pilot/solvers/tsp.py", "src/pilot/eval.py"]
    )
    assert result.outcome == "scope-violation"
    assert "src/pilot/eval.py" in result.note
    assert evaluator.calls == []  # scope checked before any measure; nothing ran


def test_forbidden_paths_are_scope_violations(tmp_path: Path) -> None:
    result, _, _ = run_climb(tmp_path, [13.876, 1.0], changed=[".github/workflows/ci.yml"])
    assert result.outcome == "scope-violation"


def test_out_of_scope_tree_is_rejected_before_the_snapshot(tmp_path: Path) -> None:
    # the out-of-scope edit could be to the ruler itself — it is never
    # snapshotted OR measured, not merely rejected after the fact.
    harness = FakeHarness(result=ok_session())
    evaluator = FakeEvaluator(values=[13.876])
    measurer, _ = _wire(evaluator, tmp_path)
    snapshotted: list[int] = []

    def snapshot() -> str:
        snapshotted.append(1)
        return "cand1"

    result = attempt_once(
        CONFIG,
        CONTRACT,
        tmp_path,
        harness,
        measurer,
        "base",
        snapshot,
        ruler="r",
        changed_paths=lambda: ["docs/secret.md"],
        created="t",
    )
    assert result.outcome == "scope-violation"
    assert snapshotted == []  # never snapshotted the out-of-scope tree


def test_improved_rejects_nonfinite_and_zero_baseline_uses_absolute() -> None:
    assert not improved(float("nan"), 1.0, "max", 0.005)
    assert not improved(1.0, float("inf"), "max", 0.005)
    assert not improved(0.0, 1e-12, "max", 0.005)  # absolute threshold at 0
    assert improved(0.0, 0.01, "max", 0.005)


def test_draw_run_seed_never_returns_the_sentinel() -> None:
    from autoresearch.orchestrator import draw_run_seed

    for _ in range(64):
        seed = draw_run_seed()
        assert 1 <= seed <= 2**30  # 0 stays the "no seed recorded" sentinel


def test_extra_env_cannot_override_managed_isolation_vars(tmp_path: Path) -> None:
    """HOME/UV_* are the eval's isolation; a colliding injection is dropped
    (the contract validator rejects such seed_env names — this is depth)."""
    from autoresearch.orchestrator import SubprocessEvaluator

    value = SubprocessEvaluator(timeout_s=30).evaluate(
        tmp_path,
        'if [ "$HOME" = "/tmp/hijack" ]; then printf \'{"m": 1}\'; '
        'else printf \'{"m": %s}\' "$M_SEED"; fi',
        "m",
        extra_env={"HOME": "/tmp/hijack", "M_SEED": "7"},
    )
    assert value == 7.0


def test_subprocess_evaluator_injects_extra_env(tmp_path: Path) -> None:
    """The seed reaches the eval subprocess (the base env is a scrubbed
    allowlist, so injection must be explicit and is tested for real)."""
    from autoresearch.orchestrator import SubprocessEvaluator

    value = SubprocessEvaluator(timeout_s=30).evaluate(
        tmp_path,
        'printf \'{"m": %s}\\n\' "$PILOT_TSP_SEED"',
        "m",
        extra_env={"PILOT_TSP_SEED": "42"},
    )
    assert value == 42.0


def test_subprocess_evaluator_real_run(tmp_path: Path) -> None:
    from autoresearch.orchestrator import SubprocessEvaluator

    value = SubprocessEvaluator(timeout_s=30).evaluate(tmp_path, """printf '{"m": 0.5}\n'""", "m")
    assert value == 0.5


def test_subprocess_evaluator_scrubs_home(tmp_path: Path) -> None:
    """The eval runs agent-written code; the orchestrator's real HOME (which
    shelters the bot PAT) must never be visible to it."""
    import os

    from autoresearch.orchestrator import SubprocessEvaluator

    real_home = os.environ.get("HOME", "")
    value = SubprocessEvaluator(timeout_s=30).evaluate(
        tmp_path, f'[ "$HOME" = "{real_home}" ] && echo \'{{"m": 1}}\' || echo \'{{"m": 0}}\'', "m"
    )
    assert value == 0.0


def test_subprocess_evaluator_nonzero_exit_is_eval_error(tmp_path: Path) -> None:
    from autoresearch.orchestrator import SubprocessEvaluator

    with pytest.raises(EvalError, match="eval failed"):
        SubprocessEvaluator(timeout_s=30).evaluate(tmp_path, "exit 3", "m")


def test_subprocess_evaluator_rejects_nonfinite(tmp_path: Path) -> None:
    from autoresearch.orchestrator import SubprocessEvaluator

    with pytest.raises(EvalError, match="not finite"):
        SubprocessEvaluator(timeout_s=30).evaluate(tmp_path, """printf '{"m": Infinity}\n'""", "m")


def test_subprocess_evaluator_container_wrapping(tmp_path: Path) -> None:
    """With an image set, the eval command runs inside apptainer."""
    import stat as stat_mod

    from autoresearch.orchestrator import SubprocessEvaluator

    fake = tmp_path / "apptainer"
    fake.write_text(
        f'#!/bin/sh\nprintf "%s " "$@" > {tmp_path}/eval_argv\nprintf \'{{"m": 2.5}}\n\'\n'
    )
    fake.chmod(fake.stat().st_mode | stat_mod.S_IEXEC)
    evaluator = SubprocessEvaluator(
        timeout_s=30, container_image="/img/pilot.sif", apptainer_binary=str(fake)
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    assert evaluator.evaluate(ws, "run-the-eval", "m") == 2.5
    argv = (tmp_path / "eval_argv").read_text()
    assert "exec --containall --cleanenv" in argv
    assert f"--bind {ws}:{ws}" in argv
    # a real-disk home: apptainer's tmpfs home is size-capped and uv blows it
    assert "--home " in argv and "ws-eval-home-" in argv
    assert "/img/pilot.sif sh -c run-the-eval" in argv


def test_pr_body_refuses_non_improved_results(tmp_path: Path) -> None:
    result, _, _ = run_climb(tmp_path, [13.876, 14.5])
    with pytest.raises(ValueError, match="requires an improved result"):
        pr_body(result, CONFIG, redact_secrets=())


def test_pr_body_carries_table_and_redacts(tmp_path: Path) -> None:
    session = ok_session(text="did the thing; key sk-secret-1 leaked")
    result, _, _ = run_climb(tmp_path, [13.876, 13.1], session=session)
    body = pr_body(result, CONFIG, redact_secrets=("sk-secret-1",))
    assert "| baseline (tsp) | 13.876 |" in body
    assert "sk-secret-1" not in body
    assert "[redacted]" in body
    assert "measured by the orchestrator" in body


def test_report_covers_failure_outcomes(tmp_path: Path) -> None:
    result, _, _ = run_climb(tmp_path, [13.876, 14.5])
    report = result.report(CONFIG)
    assert "no-improvement" in report
    assert "13.876" in report
    assert "Agent's report" in report


def test_eval_home_is_outside_the_clone(tmp_path: Path) -> None:
    """Eval cache/state must not masquerade as agent edits in the diff."""
    from autoresearch.orchestrator import SubprocessEvaluator

    ws = tmp_path / "ws"
    ws.mkdir()
    SubprocessEvaluator(timeout_s=30).evaluate(
        ws, """touch "$HOME/marker" && printf '{"m": 1}\\n'""", "m"
    )
    assert not (ws / "marker").exists()  # never lands in the clone
    # and the per-eval home is cleaned up afterward (bounded disk)
    assert list(tmp_path.glob("ws-eval-home-*")) == []


def test_report_redacts_secrets(tmp_path: Path) -> None:
    session = ok_session(text="oops the key is sk-report-leak")
    result, _, _ = run_climb(tmp_path, [13.876, 14.5], session=session)
    report = result.report(CONFIG, redact_secrets=("sk-report-leak",))
    assert "sk-report-leak" not in report
    assert "[redacted]" in report


def test_progress_render_and_ledger_roundtrip(tmp_path: Path) -> None:
    from autoresearch.progress import (
        load_leader,
        render_markdown,
        update_leader,
        write_progress,
    )

    entries = update_leader({}, "tsp", "mean_tour_length", "min", 13.876, 13.1, "r1", "2026-08-06")
    entries = update_leader(entries, "sokoban", "solve_rate", "max", 0.25, 0.31, "r2", "2026-08-06")
    write_progress(tmp_path, entries, "org/pilot")
    reloaded = load_leader(tmp_path)
    assert reloaded == entries
    table = render_markdown(reloaded, "org/pilot")
    assert "| sokoban | `solve_rate` ↑ | 0.25 | 0.31 | ▲ +24.0% |" in table
    assert "measured by the orchestrator" in table


def test_leader_survives_corruption(tmp_path: Path) -> None:
    from autoresearch.progress import LEADER_FILE, load_leader

    path = tmp_path / LEADER_FILE
    path.parent.mkdir(parents=True)
    path.write_text("{broken")
    assert load_leader(tmp_path) == {}


def test_leader_best_never_regresses() -> None:
    from autoresearch.progress import update_leader

    entries = update_leader({}, "tsp", "m", "min", 13.876, 13.1, "r1", "d1")
    # a later run improved vs its own (stale) baseline but is worse than best
    entries = update_leader(entries, "tsp", "m", "min", 13.876, 13.5, "r2", "d2")
    assert entries["tsp"].best == 13.1
    assert entries["tsp"].best_run == "r1"
    # a genuinely better run still advances it
    entries = update_leader(entries, "tsp", "m", "min", 13.1, 12.9, "r3", "d3")
    assert entries["tsp"].best == 12.9


def test_eval_cache_is_bound_alone_and_cleaned(tmp_path: Path, monkeypatch) -> None:
    """Only the per-eval cache dir is bound into the container — never the
    whole host tmp — and it dies with the eval."""
    import stat as stat_mod

    from autoresearch.orchestrator import SubprocessEvaluator

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    fake = tmp_path / "apptainer"
    fake.write_text(
        "#!/bin/sh\n"
        f'printf "%s " "$@" > {tmp_path}/eval_argv\n'
        f"env > {tmp_path}/eval_env\n"
        "printf '{\"m\": 3.5}\\n'\n"
    )
    fake.chmod(fake.stat().st_mode | stat_mod.S_IEXEC)
    ws = tmp_path / "ws"
    ws.mkdir()
    evaluator = SubprocessEvaluator(
        timeout_s=30, container_image="/img/pilot.sif", apptainer_binary=str(fake)
    )
    assert evaluator.evaluate(ws, "run", "m") == 3.5
    argv = (tmp_path / "eval_argv").read_text()
    assert f"--bind {scratch}/uv-cache-" in argv  # the cache dir, specifically
    assert f"--bind {scratch}:{scratch}" not in argv  # never the whole tmp
    seen_env = (tmp_path / "eval_env").read_text()
    assert "APPTAINERENV_UV_CACHE_DIR=" in seen_env
    # the private env crosses --cleanenv too, or the container's uv would
    # silently fall back to the workspace venv — the raced, session-built
    # state this design removes
    assert "APPTAINERENV_UV_PROJECT_ENVIRONMENT=" in seen_env
    assert list(scratch.glob("uv-cache-*")) == []  # cleaned up after


def test_subprocess_evaluator_env_is_private_per_eval(tmp_path: Path) -> None:
    """The eval never consumes a workspace venv (no shared mutable state
    with the session, no NFS close-to-open race, no session-authored
    entrypoints in the orchestrator's path): UV_PROJECT_ENVIRONMENT points
    at node-local scratch beside the uv cache, unique per eval."""
    from autoresearch.orchestrator import SubprocessEvaluator

    command = (
        """printf '{"m": %s}\\n' """
        '''"$(echo "$UV_PROJECT_ENVIRONMENT" | grep -c "cache")"'''
    )
    evaluator = SubprocessEvaluator(timeout_s=30)
    assert evaluator.evaluate(tmp_path, command, "m") == 1.0
    # and the venv path is unique per eval: capture it twice
    capture = 'printf \'{"m": 1}\\n\'; echo "$UV_PROJECT_ENVIRONMENT" >> ' + str(
        tmp_path / "seen.txt"
    )
    evaluator.evaluate(tmp_path, capture, "m")
    evaluator.evaluate(tmp_path, capture, "m")
    seen = (tmp_path / "seen.txt").read_text().split()
    assert len(seen) == 2 and seen[0] != seen[1]


# ---- suite no-regression gate --------------------------------------------

SHARED_CONTRACT = """
benchmarks:
  - name: tsp
    command: uv run python -m pilot.eval --benchmark tsp --json
    metric: mean_tour_length
    direction: min
  - name: sokoban
    command: uv run python -m pilot.eval --benchmark sokoban --json
    metric: solve_rate
    direction: max
budgets: {gpu_hours_per_run: 1, runs_per_week: 10}
scope: {allowed: [src/pilot/], shared: [src/pilot/model/]}
roadmap: docs/roadmap.md
"""


def run_shared_climb(tmp_path, values, changed, contract=SHARED_CONTRACT, **kw):
    harness = FakeHarness(result=ok_session())
    evaluator = FakeEvaluator(values=list(values))
    baseline_ws = tmp_path / "pristine"
    baseline_ws.mkdir(exist_ok=True)
    measurer, snapshot = _wire(evaluator, tmp_path, base_ws=baseline_ws)
    result = attempt_once(
        CONFIG,
        contract,
        tmp_path,
        harness,
        measurer,
        "base",
        snapshot,
        ruler="mean tour length over the frozen pool",
        changed_paths=lambda: changed,
        created="2026-08-06T00:00:00Z",
        **kw,
    )
    return result, evaluator


def test_shared_diff_measures_every_sibling_and_passes(tmp_path: Path) -> None:
    result, evaluator = run_shared_climb(
        tmp_path, [13.876, 13.10, 0.8, 0.8], changed=["src/pilot/model/encoder.py"]
    )
    assert result.outcome == "improved"
    # 2 climbed evals + a paired pass per sibling
    assert len(evaluator.calls) == 4
    assert evaluator.calls[2] == evaluator.calls[3]  # sibling: same command both sides
    # each pair measures pristine-vs-session, sibling exactly like the climbed
    # metric: [baseline ws, session ws, sibling baseline ws, sibling session ws]
    pristine = evaluator.seen_ws[0]
    assert pristine.name == "pristine"
    assert evaluator.seen_ws == [pristine, pristine.parent, pristine, pristine.parent]
    (row,) = result.suite
    assert row.name == "sokoban" and not row.regressed
    assert row.baseline == 0.8 and row.candidate == 0.8


def test_shared_diff_regressing_a_sibling_loses_credit(tmp_path: Path) -> None:
    result, _ = run_shared_climb(
        tmp_path, [13.876, 13.10, 0.8, 0.5], changed=["src/pilot/model/encoder.py"]
    )
    assert result.outcome == "suite-regression"
    assert "sokoban" in result.note and "0.8" in result.note
    assert result.branch == ""  # no PR is opened from this outcome
    (row,) = result.suite
    assert row.regressed


def test_env_specific_diff_skips_the_suite_pass(tmp_path: Path) -> None:
    result, evaluator = run_shared_climb(
        tmp_path, [13.876, 13.10], changed=["src/pilot/solvers/tsp.py"]
    )
    assert result.outcome == "improved"
    assert len(evaluator.calls) == 2  # cheap path: only the climbed benchmark
    assert result.suite == ()


def test_sibling_floor_gives_a_stochastic_eval_its_tolerance(tmp_path: Path) -> None:
    floored = SHARED_CONTRACT.replace(
        "    direction: max\n", "    direction: max\n    min_delta: 0.05\n", 1
    )
    result, _ = run_shared_climb(
        tmp_path,
        [13.876, 13.10, 0.8, 0.77],
        changed=["src/pilot/model/encoder.py"],
        contract=floored,
    )
    assert result.outcome == "improved"  # a 0.03 drop sits inside sokoban's 0.05 floor
    (row,) = result.suite
    assert not row.regressed


def test_sibling_eval_failure_is_an_eval_error_naming_it(tmp_path: Path) -> None:
    result, _ = run_shared_climb(
        tmp_path,
        [13.876, 13.10, EvalError("harness crashed")],
        changed=["src/pilot/model/encoder.py"],
    )
    assert result.outcome == "eval-error"
    assert "sokoban" in result.note  # the failing sibling measure is named


def test_seeded_sibling_pair_is_pinned_to_one_suite_seed(tmp_path: Path) -> None:
    seeded = SHARED_CONTRACT.replace(
        "    direction: max\n", "    direction: max\n    seed_env: PILOT_SOKOBAN_SEED\n", 1
    )
    result, evaluator = run_shared_climb(
        tmp_path,
        [13.876, 13.10, 0.8, 0.8],
        changed=["src/pilot/model/encoder.py"],
        contract=seeded,
    )
    assert result.outcome == "improved"
    sib_envs = evaluator.seen_env[2:]
    assert sib_envs[0] == sib_envs[1]  # paired: same seed both sides
    assert sib_envs[0] and "PILOT_SOKOBAN_SEED" in sib_envs[0]
    assert result.suite_seed > 0


def test_seeded_benchmark_shares_its_run_seed_with_the_suite(tmp_path: Path) -> None:
    # the in-job gate REUSED the climbed benchmark's run_seed for the siblings;
    # the resumable path keeps that (fixed once, persisted), rather than drawing
    # an independent suite seed.
    seeded = SHARED_CONTRACT.replace(
        "    direction: min\n", "    direction: min\n    seed_env: PILOT_TSP_SEED\n", 1
    ).replace("    direction: max\n", "    direction: max\n    seed_env: PILOT_SOKOBAN_SEED\n", 1)
    result, _ = run_shared_climb(
        tmp_path,
        [13.876, 13.10, 0.8, 0.8],
        changed=["src/pilot/model/encoder.py"],
        contract=seeded,
    )
    assert result.outcome == "improved"
    assert result.run_seed > 0 and result.suite_seed == result.run_seed


def test_suite_regressed_is_direction_aware_and_fails_closed() -> None:
    from autoresearch.orchestrator import suite_regressed

    assert suite_regressed(0.8, 0.5, "max")  # max: drop is a regression
    assert not suite_regressed(0.8, 0.9, "max")
    assert suite_regressed(10.0, 12.0, "min")  # min: rise is a regression
    assert not suite_regressed(10.0, 9.0, "min")
    assert not suite_regressed(0.8, 0.77, "max", min_delta=0.05)  # inside the floor
    assert suite_regressed(0.8, 0.70, "max", min_delta=0.05)  # beyond it
    assert suite_regressed(float("nan"), 0.8, "max")  # unmeasurable fails closed


def test_pr_body_carries_the_suite_table(tmp_path: Path) -> None:
    result, _ = run_shared_climb(
        tmp_path, [13.876, 13.10, 0.8, 0.8], changed=["src/pilot/model/encoder.py"]
    )
    body = pr_body(result, CONFIG, redact_secrets=())
    assert "| sokoban | 0.8 | 0.8 |" in body
    assert "none regressed beyond its floor" in body


def test_task_names_the_suite_gate_only_when_it_exists(tmp_path: Path) -> None:
    from autoresearch.contract import load_contract
    from autoresearch.orchestrator import make_task

    gated = make_task(load_contract(SHARED_CONTRACT, "org/pilot"), "tsp", 13.876)
    assert "suite-gated" in gated.done_criteria
    ungated = make_task(load_contract(CONTRACT, "org/pilot"), "tsp", 13.876)
    assert "suite-gated" not in ungated.done_criteria


def test_attempt_once_refuses_a_read_only_spec(tmp_path: Path) -> None:
    # the author is an editing role; a non-executing spec here is a deployment bug
    from autoresearch.rolespec import Execution, RoleSpec, SessionBudget

    read_only = RoleSpec(
        name="reviewer",
        instructions="x",
        key="reviewer",
        tools=("Read",),
        execution=Execution(environment="gh-runner", can_execute=False),
        budget=SessionBudget(max_turns=1, walltime_s=1),
    )

    with pytest.raises(ValueError, match="must allow execution"):
        run_climb(tmp_path, [13.876], spec=read_only)


def test_shared_match_is_case_folded_like_the_other_path_checks(tmp_path: Path) -> None:
    # a Model/ spelling must not dodge the gate (forbidden/steward checks fold too)
    result, evaluator = run_shared_climb(
        tmp_path, [13.876, 13.10, 0.8, 0.8], changed=["src/pilot/Model/encoder.py"]
    )
    assert result.outcome == "improved"
    assert len(evaluator.calls) == 4  # the suite pass ran


# ---- the pre-PR panel loop -------------------------------------------------


class _SeqHarness:
    """Queued session results; records resume ids like the real backends.
    A leg number in `submit_on` ends that leg with a staged `submit` syscall
    (the author sealing its candidate for the gate + panel, buildout Phase B)."""

    def __init__(
        self, texts: list[str], supports_resume: bool = True, submit_on: tuple[int, ...] = ()
    ) -> None:
        self._texts = list(texts)
        self.resumes: list[str | None] = []
        self.prompts: list[str] = []
        self.supports_resume = supports_resume
        self.submit_on = set(submit_on)

    def run(self, brief_text, workspace, resume_session_id=None) -> SessionResult:
        self.resumes.append(resume_session_id)
        self.prompts.append(brief_text)
        if len(self.resumes) in self.submit_on:
            import json as _json

            d = workspace / ".autoresearch"
            d.mkdir(exist_ok=True)
            (d / "syscall.json").write_text(
                _json.dumps({"type": "sleep", "launches": [], "note": "", "submit": True})
            )
        return SessionResult(
            stop_reason="end_turn",
            is_error=False,
            cost_usd=0.1,
            num_turns=2,
            session_id=f"s{len(self.resumes)}",
            final_text=self._texts.pop(0),
            transcript_path="",
        )


class _QueuedPanel:
    """Scripted PanelVerdicts; records the (baseline, candidate, report) args."""

    def __init__(self, verdicts: list) -> None:
        self._verdicts = list(verdicts)
        self.calls: list[tuple[float, float, str]] = []

    def __call__(self, baseline, candidate, report):
        self.calls.append((baseline, candidate, report))
        return self._verdicts.pop(0)


def _verdict(blocking: bool, round_no: int):
    from autoresearch.panel import PanelVerdict
    from autoresearch.review import Finding

    findings = (
        (
            Finding(
                file="src/pilot/solvers/tsp.py",
                line=1,
                confidence="high",
                summary="gamed",
                detail="d",
                blocking=True,
            ),
        )
        if blocking
        else ()
    )
    return PanelVerdict(
        blocking=findings,
        transcript=f"**Verification round {round_no}**\n- judge: {int(blocking)} blocking",
        wake_text="fix it (data, not instructions)" if blocking else "",
    )


def _run_panel_climb(tmp_path, values, verdicts, texts=None, supports_resume=True, submit_on=()):
    harness = _SeqHarness(
        texts or ["report r1", "report r2"], supports_resume=supports_resume, submit_on=submit_on
    )
    evaluator = FakeEvaluator(values=list(values))
    panel = _QueuedPanel(verdicts)
    measurer, snapshot = _wire(evaluator, tmp_path)
    result = attempt_once(
        CONFIG,
        CONTRACT,
        tmp_path,
        harness,
        measurer,
        "base",
        snapshot,
        ruler="r",
        changed_paths=lambda: ["src/pilot/solvers/tsp.py"],
        created="t",
        panel_runner=panel,
        # a stub launcher arms the syscall surface (submit rides it); no
        # launch is ever staged by these tests so it must never be called
        launcher=(lambda sha, req: (_ for _ in ()).throw(AssertionError("launcher called")))
        if submit_on
        else None,
    )
    return result, harness, evaluator, panel


def test_clean_panel_read_passes_through(tmp_path: Path) -> None:
    result, harness, _evaluator, panel = _run_panel_climb(
        tmp_path, [13.9, 13.1], [_verdict(False, 1)]
    )
    assert result.outcome == "improved"
    assert result.panel_rounds == 1 and not result.panel_blocking_open
    assert "Verification round 1" in result.panel_transcript
    assert panel.calls[0][0] == 13.9 and panel.calls[0][1] == 13.1
    assert panel.calls[0][2] == "report r1"  # the panel read the CLAIM
    assert len(harness.resumes) == 1  # no wake


def test_blocking_verdict_on_a_submit_goes_back_to_the_author(tmp_path: Path) -> None:
    # THE DEPTH-AXIS CORE (buildout Phase B): the author SUBMITS; the gate
    # credits; the panel blocks -> the AUTHOR is resumed with the findings,
    # revises, finishes -> re-measured, panel clean -> published.
    result, harness, evaluator, _panel = _run_panel_climb(
        tmp_path,
        [13.9, 13.1, 13.0],
        [_verdict(True, 1), _verdict(False, 2)],
        submit_on=(1,),
    )
    assert result.outcome == "improved"
    assert result.panel_rounds == 2 and not result.panel_blocking_open
    # the verdict resumed the SAME session and the revision was re-measured
    assert harness.resumes == [None, "s1"]
    assert len(evaluator.calls) == 3  # baseline + candidate + revised candidate
    assert result.candidate == 13.0
    assert "round 1" in result.panel_transcript and "round 2" in result.panel_transcript


def test_a_backend_that_cannot_resume_drafts_a_blocking_finding(tmp_path: Path) -> None:
    """a no-resume backend declares supports_resume=False: a blocking finding
    on a SUBMITTED claim must DRAFT the verified improvement, never attempt a
    resume the backend can't do that would lose it as a session-error."""
    result, harness, evaluator, _panel = _run_panel_climb(
        tmp_path, [13.9, 13.1], [_verdict(True, 1)], supports_resume=False, submit_on=(1,)
    )
    assert result.outcome == "improved"  # improvement preserved, not session-error
    assert result.panel_blocking_open and result.panel_rounds == 1
    assert harness.resumes == [None]  # NO resume attempted
    assert len(evaluator.calls) == 2  # baseline + candidate only (no revise re-measure)


def test_blocking_on_a_plain_finish_stays_open_for_a_draft_pr(tmp_path: Path) -> None:
    # a finish WITHOUT a submit gets no author loop: blocking findings stay
    # open and the caller drafts the PR for a human (the orchestrator-driven
    # revision policy is retired — the author drives depth via submit)
    result, harness, _evaluator, _panel = _run_panel_climb(
        tmp_path, [13.9, 13.1], [_verdict(True, 1)]
    )
    assert result.outcome == "improved"  # still credited; the PR will be a DRAFT
    assert result.panel_blocking_open and result.panel_rounds == 1
    assert harness.resumes == [None]  # NO resume without a submit


def test_revision_that_loses_the_improvement_is_a_named_negative(tmp_path: Path) -> None:
    # submit -> credited -> blocking -> the author revises... and the revision
    # regresses. The gate ends it with the panel-context note. (The author is
    # NOT resumed again: its submit was consumed; the regressed re-measure is
    # a plain finish.)
    result, _h, _e, _p = _run_panel_climb(
        tmp_path, [13.9, 13.1, 14.5], [_verdict(True, 1)], submit_on=(1,)
    )
    assert result.outcome == "no-improvement"
    assert "lost the improvement" in result.note
    assert result.panel_rounds == 1


def test_panel_pr_body_carries_banner_and_transcript(tmp_path: Path) -> None:
    result, _h, _e, _p = _run_panel_climb(tmp_path, [13.9, 13.1], [_verdict(True, 1)])
    body = pr_body(result, CONFIG, redact_secrets=())
    assert body.startswith("> **Draft")
    assert "## Pre-PR verification" in body
    assert "Verification round 1" in body


def test_degraded_final_read_marks_the_result_and_skips_the_wake(tmp_path: Path) -> None:
    from autoresearch.panel import PanelVerdict

    degraded = PanelVerdict(
        blocking=(),
        transcript="**Verification round 1**\n- judge: no verdict",
        wake_text="",
        degraded=True,
    )
    result, harness, _e, _p = _run_panel_climb(tmp_path, [13.9, 13.1], [degraded])
    assert result.outcome == "improved"
    assert result.panel_degraded and not result.panel_blocking_open
    assert len(harness.resumes) == 1  # nothing woke: an outage is not the author's to fix
    body = pr_body(result, CONFIG, redact_secrets=())
    assert body.startswith("> **Draft") and "degraded" in body


def test_wake_without_a_session_id_fails_closed_to_draft(tmp_path: Path) -> None:
    class _NoIdHarness(_SeqHarness):
        def run(self, brief_text, workspace, resume_session_id=None):
            session = super().run(brief_text, workspace, resume_session_id)
            return SessionResult(**{**session.__dict__, "session_id": ""})

    harness = _NoIdHarness(["report r1"])
    evaluator = FakeEvaluator(values=[13.9, 13.1])
    panel = _QueuedPanel([_verdict(True, 1)])
    measurer, snapshot = _wire(evaluator, tmp_path)
    result = attempt_once(
        CONFIG,
        CONTRACT,
        tmp_path,
        harness,
        measurer,
        "base",
        snapshot,
        ruler="r",
        changed_paths=lambda: ["src/pilot/solvers/tsp.py"],
        created="t",
        panel_runner=panel,
    )
    assert result.outcome == "improved"
    assert result.panel_blocking_open  # draft path, not a blind fresh session
    assert len(harness.resumes) == 1  # no wake attempted


FLOOR_CONTRACT = CONTRACT.replace(
    "    direction: min\n",
    "    direction: min\n    min_delta: 0.5\n",
    1,
)


def test_gate_enforces_the_contracts_declared_floor(tmp_path: Path) -> None:
    """The yolo#16 class: a delta that clears the relative default but sits
    inside the contract's declared min_delta must end no-improvement — the
    gate speaks the contract's significance language, not only the global
    relative default. (Same-seed pairing removes pool noise, not training
    stochasticity — which is what a declared floor is calibrated to.)"""
    # 13.876 -> 13.5: 2.7% relative (clears the 0.5% default) but delta
    # 0.376 < the declared 0.5 floor
    result, _, _ = run_climb(tmp_path, [13.876, 13.5], contract=FLOOR_CONTRACT)
    assert result.outcome == "no-improvement"
    assert "significance" in result.note and "floor" in result.note


def test_gate_publishes_a_floor_clearing_delta(tmp_path: Path) -> None:
    result, _, _ = run_climb(tmp_path, [13.876, 13.0], contract=FLOOR_CONTRACT)
    assert result.outcome == "improved"


def test_gate_rejects_an_exact_floor_delta(tmp_path: Path) -> None:
    # boundary matches clears_min_delta: STRICTLY greater publishes — a
    # delta exactly at the floor is the noise the floor models (terra #169)
    result, _, _ = run_climb(tmp_path, [13.876, 13.376], contract=FLOOR_CONTRACT)
    assert result.outcome == "no-improvement"
