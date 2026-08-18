"""The dispatched measurer: submit-park-resume over committed measures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoresearch.compute import CommandResult, SlurmCompute
from autoresearch.measure import (
    DispatchedMeasurer,
    Measure,
    MeasurementPending,
)
from autoresearch.orchestrator import EvalError


def _measurer(tmp_path: Path, submitted: list, job_state: str = "RUNNING") -> DispatchedMeasurer:
    def runner(argv, timeout_s):
        if argv and argv[0] == "sbatch":
            submitted.append(list(argv))
            return CommandResult(0, f"{100 + len(submitted)}\n", "")
        # sacct status query for a dispatched job
        return CommandResult(0, f"{job_state}\n", "")

    return DispatchedMeasurer(
        compute=SlurmCompute(runner=runner),
        run_dir=tmp_path,
        repo_root=tmp_path / "repo",
        image="/img.sif",
        account="a",
        partition="p",
        eval_minutes=60,
    )


def _land(tmp_path: Path, name: str, value=None, code="0", job="101"):
    """Simulate a dispatched eval job's output in the run dir."""
    ev = tmp_path / f"eval-{name}"
    ev.mkdir(parents=True, exist_ok=True)
    (ev / "submitted").write_text(job)
    (ev / "exit-code").write_text(code)
    if value is not None:
        (ev / "stdout").write_text(json.dumps({"metric": "r2", "value": value}) + "\n")


def _dispatched(tmp_path: Path, name: str, job="101"):
    """A measure dispatched but not yet complete (marker, no exit-code)."""
    ev = tmp_path / f"eval-{name}"
    ev.mkdir(parents=True, exist_ok=True)
    (ev / "submitted").write_text(job)


def _measures():
    return [
        Measure("baseline", "a" * 40, "cmd", "r2"),
        Measure("candidate", "b" * 40, "cmd", "r2"),
    ]


def test_first_pass_dispatches_all_and_parks(tmp_path):
    submitted: list = []
    m = _measurer(tmp_path, submitted)
    with pytest.raises(MeasurementPending) as exc:
        m.results(_measures())
    # both measures submitted; the wake dependency covers the set with ONE job
    assert len(submitted) == 2
    assert exc.value.afterany() == "afterany:101:102"


def test_resume_reads_completed_results_without_resubmitting(tmp_path):
    submitted: list = []
    m = _measurer(tmp_path, submitted)
    # the jobs landed while the process was gone
    _land(tmp_path, "baseline", 0.50)
    _land(tmp_path, "candidate", 0.61)
    out = m.results(_measures())
    assert out == {"baseline": 0.50, "candidate": 0.61}
    assert submitted == []  # nothing re-dispatched on resume


def test_partial_completion_reparks_only_missing(tmp_path):
    submitted: list = []
    m = _measurer(tmp_path, submitted)
    _land(tmp_path, "baseline", 0.50)  # one done, one still pending
    with pytest.raises(MeasurementPending):
        m.results(_measures())
    # only the un-done candidate is re-dispatched
    assert len(submitted) == 1
    assert any("eval-candidate" in tok for tok in submitted[0])


def test_failed_measure_raises_eval_error(tmp_path):
    m = _measurer(tmp_path, [])
    _land(tmp_path, "baseline", 0.50)
    _land(tmp_path, "candidate", code="97")  # the job wrote a nonzero exit
    with pytest.raises(EvalError, match="candidate"):
        m.results(_measures())


def test_job_died_before_wrapper_fails_not_resubmits(tmp_path):
    """A dispatched job that is TERMINAL with no exit-code (SIGKILL/node
    death) raises EvalError — resubmitting would loop forever."""
    submitted: list = []
    m = _measurer(tmp_path, submitted, job_state="FAILED")
    _land(tmp_path, "baseline", 0.50)
    _dispatched(tmp_path, "candidate", job="102")  # marker, no exit-code
    with pytest.raises(EvalError, match=r"candidate.*without writing"):
        m.results(_measures())
    assert submitted == []  # never resubmitted


def test_dispatched_and_running_reparks_on_the_real_job_id(tmp_path):
    """A measure already dispatched and still running re-parks on its real
    job id (no sentinel, no resubmit) — the completion race is impossible
    because the marker is authoritative."""
    submitted: list = []
    m = _measurer(tmp_path, submitted, job_state="RUNNING")
    _land(tmp_path, "baseline", 0.50)
    _dispatched(tmp_path, "candidate", job="102")
    with pytest.raises(MeasurementPending) as exc:
        m.results(_measures())
    assert exc.value.afterany() == "afterany:102"  # the real job id
    assert submitted == []


def test_measure_carries_paired_seed():
    m = Measure("sib", "c" * 40, "cmd", "r2", extra_env=(("PILOT_SEED", "7"),))
    assert m.env() == {"PILOT_SEED": "7"}
