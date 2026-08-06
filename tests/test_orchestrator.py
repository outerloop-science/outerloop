"""The single-benchmark climb, against fakes for every seam."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from autoresearch.harness import FakeHarness, SessionResult
from autoresearch.orchestrator import (
    ClimbConfig,
    EvalError,
    _metric_from_output,
    climb_once,
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

CONFIG = ClimbConfig(target="org/pilot", benchmark="tsp")


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

    def evaluate(self, workspace: Path, command: str, metric: str) -> float:
        self.calls.append((command, metric))
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def run_climb(tmp_path, values, session=None, config=CONFIG, **kw):
    harness = FakeHarness(result=session or ok_session())
    evaluator = FakeEvaluator(values=list(values))
    result = climb_once(
        config,
        CONTRACT,
        tmp_path,
        harness,
        evaluator,
        ruler="mean tour length over the frozen pool",
        created="2026-08-06T00:00:00Z",
        **kw,
    )
    return result, harness, evaluator


def test_improvement_end_to_end(tmp_path: Path) -> None:
    result, harness, evaluator = run_climb(tmp_path, [13.876, 13.10])
    assert result.outcome == "improved"
    assert result.baseline == 13.876
    assert result.candidate == 13.10
    assert result.branch == "feat/auto/agent-01/tsp"
    # the brief carried the real baseline and the contract verbatim
    brief_text = harness.calls[0][0]
    assert "13.876" in brief_text
    assert "mean_tour_length down from 13.876" in brief_text
    assert "src/pilot/solvers/" in brief_text
    # eval ran twice with the contract's command
    assert (
        evaluator.calls
        == [("uv run python -m pilot.eval --benchmark tsp --json", "mean_tour_length")] * 2
    )


def test_direction_min_regression_is_no_improvement(tmp_path: Path) -> None:
    result, _, _ = run_climb(tmp_path, [13.876, 14.5])
    assert result.outcome == "no-improvement"
    assert "negative result" in result.note


def test_noise_below_threshold_is_no_improvement(tmp_path: Path) -> None:
    result, _, _ = run_climb(tmp_path, [100.0, 99.9])  # 0.1% < 0.5% floor
    assert result.outcome == "no-improvement"


def test_session_error_short_circuits_before_second_eval(tmp_path: Path) -> None:
    bad = SessionResult(
        stop_reason="timeout",
        is_error=True,
        cost_usd=0.0,
        num_turns=0,
        session_id="",
        final_text="",
        transcript_path="",
    )
    result, _, evaluator = run_climb(tmp_path, [13.876, 999.0], session=bad)
    assert result.outcome == "session-error"
    assert len(evaluator.calls) == 1  # baseline only
    assert result.baseline == 13.876


def test_baseline_eval_failure_never_starts_a_session(tmp_path: Path) -> None:
    result, harness, _ = run_climb(tmp_path, [EvalError("boom")])
    assert result.outcome == "eval-error"
    assert harness.calls == []  # no session was paid for


def test_candidate_eval_failure_is_eval_error_with_session(tmp_path: Path) -> None:
    """The agent broke the eval — that's a failed run, not an improvement."""
    result, _, _ = run_climb(tmp_path, [13.876, EvalError("crashed")])
    assert result.outcome == "eval-error"
    assert result.session is not None


def test_unknown_benchmark_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not in contract"):
        run_climb(tmp_path, [1.0], config=ClimbConfig(target="org/pilot", benchmark="chess"))


def test_improved_direction_semantics() -> None:
    assert improved(0.25, 0.31, "max", 0.005)
    assert not improved(0.25, 0.2501, "max", 0.005)
    assert improved(13.876, 13.0, "min", 0.005)
    assert not improved(13.876, 14.0, "min", 0.005)
    assert not improved(0.25, 0.24, "max", 0.005)


def test_metric_parsing_json_and_fallback() -> None:
    assert _metric_from_output('{"mean_tour_length": 13.1}', "mean_tour_length") == 13.1
    noisy = 'log line\n{"other": 1}\n{"solve_rate": 0.31, "n": 40}'
    assert _metric_from_output(noisy, "solve_rate") == 0.31
    assert _metric_from_output("solve_rate: 0.28", "solve_rate") == 0.28
    assert _metric_from_output("nothing here", "solve_rate") is None
    assert _metric_from_output('{"solve_rate": "high"}', "solve_rate") is None


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
