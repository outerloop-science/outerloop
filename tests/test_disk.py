"""Disk preflight: write probes, thresholds, and launch gating."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from outerloop.disk import (
    DiskHealth,
    MountHealth,
    check_disk,
    check_mount,
    free_bytes,
    probe_writable,
)


def test_probe_writable_on_a_real_directory(tmp_path: Path) -> None:
    ok, error = probe_writable(tmp_path)
    assert ok and error == ""
    assert list(tmp_path.iterdir()) == []  # probe cleaned up after itself


def test_probe_fails_on_unwritable_directory(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        if os.access(locked, os.W_OK):  # running as root: probe cannot fail
            return
        ok, error = probe_writable(locked)
        assert not ok
        assert "ACCES" in error or "Permission" in error
    finally:
        locked.chmod(stat.S_IRWXU)


def test_free_bytes_is_positive_for_tmp(tmp_path: Path) -> None:
    assert free_bytes(tmp_path) > 0
    assert free_bytes(tmp_path / "does-not-exist") == -1


def test_mount_threshold_blocks_but_probe_alone_passes(tmp_path: Path) -> None:
    # threshold far above any real filesystem forces the early-warning block
    blocked = check_mount(tmp_path, min_free_bytes=2**62)
    assert blocked.writable and not blocked.ok()
    fine = check_mount(tmp_path, min_free_bytes=1)
    assert fine.ok()
    # unknown free space must not fail a writable mount: probe is authoritative
    unknown = MountHealth(path="x", writable=True, free_bytes=-1, min_free_bytes=2**62)
    assert unknown.ok()


def test_check_disk_home_is_warn_only(tmp_path: Path) -> None:
    state = tmp_path / "state"
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        health = check_disk(state, min_free_bytes=1, home=home)
        if not os.access(home, os.W_OK):
            assert health.warnings() and "warn-only" in health.warnings()[0]
        assert health.launch_ok()  # a broken HOME never blocks launches
    finally:
        home.chmod(stat.S_IRWXU)


def test_disk_health_dict_shape(tmp_path: Path) -> None:
    health = check_disk(tmp_path, min_free_bytes=1, home=tmp_path)
    d = health.as_dict()
    assert d["launch_ok"] is True
    assert isinstance(d["state_root"], dict) and "free_bytes" in d["state_root"]


def test_blocked_state_root_gates_launches(tmp_path: Path) -> None:
    health = DiskHealth(state_root=check_mount(tmp_path, min_free_bytes=2**62))
    assert not health.launch_ok()
    assert any("BLOCKED" in w for w in health.warnings())


def test_stale_probe_file_cannot_alias_a_healthy_mount(tmp_path: Path) -> None:
    """A probe file left by a SIGKILLed process (even one whose name embeds
    this very PID) must not make healthy storage read as BLOCKED."""
    (tmp_path / f".disk-probe.{os.getpid()}").write_text("stale")
    ok, error = probe_writable(tmp_path)
    assert ok, error
