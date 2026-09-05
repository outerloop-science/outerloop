"""The tiers in tests/conftest.py: what a plain run selects, and that asking
for the serial tier never distributes it."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _collect(*args: str) -> str:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout


def test_default_run_deselects_the_serial_tier() -> None:
    out = _collect("tests/test_sweep_git_locks.py", "tests/test_markers.py")
    assert "deselected" in out.splitlines()[-1]
    assert "test_sweep_git_locks.py" not in out


def test_serial_selection_runs_without_workers() -> None:
    out = _collect("-m", "serial", "tests/test_sweep_git_locks.py", "tests/test_markers.py")
    assert "workers" not in out  # xdist prints "N workers [...]" when it distributes
    assert "test_sweep_git_locks.py" in out and "test_markers.py" not in out


def test_minus_n0_collects_everything() -> None:
    out = _collect("-n0", "tests/test_sweep_git_locks.py", "tests/test_markers.py")
    assert "deselected" not in out.splitlines()[-1]
