"""The climb board: rows out of terminal records, idempotent merge, and the
three published views."""

import json
from pathlib import Path

from autoresearch.climbboard import (
    ClimbRow,
    collect_rows,
    contract_directions,
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


def test_views_render_the_rows_and_respect_direction(tmp_path: Path) -> None:
    _terminal_run(tmp_path, "speedrun-1")
    boards = {b: merge_rows(None, rows) for b, rows in collect_rows(tmp_path, "org/repo").items()}
    md = render_md("org/repo", boards, {"speedrun": "min"})
    assert "## speedrun" in md and "| 2026-" in md and "38146" in md
    assert "Attempts: **1**" in md
    # direction decides which candidate is "best" (contracts support max)
    two = {"acc": [{"run_id": "a", "candidate": 0.3}, {"run_id": "b", "candidate": 0.7}]}
    assert "best candidate: **0.7** (max)" in render_md("org/repo", two, {"acc": "max"})
    assert "best candidate: **0.3** (min)" in render_md("org/repo", two, {"acc": "min"})
    # the chart data is EMBEDDED (fetch() is blocked on a file:// page) and
    # carries the direction
    html = render_html("org/repo", boards, {"speedrun": "min"})
    assert "fetch(" not in html and '"boards"' in html and "38146" in html
    # agent-written text cannot break out of the inline script: "<" is
    # escaped inside the JSON, so a </script> payload stays data
    hostile = {"b": [{"run_id": "x", "hypothesis": "</script><script>alert(1)</script>"}]}
    page = render_html("org/repo", hostile, {"b": "min"})
    assert page.count("</script>") == 2  # chart.js include + our own script, nothing injected
    assert "\\u003c/script" in page


def test_contract_directions_maps_benchmarks() -> None:
    from autoresearch.contract import load_contract

    contract = load_contract(
        """
benchmarks:
  - name: tsp
    command: uv run x --json
    metric: m
    direction: min
  - name: acc
    command: uv run y --json
    metric: a
    direction: max
budgets: {gpu_hours_per_run: 1, runs_per_week: 1}
scope: {allowed: [src/]}
roadmap: docs/roadmap.md
""",
        "org/repo",
    )
    assert contract_directions(contract) == {"tsp": "min", "acc": "max"}
    assert contract_directions(None) == {}


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
    assert service_climb_board(tmp_path, gh, "org/repo") == 4
    assert sorted(gh.puts) == ["CLIMB.md", "climb.html", "climb/index.json", "climb/speedrun.json"]
    # unchanged state: nothing is written again (no commit spam)
    gh.puts.clear()
    assert service_climb_board(tmp_path, gh, "org/repo") == 0
    assert gh.puts == []
    # a new terminal run publishes exactly the changed files
    _terminal_run(tmp_path, "speedrun-2", state="in-review", ending="")
    assert service_climb_board(tmp_path, gh, "org/repo") >= 2
    assert json.loads(gh.files["climb/speedrun.json"])[-1]["run_id"] == "speedrun-2"
    # no runs and no index at all: a quiet no-op
    assert service_climb_board(tmp_path / "empty", gh, "org/repo") == 0


def test_board_remembers_benchmarks_whose_local_records_are_gone(tmp_path: Path) -> None:
    """The index on the branch is the board's memory: a benchmark published
    long ago (records reaped since) keeps its place in every view when a
    different benchmark publishes."""
    gh = _BoardGitHub()
    gh.files["climb/index.json"] = json.dumps({"old-bench": "max"})
    gh.files["climb/old-bench.json"] = json.dumps(
        [{"run_id": "old-1", "ended": "2026-01-01 00:00", "candidate": 0.5, "outcome": "improved"}]
    )
    _terminal_run(tmp_path, "speedrun-1")
    assert service_climb_board(tmp_path, gh, "org/repo", {"speedrun": "min"}) >= 3
    md = gh.files["CLIMB.md"]
    assert "## old-bench" in md and "## speedrun" in md
    assert "best candidate: **0.5** (max)" in md
    assert json.loads(gh.files["climb/index.json"]) == {"old-bench": "max", "speedrun": "min"}


def test_failed_view_upload_is_retried_next_pass(tmp_path: Path) -> None:
    _terminal_run(tmp_path, "speedrun-1")

    gh = _BoardGitHub()
    fail = {"CLIMB.md"}
    real_put = gh.put_file

    def flaky(repo, path, content, branch, message):
        if path in fail:
            return ""
        return real_put(repo, path, content, branch, message)

    gh.put_file = flaky  # type: ignore[method-assign]
    service_climb_board(tmp_path, gh, "org/repo")
    assert "CLIMB.md" not in gh.files
    fail.clear()  # the outage ends; the next pass sees the view differ and retries
    assert service_climb_board(tmp_path, gh, "org/repo") == 1
    assert "## speedrun" in gh.files["CLIMB.md"]


def test_failed_json_upload_renders_last_published_rows(tmp_path: Path) -> None:
    """Views never point at data that is not on the branch: when a fresh
    JSON cannot be uploaded, the benchmark renders from its last published
    rows."""
    gh = _BoardGitHub()
    published = [
        {"run_id": "old-1", "ended": "2026-01-01 00:00", "candidate": 9472, "outcome": "improved"}
    ]
    gh.files["climb/index.json"] = json.dumps({"speedrun": "min"})
    gh.files["climb/speedrun.json"] = json.dumps(published)
    _terminal_run(tmp_path, "speedrun-1")  # fresh row that will fail to upload
    fail = {"climb/speedrun.json"}
    real_put = gh.put_file

    def flaky(repo, path, content, branch, message):
        if path in fail:
            return ""
        return real_put(repo, path, content, branch, message)

    gh.put_file = flaky  # type: ignore[method-assign]
    service_climb_board(tmp_path, gh, "org/repo")
    md = gh.files["CLIMB.md"]
    assert "old-1" not in md  # run ids never render; check by content:
    assert "9472" in md and "38146" not in md  # published row only, not the failed fresh one
