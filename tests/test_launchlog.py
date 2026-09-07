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
    labels = why_by_job(tmp_path)
    assert {k: (v["name"], v["why"], v["sleep"]) for k, v in labels.items()} == {
        "1": ("a", "probe a", 1),
        "2": ("sw", "sweep lr", 1),
        "3": ("sw", "sweep lr", 1),
        "4": ("a", "probe a", 2),
    }
    assert (labels["2"]["array"], labels["2"]["concurrency"]) == (2, 0)


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


def test_a_re_parked_sleep_keeps_its_first_records(tmp_path: Path) -> None:
    """A submitted run re-parks the same sleep through a multi-stage gate; the
    rebuilt request must not overwrite the record that carries the why."""
    first = (Launch(name="a", command="x", minutes=5, why="probe a"),)
    append_submitted(tmp_path, sleep=1, launches=first, job_ids=["1"], at=10.0)
    rebuilt = (Launch(name="a", command="(ran)", minutes=5),)
    append_submitted(tmp_path, sleep=1, launches=rebuilt, job_ids=["1"], at=20.0)
    entries = history(tmp_path)
    assert (
        len(entries) == 1 and entries[0]["why"] == "probe a" and entries[0]["submitted_at"] == 10.0
    )
    # the same name in the next sleep is a new launch
    append_submitted(tmp_path, sleep=2, launches=rebuilt, job_ids=["2"], at=30.0)
    assert [(e["sleep"], e["why"]) for e in history(tmp_path)] == [(1, "probe a"), (2, "")]


def test_one_id_per_launch_is_the_arrays_id(tmp_path: Path) -> None:
    """A sweep is one job array: the park records one id per launch, and the
    queue view maps every task (`<id>_<k>`) back to it through the array id."""
    launches = (
        Launch(name="a", command="x", minutes=5, why="probe a"),
        Launch(name="sw", command="y", minutes=7, array=4, why="sweep lr"),
    )
    append_submitted(tmp_path, sleep=1, launches=launches, job_ids=["10", "11"], at=1.0)
    assert [e["job_ids"] for e in history(tmp_path)] == [["10"], ["11"]]
    assert why_by_job(tmp_path)["11"]["why"] == "sweep lr"
