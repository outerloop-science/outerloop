"""Operator config lives in `~/.config/outerloop/`.

It holds the `.env`, bot token or App file, and role keys.
"""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_DIR_NAME = "outerloop"


def config_dir(home: Path | None = None) -> Path:
    """The config dir for this machine."""
    return (home or Path.home()) / ".config" / CONFIG_DIR_NAME


CONFIG_DIR = config_dir()
ENV_FILE = CONFIG_DIR / ".env"


def write_private(path: Path, text: str) -> None:
    """Write `text` to `path` so no other user can read it at any moment: the
    file is created (or truncated) with mode 0600 in the same call, and a file
    that already existed has its mode forced to 0600 before the write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        os.fchmod(fd, 0o600)
        fh.write(text)
