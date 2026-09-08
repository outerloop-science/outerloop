"""The seed cache: one uv cache per target, warmed by the kernel on the tick
host and copied into each job's own scratch cache before `uv` runs there
(docs/design/eval-cache.md). Jobs never write shared state: the warmer
downloads and unpacks wheels only (`--no-build`, so no build backend of the
target's ever runs here), and a job's copy is its own from the first byte."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SEED_DIR = "eval-cache"
LOCK_HASH = ".lockfile-sha256"
# what uv sync needs to resolve a locked project without its sources
WARM_FILES = ("pyproject.toml", "uv.lock")
# the interpreter the eval jobs run under (the agent image's); wheels are
# picked for it, and the tick host's uv fetches it when it has none
EVAL_PYTHON = "3.12"
WARM_TIMEOUT_S = 30 * 60

Runner = Callable[..., Any]


def seed_dir(root: Path, target: str) -> Path:
    """Where a target's seed lives under the state root."""
    return root / SEED_DIR / target.replace("/", "__")


def lockfile_hash(files: dict[str, str]) -> str:
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode())
        h.update(b"\0")
        h.update(files[name].encode())
        h.update(b"\0")
    return h.hexdigest()


def warm(
    root: Path,
    target: str,
    github: Any,
    ref: str,
    *,
    runner: Runner = subprocess.run,
    uv: str | None = None,
    python: str = EVAL_PYTHON,
) -> str:
    """Warm the target's seed from its lockfile at `ref`, when the lockfile
    changed since the last warm. Fetches `pyproject.toml` and `uv.lock` only
    (never the sources), and runs `uv sync --frozen --no-install-project
    --no-build --all-extras` into a throwaway environment with the seed as
    uv's cache. Returns one word on what happened, for the tick's report:
    "warmed", "unchanged", or "skipped: ..." / "failed: ..." with the reason.
    A failure leaves the seed as it was and the hash unrecorded, so the next
    tick tries again."""
    files: dict[str, str] = {}
    for name in WARM_FILES:
        text = github.get_file_content(target, name, ref)
        if text is None:
            return f"skipped: no {name} at {ref}"
        files[name] = text
    digest = lockfile_hash(files)
    seed = seed_dir(root, target)
    marker = seed / LOCK_HASH
    try:
        if marker.read_text().strip() == digest:
            return "unchanged"
    except OSError:
        pass
    uv = uv or shutil.which("uv")
    if not uv:
        return "skipped: uv is not on the tick host's PATH"
    seed.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="outerloop-warm-") as tmp:
        for name, text in files.items():
            (Path(tmp) / name).write_text(text)
        env = {
            **os.environ,
            "UV_CACHE_DIR": str(seed),
            "UV_PROJECT_ENVIRONMENT": str(Path(tmp) / ".venv"),
            "UV_LINK_MODE": "copy",
        }
        argv = [
            uv,
            "sync",
            "--frozen",
            "--no-install-project",
            "--no-build",
            "--all-extras",
            "--python",
            python,
        ]
        try:
            proc = runner(
                argv, cwd=tmp, env=env, capture_output=True, text=True, timeout=WARM_TIMEOUT_S
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"failed: {type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        return f"failed: uv sync exited {proc.returncode}: {' | '.join(tail)}"
    marker.write_text(digest)
    return "warmed"
