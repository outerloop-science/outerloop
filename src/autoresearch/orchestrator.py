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
import math
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from autoresearch.brief import BriefInputs, BudgetState, Task, build_brief, render
from autoresearch.contract import Contract, load_contract, normalize_path, path_is_forbidden
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

    The command executes AGENT-WRITTEN code — it is session-grade untrusted
    execution and gets session-grade containment: with `container_image` set
    (the production configuration), the command runs under `apptainer exec
    --containall` seeing only the workspace, a throwaway tmpfs HOME, and no
    host environment. Uncontained mode exists for tests and non-cluster dev,
    with a scrubbed env that NEVER includes the real HOME (the orchestrator
    account holds the bot PAT under it)."""

    timeout_s: int = EVAL_TIMEOUT_S
    container_image: str = ""
    apptainer_binary: str = "apptainer"

    def evaluate(self, workspace: Path, command: str, metric: str) -> float:

        # Throwaway HOME OUTSIDE the clone: never the orchestrator's real home
        # (it shelters the PAT), and never the workspace — eval cache/state
        # artifacts must not masquerade as agent edits in the diff. The
        # CONTAINED eval needs it too (--home): apptainer's tmpfs home is
        # size-capped and uv blows it extracting wheels (seen live: first
        # climb died at baseline on a full tmpfs).
        # Fresh per-EVAL home (never reused): baseline and candidate cannot
        # see each other's writes, and nothing survives to any later run.
        import tempfile

        try:
            eval_home = Path(
                tempfile.mkdtemp(
                    prefix=f"{workspace.name}-eval-home-", dir=workspace.resolve().parent
                )
            )
        except OSError as exc:
            raise EvalError(f"could not create eval home: {exc}") from exc
        try:
            return self._measure(workspace, command, metric, eval_home)
        finally:
            # bounded disk: each eval's cache dies with it (re-downloading
            # wheels per eval is the accepted cost of eval isolation)
            import shutil

            shutil.rmtree(eval_home, ignore_errors=True)

    def _measure(self, workspace: Path, command: str, metric: str, eval_home: Path) -> float:
        import os
        import signal

        if self.container_image:
            argv = [
                self.apptainer_binary,
                "exec",
                "--containall",
                "--cleanenv",
                "--bind",
                f"{workspace}:{workspace}",
                "--home",
                f"{eval_home}:{eval_home}",
                "--pwd",
                str(workspace),
                self.container_image,
                "sh",
                "-c",
                command,
            ]
        else:
            argv = ["sh", "-c", command]
        env = {k: os.environ[k] for k in ("PATH", "LANG", "TMPDIR") if k in os.environ}
        env["HOME"] = str(eval_home)
        try:
            # process group, like the harness: a timed-out eval must not
            # leave orphans mutating a workspace that later gets committed
            process = subprocess.Popen(
                argv,
                cwd=workspace,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise EvalError(f"eval could not start: {exc}") from exc
        try:
            stdout, stderr = process.communicate(timeout=self.timeout_s)
        except subprocess.TimeoutExpired as exc:
            import contextlib

            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.communicate(timeout=5)
            raise EvalError(f"eval timed out after {self.timeout_s}s") from exc
        if process.returncode != 0:
            raise EvalError(f"eval failed ({process.returncode}): {stderr[-500:]}")
        value = _metric_from_output(stdout, metric)
        if value is None:
            raise EvalError(f"metric {metric!r} not found in eval output")
        if not math.isfinite(value):
            raise EvalError(f"metric {metric!r} is not finite: {value}")
        return value


def _metric_from_output(stdout: str, metric: str) -> float | None:
    """The metric from the LAST single-line JSON object that carries it.

    No regex fallback: a fuzzy match that reads the wrong number (a progress
    line, a prefixed metric name) is worse than a clean failure — the
    contract requires eval commands to print their metrics as JSON."""
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
            # the {"metric": <name>, "value": <v>} shape (what the pilot's
            # eval actually prints)
            if isinstance(data, dict) and data.get("metric") == metric and "value" in data:
                try:
                    return float(data["value"])
                except (TypeError, ValueError):
                    return None
    return None


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

    outcome: str  # improved | no-improvement | session-error | eval-error | scope-violation
    baseline: float | None = None
    candidate: float | None = None
    branch: str = ""
    # the exact paths that were scope-checked and then measured — the caller
    # must refuse to commit anything beyond this set
    measured_paths: tuple[str, ...] = ()
    session: SessionResult | None = None
    note: str = ""

    def report(self, config: ClimbConfig, redact_secrets: tuple[str, ...] = ()) -> str:
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
                # redact BEFORE truncating: a secret straddling the cut would
                # otherwise survive as an unmatchable prefix
                redact(self.session.final_text, redact_secrets)[:MAX_REPORT_BODY],
            ]
        return redact("\n".join(lines), redact_secrets)


def _benchmark(contract: Contract, name: str):
    for bench in contract.benchmarks:
        if bench.name == name:
            return bench
    raise ValueError(
        f"benchmark {name!r} not in contract ({[b.name for b in contract.benchmarks]})"
    )


def out_of_scope(paths: Sequence[str], contract: Contract) -> list[str]:
    """Changed paths the contract does not allow the agent to touch.

    Checked BEFORE the candidate eval: an out-of-scope edit could be to the
    eval harness itself, and measuring a doctored ruler would turn "CI
    re-verifies independently" into re-running the fraud."""
    allowed = [normalize_path(entry) for entry in contract.scope.allowed]
    violations = []
    for path in paths:
        if path_is_forbidden(path, contract):
            violations.append(path)
            continue
        try:
            candidate = normalize_path(path)
        except Exception:
            violations.append(path)
            continue
        if not any(candidate == a or a in candidate.parents for a in allowed):
            violations.append(path)
    return violations


def improved(baseline: float, candidate: float, direction: str, min_rel: float) -> bool:
    """Direction-aware, threshold-clearing improvement. Non-finite values
    never count (the evaluator rejects them; this is defense in depth)."""
    if not (math.isfinite(baseline) and math.isfinite(candidate)):
        return False
    if baseline == 0:
        # no relative scale exists: apply the threshold absolutely
        return candidate >= min_rel if direction == "max" else candidate <= -min_rel
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
    changed_paths: Callable[[], Sequence[str]],
    lessons: str = "",
    recent_reports: tuple[str, ...] = (),
    created: str = "",
) -> ClimbResult:
    """One implement→evaluate→verify cycle in an existing clean workspace.

    The caller owns the git side (clone before, diff/commit/push/PR after) —
    same split as the harness: this function owns the science loop only.
    `changed_paths` reports every path the session touched (the caller wires
    it to `git add -A` + staged paths); scope is enforced on it BEFORE the
    candidate eval runs.
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

    # Scope BEFORE measurement: an out-of-scope tree is never evaluated,
    # because the out-of-scope edit could be to the ruler itself.
    measured = tuple(changed_paths())
    violations = out_of_scope(list(measured), load_contract(contract_text, config.target))
    if violations:
        return ClimbResult(
            outcome="scope-violation",
            baseline=baseline,
            session=session,
            note=f"out-of-scope paths: {', '.join(sorted(violations)[:10])}",
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
            measured_paths=measured,
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
    if result.outcome != "improved" or result.baseline is None or result.candidate is None:
        raise ValueError("pr_body requires an improved result with both measurements")
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
            (
                redact(result.session.final_text, redact_secrets)[:MAX_REPORT_BODY]
                if result.session
                else ""
            ),
        ]
    )
    return redact(body, redact_secrets)
