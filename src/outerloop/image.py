"""The agent image on an adopter's machine: where it lives, and fetching the
published one. The tick reads `~/outerloop-images/agent-py312.sif` unless
`OUTERLOOP_IMAGE` names another file (tick._default_image); `init` fills that
default on Linux when Apptainer is installed, so local mode runs contained
without a hand step."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
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
_REPORT_EVERY = 256 << 20


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


def containment_available() -> bool:
    """Whether contained runs are possible here: Linux with apptainer on PATH.
    macOS has no Apptainer; its containment is a separate design."""
    return sys.platform.startswith("linux") and shutil.which("apptainer") is not None


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
    interruption removes the part file. Progress every 256 MiB."""
    dest = dest or image_dir() / IMAGE_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    try:
        with opener(url, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            report(f"downloading {url}" + (f" ({total / (1 << 30):.1f} GB)" if total else ""))
            done = 0
            next_report = _REPORT_EVERY
            with part.open("wb") as out:
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if done >= next_report:
                        report(
                            f"  {done / (1 << 30):.1f} GB"
                            + (f" of {total / (1 << 30):.1f}" if total else "")
                        )
                        next_report += _REPORT_EVERY
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
    except BaseException:
        # a failed or interrupted download never leaves a partial image where
        # a later run would find it
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
    home: Path | None = None,
    ask: Callable[[str], str] = input,
    report: Callable[[str], None] = print,
    fetch: Callable[..., Path] = download_image,
) -> str:
    """The image path `init` records: an existing image, else the published
    one downloaded to the image dir when this machine can run it and the
    adopter did not opt out. "" means uncontained (the loop says so at start).
    A failed download is a warning, never a failed setup."""
    found = find_image(home)
    if found:
        return found
    if not want or not containment_available():
        return ""
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
