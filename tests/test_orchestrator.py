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


def run_climb(tmp_path, values, session=None, config=CONFIG, changed=None, **kw):
    harness = FakeHarness(result=session or ok_session())
    evaluator = FakeEvaluator(values=list(values))
    result = climb_once(
        config,
        CONTRACT,
        tmp_path,
        harness,
        evaluator,
        ruler="mean tour length over the frozen pool",
        changed_paths=lambda: changed if changed is not None else ["src/pilot/solvers/tsp.py"],
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
    assert len(evaluator.calls) == 1  # baseline only; doctored tree unmeasured


def test_forbidden_paths_are_scope_violations(tmp_path: Path) -> None:
    result, _, _ = run_climb(tmp_path, [13.876, 1.0], changed=[".github/workflows/ci.yml"])
    assert result.outcome == "scope-violation"


def test_improved_rejects_nonfinite_and_zero_baseline_uses_absolute() -> None:
    assert not improved(float("nan"), 1.0, "max", 0.005)
    assert not improved(1.0, float("inf"), "max", 0.005)
    assert not improved(0.0, 1e-12, "max", 0.005)  # absolute threshold at 0
    assert improved(0.0, 0.01, "max", 0.005)


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
    assert list(scratch.glob("uv-cache-*")) == []  # cleaned up after


def test_subprocess_evaluator_env_is_private_per_eval(tmp_path: Path) -> None:
    """The eval never consumes a workspace venv (no shared mutable state
    with the session, no NFS close-to-open race, no session-authored
    entrypoints in the orchestrator's path): UV_PROJECT_ENVIRONMENT points
    into the eval's own throwaway home, a different path every eval."""
    from autoresearch.orchestrator import SubprocessEvaluator

    command = (
        """printf '{"m": %s}\\n' """
        '''"$(echo "$UV_PROJECT_ENVIRONMENT" | grep -c "eval-home")"'''
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
