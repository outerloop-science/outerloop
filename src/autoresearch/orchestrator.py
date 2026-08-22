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
from dataclasses import replace as dc_replace
from pathlib import Path
from secrets import randbits
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from autoresearch.measure import Measure

from autoresearch.brief import BriefInputs, BudgetState, Task, build_brief, render
from autoresearch.contract import (
    Benchmark,
    Contract,
    _fold,
    load_contract,
    normalize_path,
    path_is_forbidden,
)
from autoresearch.harness import Harness, SessionResult, budget_exhausted, outage, redact
from autoresearch.panel import PanelVerdict
from autoresearch.role_runner import run_role
from autoresearch.roles import author_spec
from autoresearch.rolespec import RoleSpec

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


class ClimbParked(Exception):
    """A dispatched climb submitted its measures and must hibernate until they
    finish. `climb_once` raises it (a park is an exceptional exit); the caller,
    which owns the run record and git, persists the fields below as the WAITING
    stage — `afterany` among them, the dependency the wake PR will submit its
    afterany wake job against (this PR only records it; `wake_job_id` stays
    empty until then) — and ends the run's turn, keeping the candidate snapshot
    alive so the wake can read it. `phase` is
    WHICH park: `baseline` (before the session — the wake reruns the session)
    or `candidate` (after it — the wake decides). The caller fills in the
    candidate snapshot ref (which it holds) when writing the stage."""

    def __init__(
        self,
        *,
        phase: str,
        afterany: str,
        base_sha: str,
        seed: int,
        suite_seed: int,
        candidate_sha: str = "",
        session: SessionResult | None = None,
    ):
        self.phase = phase
        self.afterany = afterany
        self.base_sha = base_sha
        self.seed = seed
        self.suite_seed = suite_seed
        self.candidate_sha = candidate_sha
        self.session = session
        super().__init__(f"climb parked at {phase} on {afterany or '(no dep)'}")


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
class SuiteMeasurement:
    """One sibling benchmark's paired measurement from the suite gate."""

    name: str
    baseline: float
    candidate: float
    regressed: bool
    display_digits: int | None = None


@dataclass(frozen=True)
class ClimbResult:
    """What one attempt produced — the raw material of the run report."""

    # improved | no-improvement | session-error | session-budget |
    # session-outage | eval-error | scope-violation | suite-regression
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
    # sibling measurements when the suite gate ran (shared paths touched);
    # empty when the diff was env-specific or no shared paths are declared
    suite: tuple[SuiteMeasurement, ...] = ()
    # the seed every seeded sibling's pair ran under (0 = gate did not run)
    suite_seed: int = 0
    # the pre-PR panel's record: per-round transcript for the PR body, how
    # many reads ran, whether blocking findings were still open at the cap,
    # and whether the FINAL read was degraded (a lens with no verdict, an
    # unsanitizable tree). Either flag means the caller opens a DRAFT PR
    # and never arms auto-merge.
    panel_transcript: str = ""
    panel_rounds: int = 0
    panel_blocking_open: bool = False
    panel_degraded: bool = False

    def report(self, config: ClimbConfig, redact_secrets: tuple[str, ...] = ()) -> str:
        lines = [
            f"# Run report — {config.target} / {config.benchmark}",
            f"Outcome: **{self.outcome}**",
        ]
        if self.baseline is not None:
            lines.append(f"Baseline: {self.baseline}")
        if self.candidate is not None:
            lines.append(f"Candidate: {self.candidate}")
        for row in self.suite:
            verdict = "REGRESSED" if row.regressed else "ok"
            lines.append(f"Suite {row.name}: {row.baseline} -> {row.candidate} ({verdict})")
        if self.panel_rounds:
            if self.panel_blocking_open:
                state = "blocking findings OPEN at the cap"
            elif self.panel_degraded:
                state = "DEGRADED final read (a lens produced no verdict)"
            else:
                state = "clean"
            lines.append(f"Panel: {self.panel_rounds} read(s), {state}")
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


def shared_touched(paths: Sequence[str], contract: Contract) -> list[str]:
    """Changed paths under `scope.shared` — the suite-gate trigger. Runs on
    paths that already passed `out_of_scope`, so unparseable entries are
    simply not shared (they were rejected upstream). Case-folded like the
    forbidden/steward checks: a `Model/` spelling must not dodge the gate."""
    shared = [_fold(normalize_path(entry)) for entry in contract.scope.shared]
    hits = []
    for path in paths:
        try:
            candidate = _fold(normalize_path(path))
        except Exception:
            continue
        if any(candidate == s or s in candidate.parents for s in shared):
            hits.append(path)
    return hits


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


def benchmark_floor(
    prior_best: float, min_delta: float | None, min_delta_rel: float | None
) -> float:
    """The effective absolute cross-seed floor for a comparison against the
    recorded best. The larger of the absolute floor and the relative floor
    scaled to the level, so a benchmark that sets both gets the more
    conservative one. Returns 0.0 when no floor is declared.

    A relative-only floor scales to 0 at a recorded level of 0, which means
    no floor. That is a real limit of a relative floor, not a bug: a metric
    that can sit at 0 should pair min_delta_rel with a small absolute
    min_delta as a backstop. Unbounded metrics that use a relative floor
    (wall-clock timing) do not reach 0."""
    floors = []
    if min_delta:
        floors.append(min_delta)
    if min_delta_rel and math.isfinite(prior_best):
        floors.append(min_delta_rel * abs(prior_best))
    return max(floors) if floors else 0.0


def clears_min_delta(
    prior_best: float,
    candidate: float,
    direction: str,
    min_delta: float | None,
    min_delta_rel: float | None = None,
) -> bool:
    """Cross-seed comparisons on a resampled pool must clear the
    benchmark's noise floor: the recorded best was measured under a
    different seed, so a delta inside the floor is pool luck, not progress.
    Same-seed paired comparisons never call this."""
    if not (min_delta or min_delta_rel):
        return True  # no floor declared
    if not (math.isfinite(prior_best) and math.isfinite(candidate)):
        return False  # a declared floor with non-finite inputs fails closed
    floor = benchmark_floor(prior_best, min_delta, min_delta_rel)
    delta = candidate - prior_best if direction == "max" else prior_best - candidate
    return delta > floor


def suite_regressed(
    baseline: float,
    candidate: float,
    direction: str,
    min_delta: float | None = None,
    min_delta_rel: float | None = None,
) -> bool:
    """Did a sibling benchmark move the WRONG way beyond its own floor?

    Both sides are same-seed paired, so with no floor declared any wrong-way
    move counts (paired noise is ~0 by construction); a declared floor gives
    a stochastic eval its honest tolerance. Non-finite values fail closed —
    an unmeasurable sibling must never read as "no regression"."""
    if not (math.isfinite(baseline) and math.isfinite(candidate)):
        return True
    drop = baseline - candidate if direction == "max" else candidate - baseline
    if drop <= 0:
        return False
    return drop > benchmark_floor(baseline, min_delta, min_delta_rel)


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
    contract: Contract, benchmark_name: str, baseline: float | None, hypothesis: str = ""
) -> Task:
    bench = _benchmark(contract, benchmark_name)
    better = "up" if bench.direction == "max" else "down"
    suite_gated = bool(contract.scope.shared) and len(contract.benchmarks) > 1
    # `baseline` orients the brief only (the last-known score from the ledger);
    # the GATE re-measures both sides after the session, so a missing baseline
    # (a benchmark's first run) just drops the reference number, never the gate.
    versus = f" from {baseline}" if baseline is not None else ""
    against = f" versus {baseline}" if baseline is not None else ""
    return Task(
        hypothesis=hypothesis
        or (
            f"The {bench.name} solver can be improved: study the current "
            f"implementation and the evaluation, form ONE concrete hypothesis "
            f"for why it underperforms, and implement it."
        ),
        benchmark=bench.name,
        expected_effect=f"{bench.metric} {better}{versus}",
        done_criteria=(
            f"`{bench.command}` runs clean and {bench.metric} moves {better}"
            f"{against}; repository tests pass"
            + (
                "; a change touching shared paths is suite-gated — no sibling "
                "benchmark may regress beyond its floor"
                if suite_gated
                else ""
            )
        ),
    )


class Measurer(Protocol):
    """Obtains a climb's measurements. `results` returns every measure's value
    keyed by its name, or — for a dispatched backend — raises
    `MeasurementPending` after submitting the not-yet-done jobs, for the caller
    to park on. Two backends behind this one seam: `LocalMeasurer` (inline
    eval in the caller's existing worktrees, for cheap benchmarks and local
    runs) and `measure.DispatchedMeasurer` (cluster jobs on a fresh checkout of
    each committed sha, for expensive evals); the seam is what lets
    `measure_and_decide` be re-enterable without knowing which."""

    def results(self, measures: list[Measure]) -> dict[str, float]: ...


@dataclass(frozen=True)
class MeasureOK:
    """A credited measurement: the candidate cleared the improvement threshold
    and, when the diff touched shared code, no sibling regressed. The caller's
    panel/PR path proceeds from here."""

    baseline: float
    candidate: float
    suite: tuple[SuiteMeasurement, ...] = ()
    suite_seed: int = 0


def measure_and_decide(
    contract: Contract,
    bench: Benchmark,
    *,
    base_sha: str,
    candidate_sha: str,
    seed: int,
    suite_seed: int,
    measured_paths: Sequence[str],
    measurer: Measurer,
    min_relative_improvement: float,
) -> ClimbResult | MeasureOK:
    """The post-session decision as a PURE function of committed shas and a
    `Measurer` — the re-enterable core a wake reconstructs from the record.

    Measures baseline@base_sha and candidate@candidate_sha (paired on `seed`,
    common random numbers) and, when the diff touches shared code, each
    sibling's paired base/cand (on the sibling's OWN seed var, all sharing the
    single `suite_seed`), then applies the improvement threshold and the suite
    gate. Returns a TERMINAL `ClimbResult` on any stop (scope-violation,
    eval-error, no-improvement, suite-regression), or a `MeasureOK` carrying
    the credited values. No session and no git: the same shas rebuild the same
    plan and read cached results, so a wake re-enters here unchanged. A
    dispatched measurer's `MeasurementPending` propagates untouched, for the
    caller to write the waiting record and park on.

    `suite_seed` is an INPUT, not drawn here: a wake must reproduce the seed the
    first pass used, so the caller draws it once and persists it (the in-job
    path drew it inline, which a resume could not reproduce). And because the
    baseline is a committed sha rather than a live pre-session workspace, the
    gate can always measure its siblings — the old "no pristine baseline
    workspace" fail-closed branch is gone.
    """
    # deferred like contract.py's managed_eval_env import: measure -> dispatch
    # -> orchestrator for the eval primitives, so orchestrator imports measure
    # at call time to keep the module graph acyclic.
    from autoresearch.measure import SiblingSpec, plan_measures

    # Scope BEFORE measurement: an out-of-scope tree is never evaluated,
    # because the out-of-scope edit could be to the ruler itself.
    violations = out_of_scope(list(measured_paths), contract)
    if violations:
        return ClimbResult(
            outcome="scope-violation",
            note=f"out-of-scope paths: {', '.join(sorted(violations)[:10])}",
            run_seed=seed,
        )

    seed_env = bench.seed_env or ""
    siblings = [b for b in contract.benchmarks if b.name != bench.name]
    # Both seed guards fire UP FRONT, before any (expensive) measurement: a
    # seed of 0 — the "no seed recorded" sentinel — would run a pair UNPAIRED,
    # each side drawing its own internal seed, so eval noise could read as
    # improvement. The caller must draw a real seed (and persist it, for the
    # wake to reuse) whenever a benchmark or a sibling declares seed_env; a
    # seeded sibling's suite_seed is a caller-contract precondition even on a
    # diff that won't touch shared code (the next diff might).
    if seed_env and not seed:
        raise ValueError(
            f"benchmark {bench.name!r} declares seed_env {seed_env!r} but seed is 0: "
            "a seeded benchmark needs a drawn seed, or baseline and candidate run unpaired"
        )
    # only when the suite gate is STRUCTURALLY possible: a contract with an
    # empty scope.shared can never trigger it (shared_touched always empty), so
    # a missing suite_seed there is not a misconfiguration — do not over-reject.
    if contract.scope.shared and any(b.seed_env for b in siblings) and not suite_seed:
        raise ValueError(
            "a seeded sibling needs a nonzero suite_seed (drawn once, persisted for the wake); "
            "suite_seed 0 would run the sibling pair unpaired"
        )

    # PHASE 1 — baseline + candidate only. Siblings are NOT measured until the
    # candidate has cleared the threshold: a non-improving candidate must never
    # burn the (expensive) sibling evals, matching climb_once's lazy order.
    try:
        main = measurer.results(
            plan_measures(bench.command, bench.metric, base_sha, candidate_sha, seed_env, seed)
        )
    except EvalError as exc:
        return ClimbResult(outcome="eval-error", note=str(exc), run_seed=seed)

    baseline = main["baseline"]
    candidate = main["candidate"]
    if not improved(baseline, candidate, bench.direction, min_relative_improvement):
        return ClimbResult(
            outcome="no-improvement",
            baseline=baseline,
            candidate=candidate,
            run_seed=seed,
        )

    # PHASE 2 — suite gate, only for a credited candidate whose diff touched
    # shared code. A second measure set (a second park for a dispatched
    # backend): an extra CPU wake, never a wasted GPU sibling eval.
    if not (siblings and shared_touched(measured_paths, contract)):
        return MeasureOK(baseline=baseline, candidate=candidate)

    # every seeded sibling runs its pair under the ONE suite_seed, read through
    # its own seed var (mirrors the in-job gate).
    sib_specs = tuple(
        SiblingSpec(b.name, b.command, b.metric, seed_env=b.seed_env or "", seed=suite_seed)
        for b in siblings
    )
    sib_plan = [
        m
        for m in plan_measures(
            bench.command, bench.metric, base_sha, candidate_sha, seed_env, seed, siblings=sib_specs
        )
        if m.name.startswith("sib-")  # baseline/candidate already measured (phase 1)
    ]
    try:
        vals = measurer.results(sib_plan)
    except EvalError as exc:
        # phase 1 already credited the main pair — carry it into the report,
        # exactly as the old in-job suite-error path did.
        return ClimbResult(
            outcome="eval-error",
            baseline=baseline,
            candidate=candidate,
            note=str(exc),
            run_seed=seed,
        )

    suite_rows: list[SuiteMeasurement] = []
    for b in siblings:
        sib_base = vals[f"sib-{b.name}-base"]
        sib_cand = vals[f"sib-{b.name}-cand"]
        suite_rows.append(
            SuiteMeasurement(
                name=b.name,
                baseline=sib_base,
                candidate=sib_cand,
                regressed=suite_regressed(
                    sib_base, sib_cand, b.direction, b.min_delta, b.min_delta_rel
                ),
                display_digits=b.display_digits,
            )
        )
    suite = tuple(suite_rows)
    regressed = [r for r in suite if r.regressed]
    if regressed:
        named = ", ".join(f"{r.name} {r.baseline} -> {r.candidate}" for r in regressed)
        return ClimbResult(
            outcome="suite-regression",
            baseline=baseline,
            candidate=candidate,
            note=f"shared-path diff regressed sibling benchmark(s): {named}",
            run_seed=seed,
            suite=suite,
            suite_seed=suite_seed,
        )

    return MeasureOK(
        baseline=baseline,
        candidate=candidate,
        suite=suite,
        suite_seed=suite_seed,  # reached only on the suite path
    )


def resume_climb(
    contract: Contract,
    bench: Benchmark,
    *,
    base_sha: str,
    candidate_sha: str,
    seed: int,
    suite_seed: int,
    measured_paths: Sequence[str],
    session: SessionResult,
    measurer: Measurer,
    min_relative_improvement: float,
) -> ClimbResult:
    """Re-enter a parked climb's post-session decision — the WAKE side of a
    dispatched candidate park. The candidate is already committed (its sha is in
    the record), so the session does NOT re-run: its edits are captured in
    `candidate_sha` and it was reconstructed by the caller from the record
    (`session.final_text` is the saved write-up, and its cost/turns the saved
    spend) so the PR body and panel claim need no live session. `measured_paths`
    is the caller's re-derivation of the `base_sha..candidate_sha` diff.

    The measurer reads the cached eval results and this returns the decision, OR
    the decision needs a measure not yet done — the suite pairs after an
    improving candidate, "another round of experiments" — and it re-parks by
    raising `ClimbParked`, exactly as the first pass did.

    This is the seam the depth axis (docs/design/research-loop.md) grows from:
    slice 1 wakes the GRADER with the panel off; waking the AGENT with the
    result — a panel revision, or a deeper experiment round it drives itself —
    reuses this same re-entry and lands next.
    """
    from autoresearch.measure import MeasurementPending

    try:
        outcome = measure_and_decide(
            contract,
            bench,
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            seed=seed,
            suite_seed=suite_seed,
            measured_paths=measured_paths,
            measurer=measurer,
            min_relative_improvement=min_relative_improvement,
        )
    except MeasurementPending as pending:
        # a measure is not done yet (the suite pairs this wake just dispatched):
        # re-park on the new afterany set, same shape as the first candidate park
        raise ClimbParked(
            phase="candidate",
            afterany=pending.afterany(),
            base_sha=base_sha,
            seed=seed,
            suite_seed=suite_seed,
            candidate_sha=candidate_sha,
            session=session,
        ) from None
    if isinstance(outcome, ClimbResult):
        # a terminal measurement outcome (no-improvement / suite-regression /
        # eval-error): carry the reconstructed session, and give a bare
        # no-improvement the same framing the in-job path sets (a clear
        # negative is a success), so a resumed negative does not end note-less.
        note = outcome.note
        if outcome.outcome == "no-improvement":
            note = "a negative result reported clearly is a success"
        return dc_replace(outcome, session=session, note=note)
    # credited: candidate cleared the threshold and no sibling regressed. The
    # caller sets `branch` when it opens the PR.
    return ClimbResult(
        outcome="improved",
        baseline=outcome.baseline,
        candidate=outcome.candidate,
        session=session,
        measured_paths=tuple(measured_paths),
        run_seed=seed,
        suite=outcome.suite,
        suite_seed=outcome.suite_seed,
    )


def climb_once(
    config: ClimbConfig,
    contract_text: str,
    workspace: Path,
    harness: Harness,
    measurer: Measurer,
    base_sha: str,
    snapshot: Callable[[], str],
    ruler: str,
    changed_paths: Callable[[], Sequence[str]],
    lessons: str = "",
    recent_reports: tuple[str, ...] = (),
    created: str = "",
    task_hypothesis: str = "",
    spec: RoleSpec | None = None,
    panel_runner: Callable[[float, float, str], PanelVerdict] | None = None,
    panel_revisions: int = 1,
    brief_baseline: float | None = None,
) -> ClimbResult:
    """One implement→evaluate→verify cycle in an existing clean workspace.

    The caller owns the git side (clone before, diff/commit/push/PR after) —
    same split as the harness: this function owns the science loop only. It
    measures through a `Measurer` over committed shas: `base_sha` is the
    pre-session tree, and `snapshot()` — a caller callback, since snapshotting
    is git — commits the session's current workspace and returns its
    `candidate_sha`. The caller registers each sha's worktree with the measurer
    and owns the snapshot ref lifecycle. `changed_paths` reports every path the
    session touched (the caller wires it to `git add -A` + staged paths); scope
    is enforced on it BEFORE the candidate eval runs.

    The session runs as the author role on the role-runner (`spec` defaults
    to `author_spec`; the caller that built the harness passes its spec so
    manifest and harness agree). Scope enforcement stays HERE, on the
    contract — the spec's scope is the manifest copy, filled from the same
    contract.

    With `panel_runner` (docs/design/orchestrator-verify.md), a credited
    claim is read by the verification panel BEFORE it can become a PR:
    blocking findings wake the same session (data-fenced), the revision is
    re-snapshotted, fully re-measured and re-gated, and the panel re-reads —
    up to `panel_revisions` revisions after the initial read. Blocking findings
    still open at the cap set `panel_blocking_open` (the caller posts a
    DRAFT PR carrying them). The caller supplies the runner because the
    panel's checkouts are git work (this function owns no git).
    """
    contract = load_contract(contract_text, config.target)
    bench = _benchmark(contract, config.benchmark)
    spec = spec or author_spec()
    if not spec.execution.can_execute:
        raise ValueError("climb_once runs an editing role; the spec must allow execution")
    if not spec.scope:
        spec = dc_replace(spec, scope=tuple(contract.scope.allowed))

    # deferred like measure_and_decide's import (measure -> dispatch ->
    # orchestrator for the eval primitives).
    from autoresearch.measure import MeasurementPending

    run_seed = draw_run_seed() if bench.seed_env else 0
    siblings = [b for b in contract.benchmarks if b.name != bench.name]
    # ONE suite_seed for the whole climb, fixed up front so a wake reproduces it
    # (never a re-draw), and only when a seeded sibling could gate. It REUSES the
    # climbed benchmark's run_seed when there is one (as the in-job gate did),
    # drawing a fresh seed only for an unseeded benchmark.
    suite_seed = (
        (run_seed or draw_run_seed())
        if contract.scope.shared and any(b.seed_env for b in siblings)
        else 0
    )

    # The baseline is NOT measured before the session — it is measured by the
    # GATE (`measure_and_decide`, base_sha vs candidate_sha) after the session,
    # so a dispatched climb has ONE park (the candidate), never a pre-session
    # baseline park. `brief_baseline` is the last-known score from the ledger,
    # for orienting the brief only ("improve from ~13.8"); it is None on a
    # benchmark's first run, and the gate re-measures either way.
    baseline: float | None = brief_baseline
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
    role_result = run_role(spec, harness, render(brief), workspace)
    session = role_result.session
    if not role_result.ok:
        # the role-runner's verdict, not just the raw session flag (for a
        # schema-less role they coincide today, but any failure the runner
        # learns to report must not slip through as a clean run).
        # Our caps running out is a budget ending, not a malfunction; the
        # API refusing us is an outage — neither is the run's own failure.
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
            note=role_result.error or session.error_detail or session.stop_reason,
        )

    panel_reads = 0
    panel_sections: list[str] = []
    panel_blocking_open = False
    panel_degraded = False
    candidate: float | None = baseline
    suite: tuple[SuiteMeasurement, ...] = ()
    suite_seed_ran = 0
    measured: tuple[str, ...] = ()
    while True:
        measured = tuple(changed_paths())
        # Scope BEFORE the snapshot: an out-of-scope tree is never snapshotted
        # OR measured — the out-of-scope edit could be to the ruler itself. This
        # early exit keeps the snapshot off a rejected tree; measure_and_decide
        # re-checks as the authoritative gate on every entry (including a wake,
        # which re-enters it directly).
        violations = out_of_scope(list(measured), contract)
        if violations:
            return ClimbResult(
                outcome="scope-violation",
                baseline=baseline,
                session=session,
                note=f"out-of-scope paths: {', '.join(sorted(violations)[:10])}",
                run_seed=run_seed,
                panel_transcript="\n\n".join(panel_sections),
                panel_rounds=panel_reads,
            )
        # Snapshot the session's current output to a committed sha the measurer
        # keys on; the caller (which owns git) registers its worktree and the
        # ref. A revision re-snapshots -> a NEW candidate_sha -> a fresh eval. A
        # snapshot failure is an eval failure (the session ran, the tree just
        # could not be captured), not a climb crash — same as a candidate eval
        # that raises.
        try:
            candidate_sha = snapshot()
        except EvalError as exc:
            return ClimbResult(
                outcome="eval-error",
                baseline=baseline,
                session=session,
                note=f"snapshot: {exc}",
                run_seed=run_seed,
                panel_transcript="\n\n".join(panel_sections),
                panel_rounds=panel_reads,
            )
        try:
            outcome = measure_and_decide(
                contract,
                bench,
                base_sha=base_sha,
                candidate_sha=candidate_sha,
                seed=run_seed,
                suite_seed=suite_seed,
                measured_paths=measured,
                measurer=measurer,
                min_relative_improvement=config.min_relative_improvement,
            )
        except MeasurementPending as pending:
            # PARK 2 (dispatched candidate/suite, after the session): the wake
            # reads the cached results and decides. Carries the candidate sha
            # and the session so the caller persists the snapshot ref (drop at
            # the terminal state) and the resume session id.
            raise ClimbParked(
                phase="candidate",
                afterany=pending.afterany(),
                base_sha=base_sha,
                seed=run_seed,
                suite_seed=suite_seed,
                candidate_sha=candidate_sha,
                session=session,
            ) from None
        if isinstance(outcome, ClimbResult):
            # a terminal measurement outcome (scope-violation / eval-error /
            # no-improvement / suite-regression): add the session + panel
            # context this function owns. The baseline stays whatever the GATE
            # measured (None when it never got that far, e.g. scope-violation),
            # never overwritten with the ledger's brief number.
            note = outcome.note
            if outcome.outcome == "no-improvement":
                note = (
                    "the revision addressing panel findings lost the improvement"
                    if panel_reads
                    else "a negative result reported clearly is a success"
                )
            return dc_replace(
                outcome,
                session=session,
                note=note,
                panel_transcript="\n\n".join(panel_sections),
                panel_rounds=panel_reads,
            )
        # credited: candidate cleared the threshold and no sibling regressed.
        baseline = outcome.baseline
        candidate = outcome.candidate
        suite = outcome.suite
        suite_seed_ran = outcome.suite_seed

        if panel_runner is None:
            break
        # the panel reads the CREDITED claim: improvement + suite gate passed
        panel_reads += 1
        verdict = panel_runner(baseline, candidate, session.final_text)
        panel_sections.append(verdict.transcript)
        # only the FINAL read's degradation matters: an earlier outage that a
        # later clean read supersedes is history, not state
        panel_degraded = verdict.degraded
        if not verdict.blocking:
            # a degraded clean read is NOT a certified pass: no wake (nothing
            # for the author to fix), but the caller drafts the PR
            break
        if not session.session_id or not getattr(harness, "supports_resume", True):
            # cannot resume: no session id, OR a no-resume backend (hermes;
            # claude and codex both resume). A FRESH session seeing only the
            # findings would revise blind, and a resume the backend can't do
            # would lose this verified improvement as a session-error — so fail
            # closed to the draft path (review question, #95 round 1).
            panel_blocking_open = True
            break
        if panel_reads > panel_revisions:
            # capped out with blocking findings still open: an unconverged
            # loop is information the human must see, never suppressed —
            # the caller posts a DRAFT PR with these findings on top
            panel_blocking_open = True
            break
        wake_result = run_role(
            spec,
            harness,
            verdict.wake_text,
            workspace,
            resume_session_id=session.session_id or None,
        )
        session = wake_result.session
        if not wake_result.ok:
            if outage(session):
                kind = "session-outage"
            elif budget_exhausted(session):
                kind = "session-budget"
            else:
                kind = "session-error"
            return ClimbResult(
                outcome=kind,
                baseline=baseline,
                candidate=candidate,
                session=session,
                note=wake_result.error or session.error_detail or session.stop_reason,
                run_seed=run_seed,
                panel_transcript="\n\n".join(panel_sections),
                panel_rounds=panel_reads,
            )
        # loop: the revision is a NEW candidate — re-snapshot, then scope,
        # paired eval, improvement threshold, and the suite gate all re-apply

    return ClimbResult(
        outcome="improved",
        baseline=baseline,
        candidate=candidate,
        session=session,
        branch=f"{config.branch_prefix}/{config.benchmark}",
        measured_paths=measured,
        run_seed=run_seed,
        suite=suite,
        suite_seed=suite_seed_ran,
        panel_transcript="\n\n".join(panel_sections),
        panel_rounds=panel_reads,
        panel_blocking_open=panel_blocking_open,
        panel_degraded=panel_degraded,
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
    suite_lines: list[str] = []
    if result.suite:
        suite_lines = [
            "",
            "Shared code was touched, so every sibling benchmark was re-measured "
            "on both sides (paired seed): none regressed beyond its floor.",
            "",
            "| suite benchmark | baseline | candidate |",
            "| --- | --- | --- |",
        ] + [
            f"| {row.name} | {fmt_metric(row.baseline, row.display_digits)} "
            f"| {fmt_metric(row.candidate, row.display_digits)} |"
            for row in result.suite
        ]
    if result.panel_blocking_open:
        banner = [
            "> **Draft — the verification panel capped out with blocking "
            "findings still open.** They are listed under Pre-PR "
            "verification below; the human decides.",
            "",
        ]
    elif result.panel_degraded:
        banner = [
            "> **Draft — the final panel read was degraded (a lens produced "
            "no verdict).** Not a certified pass; see Pre-PR verification "
            "below.",
            "",
        ]
    else:
        banner = []
    panel_section = (
        ["", "## Pre-PR verification", "", result.panel_transcript[:MAX_REPORT_BODY]]
        if result.panel_transcript
        else []
    )
    body = "\n".join(
        [
            *banner,
            f"Automated improvement attempt on `{config.benchmark}` "
            f"(agent `{config.agent_id}`, one hypothesis per PR).",
            "",
            "| | value |",
            "| --- | --- |",
            f"| baseline ({config.benchmark}) | {fmt_metric(result.baseline, display_digits)} |",
            f"| candidate | {fmt_metric(result.candidate, display_digits)} |",
            *suite_lines,
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
            *panel_section,
        ]
    )
    return redact(body, redact_secrets)
