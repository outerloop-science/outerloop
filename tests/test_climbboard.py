"""The climb board: rows out of terminal records, idempotent merge, and the
three published views."""

import json
from dataclasses import replace as dc_replace
from pathlib import Path

from autoresearch.climbboard import (
    ClimbRow,
    collect_rows,
    contract_directions,
    merge_rows,
    render_html,
    render_md,
    service_climb_board,
    service_status,
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
        pr_url="https://github.com/org/repo/pull/9" if ending == "merged" else "",
        created=1_787_900_000.0,
        updated=1_787_950_000.0,
        stage={"gpu_hours_used": 12.5},
    )
    save_record(root, record, 1_787_950_000.0)
    (run_dir(root, run_id) / "report.md").write_text(REPORT)


def test_collect_rows_reads_terminal_records_and_reports(tmp_path: Path) -> None:
    _terminal_run(tmp_path, "speedrun-1")
    _terminal_run(tmp_path, "speedrun-2", ending="merged")
    # non-terminal runs stay off the board: an in-review run's outcome is not
    # known yet (its PR may be rejected), and a waiting run is mid-flight
    _terminal_run(tmp_path, "speedrun-3", state="in-review", ending="")
    save_record(
        tmp_path,
        RunRecord(run_id="w", target="org/repo", task_title="t", state="waiting"),
        1.0,
    )
    # the ledger's marker gates the report link: archived -> linked,
    # adopted-unpublished -> no link
    (run_dir(tmp_path, "speedrun-1") / "ledger-published").write_text("done")
    (run_dir(tmp_path, "speedrun-2") / "ledger-published").write_text("adopted-unpublished")
    boards = collect_rows(tmp_path, "org/repo")
    rows = boards["speedrun"]
    assert [r.run_id for r in rows] == ["speedrun-1", "speedrun-2"]
    assert rows[0].report.startswith("reports/") and rows[0].report.endswith("speedrun-1.md")
    assert rows[1].report == ""
    row = rows[0]
    assert row.baseline == 9472.0 and row.candidate == 38146.0
    assert row.hypothesis.startswith("the warmdown starts too early")
    assert row.gpu_hours == 12.5 and row.agent == "agent-03"
    assert rows[1].outcome == "merged" and rows[1].pr_url.endswith("/pull/9")


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
    # the CHART data is embedded (fetch is blocked on a file:// page); the one
    # fetch in the page is the optional live strip, which fails silently there
    assert '"boards"' in html and "38146" in html
    assert html.count("fetch('") == 1 and "climb/status.json" in html
    # agent-written text cannot break out of the inline script: "<" is
    # escaped inside the JSON, so a </script> payload stays data
    hostile = {"b": [{"run_id": "x", "hypothesis": "</script><script>alert(1)</script>"}]}
    page = render_html("org/repo", hostile, {"b": "min"})
    assert page.count("</script>") == 2  # chart.js include + our own script, nothing injected
    assert "\\u003c/script" in page


def test_row_cap_is_loud_not_silent() -> None:
    import json as _json

    from autoresearch.climbboard import MAX_ROWS_PER_BENCHMARK

    rows = [{"run_id": f"r{i}", "ended": f"2026-01-01 {i:02d}:00"} for i in range(40)]
    many = [
        {"run_id": f"m{i}", "ended": f"2026-{1 + i // 10000:02d}-01 00:{i % 60:02d}"}
        for i in range(MAX_ROWS_PER_BENCHMARK + 50)
    ]
    capped = merge_rows(_json.dumps(many), [])
    assert len(capped) <= MAX_ROWS_PER_BENCHMARK
    md = render_md("org/repo", {"b": capped}, {"b": "min"})
    assert f"Only the newest {MAX_ROWS_PER_BENCHMARK} attempts" in md
    small = render_md("org/repo", {"b": rows}, {"b": "min"})
    assert "Only the newest" not in small


def test_a_benchmark_named_index_cannot_collide_with_the_index(tmp_path: Path) -> None:
    """Contract names allow `index`; its data lives under climb/data/, so the
    direction index is never overwritten by a benchmark's rows."""
    gh = _BoardGitHub()
    record = RunRecord(
        run_id="idx-1",
        target="org/repo",
        task_title="t",
        state="ended",
        ending="negative-result",
        benchmark="index",
        created=1.0,
        updated=2.0,
    )
    save_record(tmp_path, record, 2.0)
    (run_dir(tmp_path, "idx-1") / "report.md").write_text(REPORT)
    service_climb_board(tmp_path, gh, "org/repo", {"index": "min"})
    assert json.loads(gh.files["climb/data/index.json"])[0]["run_id"] == "idx-1"
    assert json.loads(gh.files["climb/index.json"]) == {"index": "min"}


def test_terminal_transition_keeps_the_spend_for_the_board(tmp_path: Path) -> None:
    """_clear_stage wipes the waiting bookkeeping but keeps gpu_hours_used —
    the production transition the board reads after (review #193 r6: rows
    published zero GPU-hours because the collector ran on a wiped stage)."""
    from autoresearch.attempt import _clear_stage

    record = RunRecord(
        run_id="r",
        target="org/repo",
        task_title="t",
        state="waiting",
        benchmark="speedrun",
        stage={"phase": "candidate", "afterany": "afterany:1", "gpu_hours_used": 36.0},
        wake_attempts=2,
        deadline=9.0,
    )
    cleared = _clear_stage(record)
    assert cleared.stage == {"gpu_hours_used": 36.0}
    assert cleared.wake_attempts == 0 and cleared.deadline == 0.0
    save_record(tmp_path, dc_replace(cleared, state="ended", ending="negative-result"), 2.0)
    (run_dir(tmp_path, "r") / "report.md").write_text(REPORT)
    rows = collect_rows(tmp_path, "org/repo")["speedrun"]
    assert rows[0].gpu_hours == 36.0


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


class _GitHubError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"{status}")
        self.status = status


class _BoardGitHub:
    """get/put fake over an in-memory branch, with the client's semantics:
    get_file raises (404 when absent), get_file_content is tolerant."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.puts: list[str] = []
        self.index_outage = False
        self.batch_fail = False
        self.head = "h0"

    def get_file(self, repo, path, ref):
        if self.index_outage:
            raise _GitHubError(500)
        if path not in self.files:
            raise _GitHubError(404)
        return self.files[path]

    def get_file_content(self, repo, path, ref):
        return self.files.get(path)

    def ensure_branch(self, repo, branch):
        return True

    def put_file(self, repo, path, content, branch, message):
        self.files[path] = content
        self.puts.append(path)
        return "created"

    def branch_head(self, repo, branch):
        return self.head

    def put_files(self, repo, files, branch, message, expected_head=None):
        if self.batch_fail:
            return False
        if expected_head and expected_head != self.head:
            return False
        self.head += "+"
        for path, content in files.items():
            self.files[path] = content
            self.puts.append(path)
        return True


def test_service_publishes_once_per_change(tmp_path: Path) -> None:
    _terminal_run(tmp_path, "speedrun-1")
    gh = _BoardGitHub()
    assert service_climb_board(tmp_path, gh, "org/repo") == 4
    assert sorted(gh.puts) == [
        "CLIMB.md",
        "climb/data/speedrun.json",
        "climb/index.json",
        "index.html",
    ]
    # unchanged state: nothing is written again (no commit spam)
    gh.puts.clear()
    assert service_climb_board(tmp_path, gh, "org/repo") == 0
    assert gh.puts == []
    # a new terminal run publishes exactly the changed files
    _terminal_run(tmp_path, "speedrun-2", ending="merged")
    assert service_climb_board(tmp_path, gh, "org/repo") >= 2
    assert json.loads(gh.files["climb/data/speedrun.json"])[-1]["run_id"] == "speedrun-2"
    # no runs and no index at all: a quiet no-op
    assert service_climb_board(tmp_path / "empty", gh, "org/repo") == 0


def test_board_remembers_benchmarks_whose_local_records_are_gone(tmp_path: Path) -> None:
    """The index on the branch is the board's memory: a benchmark published
    long ago (records reaped since) keeps its place in every view when a
    different benchmark publishes."""
    gh = _BoardGitHub()
    gh.files["climb/index.json"] = json.dumps({"old-bench": "max"})
    gh.files["climb/data/old-bench.json"] = json.dumps(
        [{"run_id": "old-1", "ended": "2026-01-01 00:00", "candidate": 0.5, "outcome": "improved"}]
    )
    _terminal_run(tmp_path, "speedrun-1")
    assert service_climb_board(tmp_path, gh, "org/repo", {"speedrun": "min"}) >= 3
    md = gh.files["CLIMB.md"]
    assert "## old-bench" in md and "## speedrun" in md
    assert "best candidate: **0.5** (max)" in md
    assert json.loads(gh.files["climb/index.json"]) == {"old-bench": "max", "speedrun": "min"}


def test_transient_json_read_failure_never_orphans_an_indexed_benchmark(tmp_path: Path) -> None:
    """get_file_content returns None on a transient API failure too: an
    indexed benchmark whose JSON could not be read this pass stays in the
    index (its history is still on the branch), and the views render it
    again once a later read succeeds."""
    gh = _BoardGitHub()
    gh.files["climb/index.json"] = json.dumps({"old-bench": "max"})
    old_rows = json.dumps(
        [{"run_id": "old-1", "ended": "2026-01-01 00:00", "candidate": 0.5, "outcome": "improved"}]
    )
    gh.files["climb/data/old-bench.json"] = old_rows
    real_get = gh.get_file
    blackout = {"climb/data/old-bench.json"}

    def flaky_get(repo, path, ref):
        if path in blackout:
            raise _GitHubError(500)
        return real_get(repo, path, ref)

    gh.get_file = flaky_get  # type: ignore[method-assign]
    _terminal_run(tmp_path, "speedrun-1")
    service_climb_board(tmp_path, gh, "org/repo", {"speedrun": "min"})
    assert "## old-bench" not in gh.files["CLIMB.md"]  # skipped this pass...
    assert json.loads(gh.files["climb/index.json"]) == {"old-bench": "max", "speedrun": "min"}
    blackout.clear()  # ...and back in the views once the read works again
    service_climb_board(tmp_path, gh, "org/repo", {"speedrun": "min"})
    assert "## old-bench" in gh.files["CLIMB.md"]


def test_unreadable_board_json_is_never_overwritten(tmp_path: Path) -> None:
    """An outage reading one benchmark's JSON must not let a fresh merge
    replace its published history: that benchmark sits the pass out and its
    index entry survives."""
    gh = _BoardGitHub()
    gh.files["climb/index.json"] = json.dumps({"speedrun": "min"})
    gh.files["climb/data/speedrun.json"] = json.dumps(
        [{"run_id": "old-1", "ended": "2026-01-01 00:00", "candidate": 9472}]
    )
    _terminal_run(tmp_path, "speedrun-1")
    real_get = gh.get_file
    blackout = {"climb/data/speedrun.json"}

    def flaky(repo, path, ref):
        if path in blackout:
            raise _GitHubError(500)
        return real_get(repo, path, ref)

    gh.get_file = flaky  # type: ignore[method-assign]
    service_climb_board(tmp_path, gh, "org/repo", {"speedrun": "min"})
    assert json.loads(gh.files["climb/data/speedrun.json"])[0]["run_id"] == "old-1"  # untouched
    assert json.loads(gh.files["climb/index.json"]) == {"speedrun": "min"}
    blackout.clear()
    service_climb_board(tmp_path, gh, "org/repo", {"speedrun": "min"})
    ids = [r["run_id"] for r in json.loads(gh.files["climb/data/speedrun.json"])]
    assert ids == ["old-1", "speedrun-1"]  # history + the fresh row, after the outage


def test_non_list_board_json_is_never_overwritten(tmp_path: Path) -> None:
    """A data file that decodes to something other than a row list is as
    opaque as an outage: the benchmark sits the pass out untouched."""
    gh = _BoardGitHub()
    gh.files["climb/index.json"] = json.dumps({"speedrun": "min"})
    gh.files["climb/data/speedrun.json"] = json.dumps({"rows": "elsewhere"})
    _terminal_run(tmp_path, "speedrun-1")
    service_climb_board(tmp_path, gh, "org/repo", {"speedrun": "min"})
    assert json.loads(gh.files["climb/data/speedrun.json"]) == {"rows": "elsewhere"}


def test_same_minute_attempts_order_by_end_time(tmp_path: Path) -> None:
    """`ended` carries seconds: it is the merge sort key, and two attempts
    in one minute must not fall back to directory order."""
    _terminal_run(tmp_path, "speedrun-b")  # updated=1_787_950_000
    record = RunRecord(
        run_id="speedrun-a",
        target="org/repo",
        task_title="t",
        state="ended",
        ending="negative-result",
        benchmark="speedrun",
        created=1_787_900_000.0,
        updated=1_787_950_030.0,  # same minute, 30 s later
    )
    save_record(tmp_path, record, record.updated)
    (run_dir(tmp_path, "speedrun-a") / "report.md").write_text(REPORT)
    rows = merge_rows(None, collect_rows(tmp_path, "org/repo")["speedrun"])
    assert [r["run_id"] for r in rows] == ["speedrun-b", "speedrun-a"]
    assert rows[0]["ended"].count(":") == 2  # seconds present


def test_failed_index_read_never_rewrites_the_index(tmp_path: Path) -> None:
    """A transient failure reading the index itself must not shrink it: the
    pass publishes what it can but leaves climb/index.json untouched."""
    gh = _BoardGitHub()
    gh.files["climb/index.json"] = "not json {"
    _terminal_run(tmp_path, "speedrun-1")
    service_climb_board(tmp_path, gh, "org/repo", {"speedrun": "min"})
    assert gh.files["climb/index.json"] == "not json {"  # malformed: untouched
    gh.files["climb/index.json"] = json.dumps({"old-bench": "max"})
    gh.index_outage = True
    service_climb_board(tmp_path, gh, "org/repo", {"speedrun": "min"})
    assert json.loads(gh.files["climb/index.json"]) == {"old-bench": "max"}  # untouched
    gh.index_outage = False  # outage over: the union is restored
    service_climb_board(tmp_path, gh, "org/repo", {"speedrun": "min"})
    assert json.loads(gh.files["climb/index.json"]) == {"old-bench": "max", "speedrun": "min"}


def test_batch_refuses_when_the_head_moved_mid_pass(tmp_path: Path) -> None:
    """A concurrent write between the pass's reads and its commit must not
    be buried: the batch refuses and the whole pass retries next tick."""
    _terminal_run(tmp_path, "speedrun-1")
    gh = _BoardGitHub()
    real_head = gh.branch_head

    def moving_head(repo, branch):
        gh.head = "h-moved"  # someone commits right after our snapshot
        return "h0"

    gh.branch_head = moving_head  # type: ignore[method-assign]
    assert service_climb_board(tmp_path, gh, "org/repo") == 0
    assert gh.files == {}
    gh.branch_head = real_head  # type: ignore[method-assign]
    assert service_climb_board(tmp_path, gh, "org/repo") == 4  # no curves here


def test_status_strip_publishes_on_shape_change_only(tmp_path: Path) -> None:
    """The strip is pushed when a run appears, leaves, or changes
    state/phase — never for timestamp drift (that would be a commit per
    tick; the page computes elapsed time itself)."""
    from autoresearch.climbboard import collect_status, service_status

    gh = _BoardGitHub()
    record = RunRecord(
        run_id="live-1",
        target="org/repo",
        task_title="t",
        state="waiting",
        benchmark="speedrun",
        agent_id="agent-02",
        created=100.0,
        updated=200.0,
        stage={"phase": "candidate", "launches_used": 2, "gpu_hours_used": 8.0},
    )
    save_record(tmp_path, record, 200.0)
    assert service_status(tmp_path, gh, "org/repo", 300.0) is True
    body = json.loads(gh.files["climb/status.json"])
    assert body["runs"][0]["agent"] == "agent-02"
    assert body["runs"][0]["state"] == "waiting" and body["runs"][0]["since"] == 200.0
    # same shape, later timestamp: no write
    assert service_status(tmp_path, gh, "org/repo", 999.0) is False
    # spend moving with no state/phase change (a same-phase re-park after new
    # launches) IS a fleet event: the strip must not show stale GPU-hours
    moved = dc_replace(record, stage={**record.stage, "gpu_hours_used": 40.0})
    save_record(tmp_path, moved, 250.0)
    assert service_status(tmp_path, gh, "org/repo", 260.0) is True
    assert json.loads(gh.files["climb/status.json"])["runs"][0]["gpu_hours_used"] == 40.0
    # a state change writes again; a terminal run leaves the strip
    save_record(tmp_path, dc_replace(record, state="ended", ending="negative-result"), 400.0)
    assert service_status(tmp_path, gh, "org/repo", 500.0) is True
    assert json.loads(gh.files["climb/status.json"])["runs"] == []
    # ended-only fleets keep an (empty) strip current; a fresh root writes once
    assert service_status(tmp_path, gh, "org/repo", 600.0) is False
    assert collect_status(tmp_path, "org/repo", 1.0)["runs"] == []


def test_html_carries_the_live_strip() -> None:
    from autoresearch.climbboard import render_html

    html = render_html("org/repo", {"b": []}, {"b": "min"})
    assert "climb/status.json" in html
    # the strip re-fetches (an open page shows runs that appear or leave) and
    # re-renders elapsed time locally between fetches
    assert "setInterval(refresh, 180000)" in html and "setInterval(render, 30000)" in html
    # a 404 page or malformed body never poisons the strip's render loop
    assert "r.ok ? r.json() : null" in html and "Array.isArray(s.runs)" in html


def test_service_boards_publishes_strip_and_views_before_any_terminal_run(tmp_path: Path) -> None:
    """The tick's one entry point (tick.service_boards) publishes both the
    views (so a fleet with only live runs still has a page that fetches the
    strip) and the strip itself — removing either call breaks this test."""
    from autoresearch.tick import service_boards

    gh = _BoardGitHub()
    record = RunRecord(
        run_id="live-1",
        target="org/repo",
        task_title="t",
        state="implementing",
        benchmark="speedrun",
        agent_id="agent-01",
        created=100.0,
        updated=100.0,
    )
    save_record(tmp_path, record, 100.0)
    service_boards(tmp_path, gh, "org/repo", None, 200.0)
    assert "index.html" in gh.files  # the page exists from the first tick
    assert json.loads(gh.files["climb/status.json"])["runs"][0]["run_id"] == "live-1"
    # a broken github client is advisory: no exception escapes
    service_boards(tmp_path, object(), "org/repo", None, 300.0)

    # and the two publishers fail independently: a board-view failure must
    # not mute a live state change
    class _BoardBroken(_BoardGitHub):
        def get_file(self, repo, path, ref):
            if path.startswith("climb/data/") or path == "climb/index.json":
                raise RuntimeError("board storage down")
            return super().get_file(repo, path, ref)

    gh2 = _BoardBroken()
    save_record(
        tmp_path,
        RunRecord(
            run_id="live-2",
            target="org/repo",
            task_title="t",
            state="waiting",
            benchmark="speedrun",
            agent_id="agent-02",
            created=100.0,
            updated=100.0,
        ),
        100.0,
    )
    service_boards(tmp_path, gh2, "org/repo", None, 400.0)
    runs = json.loads(gh2.files["climb/status.json"])["runs"]
    assert [r["run_id"] for r in runs] == ["live-1", "live-2"]


def test_status_outage_is_not_a_missing_file(tmp_path: Path) -> None:
    gh = _BoardGitHub()
    gh.files["climb/status.json"] = json.dumps({"runs": []})
    gh.index_outage = True  # the fake raises 500 on every get_file
    assert service_status(tmp_path, gh, "org/repo", 1.0) is False
    # malformed status is derived data: rewritten fresh, not preserved
    gh.index_outage = False
    # a dict WITHOUT runs must also be repaired, even when the fleet is empty
    # (an empty shape would otherwise compare equal and never rewrite)
    gh.files["climb/status.json"] = json.dumps({"published": 1.0})
    assert service_status(tmp_path / "no-runs", gh, "org/repo", 1.5) is True
    assert json.loads(gh.files["climb/status.json"])["runs"] == []
    gh.files["climb/status.json"] = json.dumps(["not", "a", "dict"])
    _live = RunRecord(run_id="r", target="org/repo", task_title="t", state="waiting", benchmark="b")
    save_record(tmp_path, _live, 1.0)
    assert service_status(tmp_path, gh, "org/repo", 2.0) is True
    assert json.loads(gh.files["climb/status.json"])["runs"][0]["run_id"] == "r"


STDOUT_WITH_CURVE = (
    "speedrun eval: seed 7\n"
    + "\n".join(
        f"step {s} val loss {4.5 - s / 10000:.6f} (bf16 screen)" for s in range(128, 9345, 128)
    )
    + "\nstep 9344 val loss 3.276098 (fp32)\n"
)


def test_curves_come_from_eval_stdout_downsampled(tmp_path: Path) -> None:
    from autoresearch.climbboard import MAX_CURVE_POINTS, collect_curves

    _terminal_run(tmp_path, "speedrun-1")
    ev = run_dir(tmp_path, "speedrun-1") / "eval-candidate-abc-def"
    ev.mkdir()
    (ev / "stdout").write_text(STDOUT_WITH_CURVE)
    # eval output is job output: a malformed number must not abort collection
    with open(ev / "stdout", "a") as fh:
        fh.write("step 9999 val loss 1.2.3 (fp32)\n")
    curves = collect_curves(tmp_path, "org/repo")["speedrun"]
    pts = curves["speedrun-1"]
    assert 2 < len(pts) <= MAX_CURVE_POINTS
    assert pts[0][0] == 128 and pts[-1] == [9344, 3.276098]
    # a run with no parsable eval simply has no curve
    _terminal_run(tmp_path, "speedrun-2", ending="merged")
    assert "speedrun-2" not in collect_curves(tmp_path, "org/repo")["speedrun"]


def test_fresh_curve_skips_garbage_points(tmp_path: Path) -> None:
    """A 400-nines 'loss' parses as float inf and a 400-digit step exceeds
    JS-safe integers; both skip the point, not the curve."""
    from autoresearch.climbboard import _curve_from_eval

    rd = tmp_path / "r"
    ev = rd / "eval-candidate-x"
    ev.mkdir(parents=True)
    ev.joinpath("stdout").write_text(
        "step 1 val loss 4.5\n"
        f"step 2 val loss {'9' * 400}\n"
        f"step {'9' * 400} val loss 4.4\n"
        f"step {'9' * 5000} val loss 4.4\n"
        "step 5 val loss 1e999\n"
        "step 3 val loss 4.3\n"
        "step 4 val loss 3.2e0\n"
        "step 5 val loss 3.1"  # no trailing newline: the point still counts
    )
    assert _curve_from_eval(rd) == [[1, 4.5], [3, 4.3], [4, 3.2], [5, 3.1]]


def test_cap_truncation_never_publishes_a_partial_number(tmp_path: Path, monkeypatch) -> None:
    import autoresearch.climbboard as cb

    rd = tmp_path / "r"
    ev = rd / "eval-candidate-x"
    ev.mkdir(parents=True)
    (ev / "stdout").write_text("step 1 val loss 4.5\nstep 2 val loss 4.4444\n")
    monkeypatch.setattr(cb, "MAX_CURVE_STDOUT_BYTES", 40)  # cuts inside 4.4444
    assert cb._curve_from_eval(rd) == [[1, 4.5]]
    # the cap is bytes, not characters: 4-byte emoji count 4x
    (ev / "stdout").write_bytes(b"step 1 val loss 4.5\n" + "🚀".encode() * 100)
    assert cb._curve_from_eval(rd) == [[1, 4.5]]


def test_fresh_curve_bounds_a_newline_free_stdout(tmp_path: Path, monkeypatch) -> None:
    """One giant line without newlines must not buffer past the cap."""
    import autoresearch.climbboard as cb

    rd = tmp_path / "r"
    ev = rd / "eval-candidate-x"
    ev.mkdir(parents=True)
    (ev / "stdout").write_text("step 1 val loss 4.5\n" + "x" * 100_000)
    monkeypatch.setattr(cb, "MAX_CURVE_STDOUT_BYTES", 64)
    assert cb._curve_from_eval(rd) == [[1, 4.5]]


def test_collect_curves_scans_only_the_publishable_tail(tmp_path: Path, monkeypatch) -> None:
    """Only the newest MAX_CURVE_RUNS attempts per benchmark are read —
    older stdouts cannot publish and must not cost I/O."""
    import autoresearch.climbboard as cb

    for i, rid in enumerate(["old-1", "mid-2", "new-3"]):
        record = RunRecord(
            run_id=rid,
            target="org/repo",
            task_title="t",
            state="ended",
            ending="negative-result",
            benchmark="speedrun",
            created=1.0,
            updated=float(i + 1),
        )
        save_record(tmp_path, record, float(i + 1))
        ev = run_dir(tmp_path, rid) / "eval-candidate-x"
        ev.mkdir(parents=True)
        (ev / "stdout").write_text(f"step {i + 1} val loss 4.{i}\n")
    monkeypatch.setattr(cb, "MAX_CURVE_RUNS", 2)
    curves = cb.collect_curves(tmp_path, "org/repo")["speedrun"]
    assert set(curves) == {"mid-2", "new-3"}


def test_fresh_curve_abandons_oversized_stdout(tmp_path: Path, monkeypatch) -> None:
    """A verbose eval must not exhaust the tick: scanning stops at the
    byte cap, keeping the points already parsed."""
    import autoresearch.climbboard as cb

    rd = tmp_path / "r"
    ev = rd / "eval-candidate-x"
    ev.mkdir(parents=True)
    ev.joinpath("stdout").write_text(
        "step 1 val loss 4.5\n" + "noise\n" * 50 + "step 2 val loss 4.1\n"
    )
    monkeypatch.setattr(cb, "MAX_CURVE_STDOUT_BYTES", 40)
    assert cb._curve_from_eval(rd) == [[1, 4.5]]


def test_curves_publish_capped_and_never_clobbered(tmp_path: Path) -> None:
    from autoresearch.climbboard import _merge_curves

    gh = _BoardGitHub()
    rows = [
        {"run_id": f"r{i}", "ended": f"2026-01-{1 + i // 24:02d} {i % 24:02d}:00"}
        for i in range(200)
    ]
    published = {"r199": [[1, 4.0]]}
    gh.files["climb/curves/b.json"] = json.dumps(published)
    fresh = {"r199": [[1, 9.9]], "r198": [[2, 3.5]]}
    merged = _merge_curves(gh, "org/repo", "b", rows, fresh)
    assert merged is not None and merged["changed"] is True
    assert merged["data"]["r199"] == [[1, 4.0]]  # published wins; never rewritten
    assert merged["data"]["r198"] == [[2, 3.5]]
    # a published EMPTY curve entry neither crashes nor blocks the fresh one
    gh.files["climb/curves/b.json"] = json.dumps({"r199": []})
    merged = _merge_curves(gh, "org/repo", "b", rows, fresh)
    assert merged is not None and merged["data"]["r199"] == [[1, 9.9]]
    # outage, malformed, or PARTLY malformed: sit the pass out
    gh.index_outage = True
    assert _merge_curves(gh, "org/repo", "b", rows, fresh) is None
    gh.index_outage = False
    gh.files["climb/curves/b.json"] = json.dumps(["nope"])
    assert _merge_curves(gh, "org/repo", "b", rows, fresh) is None
    gh.files["climb/curves/b.json"] = json.dumps({"good": [[1, 2]], "old": "bad"})
    assert _merge_curves(gh, "org/repo", "b", rows, fresh) is None
    # one null POINT is malformed too — it would throw in the chart code
    gh.files["climb/curves/b.json"] = json.dumps({"good": [[1, 2], None]})
    assert _merge_curves(gh, "org/repo", "b", rows, fresh) is None
    gh.files["climb/curves/b.json"] = json.dumps({"good": [[1, "2"]]})
    assert _merge_curves(gh, "org/repo", "b", rows, fresh) is None
    # json.loads accepts NaN; the chart must never receive it
    gh.files["climb/curves/b.json"] = '{"good": [[1, NaN]]}'
    assert _merge_curves(gh, "org/repo", "b", rows, fresh) is None
    # a 400-digit int is valid JSON but not a chart point (and must not
    # crash isfinite with OverflowError)
    gh.files["climb/curves/b.json"] = '{"good": [[' + "9" * 400 + ", 3.0]]}"
    assert _merge_curves(gh, "org/repo", "b", rows, fresh) is None


def test_board_publish_is_one_atomic_batch(tmp_path: Path) -> None:
    """Data, curves, and views land as ONE commit: a failed batch changes
    NOTHING (the page can never point at data missing from the branch),
    and the whole pass retries next time. A curve-file OUTAGE still skips
    the page rewrite instead of publishing a curveless page."""
    _terminal_run(tmp_path, "speedrun-1")
    gh = _BoardGitHub()
    ev = run_dir(tmp_path, "speedrun-1") / "eval-candidate-abc"
    ev.mkdir(parents=True)
    (ev / "stdout").write_text("step 1 val loss 4.5\nstep 2 val loss 4.1\n")

    gh.batch_fail = True
    assert service_climb_board(tmp_path, gh, "org/repo") == 0
    assert gh.files == {}  # all-or-nothing: nothing landed

    # the batch heals: everything lands together, curve on branch AND page
    gh.batch_fail = False
    assert service_climb_board(tmp_path, gh, "org/repo") == 5
    assert "climb/curves/speedrun.json" in gh.files
    assert "4.5" in gh.files["index.html"]

    # curve-file outage: CLIMB.md may still refresh, index.html sits out
    html_before = gh.files["index.html"]
    real_get = gh.get_file

    def flaky_get(repo, path, ref):
        if path.startswith("climb/curves/"):
            raise _GitHubError(500)
        return real_get(repo, path, ref)

    gh.get_file = flaky_get  # type: ignore[method-assign]
    _terminal_run(tmp_path, "speedrun-2")
    service_climb_board(tmp_path, gh, "org/repo")
    assert gh.files["index.html"] == html_before
    assert "speedrun-2" in gh.files["climb/data/speedrun.json"]
    assert gh.files["CLIMB.md"].count("negative-result") == 2


def test_curve_survives_a_vanishing_eval_dir(tmp_path: Path, monkeypatch) -> None:
    """The mtime sort must not abort the pass when an eval dir disappears
    between glob and stat."""
    from autoresearch.climbboard import _curve_from_eval

    rd = tmp_path / "r"
    for name in ("eval-candidate-a", "eval-candidate-b"):
        d = rd / name
        d.mkdir(parents=True)
        (d / "stdout").write_text("step 1 val loss 3.0\n")
    orig = Path.stat

    def vanishing(self, **kw):
        if self.name == "eval-candidate-a":
            raise FileNotFoundError(self)
        return orig(self, **kw)

    monkeypatch.setattr(Path, "stat", vanishing)
    assert _curve_from_eval(rd) == [[1, 3.0]]


def test_summarize_first_sentence_and_cap() -> None:
    from autoresearch.climbboard import summarize

    text = "The warmdown starts too early. Moving it later should preserve late learning rate."
    assert summarize(text) == "The warmdown starts too early."
    ramble = "a" * 200
    out = summarize(ramble)
    assert len(out) <= 91 and out.endswith("…")
    assert summarize("short line") == "short line"


def test_rows_carry_the_gates_verdict_note(tmp_path: Path) -> None:
    """'negative-result 9344' says nothing; the gate's own sentence ("real
    movement, not creditable") rides the row into the table and tooltip."""
    record = RunRecord(
        run_id="near-1",
        target="org/repo",
        task_title="t",
        state="ended",
        ending="negative-result",
        ending_note="delta +128 is inside the contract's significance floor (256): real movement",
        benchmark="speedrun",
        created=1.0,
        updated=2.0,
    )
    save_record(tmp_path, record, 2.0)
    (run_dir(tmp_path, "near-1") / "report.md").write_text(REPORT)
    rows = collect_rows(tmp_path, "org/repo")["speedrun"]
    assert rows[0].note.startswith("delta +128")
    from dataclasses import asdict

    md = render_md("org/repo", {"speedrun": [asdict(rows[0])]}, {"speedrun": "min"})
    assert "negative-result — delta +128" in md
    # a multi-line note must not split the table row
    row = asdict(rows[0]) | {"note": "line one\nline two | pipe"}
    md = render_md("org/repo", {"speedrun": [row]}, {"speedrun": "min"})
    (line,) = [ln for ln in md.splitlines() if "line one" in ln]
    assert "line two \\| pipe" in line


def test_report_link_uses_the_markers_own_path(tmp_path: Path) -> None:
    """An in-review archive keeps its date after the ENDED transition
    re-stamps `updated`: the marker's second line wins over a re-derived
    date (which could 404 across a UTC midnight)."""
    _terminal_run(tmp_path, "speedrun-9")
    (run_dir(tmp_path, "speedrun-9") / "ledger-published").write_text(
        "done\nreports/2026-08-19-speedrun-9.md"
    )
    (row,) = collect_rows(tmp_path, "org/repo")["speedrun"]
    assert row.report == "reports/2026-08-19-speedrun-9.md"
    # legacy marker without a path line: the derived date remains the fallback
    (run_dir(tmp_path, "speedrun-9") / "ledger-published").write_text("done")
    (row,) = collect_rows(tmp_path, "org/repo")["speedrun"]
    assert row.report.startswith("reports/2026-08-") and row.report.endswith("-speedrun-9.md")


def test_md_summarizes_and_links_reports() -> None:
    rows = [
        {
            "run_id": "speedrun-20260830-x-agent-01",
            "ended": "2026-08-30 10:00:00",
            "agent": "agent-01",
            "outcome": "negative-result",
            "candidate": 38146.0,
            "gpu_hours": 4.0,
            "hypothesis": (
                "First sentence here. Second sentence that should not appear in the cell."
            ),
        }
    ]
    rows[0]["report"] = "reports/2026-08-30-speedrun-20260830-x-agent-01.md"
    md = render_md("org/repo", {"speedrun": rows}, {"speedrun": "min"})
    assert "First sentence here." in md and "Second sentence" not in md
    assert "[report](reports/2026-08-30-speedrun-20260830-x-agent-01.md)" in md
    # a row whose report was never archived (adopted history, deferred
    # archive) renders WITHOUT a link — no 404s on the board
    rows[0]["report"] = ""
    assert "[report]" not in render_md("org/repo", {"speedrun": rows}, {"speedrun": "min"})


def test_html_carries_curves_and_direction_and_log_toggle() -> None:
    html = render_html(
        "org/repo",
        {"b": [{"run_id": "r1", "ended": "2026-01-01 00:00:00", "candidate": 5.0}]},
        {"b": "min"},
        {"b": {"r1": [[128, 4.5], [256, 4.4]]}},
    )
    assert "training curves" in html and '"curves"' in html
    assert "[[128, 4.5], [256, 4.4]]" in html
    assert "logarithmic" in html and "r.direction" in html


def test_status_carries_the_working_direction(tmp_path: Path) -> None:
    from autoresearch.climbboard import collect_status

    record = RunRecord(
        run_id="live-9",
        target="org/repo",
        task_title="t",
        state="waiting",
        benchmark="speedrun",
        agent_id="agent-03",
        created=1.0,
        updated=2.0,
        stage={
            "phase": "author-sleep",
            "syscall_note": (
                "Hypothesis: very long warmdowns preserve late LR. Sweeping 6 lengths now."
            ),
        },
    )
    save_record(tmp_path, record, 2.0)
    runs = collect_status(tmp_path, "org/repo", 3.0)["runs"]
    assert runs[0]["direction"].startswith("very long warmdowns preserve late LR")
    assert "Sweeping 6 lengths" not in runs[0]["direction"]


def test_bare_submit_still_gets_meters(tmp_path: Path) -> None:
    """A run that submitted without launching and without --minutes must
    still render meters: zero launches/spend, the contract's eval cap."""
    from types import SimpleNamespace

    from autoresearch.climbboard import collect_status

    record = RunRecord(
        run_id="bare-1",
        target="org/repo",
        task_title="t",
        state="waiting",
        benchmark="speedrun",
        agent_id="agent-04",
        created=1.0,
        updated=2.0,
        stage={"phase": "candidate", "syscall_note": "**Bold** claim: details later."},
    )
    save_record(tmp_path, record, 2.0)
    contract = SimpleNamespace(
        benchmarks=(SimpleNamespace(name="speedrun", depth_k=16, sleep_k=20, eval_minutes=240),),
        budgets=SimpleNamespace(gpu_hours_per_run=400.0),
    )
    (r,) = collect_status(tmp_path, "org/repo", 3.0, contract)["runs"]
    assert (r["launches_used"], r["sleeps_used"]) == (0, 0)
    assert r["gpu_hours_used"] == 0.0
    assert r["eval_minutes"] == 240  # the contract cap the gate runs under
    # an oversized contract value is clamped exactly like the dispatched job
    from autoresearch.dispatch import effective_eval_minutes

    contract.benchmarks[0].eval_minutes = 100_000
    (r,) = collect_status(tmp_path, "org/repo", 3.0, contract)["runs"]
    assert r["eval_minutes"] == effective_eval_minutes(100_000)
    assert r["direction"] == "Bold claim"  # markdown bold stripped
    # literal dunders survive (they are filenames, not emphasis)
    from autoresearch.climbboard import _phrase

    assert _phrase("Investigate __init__.py imports") == "Investigate __init__.py imports"


def test_status_progress_depth_and_phrases(tmp_path: Path) -> None:
    """The strip's live picture: finished/launched experiment jobs counted
    from exit-code files, depth budgets from the contract, the gate's
    walltime cap, and a direction cut to its first clause."""
    from types import SimpleNamespace

    from autoresearch.climbboard import collect_status
    from autoresearch.runstate import run_dir

    record = RunRecord(
        run_id="live-10",
        target="org/repo",
        task_title="t",
        state="waiting",
        benchmark="speedrun",
        agent_id="agent-01",
        created=1.0,
        updated=2.0,
        stage={
            "phase": "author-sleep",
            "launches_used": 3,
            "eval_minutes": 240,
            "syscall_launches": [
                {"name": "warmdown-length", "array": 3, "minutes": 180},
                {"name": "probe", "minutes": 30},
            ],
            "syscall_note": "Hypothesis: longer warmdown helps: sweeping 3 lengths plus a probe.",
        },
    )
    save_record(tmp_path, record, 2.0)
    rd = run_dir(tmp_path, "live-10")
    for name in ("warmdown-length.0", "warmdown-length.2", "probe"):
        d = rd / f"eval-launch-{name}"
        d.mkdir(parents=True)
        (d / "exit-code").write_text("0")
    contract = SimpleNamespace(
        benchmarks=(SimpleNamespace(name="speedrun", depth_k=16, sleep_k=20),),
        budgets=SimpleNamespace(gpu_hours_per_run=400.0),
    )
    (r,) = collect_status(tmp_path, "org/repo", 3.0, contract)["runs"]
    assert (r["exp_done"], r["exp_total"]) == (3, 4)
    assert (r["depth_k"], r["sleep_k"]) == (16, 20)
    assert r["eval_minutes"] == 240
    assert r["exp_minutes"] == 180
    assert r["gpu_hours_budget"] == 400.0
    # the first clause only — the strip is a glance, not a paragraph
    assert r["direction"] == "longer warmdown helps"
