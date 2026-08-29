"""The climb board: what every attempt on a benchmark tried and what came
of it, published to the target's `research-log` branch as data plus two
views whenever runs end.

- `climb/<benchmark>.json` — one row per terminal attempt, dedup by run id;
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

from autoresearch.runstate import ENDED, IN_REVIEW, list_runs, run_dir

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
        if record.target != target or record.state not in (ENDED, IN_REVIEW):
            continue
        try:
            report = (run_dir(root, record.run_id) / "report.md").read_text()
        except OSError:
            continue
        baseline, candidate, hyp = _report_fields(report)
        stage = record.stage or {}
        ended = datetime.fromtimestamp(record.updated or record.created, tz=UTC)
        outcome = record.ending or ("improved" if record.state == IN_REVIEW else "ended")
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
        "Written by the kernel when runs end. Data: `climb/<benchmark>.json`;",
        "chart: open `climb.html` from a clone of this branch.",
    ]
    for benchmark in sorted(boards):
        rows = boards[benchmark]
        direction = directions.get(benchmark, "min")
        pick = max if direction == "max" else min
        measured = [r for r in rows if isinstance(r.get("candidate"), int | float)]
        improved = [r for r in rows if r.get("outcome") == "improved"]
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
    target's branch."""
    payload = json.dumps({"boards": boards, "directions": directions})
    return (
        "<!doctype html>\n<html><head><meta charset='utf-8'>\n"
        f"<title>Climb — {target}</title>\n"
        "<script src='https://cdn.jsdelivr.net/npm/chart.js@4'></script>\n"
        "<style>body{font-family:system-ui;margin:2rem;max-width:960px}"
        "canvas{margin-bottom:2rem}</style></head><body>\n"
        f"<h1>Climb — {target}</h1>\n<div id='charts'></div>\n<script>\n"
        f"const data = {payload};\n"
        "for (const b of Object.keys(data.boards).sort()) {\n"
        "  const rows = data.boards[b];\n"
        "  const pick = data.directions[b] === 'max' ? Math.max : Math.min;\n"
        "  const measured = rows.filter(r => typeof r.candidate === 'number');\n"
        "  let best;\n"
        "  const bestSoFar = measured.map(r =>\n"
        "    best = best === undefined ? r.candidate : pick(best, r.candidate));\n"
        "  const h = document.createElement('h2'); h.textContent = b;\n"
        "  const c = document.createElement('canvas');\n"
        "  document.getElementById('charts').append(h, c);\n"
        "  new Chart(c, {data: {labels: measured.map(r => r.ended),\n"
        "    datasets: [\n"
        "      {type: 'scatter', label: 'candidate', data: measured.map(r => r.candidate),\n"
        "       pointBackgroundColor: measured.map(r =>\n"
        "         r.outcome === 'improved' ? '#2a7' : '#999')},\n"
        "      {type: 'line', label: 'best so far', data: bestSoFar, stepped: true,\n"
        "       borderColor: '#26c', pointRadius: 0},\n"
        "      {type: 'line', label: 'baseline', data: measured.map(r => r.baseline),\n"
        "       borderColor: '#c33', borderDash: [6, 4], pointRadius: 0}]},\n"
        "    options: {plugins: {tooltip: {callbacks: {afterLabel:\n"
        "      (i) => measured[i.dataIndex].hypothesis}}}}});\n"
        "}\n</script></body></html>\n"
    )


def _read_index(github: Any, target: str) -> dict[str, str]:
    """climb/index.json: {benchmark: direction} for every benchmark ever
    published — the board's own memory, so a benchmark whose local records
    were cleaned up (or that left the contract) keeps its place in the
    views."""
    raw = github.get_file_content(target, "climb/index.json", BOARD_BRANCH)
    if not raw:
        return {}
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
    names = set(local) | set(index)
    if not names:
        return 0
    directions = {**index, **(directions or {})}
    changed = 0
    boards: dict[str, list[dict[str, Any]]] = {}
    for benchmark in sorted(names):
        path = f"climb/{benchmark}.json"
        existing = github.get_file_content(target, path, BOARD_BRANCH)
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
    wanted = {b: directions.get(b, "min") for b in boards}
    for path, content in (
        ("climb/index.json", json.dumps(wanted, indent=1) + "\n"),
        ("CLIMB.md", render_md(target, boards, wanted)),
        ("climb.html", render_html(target, boards, wanted)),
    ):
        if (
            content != github.get_file_content(target, path, BOARD_BRANCH)
            and github.ensure_branch(target, BOARD_BRANCH)
            and github.put_file(target, path, content, BOARD_BRANCH, "climb board")
        ):
            changed += 1
    return changed
