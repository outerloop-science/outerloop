"""Locked, isolated LiteLLM runtime (never provisioned during a session)."""

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path

from outerloop.harness_pins import pins


def runtime_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    root = Path(env.get("OUTERLOOP_ROOT") or Path.home() / ".outerloop")
    cache = Path(env.get("OUTERLOOP_CACHE_ROOT") or root / "cache")
    return Path(env.get("OUTERLOOP_BRIDGE_RUNTIME") or cache / "bridge").expanduser().resolve()


def lock_digest() -> str:
    return hashlib.sha256(
        Path(__file__).with_name("bridge_runtime").joinpath("uv.lock").read_bytes()
    ).hexdigest()


def ready(path: Path) -> bool:
    try:
        marker = (path / ".complete").read_text().strip()
        python = path / "venv/bin/python"
        return (
            marker == f"{pins('bridge')['version']} {lock_digest()}"
            and os.access(python, os.X_OK)
            and (path / "python.sha256").read_text().strip()
            == hashlib.sha256(python.read_bytes()).hexdigest()
        )
    except OSError:
        return False
