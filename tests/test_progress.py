def test_fmt_metric_renders_at_convention() -> None:
    from autoresearch.progress import fmt_metric

    assert fmt_metric(13.879999999999999) == "13.88"  # default 6 sig figs
    assert fmt_metric(13.879999999999999, 3) == "13.9"
    assert fmt_metric(0.00022631418795999767, 4) == "0.0002263"
    assert fmt_metric(1.0) == "1"


def test_render_markdown_honors_per_benchmark_digits() -> None:
    from autoresearch.progress import LeaderEntry, render_markdown

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
