"""Read-only operator status from local records and the shared outage journal."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from outerloop.contract import CONTRACT_NAME, MAX_CONTRACT_BYTES, Channels, load_contract
from outerloop.rebind import request_status
from outerloop.runstate import RunRecord, list_runs, run_dir
from outerloop.session_evidence import session_totals

log = logging.getLogger(__name__)


def _gpu_budget(root: Path, record: RunRecord) -> float | None:
    """Best available local contract; an absent checkout means unknown."""
    path = run_dir(root, record.run_id) / "ws" / CONTRACT_NAME
    try:
        with path.open() as stream:
            contract = load_contract(stream.read(MAX_CONTRACT_BYTES + 1), record.target)
        budget = contract.budgets.gpu_hours_per_run
        if record.stage.get("review_topup"):
            budget += contract.budgets.review_topup.gpu_hours
        return budget
    except (OSError, ValueError):
        return None


def collect_status(root: Path) -> dict[str, Any]:
    """No probes, scheduler queries, GitHub calls, locks, or state writes."""
    runs = []
    for record in list_runs(root):
        if record.ended():
            continue
        stage = record.stage or {}
        wait = stage.get("endpoint_wait")
        rebind = request_status(root, record.run_id)
        runs.append(
            {
                "run_id": record.run_id,
                "target": record.target,
                "agent": record.agent_id,
                "state": record.state,
                **(
                    {
                        "verification": record.verification,
                        "channels": Channels.model_validate(record.channels).model_dump(),
                    }
                    if record.channels or record.verification != "gate"
                    else {}
                ),
                "phase": stage.get("phase", ""),
                **({"wait_note": stage["wait_note"]} if stage.get("wait_note") else {}),
                "author_backend": record.author_backend or "claude",
                "author_model": record.author_model,
                "author_overridden": record.author_overridden,
                **session_totals(run_dir(root, record.run_id)),
                "gpu_hours_used": stage.get("gpu_hours_used", 0.0),
                "gpu_hours_budget": _gpu_budget(root, record),
                **({"rebind": rebind} if rebind is not None else {}),
                "endpoint_wait": dict(wait) if isinstance(wait, dict) else None,
            }
        )
    outages = []
    # Writers use atomic rename, so reading requires no lock or journal creation.
    for path in sorted((root / "endpoint-waits").glob("*.json")):
        try:
            state = json.loads(path.read_text())
            if "since" not in state:
                continue
            outages.append(
                {
                    "endpoint": path.stem,
                    "since": state["since"],
                    "waiting_runs": sorted(state.get("runs", {})),
                }
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            log.warning("unreadable endpoint journal %s: %s", path, exc)
    return {"runs": runs, "outages": outages}


def _time(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=UTC).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return str(value)


def render_text(status: dict[str, Any]) -> str:
    lines = []
    for run in status["runs"]:
        budget = run["gpu_hours_budget"]
        line = (
            f"{run['run_id']} target={run['target']} agent={run['agent']} "
            f"state={run['state']} phase={run['phase'] or '-'} "
            f"author={run['author_backend']}/{run['author_model'] or '(default)'} "
            f"overridden={'yes' if run['author_overridden'] else 'no'} "
            f"GPU-hours={run['gpu_hours_used']}/{budget if budget is not None else 'unknown'}"
        )
        if run.get("verification") == "self_report":
            line += " verification=self_report (self-reported)"
        cost = run.get("session_cost_usd")
        line += f" session-cost={'$' + format(cost, '.4f') if cost is not None else 'unknown'}"
        if not run.get("verified", False):
            line += " (unverified)"
        line += f" unpriced-sessions={run.get('unpriced_sessions', 0)}"
        if wait := run["endpoint_wait"]:
            line += (
                f" waiting for endpoint {wait.get('endpoint', '?')} "
                f"since {_time(wait.get('since'))}"
            )
        if note := run.get("wait_note"):
            line += f" waiting: {note}"
        if rebind := run.get("rebind"):
            line += (
                f" rebind={rebind.get('status', 'pending')}"
                f" failures={rebind.get('failures', 0)}"
                f" last_error={rebind.get('last_error') or '-'}"
            )
        lines.append(line)
    if not lines:
        lines.append("No active runs.")
    for outage in status["outages"]:
        lines.append(
            f"Endpoint outage {outage['endpoint']} since {_time(outage['since'])}; "
            f"waiting runs: {', '.join(outage['waiting_runs']) or 'none'}"
        )
    if not status["outages"]:
        lines.append("No endpoint outages.")
    return "\n".join(lines)
