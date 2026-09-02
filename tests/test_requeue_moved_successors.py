"""The chain's pre-top-up sweep: same-name successors moved off our partition
are cancelled (so the top-up requeues them); the rest are left alone."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "requeue_moved_successors.sh"


def _shims(tmp_path: Path, squeue_rows: str) -> tuple[Path, Path]:
    """Fake squeue/scancel on PATH: squeue prints the given `%i %P` rows,
    scancel appends its argument to a log file."""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)
    log = tmp_path / "scancel.log"
    (bindir / "squeue").write_text(f"#!/bin/sh\nprintf '{squeue_rows}'\n")
    (bindir / "scancel").write_text(f'#!/bin/sh\necho "$1" >> "{log}"\n')
    for f in ("squeue", "scancel"):
        os.chmod(bindir / f, 0o755)
    return bindir, log


def _run(bindir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "USER": os.environ.get("USER", "u"),
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args], check=True, capture_output=True, text=True, env=env
    )


def test_moved_successors_are_cancelled_and_named(tmp_path: Path) -> None:
    bindir, log = _shims(tmp_path, "101 all\\n102 cpu_short\\n103 all\\n")
    out = _run(bindir, "autoresearch-tick", "cpu_short")
    assert log.read_text().split() == ["101", "103"]
    assert "cancelled successor 101 moved to all (asked for cpu_short)" in out.stdout
    assert "102" not in out.stdout


def test_nothing_moved_or_no_partition_is_a_no_op(tmp_path: Path) -> None:
    bindir, log = _shims(tmp_path, "101 cpu_short\\n102 cpu_short\\n")
    assert _run(bindir, "autoresearch-tick", "cpu_short").stdout == ""
    assert not log.exists()
    # no requested partition known: never cancel on doubt
    bindir2, log2 = _shims(tmp_path / "two", "101 all\\n")
    assert _run(bindir2, "autoresearch-tick", "").stdout == ""
    assert not log2.exists()
