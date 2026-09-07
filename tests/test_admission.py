"""Launch admission: queue, then stop (outerloop.admission)."""

from __future__ import annotations

from typing import Any

from outerloop import admission
from outerloop.compute import SlurmQueryError


def _row(
    job_id: str, name: str, state: str, reason: str, gres: str = "gres/gpu:1"
) -> dict[str, str]:
    return {"id": job_id, "name": name, "state": state, "reason": reason, "gres": gres}


class _Queue:
    def __init__(self, rows: list[dict[str, str]] | None, fail: bool = False) -> None:
        self.rows, self.fail = rows or [], fail

    def queue_snapshot(self) -> list[dict[str, str]]:
        if self.fail:
            raise SlurmQueryError("slurmctld down")
        return self.rows


def test_is_cap_reason() -> None:
    for r in ("QOSMaxGRESPerUser", "QOSGrpGRES", "AssocGrpGRES", "QOSMaxJobsPerAccount"):
        assert admission.is_cap_reason(r), r
    for r in ("Priority", "Resources", "Dependency", "QOSMaxWallDurationPerJobLimit", "None", ""):
        assert not admission.is_cap_reason(r), r


def test_queue_saturated_names_the_blocking_gpu_job(monkeypatch: Any) -> None:
    monkeypatch.delenv("OUTERLOOP_COMPUTE", raising=False)
    rows = [
        _row("1", "eval-x", "RUNNING", "None"),
        _row("2", "wake-r1", "PENDING", "Dependency", gres="N/A"),  # not a GPU job
        _row("3", "r1-launch-a", "PENDING", "QOSGrpGRES"),
        _row("4", "r2-launch-b", "PENDING", "QOSGrpGRES"),
    ]
    reason = admission.queue_saturated(_Queue(rows))
    assert "job 3" in reason and "QOSGrpGRES" in reason and "1 more" in reason
    assert "Sleep without launches" in reason
    # a busy queue (Priority) is not a full one: launches may still queue
    rows[2]["reason"] = rows[3]["reason"] = "Priority"
    assert admission.queue_saturated(_Queue(rows)) == ""
    # a CPU job parked on a cap does not block GPU launches
    cpu = [_row("5", "cpu", "PENDING", "QOSGrpCpuLimit", gres="N/A")]
    assert admission.queue_saturated(_Queue(cpu)) == ""


def test_queue_saturated_is_permissive_when_blind_or_local(monkeypatch: Any) -> None:
    monkeypatch.delenv("OUTERLOOP_COMPUTE", raising=False)
    assert admission.queue_saturated(_Queue(None, fail=True)) == ""  # a failed read never blocks
    assert admission.queue_saturated(object()) == ""  # a compute without a queue
    monkeypatch.setenv("OUTERLOOP_COMPUTE", "local")
    assert admission.queue_saturated(_Queue([_row("3", "l", "PENDING", "QOSGrpGRES")])) == ""
