"""The climb board: what every attempt on a benchmark tried and what came
of it, published to the target's `research-log` branch as data plus two
views whenever runs end.

- `climb/data/<benchmark>.json` — one row per terminal attempt, dedup by run
  id (its own directory: benchmark names are contract-controlled, and a
  benchmark named `index` must not collide with `climb/index.json`);
  the contract everything else derives from (and what an external dashboard
  reads after the public flip).
- `CLIMB.md` — the numbers and the attempts table, rendered by GitHub.
- `index.html` — a self-contained page that charts the JSON next to it
  (open it from a clone; a Pages site can serve it as-is later).

Publishing is idempotent by construction: rows merge by run id and files
are written only when their content changes, so the tick can call this
every pass without commit spam. Like the research-log ledger, the board is
advisory — a failure never stops the tick.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
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
    report: str = ""  # reports/<file>.md when the ledger has archived it
    note: str = ""  # the gate's own verdict sentence, when it recorded one


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


_CURVE_LINE = re.compile(
    # digit bounds in the pattern: int() never sees more digits than fit a
    # JS-safe integer, float() never sees a 400-nines mantissa (a longer
    # number simply fails the match and the line sits out)
    r"^step (\d{1,15}) val loss (\d{1,10}(?:\.\d{1,12})?(?:[eE][+-]?\d{1,3})?)(?=\s|$)",
    re.M,
)
# a verbose eval must not exhaust the tick: stdout is scanned line by
# line and abandoned past this many bytes (curves are diagnostics)
MAX_CURVE_STDOUT_BYTES = 32 * 1024 * 1024


def _curve_from_eval(run_directory: Path) -> list[list[float]]:
    """(step, val loss) points parsed from the newest candidate eval's
    stdout, downsampled — the training curve behind the row's number.
    steps.jsonl dies with the job's scratch; stdout is what survives."""

    def mtime(d: Path) -> float:
        try:
            return d.stat().st_mtime
        except OSError:
            return 0.0  # vanished between glob and sort: sorts oldest, still readable-guarded

    evals = sorted(
        (d for d in run_directory.glob("eval-candidate-*") if (d / "stdout").is_file()),
        key=mtime,
    )
    if not evals:
        return []
    try:
        with (evals[-1] / "stdout").open("rb") as fh:
            # a byte-mode bounded read caps memory whatever the content — a
            # text-mode read counts characters and 4-byte UTF-8 overshoots 4x
            raw = fh.read(MAX_CURVE_STDOUT_BYTES + 1)
    except OSError:
        return []
    if len(raw) > MAX_CURVE_STDOUT_BYTES:
        # truncated: drop the partial tail line so the EOF-tolerant pattern
        # can never publish a number the cap cut in half
        raw = raw[:MAX_CURVE_STDOUT_BYTES].rsplit(b"\n", 1)[0]
    text = raw.decode("utf-8", errors="replace")
    points = []
    for m in _CURVE_LINE.finditer(text):
        val = float(m.group(2))
        if not math.isfinite(val):  # e-notation can still overflow (1e999)
            continue
        points.append([int(m.group(1)), val])
    if len(points) > MAX_CURVE_POINTS:
        stride = len(points) / (MAX_CURVE_POINTS - 1)
        points = [points[int(i * stride)] for i in range(MAX_CURVE_POINTS - 1)] + [points[-1]]
    return points


def collect_curves(root: Path, target: str) -> dict[str, dict[str, list[list[float]]]]:
    """{benchmark: {run_id: curve}} for terminal runs with a parsable eval.
    Only the newest MAX_CURVE_RUNS per benchmark are scanned — the merge
    keeps exactly that tail, so older stdouts cannot publish anyway."""
    out: dict[str, dict[str, list[list[float]]]] = {}
    ended = [r for r in list_runs(root) if r.target == target and r.state == ENDED]
    ended.sort(key=lambda r: r.updated or r.created, reverse=True)
    scanned: dict[str, int] = {}
    for record in ended:
        bench = record.benchmark or "benchmark"
        if scanned.get(bench, 0) >= MAX_CURVE_RUNS:
            continue
        scanned[bench] = scanned.get(bench, 0) + 1
        curve = _curve_from_eval(run_dir(root, record.run_id))
        if curve:
            out.setdefault(bench, {})[record.run_id] = curve
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
        # link the report only when the ledger's own marker says it is on the
        # branch (adopted-unpublished history and not-yet-archived runs would
        # otherwise render dead links)
        report = ""
        try:
            marker = (run_dir(root, record.run_id) / "ledger-published").read_text()
            lines = marker.splitlines()
            if lines and lines[0].startswith(("archived", "pointer-pending", "done")):
                if len(lines) > 1 and lines[1].startswith("reports/") and lines[1].endswith(".md"):
                    # the ledger's own path: an in-review archive keeps its
                    # date even after the ENDED transition re-stamps updated
                    report = lines[1]
                else:  # legacy marker without a path line
                    report = f"reports/{ended.strftime('%Y-%m-%d')}-{record.run_id}.md"
        except OSError:
            pass
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
                report=report,
                note=summarize(record.ending_note or "", 120),
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
    (newest first). Plain markdown; the chart lives in index.html."""
    lines = [
        "<!-- autoresearch:climb-board -->",
        f"# Climb — {target}",
        "",
        "Written by the kernel when runs end. Data: `climb/data/<benchmark>.json`;",
        "chart: open `index.html` from a clone of this branch.",
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
            if r.get("note"):
                # the gate's reason makes a near miss legible ("real
                # movement, not creditable" reads differently from a DNF)
                # flattened: a newline inside a cell would split the table row
                flat = " ".join(str(r["note"]).split())
                outcome += " — " + summarize(flat, 80).replace("|", "\\|")
            hyp = summarize(str(r.get("hypothesis") or "")).replace("|", "\\|")
            ended = str(r.get("ended", ""))
            report = f"[report]({r['report']})" if r.get("report") else ""
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
        ":root{--bg:#f6f7f4;--card:#ffffff;--ink:#1c2025;--muted:#5d6570;\n"
        "--line:#e1e3de;--accent:#2d54ae;--win:#157a4b;--lose:#a7acb0;\n"
        "--base:#b8433a;--near:#ac7714}\n"
        "@media (prefers-color-scheme: dark){:root:not([data-theme=light]){\n"
        "--bg:#121418;--card:#1a1d23;--ink:#e6e8eb;--muted:#99a1ad;\n"
        "--line:#2a2e36;--accent:#7398e6;--win:#3cbd83;--lose:#656c76;\n"
        "--base:#de7263;--near:#cf9c33}}\n"
        ":root[data-theme=dark]{--bg:#121418;--card:#1a1d23;--ink:#e6e8eb;\n"
        "--muted:#99a1ad;--line:#2a2e36;--accent:#7398e6;--win:#3cbd83;\n"
        "--lose:#656c76;--base:#de7263;--near:#cf9c33}\n"
        "a{color:var(--accent)}\n"
        "header{display:flex;align-items:center;justify-content:space-between}\n"
        "#theme{background:var(--card);border:1px solid var(--line);\n"
        "color:var(--muted);border-radius:.4rem;padding:.2rem .6rem;\n"
        "font-size:.75rem;cursor:pointer}\n"
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
        ".run{background:var(--card);border:1px solid var(--line);border-radius:.4rem;\n"
        "padding:.45rem .7rem;font-size:.85rem;color:var(--muted);\n"
        "width:16.5rem;box-sizing:border-box}\n"
        ".run .top{display:flex;align-items:center;gap:.5rem}\n"
        ".run b{color:var(--ink)}\n"
        ".run .dir{margin-top:.15rem;font-size:.8rem}\n"
        ".run .lm{display:grid;grid-template-columns:auto 1fr auto;\n"
        "  gap:.18rem .4rem;align-items:center;margin-top:.3rem;\n"
        "  font-size:.65rem;color:var(--muted)}\n"
        ".run .bar{position:relative;height:3px;border-radius:2px;\n"
        "  background:var(--line);overflow:hidden}\n"
        ".run .bar span{position:absolute;left:0;top:0;height:100%}\n"
        ".run .meter{display:flex;gap:2px}\n"
        ".run .meter i{flex:1;height:3px;border-radius:1px;background:var(--line)}\n"
        ".clegend{display:flex;flex-wrap:wrap;gap:.4rem 1.4rem;\n"
        "margin:.5rem 0 .25rem;font-size:.75rem;color:var(--muted)}\n"
        ".clegend .lgroup{display:flex;align-items:center;gap:.55rem}\n"
        ".clegend b{cursor:pointer;font-weight:600}\n"
        ".clegend .litem{display:flex;align-items:center;gap:.3rem;cursor:pointer}\n"
        ".clegend .litem i{width:14px;height:8px;border:2px solid;\n"
        "border-radius:2px;display:inline-block;box-sizing:border-box}\n"
        ".pill{border-radius:.6rem;padding:.05rem .55rem;font-size:.72rem;\n"
        "font-weight:600;color:#fff}\n"
        ".card{background:var(--card);border:1px solid var(--line);border-radius:.5rem;\n"
        "padding:1rem}\n"
        ".card label{font-size:.8rem;color:var(--muted);margin-right:1rem}\n"
        "</style></head><body><main>\n"
        f"<header><h1>{target} <span>· climb</span></h1>\n"
        "<button id='theme' title='theme: auto / light / dark'>auto</button>\n"
        "</header>\n"
        "<div id='now' class='chips'></div>\n"
        "<div id='charts'></div>\n<script>\n"
        f"const data = {payload};\n"
        "const css = n => getComputedStyle(document.body).getPropertyValue(n);\n"
        "// theme: auto follows the OS; the button cycles auto/light/dark and\n"
        "// the charts redraw so their computed colors follow\n"
        "const themeBtn = document.getElementById('theme');\n"
        "let onTheme = () => {};\n"
        "const applyTheme = t => {\n"
        "  if (t === 'light' || t === 'dark')\n"
        "    document.documentElement.dataset.theme = t;\n"
        "  else delete document.documentElement.dataset.theme;\n"
        "  themeBtn.textContent = t;\n"
        "};\n"
        "let theme = 'auto';\n"
        "try { theme = localStorage.getItem('climb-theme') || 'auto'; }\n"
        "catch (e) {}\n"
        "applyTheme(theme);\n"
        "themeBtn.onclick = () => {\n"
        "  theme = theme === 'auto' ? 'light' : theme === 'light' ? 'dark' : 'auto';\n"
        "  try { localStorage.setItem('climb-theme', theme); } catch (e) {}\n"
        "  applyTheme(theme);\n"
        "  redraws.forEach(f => f()); onTheme();\n"
        "};\n"
        "// some hosts stamp data-theme on the root after load; without\n"
        "// this the charts keep first-load colors on the flipped ground\n"
        "new MutationObserver(() => { redraws.forEach(f => f()); onTheme(); })\n"
        "  .observe(document.documentElement,\n"
        "    {attributes: true, attributeFilter: ['data-theme']});\n"
        "const mq = matchMedia('(prefers-color-scheme: dark)');\n"
        "const onMq = () => { redraws.forEach(f => f()); onTheme(); };\n"
        "// older iOS Safari has addListener only\n"
        "if (mq.addEventListener) mq.addEventListener('change', onMq);\n"
        "else if (mq.addListener) mq.addListener(onMq);\n"
        "// legend: solid box = shown, hollow box = hidden (no strikethrough)\n"
        "const boxLegend = {labels: {generateLabels: (chart) =>\n"
        "  chart.data.datasets.map((ds, i) => {\n"
        "    const shown = chart.isDatasetVisible(i);\n"
        "    const color = ds.borderColor || ds.pointBackgroundColor;\n"
        "    return {text: ds.label, datasetIndex: i, hidden: false,\n"
        "      fillStyle: shown ? color : 'rgba(0,0,0,0)',\n"
        "      strokeStyle: color, lineWidth: 2,\n"
        "      fontColor: css('--muted')};\n"
        "  })}};\n"
        "const axis = () => ({ticks: {color: css('--muted')}, grid: {color: css('--line')}});\n"
        "// tooltip copy stays a glance: first clause, strip-sized\n"
        "const phrase = s => {\n"
        "  if (!s) return '';\n"
        "  for (const sep of ['. ', '; ']) {\n"
        "    const i = s.indexOf(sep);\n"
        "    if (i > 0 && i < 200) { s = s.slice(0, i + 1); break; }\n"
        "  }\n"
        "  for (const sep of [': ', ' \u2014 ', ' -- ']) {\n"
        "    const i = s.indexOf(sep);\n"
        "    if (i > 0) s = s.slice(0, i);\n"
        "  }\n"
        "  if (s.length > 64) s = s.slice(0, 63).trimEnd() + '\u2026';\n"
        "  return s;\n"
        "};\n"
        "const hues = [212, 152, 22, 282, 342, 62, 122, 242];\n"
        "// identity color from the agent id itself, so the tooltip, the\n"
        "// curves chart, and the strip agree even across benchmarks\n"
        "const agentHue = a => {\n"
        "  const m = /(\\d+)$/.exec(a || '');\n"
        "  const i = m ? parseInt(m[1], 10) - 1\n"
        "    : [...String(a || '')].reduce((h, c) => h + c.charCodeAt(0), 0);\n"
        "  return hues[((i % 8) + 8) % 8];\n"
        "};\n"
        "const isDark = () => document.documentElement.dataset.theme === 'dark'\n"
        "  || (document.documentElement.dataset.theme !== 'light'\n"
        "      && matchMedia('(prefers-color-scheme: dark)').matches);\n"
        "// t in [0,1]: 1 = the agent's identity color (its newest curve);\n"
        "// smaller t fades toward the background, so earlier runs recede\n"
        "// (lighter on paper, dimmer in the dark theme)\n"
        "const agentShade = (a, t) => isDark()\n"
        "  ? `hsl(${agentHue(a)} ${40 + 25 * t}% ${30 + 22 * t}%)`\n"
        "  : `hsl(${agentHue(a)} ${45 + 15 * t}% ${72 - 24 * t}%)`;\n"
        "const agentColor = a => agentShade(a, 1);\n"
        "const redraws = [];\n"
        "for (const b of Object.keys(data.boards).sort()) {\n"
        "  const rows = data.boards[b];\n"
        "  const dir = data.directions[b] === 'max' ? 'max' : 'min';\n"
        "  const pick = dir === 'max' ? Math.max : Math.min;\n"
        "  const won = new Set(['merged', 'improved']);\n"
        "  const beats = r => typeof r.baseline === 'number' && (dir === 'max'\n"
        "    ? r.candidate > r.baseline : r.candidate < r.baseline);\n"
        "  const measured = rows.filter(r => typeof r.candidate === 'number');\n"
        "  const baselines = measured.map(r => r.baseline).filter(v => typeof v === 'number');\n"
        "  // off-scale attempts (a DNF scored as the whole budget) squash the\n"
        "  // axis: hidden unless asked for. Off-scale = worse than 1.5x the\n"
        "  // worst baseline, whichever way this benchmark points.\n"
        "  const worstBase = baselines.length ? (dir === 'max'\n"
        "    ? Math.min(...baselines) : Math.max(...baselines)) : null;\n"
        "  const offScale = v => worstBase !== null && (dir === 'max'\n"
        "    ? v < worstBase / 1.5 : v > worstBase * 1.5);\n"
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
        "  chip('GPU-hours (recorded)', gpu.toFixed(1)); el.append(chips);\n"
        "  const card = document.createElement('div'); card.className = 'card';\n"
        "  const c = document.createElement('canvas'); card.append(c); el.append(card);\n"
        "  const logBox = document.createElement('label');\n"
        "  const cb = document.createElement('input'); cb.type = 'checkbox';\n"
        "  logBox.append(cb, ' log scale');\n"
        "  const outBox = document.createElement('label');\n"
        "  const ob = document.createElement('input'); ob.type = 'checkbox';\n"
        "  outBox.append(ob, ' show off-scale attempts');\n"
        "  card.append(logBox, outBox);\n"
        "  let chart;\n"
        "  const draw = () => {\n"
        "    const view = measured.filter(r => ob.checked || !offScale(r.candidate));\n"
        "    let vb;\n"
        "    const viewBest = view.map(r => vb = vb === undefined ? r.candidate\n"
        "      : pick(vb, r.candidate));\n"
        "    if (chart) chart.destroy();\n"
        "    chart = new Chart(c, {type: 'line', data: {labels: view.map(r => r.ended),\n"
        "      datasets: [\n"
        "        {label: 'candidate', data: view.map(r => r.candidate),\n"
        "         showLine: false, pointRadius: 4,\n"
        "         borderColor: css('--lose'),\n"
        "         pointBackgroundColor: view.map(r =>\n"
        "           won.has(r.outcome) ? css('--win')\n"
        "           : beats(r) ? css('--near') : css('--lose'))},\n"
        "        {label: 'best so far', data: viewBest, stepped: true,\n"
        "         borderColor: css('--accent'), borderWidth: 2, pointRadius: 0},\n"
        "        {label: 'baseline', data: view.map(r => r.baseline),\n"
        "         borderColor: css('--base'), borderDash: [6, 4], borderWidth: 1.5,\n"
        "         pointRadius: 0}]},\n"
        "      options: {color: css('--muted'),\n"
        "        scales: {x: {...axis(), ticks: {color: css('--muted'), maxTicksLimit: 8}},\n"
        "                 y: {...axis(), type: cb.checked ? 'logarithmic' : 'linear'}},\n"
        "        plugins: {legend: boxLegend,\n"
        "          tooltip: {\n"
        "            // best-so-far and baseline pass exactly through candidate\n"
        "            // points; without the filter every coincident item repeats\n"
        "            // the same row in the pop-up\n"
        "            filter: i => i.datasetIndex === 0,\n"
        "            callbacks: {\n"
        "            title: its => { const r0 = view[its[0]?.dataIndex];\n"
        "              return r0 ? r0.agent + '  ' + (r0.ended || '').slice(0, 16) : ''; },\n"
        "            labelColor: i => ({\n"
        "              borderColor: agentColor(view[i.dataIndex].agent),\n"
        "              backgroundColor: agentColor(view[i.dataIndex].agent)}),\n"
        "            afterLabel: (i) =>\n"
        "              [phrase(view[i.dataIndex].hypothesis), view[i.dataIndex].note]\n"
        "                .filter(Boolean).join('\\n')}}}}});\n"
        "  };\n"
        "  cb.onchange = draw; ob.onchange = draw; draw(); redraws.push(draw);\n"
        "  // the training curves behind the numbers: newest attempts overlaid\n"
        "  const bcurves = data.curves[b] || {};\n"
        "  const withCurve = rows.filter(r => bcurves[r.run_id]).slice(-8);\n"
        "  if (withCurve.length) {\n"
        "    const h3 = document.createElement('h2');\n"
        "    h3.textContent = b + ' — training curves (newest attempts)';\n"
        "    const card2 = document.createElement('div'); card2.className = 'card';\n"
        "    const c2 = document.createElement('canvas'); card2.append(c2);\n"
        "    const lx = document.createElement('label');\n"
        "    const lxb = document.createElement('input'); lxb.type = 'checkbox';\n"
        "    lx.append(lxb, ' log x');\n"
        "    const ly = document.createElement('label');\n"
        "    const lyb = document.createElement('input'); lyb.type = 'checkbox';\n"
        "    ly.append(lyb, ' log y');\n"
        "    // custom legend: every run stays its own item, grouped under\n"
        "    // its agent — the agent name toggles the whole group, a run's\n"
        "    // box toggles that line (solid = visible, hollow = hidden)\n"
        "    const legendEl = document.createElement('div');\n"
        "    legendEl.className = 'clegend';\n"
        "    card2.append(legendEl, lx, ly);\n"
        "    el.append(h3, card2);\n"
        "    let chart2;\n"
        "    const buildLegend = () => {\n"
        "      legendEl.textContent = '';\n"
        "      const groups = [];\n"
        "      withCurve.forEach((r, i) => {\n"
        "        let g = groups.find(x => x.agent === r.agent);\n"
        "        if (!g) { g = {agent: r.agent, items: []}; groups.push(g); }\n"
        "        g.items.push(i);\n"
        "      });\n"
        "      for (const g of groups) {\n"
        "        const wrap = document.createElement('span');\n"
        "        wrap.className = 'lgroup';\n"
        "        const name = document.createElement('b');\n"
        "        name.textContent = g.agent;\n"
        "        name.style.color = agentColor(g.agent);\n"
        "        name.title = 'toggle all ' + g.agent + ' runs';\n"
        "        name.onclick = () => {\n"
        "          const anyOn = g.items.some(i => chart2.isDatasetVisible(i));\n"
        "          g.items.forEach(i => chart2.setDatasetVisibility(i, !anyOn));\n"
        "          chart2.update(); buildLegend();\n"
        "        };\n"
        "        wrap.append(name);\n"
        "        for (const i of g.items) {\n"
        "          const it = document.createElement('span');\n"
        "          it.className = 'litem';\n"
        "          const box = document.createElement('i');\n"
        "          const color = chart2.data.datasets[i].borderColor;\n"
        "          box.style.borderColor = color;\n"
        "          if (chart2.isDatasetVisible(i)) box.style.background = color;\n"
        "          const lbl = document.createElement('span');\n"
        "          lbl.textContent = (withCurve[i].ended || '').slice(5, 16);\n"
        "          it.title = 'toggle this run';\n"
        "          it.append(box, lbl);\n"
        "          it.onclick = () => {\n"
        "            chart2.setDatasetVisibility(i, !chart2.isDatasetVisible(i));\n"
        "            chart2.update(); buildLegend();\n"
        "          };\n"
        "          wrap.append(it);\n"
        "        }\n"
        "        legendEl.append(wrap);\n"
        "      }\n"
        "    };\n"
        "    const draw2 = () => {\n"
        "      const hidden = chart2\n"
        "        ? withCurve.map((_, i) => !chart2.isDatasetVisible(i)) : [];\n"
        "      if (chart2) chart2.destroy();\n"
        "      const order = {};\n"
        "      withCurve.forEach((r, i) => {\n"
        "        (order[r.agent] = order[r.agent] || []).push(i);\n"
        "      });\n"
        "      const recency = (r, i) => {\n"
        "        const sibs = order[r.agent];\n"
        "        return sibs.length > 1 ? sibs.indexOf(i) / (sibs.length - 1) : 1;\n"
        "      };\n"
        "      chart2 = new Chart(c2, {type: 'line', data: {datasets:\n"
        "        withCurve.map((r, i) => ({\n"
        "          label: r.agent + ' ' + (r.ended || '').slice(5, 16),\n"
        "          data: bcurves[r.run_id].map(p => ({x: p[0], y: p[1]})),\n"
        "          borderColor: agentShade(r.agent, recency(r, i)),\n"
        "          hidden: hidden[i] || false,\n"
        "          borderWidth: 1.5, pointRadius: 0}))},\n"
        "        options: {color: css('--muted'), parsing: false,\n"
        "          scales: {x: {...axis(), type: lxb.checked ? 'logarithmic' : 'linear'},\n"
        "                   y: {...axis(), type: lyb.checked ? 'logarithmic' : 'linear'}},\n"
        "          plugins: {legend: {display: false}}}});\n"
        "      buildLegend();\n"
        "    };\n"
        "    lxb.onchange = draw2; lyb.onchange = draw2; draw2();\n"
        "    redraws.push(draw2);\n"
        "  }\n"
        "}\n"
        "const now = document.getElementById('now');\n"
        "let strip = null;\n"
        "const refresh = () =>\n"
        "  fetch('climb/status.json', {cache: 'no-cache'})\n"
        "    .then(r => r.ok ? r.json() : null)\n"
        "    .then(s => { if (s && Array.isArray(s.runs)) { strip = s; render(); } })\n"
        "    .catch(() => {});\n"
        "const stateHue = r => r.state === 'in-review' ? '150 55% 38%'\n"
        "  : r.state === 'implementing' ? '262 45% 52%'\n"
        "  : r.phase === 'author-sleep' ? '212 55% 46%' : '38 65% 42%';\n"
        "// kernel phase names, translated for the page: 'in gate' = the\n"
        "// kernel is measuring a submitted candidate; 'experiments' = the\n"
        "// author launched its own jobs and sleeps until they finish\n"
        "const stateName = r => r.state !== 'waiting' ? r.state.replace('-', ' ')\n"
        "  : r.phase === 'author-sleep' ? 'experiments'\n"
        "  : r.phase === 'candidate' ? 'in gate' : 'waiting';\n"
        "const render = () => {\n"
        "  if (!strip) return;\n"
        "  now.textContent = '';\n"
        "  for (const r of strip.runs) {\n"
        "    const card = document.createElement('span'); card.className = 'run';\n"
        "    const top = document.createElement('span'); top.className = 'top';\n"
        "    const pill = document.createElement('span'); pill.className = 'pill';\n"
        "    pill.style.background = `hsl(${stateHue(r)})`;\n"
        "    pill.textContent = stateName(r);\n"
        "    const who = document.createElement('b'); who.textContent = r.agent;\n"
        "    who.style.color = agentColor(r.agent);\n"
        "    const mins = Math.max(0, (Date.now() / 1000 - r.since) / 60);\n"
        "    const t = mins >= 90 ? (mins / 60).toFixed(1) + ' h' : Math.round(mins) + ' min';\n"
        "    const meta = document.createElement('span');\n"
        "    meta.title = 'time in this state '\n"
        "      + '(since the last transition: park, submit, wake)';\n"
        "    meta.textContent = t;\n"
        "    top.append(who, pill, meta);\n"
        "    if (r.pr_url) {\n"
        "      const a = document.createElement('a'); a.href = r.pr_url;\n"
        "      a.textContent = 'PR'; top.append(a);\n"
        "    }\n"
        "    card.append(top);\n"
        "    if (r.direction) {\n"
        "      const d = document.createElement('span'); d.className = 'dir';\n"
        "      d.textContent = r.direction; d.style.display = 'block';\n"
        "      card.append(d);\n"
        "    }\n"
        "    // the agent's life panel: 'exps'/'gate' is the current wait\n"
        "    // (bar = progressing), 'depth' and 'GPU-h' are budgets being\n"
        "    // consumed (dash meters = life spent)\n"
        "    const lm = document.createElement('span'); lm.className = 'lm';\n"
        "    const cell = (cls) => {\n"
        "      const s = document.createElement('span');\n"
        "      if (cls) s.className = cls; return s;\n"
        "    };\n"
        "    const addRow = (label, mid, value, title) => {\n"
        "      const l = cell(); l.textContent = label;\n"
        "      const v = cell(); v.textContent = value;\n"
        "      l.title = mid.title = v.title = title;\n"
        "      lm.append(l, mid, v);\n"
        "      return v;\n"
        "    };\n"
        "    const timeFrac = m =>\n"
        "      Math.min(1, (Date.now() / 1000 - r.since) / (m * 60));\n"
        "    const layer = (w, color) => {\n"
        "      const f = document.createElement('span');\n"
        "      f.style.width = Math.round(w * 100) + '%';\n"
        "      f.style.background = color; return f;\n"
        "    };\n"
        "    const dashes = (filled, segs) => {\n"
        "      const m = cell('meter');\n"
        "      for (let i = 0; i < segs; i++) {\n"
        "        const seg = document.createElement('i');\n"
        "        if (i < filled) seg.style.background = agentColor(r.agent);\n"
        "        m.append(seg);\n"
        "      }\n"
        "      return m;\n"
        "    };\n"
        "    if (r.exp_total) {\n"
        "      const bar = cell('bar');\n"
        "      if (r.exp_minutes)\n"
        "        bar.append(layer(timeFrac(r.exp_minutes),\n"
        "          agentColor(r.agent).slice(0, -1) + ' / .3)'));\n"
        "      bar.append(layer(r.exp_done / r.exp_total, agentColor(r.agent)));\n"
        "      addRow('exps', bar, r.exp_done + '/' + r.exp_total + ' done',\n"
        "        'experiment jobs: solid = finished, '\n"
        "        + 'faint = elapsed vs the longest walltime cap');\n"
        "    } else if (r.phase === 'candidate' && r.eval_minutes) {\n"
        "      const bar = cell('bar');\n"
        "      // the fill stays the agent's color; overdue is flagged by\n"
        "      // the number turning the baseline red\n"
        "      bar.append(layer(timeFrac(r.eval_minutes), agentColor(r.agent)));\n"
        "      const v = addRow('gate', bar,\n"
        "        ((Date.now() / 1000 - r.since) / 3600).toFixed(1)\n"
        "        + '/' + (r.eval_minutes / 60).toFixed(1) + ' h',\n"
        "        'gate eval: time since submission vs its walltime cap. Queue'\n"
        "        + ' time counts, so past the cap the job is either still'\n"
        "        + ' queued or timed out awaiting the next sweep.');\n"
        "      if (timeFrac(r.eval_minutes) >= 1) v.style.color = 'var(--base)';\n"
        "    }\n"
        "    if (r.depth_k && r.launches_used != null)\n"
        "      addRow('depth', dashes(r.launches_used, r.depth_k),\n"
        "        r.launches_used + '/' + r.depth_k,\n"
        "        'experiment launches used, of the contract depth_k');\n"
        "    if (r.gpu_hours_budget && r.gpu_hours_used != null)\n"
        "      addRow('GPU-h',\n"
        "        dashes(Math.round(20 * r.gpu_hours_used / r.gpu_hours_budget), 20),\n"
        "        Number(r.gpu_hours_used).toFixed(0)\n"
        "        + '/' + Number(r.gpu_hours_budget),\n"
        "        'GPU-hours charged to this run, of the contract budget');\n"
        "    if (lm.childNodes.length) card.append(lm);\n"
        "    now.append(card);\n"
        "  }\n"
        "  if (!strip.runs.length) now.textContent = 'no active runs';\n"
        "};\n"
        "refresh();\n"
        "// the strip re-FETCHES every few minutes (new/left/changed runs) and\n"
        "// re-renders the elapsed time locally between fetches\n"
        "onTheme = render;\n"
        "setInterval(refresh, 180000); setInterval(render, 30000);\n"
        "</script></main></body></html>\n"
    )


STATUS_PATH = "climb/status.json"
_LIVE_STATES = ("implementing", "waiting", "in-review", "concluding")


def _phrase(text: str, cap: int = 64) -> str:
    """A strip-sized phrase: the first clause of the first sentence."""
    # markdown emphasis reads as literal asterisks on the strip
    first = summarize(text.replace("**", "").replace("__", ""), 200)
    for sep in (": ", " — ", " -- "):
        first = first.split(sep, 1)[0]
    return summarize(first, cap)


def _experiment_progress(root: Path, record: Any) -> tuple[int, int, int]:
    """(finished, launched, longest walltime minutes) across the current
    park's experiment jobs, counted by the exit-code files the job wrappers
    leave — filesystem only, no Slurm calls on the board path."""
    stage = record.stage or {}
    raw = stage.get("syscall_launches", [])
    if not isinstance(raw, list):
        return (0, 0, 0)
    names: list[str] = []
    minutes = 0
    for item in raw:
        if not (isinstance(item, dict) and item.get("name")):
            continue
        try:
            array = int(item.get("array") or 1)
        except (TypeError, ValueError):
            array = 1
        with contextlib.suppress(TypeError, ValueError):
            minutes = max(minutes, int(item.get("minutes") or 0))
        name = str(item["name"])
        names += [f"{name}.{i}" for i in range(array)] if array > 1 else [name]
    rd = run_dir(root, record.run_id)
    done = sum(1 for n in names if (rd / f"eval-launch-{n}" / "exit-code").exists())
    return (done, len(names), minutes)


def collect_status(root: Path, target: str, now: float, contract: Any = None) -> dict[str, Any]:
    """The fleet's live picture for `target`: one entry per non-terminal run.
    Timestamps, not durations — the page computes elapsed time client-side,
    so the strip feels live between pushes."""
    from autoresearch.dispatch import effective_eval_minutes

    budgets = {
        b.name: (b.depth_k, b.sleep_k, getattr(b, "eval_minutes", 0) or 0)
        for b in getattr(contract, "benchmarks", ())
    }
    gpu_budget = getattr(getattr(contract, "budgets", None), "gpu_hours_per_run", None)
    runs = []
    for record in list_runs(root):
        if record.target != target or record.state not in _LIVE_STATES:
            continue
        stage = record.stage or {}
        note = str(stage.get("syscall_note") or stage.get("report") or "")
        _b, _c, hyp = _report_fields(note)
        exp_done, exp_total, exp_minutes = _experiment_progress(root, record)
        depth_k, sleep_k, bench_minutes = budgets.get(record.benchmark, (None, None, 0))
        runs.append(
            {
                "run_id": record.run_id,
                "agent": record.agent_id,
                "benchmark": record.benchmark,
                "state": record.state,
                "phase": stage.get("phase", ""),
                # the agent's own headline: what it says it is working on
                "direction": _phrase(hyp or note.replace("\n", " ")),
                "since": record.updated or record.created,
                # a run that never launched HAS used zero — absent keys must
                # not blank the card's meters (a plain submit writes none)
                "launches_used": int(stage.get("launches_used") or 0),  # type: ignore[call-overload]
                "sleeps_used": int(stage.get("sleeps_used") or 0),  # type: ignore[call-overload]
                "depth_k": depth_k,
                "sleep_k": sleep_k,
                # experiment fan-out of the current park, and the gate eval's
                # walltime cap — the page turns these into progress bars
                "exp_done": exp_done,
                "exp_total": exp_total,
                "exp_minutes": exp_minutes,
                # submit without --minutes stores nothing: fall back to the
                # contract cap, CLAMPED like the dispatched job itself is —
                # the meter must show the limit the gate actually runs under
                "eval_minutes": (
                    effective_eval_minutes(m)
                    if (m := int(stage.get("eval_minutes", 0) or 0) or bench_minutes)  # type: ignore[call-overload]
                    else 0
                ),
                "gpu_hours_used": float(stage.get("gpu_hours_used") or 0.0),  # type: ignore[arg-type]
                "gpu_hours_budget": gpu_budget,
                "pr_url": record.pr_url,
            }
        )
    runs.sort(key=lambda r: str(r.get("run_id")))
    return {"target": target, "published": now, "runs": runs}


def service_status(root: Path, github: Any, target: str, now: float, contract: Any = None) -> bool:
    """Publish the strip when the fleet's SHAPE changed — a run appearing,
    leaving, or changing state/phase — never on every tick: the page shows
    elapsed time client-side, so timestamp-only drift is not worth a commit.
    Advisory like the board; True when a write happened."""
    status = collect_status(root, target, now, contract)
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
            # exp_done/launches_used move when an experiment finishes or a
            # re-park launches more; the contract caps move when the owner
            # edits the contract — all real transitions the strip must show
            keys = (
                "run_id",
                "state",
                "phase",
                "gpu_hours_used",
                "gpu_hours_budget",
                "direction",
                "exp_done",
                "exp_total",
                "exp_minutes",
                "eval_minutes",
                "launches_used",
                "depth_k",
                "sleep_k",
            )
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


def _valid_curve(curve: Any) -> bool:
    """A publishable curve: [step, value] pairs, numbers only — one null
    point in a published file would throw in the page's chart code."""

    def finite(x: Any) -> bool:
        try:
            return math.isfinite(x)
        except OverflowError:  # an int too large for float is not a chart point
            return False

    return isinstance(curve, list) and all(
        isinstance(p, list)
        and len(p) == 2
        and all(isinstance(x, int | float) and not isinstance(x, bool) and finite(x) for x in p)
        for p in curve
    )


def _merge_curves(
    github: Any, target: str, benchmark: str, rows: list[dict[str, Any]], fresh: dict
) -> dict[str, Any] | None:
    """Published curves plus new ones, kept only for the newest rows (curves
    are heavy; the cap is by recency of the attempt, and published curves
    are never rewritten). None when the published file cannot be read — the
    same sit-the-pass-out stance as everything else on the branch."""
    path = f"climb/curves/{benchmark}.json"
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
        except ValueError:
            data = None
        if not isinstance(data, dict) or not all(_valid_curve(v) for v in data.values()):
            # PARTLY malformed is malformed: dropping the bad values and
            # rewriting would lose published curves
            log.warning("board curves malformed for %s; skipped", benchmark)
            return None
        published = {str(k): v for k, v in data.items()}
    keep = [str(r.get("run_id")) for r in rows[-MAX_CURVE_RUNS:]]
    merged: dict[str, list[list[float]]] = {}
    for run_id in keep:
        curve = published.get(run_id) or fresh.get(run_id)
        if curve:
            merged[run_id] = curve
    kept_published = {r: published[r] for r in keep if published.get(r)}
    return {"data": merged, "published": kept_published, "changed": merged != published}


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
    curves_ok = True
    for benchmark, rows in boards.items():
        merged_curves = _merge_curves(
            github, target, benchmark, rows, local_curves.get(benchmark, {})
        )
        if merged_curves is None:
            # an unreadable curve file must not republish the page without
            # its curves — the html rewrite sits this pass out
            curves_ok = False
            continue
        data = merged_curves["data"]
        if merged_curves["changed"]:
            uploaded = github.ensure_branch(target, BOARD_BRANCH) and github.put_file(
                target,
                f"climb/curves/{benchmark}.json",
                json.dumps(data, indent=1) + "\n",
                BOARD_BRANCH,
                f"climb curves: {benchmark}",
            )
            changed += bool(uploaded)
            if not uploaded:
                # the page embeds only what the branch holds; fresh points
                # return next pass, when their upload can be retried
                data = merged_curves["published"]
        curves[benchmark] = data
    if not boards and names:
        return changed  # every benchmark sat the pass out: leave the views alone
    # the index keeps every benchmark it knows — a transient failed read of
    # one benchmark's JSON must not orphan its history; the views simply
    # render without it until a later pass reads it again. An index that
    # could not be READ at all (None) is never rewritten this pass.
    wanted = {b: directions.get(b, "min") for b in sorted(set(boards) | set(index or {}))}
    views: list[tuple[str, str]] = [("CLIMB.md", render_md(target, boards, wanted))]
    if curves_ok:
        views.append(("index.html", render_html(target, boards, wanted, curves)))
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
