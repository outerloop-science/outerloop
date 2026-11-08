"""The agent image on an adopter's machine: where it lives, and fetching the
published one. The tick reads `~/outerloop-images/agent-py312.sif` (`.sqsh`
under enroot) unless `OUTERLOOP_IMAGE` names another file (tick._default_image);
`init` fills that in on Linux when the container runtime works, so local mode
runs contained without a hand step. The build publishes the image for x86-64
and ARM64 in both runtimes' formats; `init` fetches the one for this machine."""

from __future__ import annotations

import hashlib
import os
import platform
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from outerloop.container import ContainerRuntime, runtime_from_env

IMAGE_STEM = "agent-py312"
IMAGE_REPO = "https://huggingface.co/outerloop-science/agent-image/resolve/main"
IMAGE_DIR_NAME = "outerloop-images"
# the published file per processor (platform.machine()): x86-64 keeps the
# original name, ARM64 adds -arm64
_ARCH_SUFFIX = {"x86_64": "", "amd64": "", "aarch64": "-arm64", "arm64": "-arm64"}
_CHUNK = 1 << 20


def uncontained_opt_in(environ: Mapping[str, str] | None = None) -> bool:
    """`OUTERLOOP_UNCONTAINED=1`: every session and evaluation runs without a
    container on any compute backend, and the image is ignored. The local loop
    already falls back to this without an image; a cluster without a container
    runtime needs the explicit opt-in, so a missing image never silently drops
    containment there."""
    env = os.environ if environ is None else environ
    return env.get("OUTERLOOP_UNCONTAINED", "").strip() == "1"


def image_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / IMAGE_DIR_NAME


def image_name(runtime: ContainerRuntime) -> str:
    """The image's file name in the image dir, in `runtime`'s format."""
    return IMAGE_STEM + runtime.image_suffix


def published_name(runtime: ContainerRuntime, machine: str) -> str:
    """The file the build publishes for `runtime` on this processor, or "" when
    none is built for it."""
    arch = _ARCH_SUFFIX.get(machine.lower())
    return "" if arch is None else IMAGE_STEM + arch + runtime.image_suffix


def find_image(home: Path | None = None, runtime: ContainerRuntime | None = None) -> str:
    """The installed image, or an empty string when absent."""
    path = image_dir(home) / image_name(runtime_from_env() if runtime is None else runtime)
    return str(path) if path.is_file() else ""


def _fmt_mb(n: int) -> str:
    return f"{n / (1 << 20):,.0f} MB"


class _Progress:
    """Download progress: a redrawn bar with size, speed and ETA when stdout is
    a terminal; one line per 10% when it is not (a log, a CI step). `report`
    receives the non-tty lines and the final summary."""

    def __init__(self, url: str, report: Callable[[str], None]) -> None:
        self.url = url
        self.report = report
        self.total = 0
        self.t0 = 0.0
        self.tty = sys.stdout.isatty()
        self.next_mark = 10
        self.last_draw = 0.0

    def start(self, total: int) -> None:
        self.total = total
        self.t0 = time.monotonic()
        size = f" ({_fmt_mb(total)})" if total else ""
        self.report(f"downloading {self.url}{size}")

    def update(self, done: int) -> None:
        now = time.monotonic()
        if self.tty:
            if now - self.last_draw < 0.2:
                return
            self.last_draw = now
            sys.stdout.write("\r" + self._line(done, now) + "\033[K")
            sys.stdout.flush()
        elif self.total:
            pct = done * 100 // self.total
            while pct >= self.next_mark and self.next_mark <= 100:
                self.report(f"  {self.next_mark:3d}%  {_fmt_mb(done)} of {_fmt_mb(self.total)}")
                self.next_mark += 10

    def _line(self, done: int, now: float) -> str:
        elapsed = max(now - self.t0, 1e-6)
        speed = done / elapsed
        if self.total:
            frac = min(done / self.total, 1.0)
            width = 30
            filled = int(frac * width)
            bar = "#" * filled + "-" * (width - filled)
            eta = (self.total - done) / speed if speed > 0 else 0
            return (
                f"  [{bar}] {frac * 100:3.0f}%  {_fmt_mb(done)} of {_fmt_mb(self.total)}"
                f"  {speed / (1 << 20):.1f} MB/s  ETA {int(eta) // 60}:{int(eta) % 60:02d}"
            )
        return f"  {_fmt_mb(done)}  {speed / (1 << 20):.1f} MB/s"

    def finish(self, done: int) -> None:
        if self.tty:
            sys.stdout.write("\r" + self._line(done, time.monotonic()) + "\033[K\n")
            sys.stdout.flush()
        elapsed = time.monotonic() - self.t0
        self.report(f"  downloaded {_fmt_mb(done)} in {int(elapsed) // 60}:{int(elapsed) % 60:02d}")

    def abort(self) -> None:
        if self.tty:
            sys.stdout.write("\n")
            sys.stdout.flush()


def download_image(
    url: str,
    dest: Path,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    report: Callable[[str], None] = print,
    checksum_url: str = "",
) -> Path:
    """Stream `url` to `dest`, through a `.part`
    file renamed into place only after its sha256 matches the checksum the
    build published beside it (`<url>.sha256`); any failure, mismatch or
    interruption removes the part file. Progress: a live bar on a terminal,
    one line per 10% otherwise."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    bar = _Progress(url, report)
    try:
        with opener(url, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            bar.start(total)
            done = 0
            with part.open("wb") as out:
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    bar.update(done)
        bar.finish(done)
        if total and done != total:
            raise OSError(f"short download: {done} of {total} bytes")
        # the checksum the build published beside the image: an image that does
        # not match it is never installed (it would run with the bound harness
        # binary and the run's credentials)
        expected = _published_checksum(checksum_url or url + ".sha256", opener)
        if digest.hexdigest() != expected:
            raise OSError(
                f"checksum mismatch for {url}: expected {expected}, got {digest.hexdigest()}"
            )
        report("  checksum verified")
    except BaseException:
        # a failed or interrupted download never leaves a partial image where
        # a later run would find it
        bar.abort()
        part.unlink(missing_ok=True)
        raise
    os.replace(part, dest)
    report(f"  saved {dest}")
    return dest


def _published_checksum(url: str, opener: Callable[..., Any]) -> str:
    """The sha256 hex the build published at `url` (`<hex>  <file>` as sha256sum
    writes it). No readable checksum means no image."""
    try:
        with opener(url, timeout=60) as resp:
            text = resp.read(4096).decode("ascii", "replace")
    except (OSError, urllib.error.URLError) as exc:
        raise OSError(f"no checksum published at {url}: {exc}") from exc
    token = text.split()[0].strip().lower() if text.split() else ""
    if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        raise OSError(f"unreadable checksum at {url}")
    return token


def ensure_image(
    *,
    interactive: bool,
    want: bool = True,
    probe: bool = True,
    home: Path | None = None,
    ask: Callable[[str], str] = input,
    report: Callable[[str], None] = print,
    fetch: Callable[..., Path] = download_image,
    runtime: ContainerRuntime | None = None,
    machine: str = "",
) -> str:
    """The image path `init` records: an existing image, else the published
    one for this processor downloaded to the image dir, when this machine can
    run it and the adopter did not opt out. `runtime` (default: the
    deployment's) decides the format and is the one probed. "" means
    uncontained (the loop says so at start); when that is because the runtime
    is missing or cannot run containers, the exact install steps for this
    machine are printed, and an image already on disk is NOT recorded (a
    contained run would only fail later). `probe=False` skips the container
    check: on Slurm the image runs on compute nodes, which the login node
    cannot speak for. A failed download is a warning, never a failed setup."""
    if not want:
        return ""
    runtime = runtime_from_env() if runtime is None else runtime
    problem = runtime.check() if probe else ""
    found = find_image(home, runtime)
    if problem:
        report(f"Runs will be UNCONTAINED on this machine: {problem}.")
        report("  " + runtime.install_hint())
        if found:
            report(f"  (the image at {found} will be used once the container runtime works)")
        return ""
    if found:
        return found
    machine = machine or platform.machine()
    name = published_name(runtime, machine)
    if not name:
        report(f"  no published image for this processor ({machine}); pass --image to use your own")
        return ""
    dest = image_dir(home) / image_name(runtime)
    if interactive:
        answer = ask(f"Download the agent image to {dest} so runs are contained? [Y/n] ")
        if answer.strip().lower().startswith("n"):
            return ""
    try:
        return str(fetch(f"{IMAGE_REPO}/{name}", dest, report=report))
    except (OSError, urllib.error.URLError) as exc:
        report(f"  image download failed ({exc}); continuing without one, pass --image later")
        return ""
