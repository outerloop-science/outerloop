"""Dispatched measurement: run a climb's evals as their own jobs, park, resume.

Stage B (part 1) of docs/design/dispatcher.md, built on the `dispatch`
primitive. The insight that makes a climb resumable across a process death:
the SESSION cannot be re-run on wake (it already made its edits), but the
MEASURE-AND-DECIDE phase after the candidate is committed is a pure function
of committed trees + contract — so every measurement is cacheable by its
identity and the whole phase re-runs idempotently.

A `DispatchedMeasurer` turns a set of `Measure`s (each a committed tree sha +
the contract command) into: submit every not-yet-done measure as its own
eval job (one afterany wake covers the set), PARK by raising
`MeasurementPending`; on the wake, `results()` reads every job's output from
the run directory — a completed measure returns instantly, so the resumed
phase flows straight through to the decision.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoresearch.compute import Compute, JobSpec, is_terminal
from autoresearch.dispatch import (
    eval_job_spec,
    read_eval_result,
    write_eval_job,
)
from autoresearch.orchestrator import EvalError

log = logging.getLogger(__name__)


class MeasurementPending(Exception):
    """Raised when one or more measures have not completed. Carries the wake
    dependency (the colon-joined job ids: `afterany:<a>:<b>` in one wake job)
    so the caller can park the run as `waiting` on exactly this set."""

    def __init__(self, job_ids: tuple[str, ...]):
        self.job_ids = job_ids
        super().__init__(f"{len(job_ids)} measure(s) pending: {':'.join(job_ids)}")

    def afterany(self) -> str:
        """The Slurm dependency for the wake job, or "" when the pending set
        carries no known ids (a transient query failure) — the caller then
        relies on the tick sweep's deadline instead of an afterany wake."""
        return "afterany:" + ":".join(self.job_ids) if self.job_ids else ""


@dataclass(frozen=True)
class Measure:
    """One eval a climb needs: a COMMITTED tree sha measured by the contract
    command. `name` keys its result file and must be unique within a climb
    (e.g. `baseline`, `candidate`, `sib-tsp-base`). `extra_env` carries the
    paired seed for suite comparisons."""

    name: str
    tree_sha: str
    command: str
    metric: str
    extra_env: tuple[tuple[str, str], ...] = ()
    # the measured BENCHMARK's GPUs — per measure, because a suite gate
    # measures siblings that may need a different lane than the climbed one
    gpus: int = 0

    def env(self) -> dict[str, str]:
        return dict(self.extra_env)


@dataclass(frozen=True)
class SiblingSpec:
    """One suite sibling's resolved measurement facts. Each sibling carries
    its OWN seed variable — a sibling that resamples reads a DIFFERENT env var
    than the climbed benchmark (`sib.seed_env`, not the benchmark's). The
    in-job gate draws ONE `suite_seed` and hands that single value to every
    sibling through its own var, so callers set every sibling's `seed` to that
    one suite_seed; the pair (base and cand) always shares it (common random
    numbers). The per-sibling `seed` field only exists so the var, not the
    value, can differ."""

    name: str
    command: str
    metric: str
    seed_env: str = ""
    seed: int = 0
    gpus: int = 0

    def env(self) -> tuple[tuple[str, str], ...]:
        # inject only a REAL drawn seed: 0 is the ledger's "no seed recorded"
        # sentinel (draw_run_seed returns 1+), so a seeded sibling with an
        # unset (0) seed injects no var rather than a literal "0" that would
        # read as a real seed. Guards key off truthiness, per the convention.
        return ((self.seed_env, str(self.seed)),) if self.seed_env and self.seed else ()


def plan_measures(
    command: str,
    metric: str,
    base_sha: str,
    candidate_sha: str,
    seed_env: str = "",
    seed: int = 0,
    siblings: tuple[SiblingSpec, ...] = (),
    gpus: int = 0,
) -> list[Measure]:
    """The measures a climb needs, as a pure function of its committed shas
    and contract facts — the same inputs a wake process reconstructs from the
    run record, so the plan is identical before and after a park.

    Always: `baseline` @ base_sha and `candidate` @ candidate_sha, paired on
    the same `seed` (common random numbers) when the benchmark resamples.
    For a suite gate, each `SiblingSpec` contributes a paired `sib-<name>-base`
    @ base_sha and `sib-<name>-cand` @ candidate_sha — each on the SIBLING's
    OWN seed_env and seed, exactly the 2N-paired comparison the in-job gate
    computes, now dispatched.
    """
    # inject only a REAL drawn seed (>= 1); seed 0 is the "no seed recorded"
    # sentinel, never a value to run under (see SiblingSpec.env / draw_run_seed).
    env: tuple[tuple[str, str], ...] = ((seed_env, str(seed)),) if seed_env and seed else ()
    plan = [
        Measure("baseline", base_sha, command, metric, env, gpus=gpus),
        Measure("candidate", candidate_sha, command, metric, env, gpus=gpus),
    ]
    for sib in siblings:
        plan.append(
            Measure(
                f"sib-{sib.name}-base", base_sha, sib.command, sib.metric, sib.env(), gpus=sib.gpus
            )
        )
        plan.append(
            Measure(
                f"sib-{sib.name}-cand",
                candidate_sha,
                sib.command,
                sib.metric,
                sib.env(),
                gpus=sib.gpus,
            )
        )
    return plan


def _baseline_cache_path(cache_dir: Path, benchmark: str, base_sha: str) -> Path:
    return cache_dir / f"{benchmark}@{base_sha}.json"


def read_baseline_cache(
    cache_dir: Path, benchmark: str, base_sha: str, *, image: str = "", command: str = ""
) -> dict[str, Any] | None:
    """The cached base-tree measurement for (benchmark, base sha), or None.
    The entry must have been measured under the SAME determinants the
    candidate will be — eval image and contract command — or it is stale
    (terra #178): a comparison across images is not a comparison. A cache
    entry is only ever written from an orchestrator-measured value (below),
    never from anything an author produced."""
    try:
        data = json.loads(_baseline_cache_path(cache_dir, benchmark, base_sha).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "value" not in data:
        return None
    try:
        float(data["value"])
    except (TypeError, ValueError):
        return None
    if data.get("image", "") != image or data.get("command", "") != command:
        return None
    return data


def write_baseline_cache(
    cache_dir: Path,
    benchmark: str,
    base_sha: str,
    *,
    value: float,
    seed: int,
    run_tag: str,
    image: str = "",
    command: str = "",
) -> None:
    """Record an orchestrator-measured baseline for every later attempt on
    this base, with the determinants it was measured under. Atomic (a
    unique tmp per writer + replace): two width slots measuring the same
    base concurrently both land a valid file; last writer wins, and both
    values are real measurements."""
    import os
    import tempfile

    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _baseline_cache_path(cache_dir, benchmark, base_sha)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=cache_dir)
    with os.fdopen(fd, "w") as fh:
        json.dump(
            {
                "value": value,
                "seed": seed,
                "run": run_tag,
                "base_sha": base_sha,
                "image": image,
                "command": command,
            },
            fh,
        )
    Path(tmp_name).replace(path)


@dataclass
class DispatchedMeasurer:
    """Submits and reads a climb's measures as jobs on any `Compute` backend.
    Stateless beyond the compute handle: all durable state is the eval job
    output in the run dir, so a fresh process on wake reads exactly what the
    parked one submitted. On a synchronous backend (`LocalCompute`) every job
    is done by the time it is checked, so nothing parks and `results()` flows
    straight through — the one measurer covers dispatched and local evals."""

    compute: Compute
    run_dir: Path
    repo_root: Path
    image: str
    account: str
    partition: str
    eval_minutes: int
    run_tag: str = "run"  # disambiguates job names across runs on one account
    # the GPU lane; a measure with `gpus > 0` is placed there at dispatch
    # time (per MEASURE — a suite's siblings may differ from the climbed
    # benchmark), everything else on account/partition
    gpu_partition: str = ""
    gpu_account: str = ""
    # where a `baseline: cached` benchmark's base-tree measurements live
    # (target-wide); None = no cache, every gate measures its own baseline
    baseline_cache: Path | None = None

    def _placement(self, m: Measure) -> tuple[str, str]:
        if m.gpus <= 0:
            return self.account, self.partition
        if not self.gpu_partition:
            raise ValueError(
                f"measure {m.name} needs {m.gpus} GPU(s) but no GPU lane is configured "
                "(set AUTORESEARCH_GPU_PARTITION)"
            )
        return self.gpu_account or self.account, self.gpu_partition

    def _det(self, m: Measure) -> str:
        # Everything a measure's RESULT depends on and that can vary across a
        # PARK/RESUME (when a fresh measurer reads this run_dir): the container
        # image, the measure's logical role, the code (tree_sha), and the
        # contract facts it is evaluated under (command, metric, seeded env).
        # A cache key missing any of these would return a value computed under
        # DIFFERENT inputs — e.g. a resume that re-fetched the contract after
        # its command changed, or ran under a rebuilt image, reading the stale
        # pre-change result. (account / walltime don't change a result's value,
        # only whether it completes.) NUL separators keep the parts unambiguous
        # (`a`+`bc` != `ab`+`c`).
        env = "".join(f"\0{k}={v}" for k, v in sorted(m.env().items()))
        return f"{self.image}\0{m.name}\0{m.tree_sha}\0{m.command}\0{m.metric}{env}"

    def _slot(self, m: Measure) -> str:
        # Storage identity = the full determinant, with NOTHING truncated: the
        # eval dir is the durable result cache, so any prefix could alias two
        # distinct measurements into one stale read. Readable role + FULL sha
        # (verbatim, debuggable) + the FULL sha1 hex of the whole determinant
        # (the collision-free disambiguator for the contract facts the sha
        # alone does not pin — command/metric/seed). A re-measure that changes
        # the sha OR any contract input lands in a fresh dir; a resume with
        # identical inputs reuses it. `m.name` stays the caller-facing key
        # (results["candidate"]). A dir has 255 chars to spare (~91 used).
        h = hashlib.sha1(self._det(m).encode()).hexdigest()
        return f"{m.name}-{m.tree_sha}-{h}"

    def _ev(self, m: Measure) -> Path:
        return self.run_dir / f"eval-{self._slot(m)}"

    def _job_name(self, m: Measure) -> str:
        # A LIVENESS HINT, not a durable key: the cluster is asked "is this
        # measure's job live" by name. Slurm caps name length, so this hash is
        # necessarily bounded (16 hex = 64 bits) rather than full-width like the
        # slot. That bound is safe because a job-name collision cannot cause a
        # stale RESULT — results are read from the collision-free slot; the
        # worst case is one measure seeing another's job as "live" and parking
        # instead of dispatching, which the deadline sweep then re-checks. The
        # hash covers the whole determinant (+ run_tag, which disambiguates
        # jobs across runs sharing one Slurm account); the readable prefixes
        # are for a human reading squeue.
        h = hashlib.sha1(f"{self.run_tag}\0{self._det(m)}".encode()).hexdigest()[:16]
        return f"eval-{self.run_tag[:10]}-{m.name[:12]}-{h}"

    def _done(self, m: Measure) -> bool:
        return (self._ev(m) / "exit-code").exists()

    def _marker(self, m: Measure) -> str:
        f = self._ev(m) / "submitted"
        return f.read_text().strip() if f.exists() else ""

    def _dispatch(self, m: Measure) -> str:
        script = write_eval_job(
            self.run_dir,
            self._slot(m),
            repo_root=self.repo_root,
            snapshot_sha=m.tree_sha,
            command=m.command,
            image=self.image,
            extra_env=m.env(),
            gpus=m.gpus,
        )
        account, partition = self._placement(m)
        spec: JobSpec = eval_job_spec(
            script,
            job_name=self._job_name(m),
            account=account,
            partition=partition,
            eval_minutes=self.eval_minutes,
            gpus=m.gpus,
        )
        job_id = self.compute.submit(spec)
        (self._ev(m) / "submitted").write_text(job_id)
        log.info("dispatched measure %s (sha %s) as job %s", m.name, m.tree_sha[:12], job_id)
        return job_id

    def results(self, measures: list[Measure]) -> dict[str, float]:
        """Every measure's value, or PARK / FAIL. The CLUSTER is the source of
        truth for liveness (a job named for the measure, found by squeue),
        never just the local marker — so a submitter that died before writing
        its id cannot cause a duplicate submit. Per not-yet-done measure:
          * a live job with this name -> park on its id (authoritative);
          * a marker but NO live job -> the job ran and vanished without a
            result (SIGKILL / node death / GONE) -> EvalError, never resubmit;
          * no live job and no marker -> never dispatched -> dispatch;
          * squeue unavailable -> park (marker id if any) rather than risk a
            duplicate; the wake set may be empty -> the sweep deadline retries.
        """
        pending: list[str] = []
        blind = False
        for m in measures:
            if self._done(m):
                continue
            try:
                live = self.compute.job_id_for_name(self._job_name(m))
            except Exception:
                # cannot tell if it is running: do NOT dispatch (would risk a
                # duplicate) and do NOT declare it dead — re-check next wake
                marker = self._marker(m)
                if marker:
                    pending.append(marker)
                else:
                    blind = True
                continue
            if live:
                pending.append(live)  # queued or running — the real job id
                continue
            if self._marker(m):
                # was dispatched, not live, no result -> died before result
                raise EvalError(f"measure {m.name}: dispatched job vanished without a result")
            # No marker, not live, no result -> never dispatched -> dispatch.
            # RESIDUAL (bounded, accepted): if a prior process died in the
            # microsecond gap between sbatch returning and _dispatch writing
            # the marker, AND that orphaned job then ran and died without a
            # result, its name is gone from squeue and this redispatches once
            # (never loops — the redispatch writes a marker). Cost is one
            # wasted eval in a triple-failure conjunction; fully closing it
            # needs sacct-by-name over job history, not worth that surface.
            job_id = self._dispatch(m)
            if self._done(m):
                continue  # a synchronous compute finished the job inside submit
            try:
                state = self.compute.status(job_id)
            except Exception:
                state = ""  # status unknown right after submit is normal; park
            if state and is_terminal(state):
                # the job already ENDED without writing a result (a local
                # timeout, an instant cluster failure): parking would wait on
                # a job that will never deliver — fail like a vanished job
                raise EvalError(f"measure {m.name}: job {job_id} ended {state} without a result")
            pending.append(job_id)
        if pending or blind:
            raise MeasurementPending(tuple(pending))
        return {m.name: read_eval_result(self.run_dir, self._slot(m), m.metric) for m in measures}


@dataclass(frozen=True)
class DispatchSettings:
    """The cluster coordinates a dispatched measurer needs, grouped so the
    composition root (the climb CLI) reads them ONCE from its args/env and the
    climb just carries them. The per-run pieces (run dir, snapshot repo, the
    benchmark's eval hint, the run tag) are bound at build time by `measurer`,
    so this stays a static description of WHERE to dispatch, not a live handle
    to one run."""

    compute: Compute
    image: str
    account: str
    partition: str
    # the GPU lane: where jobs of a benchmark with `gpus > 0` go. Empty
    # gpu_partition = this deployment cannot place GPU jobs (the tick refuses
    # to launch such benchmarks); empty gpu_account = same account as CPU jobs.
    gpu_partition: str = ""
    gpu_account: str = ""

    def placement(self, gpus: int) -> tuple[str, str]:
        """(account, partition) for a job needing `gpus` GPUs. Raises when a
        GPU job has no lane — a queue that can never run is worse than a
        loud refusal."""
        if gpus <= 0:
            return self.account, self.partition
        if not self.gpu_partition:
            raise ValueError(
                f"benchmark needs {gpus} GPU(s) but no GPU lane is configured "
                "(set AUTORESEARCH_GPU_PARTITION)"
            )
        return self.gpu_account or self.account, self.gpu_partition

    def measurer(
        self, run_dir: Path, repo_root: Path, eval_minutes: int, run_tag: str
    ) -> DispatchedMeasurer:
        """Bind these coordinates to one run's dispatched measurer. `repo_root`
        is the workspace whose `refs/dispatch/*` snapshots the eval jobs check
        out; `eval_minutes` is the benchmark's contract hint (clamped in the
        job spec). GPUs are per MEASURE (Measure.gpus): the measurer carries
        the lane and places each measure when it dispatches it."""
        return DispatchedMeasurer(
            compute=self.compute,
            run_dir=run_dir,
            repo_root=repo_root,
            image=self.image,
            account=self.account,
            partition=self.partition,
            eval_minutes=eval_minutes,
            run_tag=run_tag,
            gpu_partition=self.gpu_partition,
            gpu_account=self.gpu_account,
            # target-wide, beside the run dirs: every attempt on one base
            # shares its cached baseline measurement (Benchmark.baseline)
            baseline_cache=run_dir.parent / "baselines",
        )
