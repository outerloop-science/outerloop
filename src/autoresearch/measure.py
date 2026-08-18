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

import logging
from dataclasses import dataclass
from pathlib import Path

from autoresearch.compute import JobSpec, SlurmCompute
from autoresearch.dispatch import (
    eval_job_spec,
    read_eval_result,
    write_eval_job,
)

log = logging.getLogger(__name__)


class MeasurementPending(Exception):
    """Raised when one or more measures have not completed. Carries the wake
    dependency (the colon-joined job ids: `afterany:<a>:<b>` in one wake job)
    so the caller can park the run as `waiting` on exactly this set."""

    def __init__(self, job_ids: tuple[str, ...]):
        self.job_ids = job_ids
        super().__init__(f"{len(job_ids)} measure(s) pending: {':'.join(job_ids)}")

    def afterany(self) -> str:
        return "afterany:" + ":".join(self.job_ids)


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

    def _done(self, m: Measure) -> bool:
        return (self.run_dir / f"eval-{m.name}" / "exit-code").exists()

    def submit_missing(self, measures: list[Measure]) -> list[str]:
        """Submit every measure whose result is not already on disk. Returns
        the submitted job ids (empty when all are already done)."""
        job_ids: list[str] = []
        for m in measures:
            if self._done(m):
                continue  # a prior pass already dispatched-and-completed this
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
                job_name=f"eval-{m.name}",
                account=self.account,
                partition=self.partition,
                eval_minutes=self.eval_minutes,
            )
            job_ids.append(self.compute.submit(spec))
            log.info(
                "dispatched measure %s (sha %s) as job %s", m.name, m.tree_sha[:12], job_ids[-1]
            )
        return job_ids

    def results(self, measures: list[Measure]) -> dict[str, float]:
        """Every measure's value, or PARK. Submits any missing measures and
        raises MeasurementPending if not all are done; raises EvalError if a
        completed measure failed (the honest-negative path is the caller's to
        interpret from the ending, exactly as an in-job eval failure is)."""
        missing = [m for m in measures if not self._done(m)]
        if missing:
            ids = self.submit_missing(measures)
            # a measure with no id here is one that completed between the
            # done-check and now — re-check on the next wake rather than
            # parking on an empty set
            raise MeasurementPending(tuple(ids) or ("__recheck__",))
        return {m.name: read_eval_result(self.run_dir, m.name, m.metric) for m in measures}
