"""The climb board: rows out of terminal records, idempotent merge, and the
three published views."""

import json
from pathlib import Path

from autoresearch.climbboard import (
    ClimbRow,
    collect_rows,
    merge_rows,
    render_html,
    render_md,
    service_climb_board,
)
from autoresearch.runstate import RunRecord, run_dir, save_record

REPORT = """# Run report — org/repo / speedrun
Outcome: **no-improvement**
Baseline: 9472.0
Candidate: 38146.0

## Agent's report
- **Hypothesis:** the warmdown starts too early for the crossing.
- **Change:** moved it.
"""


def _terminal_run(root: Path, run_id: str, *, state="ended", ending="negative-result") -> None:
    record = RunRecord(
        run_id=run_id,
        target="org/repo",
        task_title="t",
        state=state,
        ending=ending,
        benchmark="speedrun",
        agent_id="agent-03",
        pr_url="https://github.com/org/repo/pull/9" if state == "in-review" else "",
        created=1_787_900_000.0,
        updated=1_787_950_000.0,
        stage={"gpu_hours_used": 12.5},
    )
    save_record(root, record, 1_787_950_000.0)
    (run_dir(root, run_id) / "report.md").write_text(REPORT)


def test_collect_rows_reads_terminal_records_and_reports(tmp_path: Path) -> None:
    _terminal_run(tmp_path, "speedrun-1")
    _terminal_run(tmp_path, "speedrun-2", state="in-review", ending="")
    # non-terminal and reportless runs stay off the board
    save_record(
        tmp_path,
        RunRecord(run_id="w", target="org/repo", task_title="t", state="waiting"),
        1.0,
    )
    boards = collect_rows(tmp_path, "org/repo")
    rows = boards["speedrun"]
    assert [r.run_id for r in rows] == ["speedrun-1", "speedrun-2"]
    row = rows[0]
    assert row.baseline == 9472.0 and row.candidate == 38146.0
    assert row.hypothesis.startswith("the warmdown starts too early")
    assert row.gpu_hours == 12.5 and row.agent == "agent-03"
    assert rows[1].outcome == "improved" and rows[1].pr_url.endswith("/pull/9")


def test_merge_is_idempotent_and_keeps_published_history(tmp_path: Path) -> None:
    row = ClimbRow(
        run_id="r1",
        agent="agent-01",
        ended="2026-08-29 10:00",
        outcome="negative-result",
        baseline=9472.0,
        candidate=None,
        gpu_hours=4.0,
        hypothesis="h",
        pr_url="",
    )
    first = merge_rows(None, [row])
    assert [r["run_id"] for r in first] == ["r1"]
    # a published row wins over a re-collected one (history is never rewritten),
    # merging again changes nothing, and unreadable JSON falls back to fresh rows
    edited = json.dumps([{**first[0], "hypothesis": "published wording"}])
    again = merge_rows(edited, [row])
    assert again[0]["hypothesis"] == "published wording"
    assert merge_rows(json.dumps(again), [row]) == again
    assert [r["run_id"] for r in merge_rows("not json", [row])] == ["r1"]


def test_views_render_the_rows(tmp_path: Path) -> None:
    _terminal_run(tmp_path, "speedrun-1")
    boards = {b: merge_rows(None, rows) for b, rows in collect_rows(tmp_path, "org/repo").items()}
    md = render_md("org/repo", boards)
    assert "## speedrun" in md and "| 2026-" in md and "38146" in md
    assert "Attempts: **1**" in md
    html = render_html("org/repo", list(boards))
    assert "climb/${b}.json" in html and '"speedrun"' in html


class _BoardGitHub:
    """get/put fake over an in-memory branch."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.puts: list[str] = []

    def get_file_content(self, repo, path, ref):
        return self.files.get(path)

    def ensure_branch(self, repo, branch):
        return True

    def put_file(self, repo, path, content, branch, message):
        self.files[path] = content
        self.puts.append(path)
        return "created"


def test_service_publishes_once_per_change(tmp_path: Path) -> None:
    _terminal_run(tmp_path, "speedrun-1")
    gh = _BoardGitHub()
    assert service_climb_board(tmp_path, gh, "org/repo") == 3
    assert sorted(gh.puts) == ["CLIMB.md", "climb.html", "climb/speedrun.json"]
    # unchanged state: nothing is written again (no commit spam)
    gh.puts.clear()
    assert service_climb_board(tmp_path, gh, "org/repo") == 0
    assert gh.puts == []
    # a new terminal run publishes exactly the changed files
    _terminal_run(tmp_path, "speedrun-2", state="in-review", ending="")
    assert service_climb_board(tmp_path, gh, "org/repo") >= 2
    assert json.loads(gh.files["climb/speedrun.json"])[-1]["run_id"] == "speedrun-2"
    # no runs for the target at all: a quiet no-op
    assert service_climb_board(tmp_path / "empty", gh, "org/repo") == 0
