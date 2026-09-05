"""Test tiers, applied at collection.

- slow / llm / slurm are deselected unless your `-m` names one of them, so a plain
  `pytest` is the unit tier. This lives here rather than in `addopts` because a
  `-m` in addopts disables pytest-testmon's change-based selection.
- serial tests inspect the process table and cannot run beside other workers'
  git processes. They are deselected while xdist is distributing and run alone
  with `pytest -n0 -m serial` (CI does both).
"""

from __future__ import annotations

import pytest

TIERS = ("slow", "llm", "slurm")
SERIAL = "serial"


def _distributing(config: pytest.Config) -> bool:
    """Is this session spread over xdist workers? True on the controller that
    asked for workers and inside every worker (they collect for themselves and
    do not carry the controller's `-n`)."""
    if hasattr(config, "workerinput"):
        return True
    workers = getattr(config.option, "numprocesses", 0)
    return bool(workers) or getattr(config.option, "dist", "no") != "no"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    expr = config.getoption("-m") or ""
    drop_tiers = () if any(t in expr for t in TIERS) else TIERS
    drop_serial = _distributing(config) and SERIAL not in expr
    kept: list[pytest.Item] = []
    dropped: list[pytest.Item] = []
    for item in items:
        tiered = any(item.get_closest_marker(t) for t in drop_tiers)
        serial = drop_serial and item.get_closest_marker(SERIAL) is not None
        (dropped if tiered or serial else kept).append(item)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept
