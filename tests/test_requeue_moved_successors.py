"""The chain's pre-top-up sweep: a same-name successor relocated off our
partition is cancelled only when it is STARVING there — eligible and not
started for a while. Relocation alone (or a job still waiting on its slot or
a dependency) is left alone: cancelling would only reset its queue age."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "requeue_moved_successors.sh"


def _iso(minutes_ago: int) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def _shims(
    tmp_path: Path, rows: list[tuple[str, str, str]], eligible: dict[str, str]
) -> tuple[Path, Path]:
    """Fake squeue (`%i|%P|%r` rows), scontrol (EligibleTime per job) and
    scancel (logs its argument) on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)
    log = tmp_path / "scancel.log"
    body = "".join(f"{jid}|{part}|{reason}\\n" for jid, part, reason in rows)
    (bindir / "squeue").write_text(f"#!/bin/sh\nprintf '{body}'\n")
    cases = "".join(
        f'  {jid}) echo "JobId={jid} EligibleTime={t} JobState=PENDING";;\n'
        for jid, t in eligible.items()
    )
    (bindir / "scontrol").write_text(
        f'#!/bin/sh\ncase "$3" in\n{cases}  *) echo "JobId=$3";;\nesac\n'
    )
    (bindir / "scancel").write_text(f'#!/bin/sh\necho "$1" >> "{log}"\n')
    for f in ("squeue", "scontrol", "scancel"):
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


def test_only_a_starving_relocated_successor_is_cancelled(tmp_path: Path) -> None:
    rows = [
        ("101", "cs", "Priority"),  # relocated, eligible, starving -> cancel
        ("102", "cs", "BeginTime"),  # relocated but waiting for its slot -> keep
        ("103", "cs", "Dependency"),  # relocated but singleton-blocked -> keep
        ("104", "cs", "Priority"),  # relocated, eligible only 5 minutes -> keep
        ("105", "cpu_short", "Priority"),  # not relocated -> keep
        ("106", "cs", "Priority"),  # relocated, eligibility absent -> keep (doubt)
        ("107", "cs", "Priority"),  # relocated, EligibleTime=Unknown -> keep (doubt)
    ]
    eligible = {"101": _iso(45), "104": _iso(5), "107": "Unknown"}
    bindir, log = _shims(tmp_path, rows, eligible)
    out = _run(bindir, "autoresearch-tick", "cpu_short", "20")
    assert log.read_text().split() == ["101"]
    assert "cancelled successor 101 starving on cs" in out.stdout


def test_partition_lists_match_by_membership_and_no_partition_is_a_no_op(tmp_path: Path) -> None:
    rows = [
        ("201", "cpu_short,all", "Priority"),
        ("202", "all", "Priority"),
        ("203", "", "Priority"),
    ]
    eligible = {"201": _iso(60), "202": _iso(60), "203": _iso(60)}
    bindir, log = _shims(tmp_path, rows, eligible)
    out = _run(bindir, "autoresearch-tick", "cpu_short,cpu_prem")
    assert log.read_text().split() == ["202"]
    assert "201" not in out.stdout and "203" not in out.stdout
    bindir2, log2 = _shims(tmp_path / "two", rows, eligible)
    assert _run(bindir2, "autoresearch-tick", "").stdout == "" and not log2.exists()
