"""The climb board: what every attempt on a benchmark tried and what came
of it, published to the target's `research-log` branch as data plus two
views whenever runs end.

- `climb/data/<benchmark>.json` — one row per terminal attempt, dedup by run
  id (its own directory: benchmark names are contract-controlled, and a
  benchmark named `index` must not collide with `climb/index.json`);
  the contract everything else derives from (and what an external dashboard
  reads after the public flip).
- `CLIMB.md` — the numbers and the attempts table, rendered by GitHub.
- `climb.html` — a self-contained page that charts the JSON next to it
  (open it from a clone; a Pages site can serve it as-is later).

Publishing is idempotent by construction: rows merge by run id and files
are written only when their content changes, so the tick can call this
every pass without commit spam. Like the research-log ledger, the board is
advisory — a failure never stops the tick.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from autoresearch.runstate import ENDED, list_runs, run_dir

log = logging.getLogger("autoresearch.climbboard")

BOARD_BRANCH = "research-log"
MAX_HYPOTHESIS_CHARS = 160
MAX_ROWS_PER_BENCHMARK = 2000


@dataclass(frozen=True)
class ClimbRow:
    run_id: str
    agent: str
    ended: str  # ISO date
    outcome: str
    baseline: float | None
    candidate: float | None
    gpu_hours: float
    hypothesis: str
    pr_url: str


_NUM = re.compile(r"^(Baseline|Candidate): ([-+0-9.e]+)", re.M)
_HYP = re.compile(r"Hypothesis[:*\s]+(.+)", re.I)


def _report_fields(text: str) -> tuple[float | None, float | None, str]:
    """(baseline, candidate, hypothesis one-liner) out of a run report."""
    baseline = candidate = None
    for key, raw in _NUM.findall(text):
        try:
            value = float(raw)
        except ValueError:
            continue
        if key == "Baseline":
            baseline = value
        else:
            candidate = value
    hyp = ""
    m = _HYP.search(text)
    if m:
        hyp = re.sub(r"[`*_]|\s+", lambda g: " " if g.group().isspace() else "", m.group(1))
        hyp = hyp.strip().rstrip("-").strip()[:MAX_HYPOTHESIS_CHARS]
    return baseline, candidate, hyp


def collect_rows(root: Path, target: str) -> dict[str, list[ClimbRow]]:
    """Terminal attempts of `target` with a report, grouped by benchmark."""
    from datetime import UTC, datetime

    out: dict[str, list[ClimbRow]] = {}
    for record in list_runs(root):
        # only ENDED runs: an in-review run's outcome is not known yet (its
        # PR may be rejected), and a published row is never rewritten
        if record.target != target or record.state != ENDED:
            continue
        try:
            report = (run_dir(root, record.run_id) / "report.md").read_text()
        except OSError:
            continue
        baseline, candidate, hyp = _report_fields(report)
        stage = record.stage or {}
        ended = datetime.fromtimestamp(record.updated or record.created, tz=UTC)
        outcome = record.ending or "ended"
        out.setdefault(record.benchmark or "benchmark", []).append(
            ClimbRow(
                run_id=record.run_id,
                agent=record.agent_id,
                ended=ended.strftime("%Y-%m-%d %H:%M"),
                outcome=outcome,
                baseline=baseline,
                candidate=candidate,
                gpu_hours=round(float(stage.get("gpu_hours_used") or 0.0), 2),  # type: ignore[arg-type]
                hypothesis=hyp,
                pr_url=record.pr_url,
            )
        )
    return out


def merge_rows(existing_json: str | None, fresh: list[ClimbRow]) -> list[dict[str, Any]]:
    """Existing board rows plus any new ones, one per run id, oldest first.
    A run already on the board keeps its published row (reports are final at
    terminal state; the board never rewrites history)."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    if existing_json:
        try:
            for item in json.loads(existing_json):
                if isinstance(item, dict) and item.get("run_id") not in seen:
                    rows.append(item)
                    seen.add(str(item.get("run_id")))
        except ValueError:
            log.warning("unreadable board JSON; rebuilding from local records")
    for row in fresh:
        if row.run_id not in seen:
            rows.append(asdict(row))
            seen.add(row.run_id)
    rows.sort(key=lambda r: str(r.get("ended", "")))
    # the board is a bounded VIEW (the contents API caps file sizes); the
    # full history stays in reports/ on this same branch, and the trim is
    # said out loud in CLIMB.md, never silent
    return rows[-MAX_ROWS_PER_BENCHMARK:]


def _fmt(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def render_md(
    target: str, boards: dict[str, list[dict[str, Any]]], directions: dict[str, str]
) -> str:
    """CLIMB.md: per benchmark, the headline numbers and the attempts table
    (newest first). Plain markdown; the chart lives in climb.html."""
    lines = [
        "<!-- autoresearch:climb-board -->",
        f"# Climb — {target}",
        "",
        "Written by the kernel when runs end. Data: `climb/data/<benchmark>.json`;",
        "chart: open `climb.html` from a clone of this branch.",
    ]
    for benchmark in sorted(boards):
        rows = boards[benchmark]
        direction = directions.get(benchmark, "min")
        pick = max if direction == "max" else min
        measured = [r for r in rows if isinstance(r.get("candidate"), int | float)]
        improved = [r for r in rows if r.get("outcome") in ("merged", "improved")]
        baselines = [r["baseline"] for r in rows if isinstance(r.get("baseline"), int | float)]
        best = pick((r["candidate"] for r in measured), default=None)
        gpu = sum(float(r.get("gpu_hours") or 0.0) for r in rows)
        lines += [
            "",
            f"## {benchmark}",
            "",
            f"Attempts: **{len(rows)}** ({len(improved)} improved) · best candidate: "
            f"**{_fmt(best)}** ({direction}) · latest baseline: "
            f"**{_fmt(baselines[-1] if baselines else None)}** · GPU-hours: **{gpu:.1f}**",
        ]
        if len(rows) >= MAX_ROWS_PER_BENCHMARK:
            lines += [
                "",
                f"Only the newest {MAX_ROWS_PER_BENCHMARK} attempts are on the board; "
                "archived reports stay in `reports/` on this branch.",
            ]
        lines += [
            "",
            "| ended (UTC) | agent | hypothesis | outcome | candidate | GPU-h |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for r in reversed(rows):
            outcome = str(r.get("outcome", ""))
            if r.get("pr_url"):
                outcome = f"[{outcome}]({r['pr_url']})"
            hyp = str(r.get("hypothesis") or "").replace("|", "\\|")
            lines.append(
                f"| {r.get('ended', '')} | {r.get('agent', '')} | {hyp} | {outcome} "
                f"| {_fmt(r.get('candidate'))} | {_fmt(r.get('gpu_hours'))} |"
            )
    return "\n".join(lines) + "\n"


def render_html(
    target: str, boards: dict[str, list[dict[str, Any]]], directions: dict[str, str]
) -> str:
    """One self-contained page: the data is EMBEDDED (a browser blocks
    fetch() from a file:// page, and the direct-from-clone view must work),
    only chart.js arrives from its CDN — nothing bulky is committed to the
    target's branch. Light and dark follow the viewer's system theme."""
    # "<" is escaped INSIDE the JSON (still valid JSON): a hypothesis line is
    # agent-written text, and a literal </script> in it would close the inline
    # script and run whatever follows in the published page
    payload = json.dumps({"boards": boards, "directions": directions}).replace("<", "\\u003c")
    return (
        "<!doctype html>\n<html><head><meta charset='utf-8'>\n"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>\n"
        f"<title>Climb — {target}</title>\n"
        "<script src='https://cdn.jsdelivr.net/npm/chart.js@4'></script>\n"
        "<style>\n"
        ":root{--bg:#fafaf8;--card:#ffffff;--ink:#1a1d21;--muted:#6b7280;\n"
        "--line:#e3e4e0;--accent:#2456c8;--win:#1e8f5a;--lose:#9aa1a9;--base:#c23d3d}\n"
        "@media (prefers-color-scheme: dark){:root{--bg:#14161a;--card:#1c1f24;\n"
        "--ink:#e8eaed;--muted:#8b93a1;--line:#2a2e35;--accent:#5b8def;\n"
        "--win:#3fbf85;--lose:#6b7280;--base:#e06c5f}}\n"
        "body{font-family:system-ui,-apple-system,sans-serif;background:var(--bg);\n"
        "color:var(--ink);margin:0;padding:2.5rem 1.5rem;line-height:1.5}\n"
        "main{max-width:920px;margin:0 auto}\n"
        "header p{color:var(--muted);margin:.25rem 0 0;font-size:.9rem}\n"
        "h1{font-size:1.5rem;margin:0}\n"
        "h1 span{color:var(--muted);font-weight:400}\n"
        "h2{font-size:1.1rem;margin:2.5rem 0 .75rem}\n"
        ".chips{display:flex;flex-wrap:wrap;gap:.5rem;margin-bottom:1rem}\n"
        ".chip{background:var(--card);border:1px solid var(--line);border-radius:.4rem;\n"
        "padding:.35rem .7rem;font-size:.85rem;color:var(--muted)}\n"
        ".chip b{color:var(--ink);font-variant-numeric:tabular-nums;font-weight:600}\n"
        ".card{background:var(--card);border:1px solid var(--line);border-radius:.5rem;\n"
        "padding:1rem}\n"
        "</style></head><body><main>\n"
        f"<header><h1>{target} <span>· climb</span></h1>\n"
        "<p>Written by the kernel when runs end. Full reports live in "
        "<code>reports/</code> on this branch.</p></header>\n"
        "<div id='charts'></div>\n<script>\n"
        f"const data = {payload};\n"
        "const css = n => getComputedStyle(document.body).getPropertyValue(n);\n"
        "for (const b of Object.keys(data.boards).sort()) {\n"
        "  const rows = data.boards[b];\n"
        "  const dir = data.directions[b] === 'max' ? 'max' : 'min';\n"
        "  const pick = dir === 'max' ? Math.max : Math.min;\n"
        "  const won = new Set(['merged', 'improved']);\n"
        "  const measured = rows.filter(r => typeof r.candidate === 'number');\n"
        "  let best;\n"
        "  const bestSoFar = measured.map(r =>\n"
        "    best = best === undefined ? r.candidate : pick(best, r.candidate));\n"
        "  const wins = rows.filter(r => won.has(r.outcome)).length;\n"
        "  const gpu = rows.reduce((a, r) => a + (r.gpu_hours || 0), 0);\n"
        "  const el = document.getElementById('charts');\n"
        "  const h = document.createElement('h2'); h.textContent = b; el.append(h);\n"
        "  const chips = document.createElement('div'); chips.className = 'chips';\n"
        "  const chip = (label, value) => {\n"
        "    const c = document.createElement('span'); c.className = 'chip';\n"
        "    const strong = document.createElement('b'); strong.textContent = value;\n"
        "    c.append(label + ' ', strong); chips.append(c); };\n"
        "  chip('attempts', rows.length); chip('improved', wins);\n"
        "  chip('best (' + dir + ')', measured.length ? bestSoFar[bestSoFar.length - 1] : '—');\n"
        "  chip('GPU-hours', gpu.toFixed(1)); el.append(chips);\n"
        "  const card = document.createElement('div'); card.className = 'card';\n"
        "  const c = document.createElement('canvas'); card.append(c); el.append(card);\n"
        "  new Chart(c, {data: {labels: measured.map(r => r.ended),\n"
        "    datasets: [\n"
        "      {type: 'scatter', label: 'candidate', data: measured.map(r => r.candidate),\n"
        "       pointRadius: 4, pointBackgroundColor: measured.map(r =>\n"
        "         won.has(r.outcome) ? css('--win') : css('--lose'))},\n"
        "      {type: 'line', label: 'best so far', data: bestSoFar, stepped: true,\n"
        "       borderColor: css('--accent'), borderWidth: 2, pointRadius: 0},\n"
        "      {type: 'line', label: 'baseline', data: measured.map(r => r.baseline),\n"
        "       borderColor: css('--base'), borderDash: [6, 4], borderWidth: 1.5,\n"
        "       pointRadius: 0}]},\n"
        "    options: {color: css('--muted'),\n"
        "      scales: {x: {ticks: {color: css('--muted'), maxTicksLimit: 8},\n"
        "                   grid: {color: css('--line')}},\n"
        "               y: {ticks: {color: css('--muted')}, grid: {color: css('--line')}}},\n"
        "      plugins: {legend: {labels: {color: css('--muted')}},\n"
        "        tooltip: {callbacks: {afterLabel:\n"
        "          (i) => measured[i.dataIndex].hypothesis}}}}});\n"
        "}\n</script></main></body></html>\n"
    )


def contract_directions(contract: Any) -> dict[str, str]:
    """{benchmark: direction} out of a loaded contract (None -> {})."""
    if contract is None:
        return {}
    return {b.name: b.direction for b in contract.benchmarks}


def _read_index(github: Any, target: str) -> dict[str, str] | None:
    """climb/index.json: {benchmark: direction} for every benchmark ever
    published — the board's own memory, so a benchmark whose local records
    were cleaned up (or that left the contract) keeps its place in the
    views. {} means the board has no index yet (a 404); None means the read
    FAILED — the caller must not rewrite the index it could not see, or a
    transient outage would shrink it."""
    try:
        raw = github.get_file(target, "climb/index.json", BOARD_BRANCH)
    except Exception as exc:
        status = getattr(exc, "status", None)
        if status == 404:
            return {}
        log.warning("climb index unreadable (%s); not rewriting it", exc)
        return None
    try:
        data = json.loads(raw)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except ValueError:
        return {}


def service_climb_board(
    root: Path, github: Any, target: str, directions: dict[str, str] | None = None
) -> int:
    """Publish the board for `target`. Returns how many files changed.

    Every file is compared against the branch and written only when
    different — which is also the retry: a view (or index) whose upload
    failed last pass differs again next pass. A benchmark whose fresh JSON
    could not be uploaded is rendered from its last PUBLISHED rows, so the
    views never point at data that is not on the branch."""
    local = collect_rows(root, target)
    index = _read_index(github, target)
    names = set(local) | set(index or {})
    if not names:
        return 0
    directions = {**(index or {}), **(directions or {})}
    changed = 0
    boards: dict[str, list[dict[str, Any]]] = {}
    for benchmark in sorted(names):
        path = f"climb/data/{benchmark}.json"
        try:
            existing: str | None = github.get_file(target, path, BOARD_BRANCH)
        except Exception as exc:
            if getattr(exc, "status", None) != 404:
                # an outage is not an empty board: overwriting would replace
                # this benchmark's published history — sit the pass out (its
                # index entry survives)
                log.warning("board JSON unreadable for %s (%s); skipped", benchmark, exc)
                continue
            existing = None
        rows = merge_rows(existing, local.get(benchmark, []))
        if not rows:
            continue
        text = json.dumps(rows, indent=1) + "\n"
        if text == existing:
            boards[benchmark] = rows
            continue
        if github.ensure_branch(target, BOARD_BRANCH) and github.put_file(
            target, path, text, BOARD_BRANCH, f"climb board: {benchmark}"
        ):
            changed += 1
            boards[benchmark] = rows
        elif existing:
            # the branch still holds the previous rows: render those
            boards[benchmark] = merge_rows(existing, [])
    if not boards:
        return changed
    # the index keeps every benchmark it knows — a transient failed read of
    # one benchmark's JSON must not orphan its history; the views simply
    # render without it until a later pass reads it again. An index that
    # could not be READ at all (None) is never rewritten this pass.
    wanted = {b: directions.get(b, "min") for b in sorted(set(boards) | set(index or {}))}
    views: list[tuple[str, str]] = [
        ("CLIMB.md", render_md(target, boards, wanted)),
        ("climb.html", render_html(target, boards, wanted)),
    ]
    if index is not None:
        views.insert(0, ("climb/index.json", json.dumps(wanted, indent=1) + "\n"))
    for path, content in views:
        if (
            content != github.get_file_content(target, path, BOARD_BRANCH)
            and github.ensure_branch(target, BOARD_BRANCH)
            and github.put_file(target, path, content, BOARD_BRANCH, "climb board")
        ):
            changed += 1
    return changed
