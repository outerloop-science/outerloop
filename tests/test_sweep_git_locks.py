"""The tick chain's pre-deploy lock sweep: stale locks go, fresh ones stay."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sweep_git_locks.sh"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "checkout"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    return repo


def _sweep(repo: Path, min_age: str = "10") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), str(repo), min_age], check=True, capture_output=True, text=True
    )


def test_stale_locks_are_swept_and_named(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    old = time.time() - 3600
    stale = [repo / ".git" / "index.lock", repo / ".git" / "refs" / "heads" / "main.lock"]
    for lock in stale:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("")
        os.utime(lock, (old, old))
    out = _sweep(repo)
    assert not any(lock.exists() for lock in stale)
    for lock in stale:
        assert f"swept stale git lock {lock}" in out.stdout


def test_a_fresh_lock_is_left_alone(tmp_path: Path) -> None:
    """A live git op holds its lock for seconds — never race it."""
    repo = _repo(tmp_path)
    fresh = repo / ".git" / "HEAD.lock"
    fresh.write_text("")
    out = _sweep(repo)
    assert fresh.exists() and out.stdout == ""


def test_only_lock_files_are_touched_and_no_git_dir_is_a_no_op(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    old = time.time() - 3600
    keep = repo / ".git" / "FETCH_HEAD"
    keep.write_text("abc")
    os.utime(keep, (old, old))
    _sweep(repo)
    assert keep.read_text() == "abc"
    plain = tmp_path / "not-a-checkout"
    plain.mkdir()
    assert _sweep(plain).returncode == 0
