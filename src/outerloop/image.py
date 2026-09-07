"""The agent image on an adopter's machine: where it lives, and fetching the
published one. The tick reads `~/outerloop-images/agent-py312.sif` unless
`OUTERLOOP_IMAGE` names another file (tick._default_image); `init` fills that
default on Linux when Apptainer is installed, so local mode runs contained
without a hand step."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

IMAGE_NAME = "agent-py312.sif"
IMAGE_URL = f"https://huggingface.co/outerloop-science/agent-image/resolve/main/{IMAGE_NAME}"
IMAGE_DIR_NAME = "outerloop-images"
LEGACY_IMAGE_DIR_NAME = "autoresearch-images"  # pre-rename; honored until the release after 0.1
_CHUNK = 1 << 20
_PROBE_TIMEOUT_S = 60
APPTAINER_RELEASES = "https://github.com/apptainer/apptainer/releases"
APPTAINER_VERSION = "1.5.3"
# the GitHub release ships Debian packages for x86-64 only; the project's PPA
# builds amd64 and arm64 for every current Ubuntu
APPTAINER_DEB = f"apptainer_{APPTAINER_VERSION}_amd64.deb"
APPTAINER_DEB_URL = f"{APPTAINER_RELEASES}/download/v{APPTAINER_VERSION}/{APPTAINER_DEB}"
APPTAINER_PPA = "ppa:apptainer/ppa"
# the unprivileged installer, pinned to the release tag (never a moving branch)
APPTAINER_UNPRIV_URL = (
    "https://raw.githubusercontent.com/apptainer/apptainer/"
    f"v{APPTAINER_VERSION}/tools/install-unprivileged.sh"
)
PROBE_CMD = "apptainer exec docker://alpine:3.20 cat /etc/alpine-release"


def image_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / IMAGE_DIR_NAME


def find_image(home: Path | None = None) -> str:
    """An existing image on this machine, new location first, or ""."""
    home = home or Path.home()
    for d in (IMAGE_DIR_NAME, LEGACY_IMAGE_DIR_NAME):
        p = home / d / IMAGE_NAME
        if p.is_file():
            return str(p)
    return ""


def _linux_flavor() -> tuple[str, str]:
    """(distribution id, version) from os-release, or ("", "")."""
    try:
        info = platform.freedesktop_os_release()
    except OSError:
        return "", ""
    return info.get("ID", ""), info.get("VERSION_ID", "")


def install_hint() -> str:
    """The exact steps for THIS machine, written for someone installing Apptainer
    for the first time. Every path is a root operation or an admin request, which
    is why init never runs it."""
    if sys.platform == "darwin":
        return (
            "macOS has no Apptainer; runs stay uncontained here (a macOS containment is on the\n"
            "  roadmap). A Linux machine, or Slurm, runs contained."
        )
    dist, _version = _linux_flavor()
    apparmor_note = (
        "  Ubuntu 23.10+ blocks unprivileged user namespaces unless an AppArmor profile allows\n"
        "  them; the official package carries that profile (the unprivileged installer does\n"
        "  not, and fails with 'Could not write info to setgroups').\n"
    )
    check = (
        f"    3. {PROBE_CMD}\n"
        "       (prints a version number)\n"
        "  then run `outerloop init --force` again to download the image."
    )
    if dist in ("ubuntu", "linuxmint", "pop"):
        return (
            "Install Apptainer from the project's Ubuntu PPA (amd64 and arm64):\n"
            + apparmor_note
            + f"    1. sudo add-apt-repository -y {APPTAINER_PPA}\n"
            "    2. sudo apt-get update && sudo apt-get install -y apptainer\n" + check
        )
    if dist == "debian":
        if platform.machine() == "x86_64":
            return (
                "Install the official Apptainer package (Debian, x86-64):\n"
                + apparmor_note
                + f"    1. curl -fsSLO {APPTAINER_DEB_URL}\n"
                f"    2. sudo apt-get install -y ./{APPTAINER_DEB}\n" + check
            )
        return (
            f"No Apptainer Debian package is published for this machine ({platform.machine()}).\n"
            "  Build from source (https://apptainer.org/docs/admin/main/installation.html) or use\n"
            "  the unprivileged installer. Download it, read it, then run it:\n"
            f"    curl -fsSLO {APPTAINER_UNPRIV_URL}\n"
            "    less install-unprivileged.sh && bash install-unprivileged.sh ~/apptainer\n"
            "    export PATH=$HOME/apptainer/bin:$PATH\n"
            f"  Check with `{PROBE_CMD}`, then run `outerloop init --force` again."
        )
    if dist == "fedora":
        return (
            "Install Apptainer from the Fedora repositories:\n"
            "    sudo dnf install -y apptainer\n"
            f"    {PROBE_CMD}   # prints a version number\n"
            "  then run `outerloop init --force` again to download the image."
        )
    if dist in ("rhel", "centos", "rocky", "almalinux"):
        return (
            "Install Apptainer from EPEL:\n"
            "    sudo dnf install -y epel-release\n"
            "    sudo dnf install -y apptainer\n"
            f"    {PROBE_CMD}   # prints a version number\n"
            "  then run `outerloop init --force` again to download the image."
        )
    return (
        "Install Apptainer for your distribution:\n"
        "    https://apptainer.org/docs/admin/main/installation.html\n"
        "  Without root, the project's unprivileged installer works on most systems.\n"
        "  Download it, read it, then run it:\n"
        f"    curl -fsSLO {APPTAINER_UNPRIV_URL}\n"
        "    less install-unprivileged.sh && bash install-unprivileged.sh ~/apptainer\n"
        "    export PATH=$HOME/apptainer/bin:$PATH\n"
        "  On a Slurm cluster, ask the administrators; most already provide it.\n"
        f"  Check with `{PROBE_CMD}`, then run\n"
        "  `outerloop init --force` again to download the image."
    )


def containment_check(*, runner: Callable[..., Any] = subprocess.run) -> str:
    """Why contained runs are NOT possible here, or "" when they are. Finds
    apptainer and actually runs a container: being on PATH is not enough
    (Ubuntu 24.04 lets any program create a user namespace but strips its
    capabilities unless an AppArmor profile allows it, so a hand-installed
    apptainer fails at exec time with `Could not write info to setgroups`).
    The probe needs no network: a throwaway sandbox with the standard mount
    points, the host's /bin, /lib and /usr bound in, running /bin/true —
    about 0.1 s on a working install."""
    if sys.platform == "darwin":
        return "apptainer does not exist on macOS"
    if not sys.platform.startswith("linux"):
        return f"apptainer is not available on {sys.platform}"
    binary = shutil.which("apptainer")
    if binary is None:
        return "apptainer is not installed (not on PATH)"
    binds = ",".join(d for d in ("/bin", "/lib", "/lib64", "/usr") if os.path.exists(d))
    with tempfile.TemporaryDirectory(prefix="outerloop-probe-") as sandbox:
        for d in (
            "dev",
            "proc",
            "sys",
            "tmp",
            "etc",
            "bin",
            "lib",
            "lib64",
            "usr",
            "root",
            "home",
            "var/tmp",
        ):
            os.makedirs(os.path.join(sandbox, d), exist_ok=True)
        for f in ("etc/passwd", "etc/group"):
            open(os.path.join(sandbox, f), "a").close()
        argv = [binary, "exec", "--containall", "--cleanenv", "--no-home", "--pwd", "/"]
        if binds:
            argv += ["-B", binds]
        argv += [sandbox, "/bin/true"]
        try:
            proc = runner(argv, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"apptainer failed to start: {exc}"
    err = (proc.stderr or "").strip()
    if "setgroups" in err or "user namespace" in err.lower() or "unshare" in err.lower():
        last = err.splitlines()[-1] if err else "user namespace refused"
        return f"apptainer cannot create containers here ({last})"
    if proc.returncode != 0:
        last = err.splitlines()[-1] if err else f"exit {proc.returncode}"
        return f"apptainer exec failed ({last})"
    return ""


def containment_available() -> bool:
    return containment_check() == ""


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
    url: str = IMAGE_URL,
    dest: Path | None = None,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    report: Callable[[str], None] = print,
    checksum_url: str = "",
) -> Path:
    """Stream `url` to `dest` (default: the image dir), through a `.part`
    file renamed into place only after its sha256 matches the checksum the
    build published beside it (`<url>.sha256`); any failure, mismatch or
    interruption removes the part file. Progress: a live bar on a terminal,
    one line per 10% otherwise."""
    dest = dest or image_dir() / IMAGE_NAME
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
) -> str:
    """The image path `init` records: an existing image, else the published
    one downloaded to the image dir, when this machine can run it and the
    adopter did not opt out. "" means uncontained (the loop says so at start);
    when that is because Apptainer is missing or cannot run containers, the
    exact install steps for this machine are printed, and an image already on
    disk is NOT recorded (a contained run would only fail later). `probe=False`
    skips the container check: on Slurm the image runs on compute nodes, which
    the login node cannot speak for. A failed download is a warning, never a
    failed setup."""
    if not want:
        return ""
    problem = containment_check() if probe else ""
    found = find_image(home)
    if problem:
        report(f"Runs will be UNCONTAINED on this machine: {problem}.")
        report("  " + install_hint())
        if found:
            report(f"  (the image at {found} will be used once apptainer works)")
        return ""
    if found:
        return found
    dest = image_dir(home) / IMAGE_NAME
    if interactive:
        answer = ask(f"Download the agent image to {dest} so runs are contained? [Y/n] ")
        if answer.strip().lower().startswith("n"):
            return ""
    try:
        return str(fetch(IMAGE_URL, dest, report=report))
    except (OSError, urllib.error.URLError) as exc:
        report(f"  image download failed ({exc}); continuing without one, pass --image later")
        return ""
