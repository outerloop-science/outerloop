"""The pinned Hermes runtime layout shared by setup and session launch."""

import os
import subprocess
from pathlib import Path

from outerloop.harness_pins import effective, pins

HERMES_SHA = pins("hermes")["sha"]


def source_sha(repo: Path) -> str:
    if (repo / ".git").exists():
        return subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, timeout=15
        ).strip()
    return effective("hermes")["sha"]


def hermes_runtime(repo: Path) -> Path:
    return Path(f"{repo.expanduser().resolve()}.runtime") / source_sha(repo)


def hermes_ready(repo: Path) -> bool:
    try:
        runtime = hermes_runtime(repo)
        return (
            (repo / "run_agent.py").is_file()
            and (runtime / ".complete").read_text().strip() == runtime.name
            and (runtime / "venv/bin/python").is_file()
            and os.access(runtime / "venv/bin/python", os.X_OK)
        )
    except (OSError, subprocess.SubprocessError):
        return False
