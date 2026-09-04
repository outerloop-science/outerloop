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

    from outerloop.progress import LEADER_FILE, load_leader, update_leader, write_progress

    entries = update_leader(
        {},
        benchmark="reach",
        metric="success_rate",
        direction="max",
        baseline=0.54,
        candidate=0.54,
        run_id="r1",
        date="2026-08-09",
        run_seed=123456789,
    )
    write_progress(tmp_path, entries, "org/pilot")
    raw = _json.loads((tmp_path / LEADER_FILE).read_text())
    assert raw["reach"]["run_seed"] == 123456789
    assert load_leader(tmp_path)["reach"].run_seed == 123456789
    # pre-field ledger: run_seed absent -> 0
    del raw["reach"]["run_seed"]
    (tmp_path / LEADER_FILE).write_text(_json.dumps(raw))
    assert load_leader(tmp_path)["reach"].run_seed == 0
