"""Orchestrator v1: one climb attempt on one benchmark of one target.

Deliberately narrow (Mengye, 2026-08-06: "choose one benchmark on pilot
instead of launching everything"): `climb_once` runs a single
implement→evaluate→verify→PR cycle for the configured benchmark. Task
selection across benchmarks, the planner, experiment sbatch + wakes, and
notebook reports grow from here — each behind a seam that already exists.

The verification stance is the architecture's: the agent's claim is never
trusted. The orchestrator re-runs the benchmark command itself — baseline at
the pre-session tree, candidate after — and only a direction-consistent,
threshold-clearing delta opens a PR.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from autoresearch.brief import BriefInputs, BudgetState, Task, build_brief, render
from autoresearch.contract import Contract, load_contract
from autoresearch.harness import Harness, SessionResult, redact

log = logging.getLogger(__name__)

EVAL_TIMEOUT_S = 1800
MAX_REPORT_BODY = 20_000


class EvalError(RuntimeError):
    """The benchmark command failed or produced no readable metric."""


class Evaluator(Protocol):
    """Runs a benchmark command in a workspace, returns the metric value."""

    def evaluate(self, workspace: Path, command: str, metric: str) -> float: ...


@dataclass
class SubprocessEvaluator:
    """Runs the contract's benchmark command and reads `metric` from its
    JSON output (the contract requires commands to print their metrics).

    The command executes agent-written code: no network access is assumed to
    be needed, the environment is scrubbed, and the run is bounded.
    """

    timeout_s: int = EVAL_TIMEOUT_S

    def evaluate(self, workspace: Path, command: str, metric: str) -> float:
        import os

        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR") if k in os.environ}
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=workspace,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise EvalError(f"eval timed out after {self.timeout_s}s") from exc
        if completed.returncode != 0:
            raise EvalError(f"eval failed ({completed.returncode}): {completed.stderr[-500:]}")
        value = _metric_from_output(completed.stdout, metric)
        if value is None:
            raise EvalError(f"metric {metric!r} not found in eval output")
        return value


def _metric_from_output(stdout: str, metric: str) -> float | None:
    """Find `metric` in the command's output: last JSON object wins, with a
    `metric: value` line-scan fallback."""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and metric in data:
                try:
                    return float(data[metric])
                except (TypeError, ValueError):
                    return None
    match = re.search(rf"{re.escape(metric)}[\s:=]+(-?\d+(?:\.\d+)?)", stdout)
    return float(match.group(1)) if match else None


@dataclass(frozen=True)
class ClimbConfig:
    target: str  # owner/repo
    benchmark: str  # the ONE benchmark this loop works on
    branch_prefix: str = "feat/auto/agent-01"
    agent_id: str = "agent-01"
    # relative improvement below this is noise, not a PR (ε is contract-
    # configurable later; this is the loop-side floor)
    min_relative_improvement: float = 0.005
    budget: BudgetState = field(default_factory=lambda: BudgetState(0.0, 1))


@dataclass(frozen=True)
class ClimbResult:
    """What one attempt produced — the raw material of the run report."""

    outcome: str  # "improved" | "no-improvement" | "session-error" | "eval-error"
    baseline: float | None = None
    candidate: float | None = None
    branch: str = ""
    session: SessionResult | None = None
    note: str = ""

    def report(self, config: ClimbConfig) -> str:
        lines = [
            f"# Run report — {config.target} / {config.benchmark}",
            f"Outcome: **{self.outcome}**",
        ]
        if self.baseline is not None:
            lines.append(f"Baseline: {self.baseline}")
        if self.candidate is not None:
            lines.append(f"Candidate: {self.candidate}")
        if self.note:
            lines.append(f"Note: {self.note}")
        if self.session is not None:
            lines += [
                f"Session: cost=${self.session.cost_usd:.2f}, "
                f"turns={self.session.num_turns}, stop={self.session.stop_reason}",
                "",
                "## Agent's report",
                self.session.final_text[:MAX_REPORT_BODY],
            ]
        return "\n".join(lines)


def _benchmark(contract: Contract, name: str):
    for bench in contract.benchmarks:
        if bench.name == name:
            return bench
    raise ValueError(
        f"benchmark {name!r} not in contract ({[b.name for b in contract.benchmarks]})"
    )


def improved(baseline: float, candidate: float, direction: str, min_rel: float) -> bool:
    """Direction-aware, threshold-clearing improvement."""
    if baseline == 0:
        return (candidate > 0) if direction == "max" else (candidate < 0)
    rel = (candidate - baseline) / abs(baseline)
    return rel >= min_rel if direction == "max" else rel <= -min_rel


def make_task(contract: Contract, benchmark_name: str, baseline: float) -> Task:
    bench = _benchmark(contract, benchmark_name)
    better = "up" if bench.direction == "max" else "down"
    return Task(
        hypothesis=(
            f"The {bench.name} solver can be improved: study the current "
            f"implementation and the evaluation, form ONE concrete hypothesis "
            f"for why it underperforms, and implement it."
        ),
        benchmark=bench.name,
        expected_effect=f"{bench.metric} {better} from {baseline}",
        done_criteria=(
            f"`{bench.command}` runs clean and {bench.metric} moves {better} "
            f"versus {baseline}; repository tests pass"
        ),
    )


def climb_once(
    config: ClimbConfig,
    contract_text: str,
    workspace: Path,
    harness: Harness,
    evaluator: Evaluator,
    ruler: str,
    lessons: str = "",
    recent_reports: tuple[str, ...] = (),
    created: str = "",
) -> ClimbResult:
    """One implement→evaluate→verify cycle in an existing clean workspace.

    The caller owns the git side (clone before, diff/commit/push/PR after) —
    same split as the harness: this function owns the science loop only.
    """
    contract = load_contract(contract_text, config.target)
    bench = _benchmark(contract, config.benchmark)

    # Baseline from the PRE-session tree — never from a stored number
    # (architecture: "baseline re-run at the merge-base, not a trusted
    # static file").
    try:
        baseline = evaluator.evaluate(workspace, bench.command, bench.metric)
    except EvalError as exc:
        return ClimbResult(outcome="eval-error", note=f"baseline: {exc}")

    task = make_task(contract, config.benchmark, baseline)
    brief = build_brief(
        BriefInputs(
            task=task,
            contract_text=contract_text,
            ruler=ruler,
            lessons=lessons,
            recent_reports=recent_reports,
            budget=config.budget,
        ),
        created=created,
    )
    session = harness.run(render(brief), workspace)
    if session.is_error:
        return ClimbResult(
            outcome="session-error",
            baseline=baseline,
            session=session,
            note=session.stop_reason,
        )

    try:
        candidate = evaluator.evaluate(workspace, bench.command, bench.metric)
    except EvalError as exc:
        return ClimbResult(
            outcome="eval-error", baseline=baseline, session=session, note=f"candidate: {exc}"
        )

    if improved(baseline, candidate, bench.direction, config.min_relative_improvement):
        return ClimbResult(
            outcome="improved",
            baseline=baseline,
            candidate=candidate,
            session=session,
            branch=f"{config.branch_prefix}/{config.benchmark}",
        )
    return ClimbResult(
        outcome="no-improvement",
        baseline=baseline,
        candidate=candidate,
        session=session,
        note="a negative result reported clearly is a success",
    )


def pr_body(result: ClimbResult, config: ClimbConfig, redact_secrets: tuple[str, ...]) -> str:
    """The PR body for an improved run: results table + the agent's report."""
    assert result.baseline is not None and result.candidate is not None
    body = "\n".join(
        [
            f"Automated improvement attempt on `{config.benchmark}` "
            f"(agent `{config.agent_id}`, one hypothesis per PR).",
            "",
            "| | value |",
            "| --- | --- |",
            f"| baseline ({config.benchmark}) | {result.baseline} |",
            f"| candidate | {result.candidate} |",
            "",
            "Both numbers were measured by the orchestrator re-running the "
            "contract's eval command — not taken from the session. CI "
            "re-verifies independently.",
            "",
            "## Research report",
            "",
            (result.session.final_text[:MAX_REPORT_BODY] if result.session else ""),
        ]
    )
    return redact(body, redact_secrets)
