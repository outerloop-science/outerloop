"""Where the operator's config lives.

`~/.config/outerloop/` holds the `.env`, the bot's token or App file, and the
role keys. `~/.config/autoresearch/` — the pre-rename name — is still honored:
a machine set up before the rename keeps working untouched, and `outerloop init`
on a fresh machine creates the new one. Resolution, once per process: the new
dir if it exists, else the legacy dir if it exists, else the new dir.
"""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_DIR_NAMES: tuple[str, ...] = ("outerloop", "autoresearch")  # new first


def config_dir(home: Path | None = None) -> Path:
    """The config dir for this machine (see the module docstring)."""
    base = (home or Path.home()) / ".config"
    for name in CONFIG_DIR_NAMES:
        if (base / name).is_dir():
            return base / name
    return base / CONFIG_DIR_NAMES[0]


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
