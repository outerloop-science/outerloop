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
MAX_SUMMARY_CHARS = 90  # what the table shows; the full line stays in the row
MAX_CURVE_POINTS = 160
MAX_CURVE_RUNS = 150  # curves are heavy; the newest runs keep theirs
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


def summarize(text: str, cap: int = MAX_SUMMARY_CHARS) -> str:
    """The first sentence, capped — a table cell, not a paragraph."""
    text = text.strip()
    for stop in (". ", "; "):
        i = text.find(stop)
        if 0 < i < cap:
            return text[: i + 1]
    return text if len(text) <= cap else text[: cap - 1].rsplit(" ", 1)[0] + "…"


_CURVE_LINE = re.compile(r"^step (\d+) val loss ([0-9.]+)", re.M)


def _curve_from_eval(run_directory: Path) -> list[list[float]]:
    """(step, val loss) points parsed from the newest candidate eval's
    stdout, downsampled — the training curve behind the row's number.
    steps.jsonl dies with the job's scratch; stdout is what survives."""
    evals = sorted(
        (d for d in run_directory.glob("eval-candidate-*") if (d / "stdout").is_file()),
        key=lambda d: d.stat().st_mtime,
    )
    if not evals:
        return []
    try:
        text = (evals[-1] / "stdout").read_text(errors="replace")
    except OSError:
        return []
    points = [[int(s), float(v)] for s, v in _CURVE_LINE.findall(text)]
    if len(points) > MAX_CURVE_POINTS:
        stride = len(points) / (MAX_CURVE_POINTS - 1)
        points = [points[int(i * stride)] for i in range(MAX_CURVE_POINTS - 1)] + [points[-1]]
    return points


def collect_curves(root: Path, target: str) -> dict[str, dict[str, list[list[float]]]]:
    """{benchmark: {run_id: curve}} for terminal runs with a parsable eval."""
    out: dict[str, dict[str, list[list[float]]]] = {}
    for record in list_runs(root):
        if record.target != target or record.state != ENDED:
            continue
        curve = _curve_from_eval(run_dir(root, record.run_id))
        if curve:
            out.setdefault(record.benchmark or "benchmark", {})[record.run_id] = curve
    return out


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
                ended=ended.strftime("%Y-%m-%d %H:%M:%S"),
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
            "| ended (UTC) | agent | hypothesis | outcome | candidate | GPU-h | full |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for r in reversed(rows):
            outcome = str(r.get("outcome", ""))
            if r.get("pr_url"):
                outcome = f"[{outcome}]({r['pr_url']})"
            hyp = summarize(str(r.get("hypothesis") or "")).replace("|", "\\|")
            ended = str(r.get("ended", ""))
            report = f"[report](reports/{ended[:10]}-{r.get('run_id', '')}.md)" if ended else ""
            lines.append(
                f"| {ended} | {r.get('agent', '')} | {hyp} | {outcome} "
                f"| {_fmt(r.get('candidate'))} | {_fmt(r.get('gpu_hours'))} | {report} |"
            )
    return "\n".join(lines) + "\n"


def render_html(
    target: str,
    boards: dict[str, list[dict[str, Any]]],
    directions: dict[str, str],
    curves: dict[str, dict[str, list[list[float]]]] | None = None,
) -> str:
    """One self-contained page: the data is EMBEDDED (a browser blocks
    fetch() from a file:// page, and the direct-from-clone view must work),
    only chart.js arrives from its CDN — nothing bulky is committed to the
    target's branch. Light and dark follow the viewer's system theme."""
    # "<" is escaped INSIDE the JSON (still valid JSON): a hypothesis line is
    # agent-written text, and a literal </script> in it would close the inline
    # script and run whatever follows in the published page
    payload = json.dumps(
        {"boards": boards, "directions": directions, "curves": curves or {}}
    ).replace("<", "\\u003c")
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
        "<div id='now' class='chips'></div>\n"
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
        "  const logBox = document.createElement('label');\n"
        "  logBox.style.cssText = 'font-size:.8rem;color:var(--muted)';\n"
        "  const cb = document.createElement('input'); cb.type = 'checkbox';\n"
        "  logBox.append(cb, ' log scale'); card.append(logBox);\n"
        "  new Chart(c, {type: 'line', data: {labels: measured.map(r => r.ended),\n"
        "    datasets: [\n"
        "      {label: 'candidate', data: measured.map(r => r.candidate),\n"
        "       showLine: false, pointRadius: 4,\n"
        "       pointBackgroundColor: measured.map(r =>\n"
        "         won.has(r.outcome) ? css('--win') : css('--lose'))},\n"
        "      {label: 'best so far', data: bestSoFar, stepped: true,\n"
        "       borderColor: css('--accent'), borderWidth: 2, pointRadius: 0},\n"
        "      {label: 'baseline', data: measured.map(r => r.baseline),\n"
        "       borderColor: css('--base'), borderDash: [6, 4], borderWidth: 1.5,\n"
        "       pointRadius: 0}]},\n"
        "    options: {color: css('--muted'),\n"
        "      scales: {x: {ticks: {color: css('--muted'), maxTicksLimit: 8},\n"
        "                   grid: {color: css('--line')}},\n"
        "               y: {ticks: {color: css('--muted')}, grid: {color: css('--line')}}},\n"
        "      plugins: {legend: {labels: {color: css('--muted')}},\n"
        "        tooltip: {callbacks: {afterLabel:\n"
        "          (i) => measured[i.dataIndex].hypothesis}}}}});\n"
        "  const chart = Chart.getChart(c);\n"
        "  cb.onchange = () => {\n"
        "    chart.options.scales.y.type = cb.checked ? 'logarithmic' : 'linear';\n"
        "    chart.update();\n"
        "  };\n"
        "  // the training curves behind the numbers: newest attempts overlaid\n"
        "  const bcurves = data.curves[b] || {};\n"
        "  const withCurve = rows.filter(r => bcurves[r.run_id]).slice(-8);\n"
        "  if (withCurve.length) {\n"
        "    const h3 = document.createElement('h2');\n"
        "    h3.textContent = b + ' — training curves (newest attempts)';\n"
        "    const card2 = document.createElement('div'); card2.className = 'card';\n"
        "    const c2 = document.createElement('canvas'); card2.append(c2);\n"
        "    el.append(h3, card2);\n"
        "    const hues = [212, 152, 22, 282, 342, 62, 122, 242];\n"
        "    new Chart(c2, {type: 'line', data: {datasets: withCurve.map((r, i) => ({\n"
        "      label: r.agent + ' ' + (r.ended || '').slice(5, 16),\n"
        "      data: bcurves[r.run_id].map(p => ({x: p[0], y: p[1]})),\n"
        "      borderColor: `hsl(${hues[i % 8]} 60% 50%)`,\n"
        "      borderWidth: 1.5, pointRadius: 0}))},\n"
        "      options: {color: css('--muted'), parsing: false,\n"
        "        scales: {x: {type: 'linear', ticks: {color: css('--muted')},\n"
        "                     grid: {color: css('--line')}},\n"
        "                 y: {ticks: {color: css('--muted')}, grid: {color: css('--line')}}},\n"
        "        plugins: {legend: {labels: {color: css('--muted')}},\n"
        "          tooltip: {callbacks: {afterTitle: () => ''}}}}});\n"
        "  }\n"
        "}\n"
        "// the live strip: status.json is pushed on fleet state changes; the\n"
        "// elapsed time ticks locally. fetch() fails on a file:// page — the\n"
        "// strip just stays absent there.\n"
        "const now = document.getElementById('now');\n"
        "let strip = null;\n"
        "const refresh = () =>\n"
        "  fetch('climb/status.json', {cache: 'no-cache'})\n"
        "    .then(r => r.ok ? r.json() : null)\n"
        "    .then(s => { if (s && Array.isArray(s.runs)) { strip = s; render(); } })\n"
        "    .catch(() => {});\n"
        "const render = () => {\n"
        "  if (!strip) return;\n"
        "  {\n"
        "    const s = strip;\n"
        "    now.textContent = '';\n"
        "    for (const r of s.runs) {\n"
        "      const c = document.createElement('span'); c.className = 'chip';\n"
        "      const mins = Math.max(0, (Date.now() / 1000 - r.since) / 60);\n"
        "      const t = mins >= 90 ? (mins / 60).toFixed(1) + ' h' : Math.round(mins) + ' min';\n"
        "      const b = document.createElement('b');\n"
        "      b.textContent = r.agent + ' ' + r.state + (r.phase ? '/' + r.phase : '');\n"
        "      const gpu = r.gpu_hours_used ? ' · ' + Number(r.gpu_hours_used).toFixed(1)\n"
        "        + ' GPU-h' : '';\n"
        "      const dir = r.direction ? ' — ' + r.direction : '';\n"
        "      c.append(b, ' ' + t + gpu + dir);\n"
        "      if (r.pr_url) {\n"
        "        const a = document.createElement('a'); a.href = r.pr_url;\n"
        "        a.textContent = ' PR'; c.append(a);\n"
        "      }\n"
        "      now.append(c);\n"
        "    }\n"
        "    if (!s.runs.length) now.textContent = 'no active runs';\n"
        "  }\n"
        "};\n"
        "refresh();\n"
        "// the strip re-FETCHES every few minutes (new/left/changed runs) and\n"
        "// re-renders the elapsed time locally between fetches\n"
        "setInterval(refresh, 180000); setInterval(render, 30000);\n"
        "</script></main></body></html>\n"
    )


STATUS_PATH = "climb/status.json"
_LIVE_STATES = ("implementing", "waiting", "in-review", "concluding")


def collect_status(root: Path, target: str, now: float) -> dict[str, Any]:
    """The fleet's live picture for `target`: one entry per non-terminal run.
    Timestamps, not durations — the page computes elapsed time client-side,
    so the strip feels live between pushes."""
    runs = []
    for record in list_runs(root):
        if record.target != target or record.state not in _LIVE_STATES:
            continue
        stage = record.stage or {}
        note = str(stage.get("syscall_note") or stage.get("report") or "")
        _b, _c, hyp = _report_fields(note)
        runs.append(
            {
                "run_id": record.run_id,
                "agent": record.agent_id,
                "benchmark": record.benchmark,
                "state": record.state,
                "phase": stage.get("phase", ""),
                # the agent's own headline: what it says it is working on
                "direction": summarize(hyp or note.replace("\n", " ")),
                "since": record.updated or record.created,
                "launches_used": stage.get("launches_used"),
                "sleeps_used": stage.get("sleeps_used"),
                "gpu_hours_used": stage.get("gpu_hours_used"),
                "pr_url": record.pr_url,
            }
        )
    runs.sort(key=lambda r: str(r.get("run_id")))
    return {"target": target, "published": now, "runs": runs}


def service_status(root: Path, github: Any, target: str, now: float) -> bool:
    """Publish the strip when the fleet's SHAPE changed — a run appearing,
    leaving, or changing state/phase — never on every tick: the page shows
    elapsed time client-side, so timestamp-only drift is not worth a commit.
    Advisory like the board; True when a write happened."""
    status = collect_status(root, target, now)
    try:
        existing_raw: str | None = github.get_file(target, STATUS_PATH, BOARD_BRANCH)
    except Exception as exc:
        if getattr(exc, "status", None) != 404:
            # an outage must not look like a missing file: a rewrite here
            # would commit a new timestamp on every affected tick
            log.warning("status unreadable (%s); not rewritten", exc)
            return False
        existing_raw = None
    if existing_raw:
        try:
            existing = json.loads(existing_raw)
            # spend belongs in the shape: a same-phase re-park after new
            # launches moves gpu_hours_used and the strip must not go stale
            keys = ("run_id", "state", "phase", "gpu_hours_used")
            shape = lambda runs: [{k: r.get(k) for k in keys} for r in runs]
            if (
                isinstance(existing, dict)
                and isinstance(existing.get("runs"), list)
                and shape(existing["runs"]) == shape(status["runs"])
            ):
                return False
        except (ValueError, TypeError, AttributeError):
            pass  # malformed: the strip is derived, not history — rewrite it
    if not github.ensure_branch(target, BOARD_BRANCH):
        return False
    return bool(
        github.put_file(
            target, STATUS_PATH, json.dumps(status, indent=1) + "\n", BOARD_BRANCH, "fleet status"
        )
    )


def _merge_curves(
    github: Any, target: str, benchmark: str, rows: list[dict[str, Any]], fresh: dict
) -> dict[str, Any] | None:
    """Published curves plus new ones, kept only for the newest rows (curves
    are heavy; the cap is by recency of the attempt, and published curves
    are never rewritten). None when the published file cannot be read — the
    same sit-the-pass-out stance as everything else on the branch."""
    path = f"climb/data/{benchmark}-curves.json"
    try:
        raw: str | None = github.get_file(target, path, BOARD_BRANCH)
    except Exception as exc:
        if getattr(exc, "status", None) != 404:
            log.warning("board curves unreadable for %s (%s); skipped", benchmark, exc)
            return None
        raw = None
    published: dict[str, list[list[float]]] = {}
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                published = {str(k): v for k, v in data.items() if isinstance(v, list)}
            else:
                log.warning("board curves malformed for %s; skipped", benchmark)
                return None
        except ValueError:
            log.warning("board curves malformed for %s; skipped", benchmark)
            return None
    keep = [str(r.get("run_id")) for r in rows[-MAX_CURVE_RUNS:]]
    merged = {
        run_id: published.get(run_id) or fresh[run_id]
        for run_id in keep
        if run_id in published or run_id in fresh
    }
    return {"data": merged, "changed": merged != published}


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
    except ValueError:
        log.warning("climb index malformed; not rewriting it")
        return None  # same stance as an outage: never rewrite what we cannot see
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else None


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
    local_curves = collect_curves(root, target)
    index = _read_index(github, target)
    names = set(local) | set(index or {})
    directions = {**(index or {}), **(directions or {})}
    changed = 0
    boards: dict[str, list[dict[str, Any]]] = {}
    for benchmark in sorted(names):
        path = f"climb/data/{benchmark}.json"
        try:
            existing: str | None = github.get_file(target, path, BOARD_BRANCH)
            if existing is not None:
                parsed = json.loads(existing)
                if not isinstance(parsed, list):
                    raise ValueError("board data is not a list")
        except (ValueError, TypeError) as exc:
            # readable but not a row list: same stance as an outage — never
            # let a fresh merge overwrite what we cannot interpret
            log.warning("board JSON malformed for %s (%s); skipped", benchmark, exc)
            continue
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
    curves: dict[str, dict[str, list[list[float]]]] = {}
    for benchmark, rows in boards.items():
        merged_curves = _merge_curves(
            github, target, benchmark, rows, local_curves.get(benchmark, {})
        )
        if merged_curves is not None:
            curves[benchmark] = merged_curves["data"]
            if merged_curves["changed"] and github.ensure_branch(target, BOARD_BRANCH):
                changed += bool(
                    github.put_file(
                        target,
                        f"climb/data/{benchmark}-curves.json",
                        json.dumps(merged_curves["data"], indent=1) + "\n",
                        BOARD_BRANCH,
                        f"climb curves: {benchmark}",
                    )
                )
    if not boards and names:
        return changed  # every benchmark sat the pass out: leave the views alone
    # the index keeps every benchmark it knows — a transient failed read of
    # one benchmark's JSON must not orphan its history; the views simply
    # render without it until a later pass reads it again. An index that
    # could not be READ at all (None) is never rewritten this pass.
    wanted = {b: directions.get(b, "min") for b in sorted(set(boards) | set(index or {}))}
    views: list[tuple[str, str]] = [
        ("CLIMB.md", render_md(target, boards, wanted)),
        ("climb.html", render_html(target, boards, wanted, curves)),
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
