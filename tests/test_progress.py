import json
from dataclasses import asdict, replace

import pytest

from outerloop.progress import (
    LEADER_FILE,
    LeaderEntry,
    LedgerReadError,
    PendingSubmission,
    confirm,
    load_leader,
    load_leader_strict,
    parse_pending,
    record_pending,
    reject,
    render_markdown,
    write_progress,
)


def test_fmt_metric_renders_at_convention() -> None:
    from outerloop.progress import fmt_metric

    assert fmt_metric(13.879999999999999) == "13.88"  # default 6 sig figs
    assert fmt_metric(13.879999999999999, 3) == "13.9"
    assert fmt_metric(0.00022631418795999767, 4) == "0.0002263"
    assert fmt_metric(1.0) == "1"


def test_render_markdown_honors_per_benchmark_digits() -> None:
    from outerloop.progress import LeaderEntry, render_markdown

    entries = {
        "tsp": LeaderEntry(
            benchmark="tsp",
            metric="mean_tour_length",
            direction="min",
            baseline=13.875696168157484,
            best=10.844662077277105,
            best_run="r1",
            updated="2026-08-09",
        )
    }
    md = render_markdown(entries, "org/pilot", digits={"tsp": 4})
    assert "| 13.88 |" in md and "| 10.84 |" in md
    md_default = render_markdown(entries, "org/pilot")
    assert "13.8757" in md_default  # 6 sig figs


def test_run_seed_round_trips_and_old_rows_load(tmp_path) -> None:
    """New rows carry the seed they were measured under; ledgers written
    before the field existed load with 0 (fixed pool / none recorded)."""
    import json as _json

    from outerloop.progress import LEADER_FILE, LeaderEntry, load_leader, write_progress

    entries = {
        "reach": LeaderEntry(
            benchmark="reach",
            metric="success_rate",
            direction="max",
            baseline=0.54,
            best=0.54,
            best_run="r1",
            updated="2026-08-09",
            run_seed=123456789,
        )
    }
    write_progress(tmp_path, entries, "org/pilot")
    raw = _json.loads((tmp_path / LEADER_FILE).read_text())
    assert raw["reach"]["run_seed"] == 123456789
    assert load_leader(tmp_path)["reach"].run_seed == 123456789
    # pre-field ledger: run_seed absent -> 0
    del raw["reach"]["run_seed"]
    (tmp_path / LEADER_FILE).write_text(_json.dumps(raw))
    assert load_leader(tmp_path)["reach"].run_seed == 0


def submission(**changes) -> PendingSubmission:
    return replace(
        PendingSubmission(
            benchmark="bench",
            metric="score",
            direction="max",
            baseline=0.123456789012345,
            candidate=0.234567890123456,
            run_id="run-1",
            run_seed=123456,
            ruler="ruler-1",
            measurement_signature="signature-1",
            measured_sha="sealed",
            pr_number=42,
            published_head="published",
            timestamp="2026-09-21",
        ),
        **changes,
    )


def ancestor(a: str, b: str) -> bool:
    return int(a) <= int(b)


def test_confirm_precision_provenance_and_seed_roundtrip(tmp_path):
    p = submission()
    entries = confirm({}, p, "1", is_ancestor=ancestor)
    write_progress(tmp_path, entries, "org/repo")
    assert load_leader_strict(tmp_path) == entries
    e = entries["bench"]
    assert (e.baseline, e.best, e.run_seed) == (p.baseline, p.candidate, p.run_seed)
    assert (e.main_commit, e.measured_sha) == ("1", "sealed")
    md = render_markdown(entries, "org/repo")
    assert "main commit" in md and "[1](https://github.com/org/repo/commit/1)" in md
    imported = replace(e, main_commit="", measured_sha="")
    assert "provenance unknown" in render_markdown({"bench": imported}, "org/repo")


def test_pending_roundtrip_and_rejection():
    p = submission()
    assert p.path == "results/submissions/run-1/published.json"
    assert parse_pending(record_pending(p)[p.path]) == p
    assert parse_pending(reject(p)[p.path]) is None
    assert LEADER_FILE not in record_pending(p) and LEADER_FILE not in reject(p)


@pytest.mark.parametrize("direction,better,worse", [("max", 3.0, 1.0), ("min", 1.0, 3.0)])
def test_solver_monotonic(direction, better, worse):
    p = submission(direction=direction, baseline=2.0, candidate=better)
    entries = confirm({}, p, "1", is_ancestor=ancestor)
    assert confirm(entries, replace(p, candidate=worse), "2", is_ancestor=ancestor) == entries
    assert confirm(entries, p, "1", is_ancestor=ancestor) == entries


def test_reset_order_and_late_old_solver():
    p = submission(candidate=10.0)
    initial = confirm({}, p, "1", is_ancestor=ancestor)
    reset = submission(kind="RESET", candidate=1.0, ruler="ruler-2", measurement_signature="sig-2")
    reset_first = confirm(initial, reset, "3", is_ancestor=ancestor)
    assert reset_first["bench"].baseline == reset_first["bench"].best == 1.0
    assert (
        confirm(reset_first, replace(p, candidate=100.0), "2", is_ancestor=ancestor) == reset_first
    )
    assert (
        confirm(reset_first, replace(p, candidate=100.0), "4", is_ancestor=ancestor) == reset_first
    )
    newer = replace(reset, kind="SOLVER", candidate=2.0)
    forward = confirm(reset_first, newer, "4", is_ancestor=ancestor)
    reverse = confirm(
        confirm({}, newer, "4", is_ancestor=ancestor), reset, "3", is_ancestor=ancestor
    )
    assert forward == reverse
    assert confirm(forward, replace(reset, candidate=99.0), "2", is_ancestor=ancestor) == forward


@pytest.mark.parametrize(
    "content",
    [
        "{",
        "[]",
        '{"bench": {}}',
        json.dumps(
            {"bench": asdict(LeaderEntry("bench", "m", "min", float("nan"), 1.0, "r", "d"))}
        ),
    ],
)
def test_strict_corruption_and_tolerant_display(tmp_path, content):
    path = tmp_path / LEADER_FILE
    path.parent.mkdir()
    path.write_text(content)
    with pytest.raises(LedgerReadError):
        load_leader_strict(tmp_path)
    assert load_leader(tmp_path) == {}


def test_strict_unreadable_and_missing(tmp_path):
    assert load_leader_strict(tmp_path) == {}
    (tmp_path / LEADER_FILE).mkdir(parents=True)
    with pytest.raises(LedgerReadError):
        load_leader_strict(tmp_path)
    assert load_leader(tmp_path) == {}


def test_invalid_pending_refused():
    with pytest.raises(LedgerReadError):
        record_pending(submission(run_id="../escape"))
