"""Launch admission: queue, then stop.

Slurm publishes no per-user GPU cap a submitter can trust (on Torch the cap
that parks launches is a moving GROUP cap on the job QOS), but it always says
why a job waits. So the rule needs no number: a launch queues as long as none
of this account's GPU jobs is pending on a cap reason, and is refused while
one is. The parked jobs accrue priority where they sit, the sweep already
treats a cap reason as a wait (tick.is_queue_wait), and the author is told
exactly what is blocking so it can wait for its results or finish.
"""

from __future__ import annotations

from typing import Any

from outerloop.compute import SlurmQueryError, gpus_in_gres, is_pending, local_mode


def is_cap_reason(reason: str) -> bool:
    """A squeue pending reason that means "this account holds as much as the
    QOS allows": per-user, per-account or group limits. Per-job limits are a
    job that can never run, not a full queue."""
    head = reason.strip().split(",")[0].split(" ")[0]
    return any(tag in head for tag in ("PerUser", "PerAccount", "Grp")) and "PerJob" not in head


def queue_saturated(compute: Any) -> str:
    """Why a new GPU launch should not be queued now, or "" when it may: names
    the first of this account's GPU jobs pending on a cap reason. Local compute
    has no queue; a failed queue read never blocks science (blind = allow)."""
    if local_mode():
        return ""
    snapshot = getattr(compute, "queue_snapshot", None)
    if snapshot is None:
        return ""
    try:
        rows = snapshot()
    except SlurmQueryError:
        return ""
    parked = [
        r
        for r in rows
        if is_pending(r.get("state", ""))
        and gpus_in_gres(r.get("gres", "")) > 0
        and is_cap_reason(r.get("reason", ""))
    ]
    if not parked:
        return ""
    first = parked[0]
    more = f" and {len(parked) - 1} more" if len(parked) > 1 else ""
    return (
        f"the GPU queue is full for this account: job {first.get('id', '?')} "
        f"({first.get('name', '')}) is waiting on {first.get('reason', '').split(',')[0]}{more}. "
        "Launching more would only queue behind them. Sleep without launches to wait for "
        "the results you have, or finish."
    )
