"""Live, tighten-only admissions using scheduler-reported fleet usage."""

from __future__ import annotations

import contextlib
import json
import logging
import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from outerloop.compute import Compute, JobSpec
from outerloop.job_names import run_key

log = logging.getLogger(__name__)
LIMITS_FILE = "limits.toml"
KEYS = {"max_gpus", "max_active_attempts"}


class CapacityError(RuntimeError):
    """An admission must wait or be returned to its author, without a charge."""


def minimum(*values: int | None) -> int | None:
    defined = [v for v in values if v is not None]
    return min(defined) if defined else None


@dataclass(frozen=True)
class OperatorLimits:
    defaults: dict[str, int] = field(default_factory=dict)
    targets: dict[str, dict[str, int]] = field(default_factory=dict)
    error: str = ""

    def value(self, target: str, key: str) -> int | None:
        return minimum(self.defaults.get(key), self.targets.get(target, {}).get(key))


def read_limits(root: Path) -> OperatorLimits:
    try:
        raw = (root / LIMITS_FILE).read_bytes()
    except FileNotFoundError:
        if (root / LIMITS_FILE).is_symlink():
            return _invalid("dangling control-file symlink")
        return OperatorLimits()
    except OSError as exc:
        return _invalid(str(exc))
    try:
        data = tomllib.loads(raw.decode())
        if set(data) - {"defaults", "targets"}:
            raise ValueError("expected only defaults and targets tables")
        defaults = _scope(data.get("defaults", {}))
        targets = data.get("targets", {})
        if not isinstance(targets, dict):
            raise ValueError("targets must be a table")
        for target, values in targets.items():
            if not re.fullmatch(r"[^/\s]+/[^/\s]+", target):
                raise ValueError("target keys must be owner/repo")
            _scope(values)
        return OperatorLimits(defaults, targets)
    except (ValueError, UnicodeError) as exc:
        return _invalid(str(exc))


def _scope(values: Any) -> dict[str, int]:
    if not isinstance(values, dict) or set(values) - KEYS:
        raise ValueError("scope accepts only max_gpus and max_active_attempts")
    if any(type(v) is not int or v < 0 for v in values.values()):
        raise ValueError("limits must be nonnegative integers")
    return values


def _invalid(detail: str) -> OperatorLimits:
    error = f"invalid {LIMITS_FILE}: {detail}; blocking new GPU admissions and attempts"
    log.error(error)
    return OperatorLimits(dict.fromkeys(KEYS, 0), error=error)


def attempt_width(root: Path, target: str, contract_width: int) -> int:
    value = minimum(contract_width, read_limits(root).value(target, "max_active_attempts"))
    assert value is not None
    return value


def gpu_demand(spec: JobSpec) -> int:
    if type(spec.gpus) is not int or spec.gpus < 0:
        raise CapacityError("invalid requested GPU quantity")
    if not spec.gpus:
        return 0
    if not spec.array:
        return spec.gpus
    match = re.fullmatch(r"(\d+)-(\d+)(?:%(\d+))?", spec.array)
    if not match:
        raise CapacityError(f"cannot account for GPU array {spec.array!r}")
    first, last = int(match[1]), int(match[2])
    count = last - first + 1
    if count <= 0 or (match[3] is not None and int(match[3]) <= 0):
        raise CapacityError("invalid GPU array")
    return spec.gpus * min(count, int(match[3]) if match[3] else count)


def usage(root: Path, compute: Compute) -> dict[str, int]:
    """One scheduler snapshot; attribute full run IDs and shortened stable run keys."""
    jobs = compute.gpu_jobs()
    run_ids = [p.name for p in (root / "runs").iterdir()] if (root / "runs").exists() else []
    keys = {rid: run_key(rid) for rid in run_ids}
    targets: dict[str, str] = {}
    totals: dict[str, int] = {}
    for name, gpus in jobs:
        if not gpus:
            continue
        matches = [rid for rid in run_ids if rid in name or keys[rid] in name]
        if not matches:
            continue
        # Prefer the full ID if one run's ID is a prefix of another's.
        rid = max(matches, key=len)
        if rid not in targets:
            targets[rid] = str(
                json.loads((root / "runs" / rid / "state.json").read_text())["target"]
            )
        target = targets[rid]
        totals[target] = totals.get(target, 0) + gpus
    return totals


def check_batch(root: Path, target: str, compute: Compute, specs: Sequence[JobSpec]) -> str:
    limits = read_limits(root)
    cap = limits.value(target, "max_gpus")
    if cap is None and not (target == "unknown/unknown" and limits.targets):
        return ""
    demand = sum(gpu_demand(s) for s in specs)
    if not demand:
        return ""
    if limits.error:
        return limits.error
    if target == "unknown/unknown" and any("max_gpus" in s for s in limits.targets.values()):
        return "cannot attribute GPU admission to a target; scoped ceiling may apply"
    if cap is None:
        return ""
    try:
        totals = usage(root, compute)
    except Exception as exc:
        return f"GPU admission blocked: scheduler usage unavailable: {exc} (finite ceiling applies)"
    for scope, ceiling, used in (
        (target, cap, totals.get(target, 0)),
        ("fleet", limits.defaults.get("max_gpus"), sum(totals.values())),
    ):
        if ceiling is not None and used + demand > ceiling:
            return (
                f"operator GPU limit for {scope}: requested {demand}, running/pending {used}, "
                f"effective max_gpus {ceiling}. "
                "Retry when capacity is available or reduce the launch."
            )
    return ""


def submit_batch(root: Path, target: str, compute: Compute, specs: Sequence[JobSpec]) -> list[str]:
    """Check once, then submit. Concurrent admissions may overshoot by one batch."""
    if error := check_batch(root, target, compute, specs):
        raise CapacityError(error)
    ids: list[str] = []
    try:
        for spec in specs:
            ids.append(compute.submit(spec))
    except Exception:
        for job_id in ids:
            with contextlib.suppress(Exception):
                compute.cancel(job_id)
        raise
    return ids


def submit(root: Path, target: str, compute: Compute, spec: JobSpec) -> str:
    return submit_batch(root, target, compute, [spec])[0]


def state_root(run_dir: Path) -> Path:
    return run_dir.parent.parent if run_dir.parent.name == "runs" else run_dir


def run_target(run_dir: Path) -> str:
    # With no control file, do not add state reads to ordinary admissions.
    control = state_root(run_dir) / LIMITS_FILE
    if not control.exists() and not control.is_symlink():
        return "unknown/unknown"
    try:
        return str(json.loads((run_dir / "state.json").read_text())["target"])
    except (OSError, ValueError, KeyError, TypeError):
        # Standalone measurers have no run record; the global ceiling still applies.
        return "unknown/unknown"


def report(root: Path, compute: Compute) -> str:
    limits = read_limits(root)
    lines = ["Operator ceilings (attempt widths also clamp to the current contract)"]
    if limits.error:
        lines.append(limits.error)
    available = True
    try:
        totals = usage(root, compute)
    except Exception as exc:
        available = False
        totals = {}
        lines.append(
            f"scheduler usage unavailable: {exc}; "
            "GPU admissions fail closed only with a finite ceiling"
        )
    else:
        lines.append(f"fleet running/pending GPUs={sum(totals.values())}")
    lines.append(f"defaults: {limits.defaults}")
    for target in sorted(set(limits.targets) | set(totals)):
        lines.append(
            f"{target}: max_gpus={limits.value(target, 'max_gpus')} "
            f"max_active_attempts={limits.value(target, 'max_active_attempts')} "
            f"running/pending GPUs={totals.get(target, 0) if available else 'unknown'}"
        )
    return "\n".join(lines)
