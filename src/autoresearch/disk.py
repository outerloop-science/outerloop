"""Disk preflight: refuse to launch work onto storage that cannot hold it.

Quota exhaustion is INVISIBLE on some clusters until a write fails: user
quotas on VAST/NFS homes are not exposed through statvfs (df reports the
whole filesystem), and rquota RPCs may be administratively blocked — the
first symptom is EDQUOT from a write that already lost data. (Verified on
Torch 2026-08-07, the day a full home quota crashed a live climb and its
error handling with it.)

So the preflight is built on two honest signals:

- a WRITE PROBE — create, fsync, and remove a small file; this surfaces
  EDQUOT/ENOSPC/EROFS exactly the way real work would hit them, and is the
  only quota check that works everywhere;
- statvfs free space — meaningful for cluster-level exhaustion (a shared
  scratch filesystem filling up), used as an early-warning threshold where
  the numbers are real.

The tick probes before launching sessions; the climb probes before touching
its run directory. A failed probe skips NEW work and surfaces in the
heartbeat — it never kills the tick chain.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

PROBE_NAME = ".disk-probe"
# Enough for a clone + venv + eval scratch with margin; overridable per call.
DEFAULT_MIN_FREE_BYTES = 10 * 1024**3


@dataclass(frozen=True)
class MountHealth:
    path: str
    writable: bool
    error: str = ""  # why the probe failed, when it did
    free_bytes: int = -1  # statvfs; -1 when the query itself failed
    min_free_bytes: int = 0

    def ok(self) -> bool:
        if not self.writable:
            return False
        # Unknown free space is not a failure: the write probe is the
        # authoritative check, the threshold is early warning on top.
        if self.free_bytes >= 0 and self.min_free_bytes > 0:
            return self.free_bytes >= self.min_free_bytes
        return True

    def describe(self) -> str:
        state = "ok" if self.ok() else "BLOCKED"
        free = f"{self.free_bytes / 1024**3:.1f}G free" if self.free_bytes >= 0 else "free=?"
        detail = f" ({self.error})" if self.error else ""
        return f"{self.path}: {state}, {free}{detail}"


def probe_writable(path: Path) -> tuple[bool, str]:
    """Try to actually write in `path` the way real work would.

    fsync is deliberate: quota errors on networked filesystems can be
    deferred until the data is forced out, and a probe that skips the flush
    reports healthy right up to the crash.
    """
    probe = path / f"{PROBE_NAME}.{os.getpid()}"
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, b"autoresearch disk probe\n" * 32)
            os.fsync(fd)
        finally:
            os.close(fd)
        return True, ""
    except OSError as exc:
        name = errno.errorcode.get(exc.errno, "") if exc.errno else ""
        return False, f"{name or type(exc).__name__}: {exc}"
    finally:
        with contextlib.suppress(OSError):
            probe.unlink(missing_ok=True)


def free_bytes(path: Path) -> int:
    """Free bytes from statvfs, or -1 when the query fails. Honest for
    cluster-level fullness; blind to per-user quotas on some filesystems —
    that is what the write probe is for."""
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize
    except OSError:
        return -1


def check_mount(path: Path, min_free_bytes: int = 0) -> MountHealth:
    writable, error = probe_writable(path)
    return MountHealth(
        path=str(path),
        writable=writable,
        error=error,
        free_bytes=free_bytes(path),
        min_free_bytes=min_free_bytes,
    )


@dataclass(frozen=True)
class DiskHealth:
    """What the tick needs: may new work launch, and what should humans see."""

    state_root: MountHealth
    home: MountHealth | None = None  # warn-only: the agent barely writes there

    def launch_ok(self) -> bool:
        return self.state_root.ok()

    def warnings(self) -> list[str]:
        out = []
        if not self.state_root.ok():
            out.append(self.state_root.describe())
        if self.home is not None and not self.home.ok():
            out.append(f"home {self.home.describe()} (warn-only)")
        return out

    def as_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "launch_ok": self.launch_ok(),
            "state_root": self.state_root.__dict__,
        }
        if self.home is not None:
            d["home"] = self.home.__dict__
        return d


def check_disk(
    root: Path,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    home: Path | None = None,
) -> DiskHealth:
    """Preflight for one tick: the state root gates launches; the home probe
    (default: the real home) is a warn-only signal for humans, because a
    full home breaks logins and tooling long before it breaks the agent."""
    home_path = Path.home() if home is None else home
    return DiskHealth(
        state_root=check_mount(root, min_free_bytes),
        home=check_mount(home_path, 0),
    )
