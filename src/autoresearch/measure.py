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
import logging
from dataclasses import dataclass
from pathlib import Path

from autoresearch.compute import JobSpec, SlurmCompute
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

    def env(self) -> dict[str, str]:
        return dict(self.extra_env)


@dataclass(frozen=True)
class SiblingSpec:
    """One suite sibling's resolved measurement facts. Each sibling carries
    its OWN seed variable and drawn seed — a sibling that resamples reads a
    DIFFERENT env var than the climbed benchmark, and its pairing seed is its
    own (the in-job suite gate draws one seed per benchmark, not one global
    seed)."""

    name: str
    command: str
    metric: str
    seed_env: str = ""
    seed: int = 0

    def env(self) -> tuple[tuple[str, str], ...]:
        return ((self.seed_env, str(self.seed)),) if self.seed_env else ()


def plan_measures(
    benchmark: str,
    command: str,
    metric: str,
    base_sha: str,
    candidate_sha: str,
    seed_env: str = "",
    seed: int = 0,
    siblings: tuple[SiblingSpec, ...] = (),
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
    env: tuple[tuple[str, str], ...] = ((seed_env, str(seed)),) if seed_env else ()
    plan = [
        Measure("baseline", base_sha, command, metric, env),
        Measure("candidate", candidate_sha, command, metric, env),
    ]
    for sib in siblings:
        plan.append(Measure(f"sib-{sib.name}-base", base_sha, sib.command, sib.metric, sib.env()))
        plan.append(
            Measure(f"sib-{sib.name}-cand", candidate_sha, sib.command, sib.metric, sib.env())
        )
    return plan


@dataclass
class DispatchedMeasurer:
    """Submits and reads a climb's dispatched measures. Stateless beyond the
    Slurm handle: all durable state is the eval job output in the run dir, so
    a fresh process on wake reads exactly what the parked one submitted."""

    compute: SlurmCompute
    run_dir: Path
    repo_root: Path
    image: str
    account: str
    partition: str
    eval_minutes: int
    run_tag: str = "run"  # disambiguates job names across runs on one account

    def _ev(self, m: Measure) -> Path:
        return self.run_dir / f"eval-{m.name}"

    def _job_name(self, m: Measure) -> str:
        # deterministic + unique per (run, measure): the CLUSTER can then be
        # asked "is this measure's job live" by name, independent of any local
        # marker. The hash covers the FULL (run_tag, measure name) identity —
        # so no truncation of either the run tag OR the name (both readable
        # prefixes only, for a human reading squeue) can collide two distinct
        # jobs. The NUL separator keeps `a`+`bc` distinct from `ab`+`c`.
        h = hashlib.sha1(f"{self.run_tag}\0{m.name}".encode()).hexdigest()[:12]
        return f"eval-{self.run_tag[:12]}-{m.name[:12]}-{h}"

    def _done(self, m: Measure) -> bool:
        return (self._ev(m) / "exit-code").exists()

    def _marker(self, m: Measure) -> str:
        f = self._ev(m) / "submitted"
        return f.read_text().strip() if f.exists() else ""

    def _dispatch(self, m: Measure) -> str:
        script = write_eval_job(
            self.run_dir,
            m.name,
            repo_root=self.repo_root,
            snapshot_sha=m.tree_sha,
            command=m.command,
            image=self.image,
            extra_env=m.env(),
        )
        spec: JobSpec = eval_job_spec(
            script,
            job_name=self._job_name(m),
            account=self.account,
            partition=self.partition,
            eval_minutes=self.eval_minutes,
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
            pending.append(self._dispatch(m))
        if pending or blind:
            raise MeasurementPending(tuple(pending))
        return {m.name: read_eval_result(self.run_dir, m.name, m.metric) for m in measures}
