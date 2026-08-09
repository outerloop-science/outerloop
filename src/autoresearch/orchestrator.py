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
from secrets import randbits
from typing import Protocol

from autoresearch.brief import BriefInputs, BudgetState, Task, build_brief, render
from autoresearch.contract import (
    Contract,
    _fold,
    load_contract,
    normalize_path,
    path_is_forbidden,
)
from autoresearch.harness import Harness, SessionResult, budget_exhausted, outage, redact

log = logging.getLogger(__name__)

EVAL_TIMEOUT_S = 1800
MAX_REPORT_BODY = 20_000


# Environment keys the evaluator manages itself; a contract's seed_env may
# never name one (validated at load; filtered again at injection).
PROTECTED_EVAL_ENV = frozenset(
    {
        "HOME",
        "PATH",
        "TMPDIR",
        "LANG",
        "VIRTUAL_ENV",
        # interpreter/loader steering: a random-integer value cannot carry a
        # payload, but a contract naming one of these would silently break
        # every eval in a way that reads as measurement failure
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
    }
)
# ...and whole families: any UV_* steers uv's env/cache resolution, and any
# APPTAINERENV_* is translated into the CONTAINER's environment by apptainer
# (APPTAINERENV_HOME becomes HOME inside), so exact-name checks cannot
# enumerate them (review finding).
# APPTAINER_* configures the HOST-side apptainer CLI (bind paths, home,
# containment) — same family logic, different side of the boundary.
# LD_/DYLD_ steer the dynamic loader; GIT_ redirects repo resolution.
# (PYTHONHASHSEED stays allowed — it IS a seed, and a legitimate seed_env.)
PROTECTED_ENV_PREFIXES = ("UV_", "APPTAINERENV_", "APPTAINER_", "LD_", "DYLD_", "GIT_")


def managed_eval_env(name: str) -> bool:
    """True when injecting `name` could disturb the eval's own isolation."""
    return name in PROTECTED_EVAL_ENV or name.startswith(PROTECTED_ENV_PREFIXES)


class EvalError(RuntimeError):
    """The benchmark command failed or produced no readable metric."""


class Evaluator(Protocol):
    """Runs a benchmark command in a workspace, returns the metric value."""

    def evaluate(
        self, workspace: Path, command: str, metric: str, extra_env: dict[str, str] | None = None
    ) -> float: ...


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

    def evaluate(
        self, workspace: Path, command: str, metric: str, extra_env: dict[str, str] | None = None
    ) -> float:

        # Throwaway HOME OUTSIDE the clone: never the orchestrator's real home
        # (it shelters the PAT), and never the workspace — eval cache/state
        # artifacts must not masquerade as agent edits in the diff. The
        # CONTAINED eval needs it too (--home): apptainer's tmpfs home is
        # size-capped and uv blows it extracting wheels (seen live: first
        # climb died at baseline on a full tmpfs).
        # Fresh per-EVAL home (never reused): baseline and candidate cannot
        # see each other's writes, and nothing survives to any later run.
        import os
        import tempfile

        try:
            eval_home = Path(
                tempfile.mkdtemp(
                    prefix=f"{workspace.name}-eval-home-", dir=workspace.resolve().parent
                )
            )
        except OSError as exc:
            raise EvalError(f"could not create eval home: {exc}") from exc
        # per-EVAL cache on node-local scratch: local IO (NFS caches flake),
        # no state crossing evals (agent code runs during the candidate eval
        # and must not poison later baselines), and only THIS directory is
        # bound into the container — never the whole host /tmp.
        try:
            cache_dir = Path(
                tempfile.mkdtemp(prefix="uv-cache-", dir=os.environ.get("TMPDIR", "/tmp"))
            )
        except OSError as exc:
            import shutil

            shutil.rmtree(eval_home, ignore_errors=True)
            raise EvalError(f"could not create eval cache dir: {exc}") from exc
        try:
            return self._measure(workspace, command, metric, eval_home, cache_dir, extra_env)
        finally:
            # bounded disk: each eval's home AND cache die with it
            # (re-downloading wheels per eval is the accepted isolation cost)
            import shutil

            shutil.rmtree(eval_home, ignore_errors=True)
            shutil.rmtree(cache_dir, ignore_errors=True)

    def _measure(
        self,
        workspace: Path,
        command: str,
        metric: str,
        eval_home: Path,
        cache_dir: Path,
        extra_env: dict[str, str] | None = None,
    ) -> float:
        return self._parse_measured(
            self._run(workspace, command, eval_home, cache_dir, extra_env), metric
        )

    def _run(
        self,
        workspace: Path,
        command: str,
        eval_home: Path,
        cache_dir: Path,
        extra_env: dict[str, str] | None = None,
    ) -> str:
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
                # node-local scratch for uv's cache: the container's own /tmp
                # is a size-capped tmpfs, and shared-FS caches flake (NFS)
                "--bind",
                f"{cache_dir}:{cache_dir}",
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
        # uv's cache does heavy small-file IO; on shared filesystems (NFS)
        # that flakes with stale-handle/copy errors (seen live twice). Keep
        # the cache on node-local scratch and copy across filesystems.
        env["UV_CACHE_DIR"] = str(cache_dir)
        env["UV_LINK_MODE"] = "copy"
        # PRIVATE project env per eval (maintainer decision 2026-08-09:
        # "either not share, or have a lock" — we don't share): the session
        # builds ws/.venv for its own use, and a second process consuming a
        # venv another process just wrote races NFS close-to-open
        # consistency (seen live: the first steward validation spawned a
        # pytest whose binary was not yet visible). The eval builds its own
        # environment from the LOCKFILE on NODE-LOCAL scratch (beside the
        # uv cache: fast IO, zero NFS in the venv path, dies with the
        # eval) — no shared mutable state, and the orchestrator never
        # executes session-authored entrypoints.
        env["UV_PROJECT_ENVIRONMENT"] = str(cache_dir / "venv")
        if self.container_image:
            # --cleanenv drops the host env; APPTAINERENV_* survives it
            env["APPTAINERENV_UV_CACHE_DIR"] = env["UV_CACHE_DIR"]
            env["APPTAINERENV_UV_LINK_MODE"] = "copy"
            env["APPTAINERENV_UV_PROJECT_ENVIRONMENT"] = env["UV_PROJECT_ENVIRONMENT"]
        env["HOME"] = str(eval_home)
        if extra_env:
            # explicit injections only (the base env is a scrubbed
            # allowlist): today this carries the benchmark's run seed.
            # Managed keys are dropped, never overwritten — the contract
            # validator already rejects them, this is defense in depth
            # (an injected HOME/UV_* would defeat per-eval isolation)
            for key, value in extra_env.items():
                if managed_eval_env(key):
                    log.warning("refusing extra_env override of managed %s", key)
                    continue
                env[key] = value
                if self.container_image:
                    env[f"APPTAINERENV_{key}"] = value
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
        return stdout

    def check(self, workspace: Path, command: str) -> None:
        """Run `command` with eval-grade containment, requiring only exit 0.

        The steward's validation suite (pytest, per-benchmark smoke runs)
        executes STEWARD-written env code — same trust level as agent
        code, same containment, no metric parsed."""
        import shutil
        import tempfile

        try:
            eval_home = Path(
                tempfile.mkdtemp(
                    prefix=f"{workspace.name}-check-home-", dir=workspace.resolve().parent
                )
            )
        except OSError as exc:
            raise EvalError(f"could not create check home: {exc}") from exc
        cache_dir = Path(tempfile.mkdtemp(prefix="autoresearch-check-cache-"))
        try:
            self._run(workspace, command, eval_home, cache_dir)
        finally:
            shutil.rmtree(eval_home, ignore_errors=True)
            shutil.rmtree(cache_dir, ignore_errors=True)

    def _parse_measured(self, stdout: str, metric: str) -> float:
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
    # Commits are AUTHORED as the bot account (a real GitHub identity):
    # a bare "agent-01" noreply address links to whoever owns that login.
    # The agent id lives in a commit trailer instead.
    bot_login: str = "agentic-learning-bot"
    # relative improvement below this is noise, not a PR (ε is contract-
    # configurable later; this is the loop-side floor)
    min_relative_improvement: float = 0.005
    budget: BudgetState = field(default_factory=lambda: BudgetState(0.0, 1))


@dataclass(frozen=True)
class ClimbResult:
    """What one attempt produced — the raw material of the run report."""

    # improved | no-improvement | session-error | session-budget |
    # session-outage | eval-error | scope-violation
    outcome: str
    baseline: float | None = None
    candidate: float | None = None
    branch: str = ""
    # the exact paths that were scope-checked and then measured — the caller
    # must refuse to commit anything beyond this set
    measured_paths: tuple[str, ...] = ()
    session: SessionResult | None = None
    note: str = ""
    # the seed both measurements ran under (0 = benchmark has no seed_env):
    # recorded in the ledger row so the number is re-derivable
    run_seed: int = 0

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


def steward_out_of_scope(paths: Sequence[str], contract: Contract) -> list[str]:
    """Changed paths the STEWARD may not touch.

    The inversion of `out_of_scope`: the steward edits the env/ruler
    territory (`contract.steward.allowed`) and may NEVER touch the solver's
    territory (`contract.scope.allowed`) — the roles' separation is what
    makes verifier-checked stewardship trustworthy. The always-forbidden
    set (contract, `.github/`, roadmap) binds here too. No steward section
    in the contract means everything is out of scope.
    """
    if contract.steward is None:
        return list(paths)
    allowed = [_fold(normalize_path(entry)) for entry in contract.steward.allowed]
    solver = [_fold(normalize_path(entry)) for entry in contract.scope.allowed]
    violations = []
    for path in paths:
        if path_is_forbidden(path, contract):
            violations.append(path)
            continue
        try:
            candidate = _fold(normalize_path(path))
        except Exception:
            violations.append(path)
            continue
        # case-folded both directions, like path_is_forbidden: on a
        # case-insensitive checkout, Solvers/ IS solvers/
        if any(candidate == sp or sp in candidate.parents for sp in solver):
            violations.append(path)  # solver territory: never the steward's
            continue
        if not any(candidate == a or a in candidate.parents for a in allowed):
            violations.append(path)
    return violations


def draw_run_seed() -> int:
    """A fresh measurement seed, never 0 — zero is the ledger's "no seed
    recorded" sentinel, and the injection guards key off truthiness."""
    return 1 + randbits(30)


def clears_min_delta(
    prior_best: float, candidate: float, direction: str, min_delta: float | None
) -> bool:
    """Cross-seed comparisons on a resampled pool must clear the
    benchmark's ABSOLUTE noise floor: the recorded best was measured under
    a different seed, so a delta inside the floor is pool luck, not
    progress. Same-seed paired comparisons never call this."""
    if not min_delta:
        return True
    if not (math.isfinite(prior_best) and math.isfinite(candidate)):
        return False
    delta = candidate - prior_best if direction == "max" else prior_best - candidate
    return delta > min_delta


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


def make_task(
    contract: Contract, benchmark_name: str, baseline: float, hypothesis: str = ""
) -> Task:
    bench = _benchmark(contract, benchmark_name)
    better = "up" if bench.direction == "max" else "down"
    return Task(
        hypothesis=hypothesis
        or (
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
    task_hypothesis: str = "",
    baseline_workspace: Path | None = None,
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
        run_seed = draw_run_seed() if bench.seed_env else 0
        seed_env = {bench.seed_env: str(run_seed)} if bench.seed_env else None
        # measured in the caller's pristine snapshot when one is given: any
        # artifact the eval persists (a seed file, sampled instances) lands
        # OUTSIDE the workspace the solver session sees next, so a pinned
        # seed cannot become pool foreknowledge (round-4 review finding)
        baseline = evaluator.evaluate(
            baseline_workspace or workspace, bench.command, bench.metric, extra_env=seed_env
        )
    except EvalError as exc:
        return ClimbResult(outcome="eval-error", note=f"baseline: {exc}")

    task = make_task(contract, config.benchmark, baseline, hypothesis=task_hypothesis)
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
        # our caps running out is a budget ending, not a malfunction; the
        # API refusing us is an outage — neither is the run's own failure
        if outage(session):
            kind = "session-outage"
        elif budget_exhausted(session):
            kind = "session-budget"
        else:
            kind = "session-error"
        return ClimbResult(
            outcome=kind,
            baseline=baseline,
            session=session,
            note=session.error_detail or session.stop_reason,
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
        # SAME seed as the baseline: paired measurement (common random
        # numbers) — the improvement claim compares like against like even
        # on a resampled pool, and the seed is fresh per climb so nothing
        # about the pool was knowable when the solver wrote its code
        candidate = evaluator.evaluate(workspace, bench.command, bench.metric, extra_env=seed_env)
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
            run_seed=run_seed,
        )
    return ClimbResult(
        outcome="no-improvement",
        baseline=baseline,
        candidate=candidate,
        session=session,
        note="a negative result reported clearly is a success",
        run_seed=run_seed,
    )


def pr_body(
    result: ClimbResult,
    config: ClimbConfig,
    redact_secrets: tuple[str, ...],
    display_digits: int | None = None,
) -> str:
    """The PR body for an improved run: results table + the agent's report.

    Human surfaces render at the benchmark's conventional precision
    (maintainer decision 2026-08-09); full precision lives only in
    results/leader.json, and every comparison runs on full floats.
    """
    from autoresearch.progress import fmt_metric

    if result.outcome != "improved" or result.baseline is None or result.candidate is None:
        raise ValueError("pr_body requires an improved result with both measurements")
    body = "\n".join(
        [
            f"Automated improvement attempt on `{config.benchmark}` "
            f"(agent `{config.agent_id}`, one hypothesis per PR).",
            "",
            "| | value |",
            "| --- | --- |",
            f"| baseline ({config.benchmark}) | {fmt_metric(result.baseline, display_digits)} |",
            f"| candidate | {fmt_metric(result.candidate, display_digits)} |",
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
