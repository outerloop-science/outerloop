"""Where the operator's config lives.

`~/.config/outerloop/` holds the `.env`, the bot's token or App file, and the
role keys. `~/.config/autoresearch/` — the pre-rename name — is still honored:
a machine set up before the rename keeps working untouched, and `outerloop init`
on a fresh machine creates the new one. Resolution, once per process: the new
dir if it exists, else the legacy dir if it exists, else the new dir.
"""

from __future__ import annotations

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
