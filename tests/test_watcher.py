"""The session watcher answers `queue` and `history` from beside the session."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from outerloop.launchlog import append_submitted
from outerloop.runstate import RunRecord, run_dir, save_record
from outerloop.syscall import Launch
from outerloop.watcher import SessionWatcher, WatcherContext


class _Compute:
    def __init__(
        self, rows: list[dict[str, str]], fail: bool = False, lane_fail: bool = False
    ) -> None:
        self.rows, self.fail, self.calls = rows, fail, 0
        self.lane_fail, self.lane_calls = lane_fail, 0

    def queue_snapshot(self) -> list[dict[str, str]]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("slurmctld down")
        return list(self.rows)

    def lane_load(self, partition: str) -> dict[str, int]:
        self.lane_calls += 1
        if self.lane_fail:
            raise RuntimeError("sinfo failed (1)")
        return {"idle": 3, "mixed": 20}


def _row(
    job_id: str, name: str, state: str = "PENDING", reason: str = "QOSGrpGRES"
) -> dict[str, str]:
    return {
        "id": job_id,
        "name": name,
        "state": state,
        "elapsed": "0:00",
        "partition": "h200",
        "submitted": "2026-09-07T10:00:00",
        "reason": reason,
        "gres": "gpu:1",
        "limit": "4:00:00",
    }


def _fleet(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "root"
    for rid, agent in (("r1", "agent-01"), ("r2", "agent-02")):
        save_record(
            root,
            RunRecord(
                run_id=rid, target="o/r", task_title="t", state="implementing", agent_id=agent
            ),
            now=1.0,
        )
    append_submitted(
        run_dir(root, "r2"),
        sleep=1,
        launches=(Launch(name="lr", command="c", minutes=10, why="try lr 3e-4"),),
        job_ids=["555"],
        at=5.0,
    )
    ws = tmp_path / "ws"
    (ws / ".outerloop").mkdir(parents=True)
    return root, ws


def _ctx(root: Path, ws: Path, compute: object, **kw) -> WatcherContext:
    return WatcherContext(
        workspace=ws,
        run_root=root,
        run_id="r1",
        target="o/r",
        agent_id="agent-01",
        compute=compute,
        gpu_partition="h200",
        **kw,
    )


def _ask(ws: Path, verb: str, at: float) -> None:
    marker = ws / ".outerloop" / f"{verb}-request"
    marker.touch()
    os.utime(marker, (at, at))


def _answered(ws: Path, verb: str) -> dict:
    return json.loads((ws / ".outerloop" / f"{verb}.json").read_text())


def test_queue_view_attributes_every_agents_jobs_and_labels_launches(tmp_path: Path) -> None:
    root, ws = _fleet(tmp_path)
    compute = _Compute(
        [
            _row("555", "r2-launch-lr"),
            _row("600", "r1-launch-mine", "RUNNING", "None"),
            _row("7", "wake-r1"),
        ]
    )
    clock = [100.0]
    watcher = SessionWatcher(_ctx(root, ws, compute, clock=lambda: clock[0]))
    _ask(ws, "queue", 50.0)
    watcher.service()
    view = _answered(ws, "queue")
    assert float((ws / ".outerloop" / "queue-done").read_text()) >= 50.0
    by_id = {j["id"]: j for j in view["jobs"]}
    theirs, mine, wake = by_id["555"], by_id["600"], by_id["7"]
    assert (
        theirs["agent"],
        theirs["experiment"],
        theirs["why"],
        theirs["mine"],
        theirs["kind"],
    ) == (
        "agent-02",
        "lr",
        "try lr 3e-4",
        False,
        "launch",
    )
    assert (
        theirs["reason"] == "QOSGrpGRES"
        and theirs["limit"] == "4:00:00"
        and theirs["gres"] == "gpu:1"
    )
    assert (mine["agent"], mine["experiment"], mine["mine"], mine["why"]) == (
        "agent-01",
        "mine",
        True,
        "",
    )
    assert (wake["kind"], wake["experiment"]) == ("wake", "")
    assert view["lane"] == {"partition": "h200", "nodes": {"idle": 3, "mixed": 20}}
    assert view["error"] == "" and view["agent"] == "agent-01"


def test_queue_queries_are_rate_limited_per_request_storm(tmp_path: Path) -> None:
    root, ws = _fleet(tmp_path)
    compute = _Compute([_row("555", "r2-launch-lr")])
    clock = [100.0]
    watcher = SessionWatcher(_ctx(root, ws, compute, clock=lambda: clock[0], query_gap_s=5.0))
    _ask(ws, "queue", 50.0)
    watcher.service()
    clock[0] = 101.0
    _ask(ws, "queue", 60.0)  # rewritten a second later
    watcher.service()
    assert compute.calls == 1 and _answered(ws, "queue")["cached"] is True
    clock[0] = 106.0
    _ask(ws, "queue", 70.0)
    watcher.service()
    assert compute.calls == 2 and "cached" not in _answered(ws, "queue")


def test_a_failed_scheduler_query_answers_with_the_error(tmp_path: Path) -> None:
    root, ws = _fleet(tmp_path)
    watcher = SessionWatcher(_ctx(root, ws, _Compute([], fail=True)))
    _ask(ws, "queue", 50.0)
    watcher.service()
    view = _answered(ws, "queue")
    assert view["jobs"] == [] and "slurmctld down" in view["error"]
    assert (ws / ".outerloop" / "queue-done").exists()  # answered, not left hanging


def test_no_compute_means_an_empty_queue(tmp_path: Path) -> None:
    root, ws = _fleet(tmp_path)
    watcher = SessionWatcher(_ctx(root, ws, None))
    _ask(ws, "queue", 50.0)
    watcher.service()
    view = _answered(ws, "queue")
    assert view["jobs"] == [] and view["lane"] == {} and view["error"] == ""


def test_history_view_is_this_runs_ledger(tmp_path: Path) -> None:
    root, ws = _fleet(tmp_path)
    ctx = _ctx(root, ws, _Compute([]))
    ctx.run_id = "r2"
    _ask(ws, "history", 50.0)
    SessionWatcher(ctx).service()
    view = _answered(ws, "history")
    assert view["run_id"] == "r2"
    assert [(e["name"], e["why"], e["job_ids"]) for e in view["history"]] == [
        ("lr", "try lr 3e-4", ["555"])
    ]


def test_the_thread_answers_while_the_session_runs(tmp_path: Path) -> None:
    root, ws = _fleet(tmp_path)
    with SessionWatcher(_ctx(root, ws, _Compute([_row("555", "r2-launch-lr")]), poll_s=0.05)):
        (ws / ".outerloop" / "queue-request").touch()
        deadline = time.time() + 5
        while not (ws / ".outerloop" / "queue-done").exists() and time.time() < deadline:
            time.sleep(0.05)
        assert (ws / ".outerloop" / "queue-done").exists()
    assert _answered(ws, "queue")["jobs"][0]["experiment"] == "lr"


def test_a_symlinked_channel_is_never_followed(tmp_path: Path) -> None:
    root, ws = _fleet(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "queue-request").touch()
    channel = ws / ".outerloop"
    channel.rmdir()
    channel.symlink_to(elsewhere)
    SessionWatcher(_ctx(root, ws, _Compute([]))).service()  # no exception
    assert not (elsewhere / "queue.json").exists() and not (elsewhere / "queue-done").exists()


def test_lane_load_is_asked_once_a_minute_and_its_failure_is_said(tmp_path: Path) -> None:
    root, ws = _fleet(tmp_path)
    compute = _Compute([_row("555", "r2-launch-lr")])
    clock = [100.0]
    watcher = SessionWatcher(_ctx(root, ws, compute, clock=lambda: clock[0], query_gap_s=5.0))
    for t, at in ((100.0, 50.0), (110.0, 60.0), (170.0, 70.0)):
        clock[0] = t
        _ask(ws, "queue", at)
        watcher.service()
    # three uncached queue views (5 s apart or more) cost three squeue and two sinfo
    assert (compute.calls, compute.lane_calls) == (3, 2)
    failing = _Compute([], lane_fail=True)
    watcher = SessionWatcher(_ctx(root, ws, failing))
    _ask(ws, "queue", 80.0)
    watcher.service()
    view = _answered(ws, "queue")
    assert view["lane"] == {"partition": "h200", "nodes": {}, "error": "sinfo failed (1)"}
    assert view["error"] == ""  # the queue itself was fine


def test_nothing_is_written_once_the_session_ended(tmp_path: Path) -> None:
    root, ws = _fleet(tmp_path)
    watcher = SessionWatcher(_ctx(root, ws, _Compute([_row("555", "r2-launch-lr")])))
    _ask(ws, "queue", 50.0)
    watcher.__exit__(None, None, None)  # stopped (never started): the request stands
    watcher.service()
    assert not (ws / ".outerloop" / "queue.json").exists()
    assert not (ws / ".outerloop" / "queue-done").exists()


def test_array_rows_carry_the_launch_label_and_the_pace(tmp_path: Path) -> None:
    """squeue names a pending array `<id>_[0-7%4]` and a running task `<id>_3`;
    both map back to the ledger's array id, and the pace is read off the range."""
    root, ws = _fleet(tmp_path)
    rows = [_row("555_[0-7%4]", "r2-launch-lr"), _row("555_3", "r2-launch-lr", "RUNNING", "None")]
    watcher = SessionWatcher(_ctx(root, ws, _Compute(rows)))
    _ask(ws, "queue", 50.0)
    watcher.service()
    by_id = {j["id"]: j for j in _answered(ws, "queue")["jobs"]}
    assert by_id["555_[0-7%4]"]["why"] == "try lr 3e-4" and by_id["555_[0-7%4]"]["concurrency"] == 4
    assert by_id["555_3"]["why"] == "try lr 3e-4" and "concurrency" not in by_id["555_3"]
