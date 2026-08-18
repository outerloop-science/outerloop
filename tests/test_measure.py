"""The dispatched measurer: submit-park-resume with the cluster as the
authoritative liveness source (crash-safe against the submit/marker gap)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoresearch.compute import CommandResult, SlurmCompute
from autoresearch.measure import DispatchedMeasurer, Measure, MeasurementPending
from autoresearch.orchestrator import EvalError


def _measurer(
    tmp_path: Path, submitted: list, live: dict | None = None, squeue_fails: bool = False
) -> DispatchedMeasurer:
    """`live` maps job-name -> id for jobs squeue should report as running."""
    live = live or {}

    def runner(argv, timeout_s):
        if argv and argv[0] == "sbatch":
            submitted.append(list(argv))
            return CommandResult(0, f"{100 + len(submitted)}\n", "")
        if argv and argv[0] == "squeue":
            if squeue_fails:
                return CommandResult(1, "", "squeue down")
            name = argv[argv.index("--name") + 1]
            return CommandResult(0, (live.get(name, "") + "\n") if live.get(name) else "", "")
        return CommandResult(0, "COMPLETED\n", "")

    return DispatchedMeasurer(
        compute=SlurmCompute(runner=runner),
        run_dir=tmp_path,
        repo_root=tmp_path / "repo",
        image="/img.sif",
        account="a",
        partition="p",
        eval_minutes=60,
        run_tag="r1",
    )


def _land(tmp_path: Path, name: str, value=None, code="0", job="101"):
    ev = tmp_path / f"eval-{name}"
    ev.mkdir(parents=True, exist_ok=True)
    (ev / "submitted").write_text(job)
    (ev / "exit-code").write_text(code)
    if value is not None:
        (ev / "stdout").write_text(json.dumps({"metric": "r2", "value": value}) + "\n")


def _dispatched(tmp_path: Path, name: str, job="101"):
    ev = tmp_path / f"eval-{name}"
    ev.mkdir(parents=True, exist_ok=True)
    (ev / "submitted").write_text(job)


def _measures():
    return [Measure("baseline", "a" * 40, "cmd", "r2"), Measure("candidate", "b" * 40, "cmd", "r2")]


def test_first_pass_dispatches_all_and_parks(tmp_path):
    submitted: list = []
    m = _measurer(tmp_path, submitted)  # nothing live -> both dispatched
    with pytest.raises(MeasurementPending) as exc:
        m.results(_measures())
    assert len(submitted) == 2
    assert exc.value.afterany() == "afterany:101:102"


def test_resume_reads_completed_without_resubmitting(tmp_path):
    submitted: list = []
    m = _measurer(tmp_path, submitted)
    _land(tmp_path, "baseline", 0.50)
    _land(tmp_path, "candidate", 0.61)
    assert m.results(_measures()) == {"baseline": 0.50, "candidate": 0.61}
    assert submitted == []


def test_live_job_reparks_on_real_id_never_resubmits(tmp_path):
    """A job squeue reports as running is parked on its real id — even if its
    local marker is MISSING (the crash-before-marker case)."""
    submitted: list = []
    m = _measurer(tmp_path, submitted, live={"eval-r1-candidate": "102"})
    _land(tmp_path, "baseline", 0.50)
    # candidate has NO marker (submitter died in the gap) but IS live
    with pytest.raises(MeasurementPending) as exc:
        m.results(_measures())
    assert exc.value.afterany() == "afterany:102"
    assert submitted == []  # not resubmitted despite the missing marker


def test_dispatched_but_vanished_fails_not_resubmits(tmp_path):
    """A measure with a marker but no live job and no result died before
    producing a result (SIGKILL / GONE) -> EvalError, never resubmit."""
    submitted: list = []
    m = _measurer(tmp_path, submitted, live={})  # nothing live
    _land(tmp_path, "baseline", 0.50)
    _dispatched(tmp_path, "candidate", job="102")  # marker, not live, no result
    with pytest.raises(EvalError, match="vanished"):
        m.results(_measures())
    assert submitted == []


def test_nonzero_exit_raises(tmp_path):
    m = _measurer(tmp_path, [])
    _land(tmp_path, "baseline", 0.50)
    _land(tmp_path, "candidate", code="97")
    with pytest.raises(EvalError, match="candidate"):
        m.results(_measures())


def test_squeue_blind_parks_without_dispatch(tmp_path):
    """A transient squeue failure must not risk a duplicate: park (on the
    marker id if any), let the sweep deadline retry; never dispatch blind."""
    submitted: list = []
    m = _measurer(tmp_path, submitted, squeue_fails=True)
    _dispatched(tmp_path, "baseline", job="102")  # has a marker
    # candidate has no marker; blind -> park, empty dep -> sweep handles it
    with pytest.raises(MeasurementPending) as exc:
        m.results(_measures())
    assert submitted == []
    assert exc.value.afterany() in ("afterany:102", "")  # marker id, never a resubmit


def test_measure_carries_paired_seed():
    m = Measure("sib", "c" * 40, "cmd", "r2", extra_env=(("PILOT_SEED", "7"),))
    assert m.env() == {"PILOT_SEED": "7"}


def test_empty_pending_afterany_is_blank():
    assert MeasurementPending(()).afterany() == ""
