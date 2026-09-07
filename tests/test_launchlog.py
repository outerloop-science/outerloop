"""The per-run launch ledger: append-only, joined by (sleep, name)."""

from __future__ import annotations

from pathlib import Path

from outerloop.launchlog import (
    LEDGER,
    append_ended,
    append_submitted,
    history,
    read_ledger,
    why_by_job,
)
from outerloop.syscall import Launch, LaunchResult


def _result(name: str, code: int | None, state: str = "") -> LaunchResult:
    return LaunchResult(
        name=name,
        exit_code=code,
        stdout_tail="",
        stderr_tail="",
        delivered=(),
        skipped=(),
        slurm_state=state,
    )


def test_history_joins_submits_with_their_ended_jobs(tmp_path: Path) -> None:
    launches = (
        Launch(name="a", command="x", minutes=5, why="probe a"),
        Launch(name="sw", command="y", minutes=7, array=2, why="sweep lr"),
    )
    append_submitted(tmp_path, sleep=1, launches=launches, job_ids=["1", "2", "3"], at=10.0)
    append_ended(
        tmp_path,
        sleep=1,
        results=(_result("a", 0), _result("sw.0", 1), _result("sw.1", None, "TIMEOUT")),
        at=20.0,
    )
    # the same name in a later sleep is a different launch
    append_submitted(tmp_path, sleep=2, launches=launches[:1], job_ids=["4"], at=30.0)

    entries = history(tmp_path)
    assert [(e["sleep"], e["name"], e["job_ids"]) for e in entries] == [
        (1, "a", ["1"]),
        (1, "sw", ["2", "3"]),
        (2, "a", ["4"]),
    ]
    assert entries[0]["why"] == "probe a" and entries[0]["submitted_at"] == 10.0
    assert [j["exit_code"] for j in entries[0]["jobs"]] == [0]
    assert [(j["name"], j["exit_code"], j["state"]) for j in entries[1]["jobs"]] == [
        ("sw.0", 1, ""),
        ("sw.1", None, "TIMEOUT"),
    ]
    assert entries[2]["jobs"] == []  # not back yet
    assert why_by_job(tmp_path) == {
        "1": {"name": "a", "why": "probe a", "sleep": 1},
        "2": {"name": "sw", "why": "sweep lr", "sleep": 1},
        "3": {"name": "sw", "why": "sweep lr", "sleep": 1},
        "4": {"name": "a", "why": "probe a", "sleep": 2},
    }


def test_ledger_tolerates_a_missing_file_and_a_torn_line(tmp_path: Path) -> None:
    assert read_ledger(tmp_path) == [] and history(tmp_path) == [] and why_by_job(tmp_path) == {}
    append_submitted(
        tmp_path,
        sleep=1,
        launches=(Launch(name="a", command="x", minutes=5),),
        job_ids=["9"],
        at=1.0,
    )
    with (tmp_path / LEDGER).open("a") as fh:
        fh.write("{torn")  # a crash mid-append
    assert [r["name"] for r in read_ledger(tmp_path)] == ["a"]
    # ids missing for an older park: the launch is still listed, with none
    append_submitted(
        tmp_path, sleep=2, launches=(Launch(name="b", command="x", minutes=5),), job_ids=[], at=2.0
    )
    assert history(tmp_path)[-1]["job_ids"] == []
