"""Seatbelt containment for launches without a container image."""

from __future__ import annotations

import functools
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable
from importlib.resources import files
from pathlib import Path

SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
_warned = False


def seatbelt_available() -> bool:
    return sys.platform == "darwin" and SANDBOX_EXEC.is_file()


def executable_roots(*executables: str) -> list[Path]:
    """Include launch prefixes as well as symlink targets (venvs need both)."""
    roots = {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()}
    for executable in (*executables, sys.executable, "uv", "python", "python3", "node"):
        located = shutil.which(executable)
        if not located:
            continue
        for path in (Path(located).absolute(), Path(located).resolve()):
            directory = path.parent
            prefix = directory.parent
            needs_prefix = directory.name == "bin" and (
                path.name.startswith("python")
                or path.name == "node"
                or (prefix / "pyvenv.cfg").is_file()
                or (prefix / "package.json").is_file()
            )
            roots.add(prefix if needs_prefix else directory)
    uv_python = Path(
        os.environ.get(
            "UV_PYTHON_INSTALL_DIR",
            str(
                Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
                / "uv/python"
            ),
        )
    )
    if uv_python.is_dir():
        roots.add(uv_python.resolve())
    return _outside_home(roots)


def _outside_home(paths: Iterable[Path]) -> list[Path]:
    """A tool installed straight under home (~/bin/python) must not open all of home."""
    home = Path.home().resolve()
    resolved = {path.resolve() for path in paths}
    return sorted(path for path in resolved if path not in (home, *home.parents))


@functools.cache
def _user_cache_dir() -> str:
    """Per-user cache dir under /private/var/folders (the CLI's keychain access
    writes there); the rest of /private/var/folders holds other runs' temp files."""
    try:
        out = subprocess.run(
            ["/usr/bin/getconf", "DARWIN_USER_CACHE_DIR"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return str(Path(out).resolve()) if out else "/nonexistent"
    except (OSError, subprocess.CalledProcessError):
        return "/nonexistent"


def seatbelt_wrap(
    argv: list[str],
    *,
    workspace: Path,
    scratch: Path,
    read_only: Iterable[Path],
    read_files: Iterable[Path] = (),
    contained: bool = False,
) -> list[str]:
    """Wrap an uncontained macOS launch; callers own the scratch lifetime."""
    global _warned
    if contained or sys.platform != "darwin":
        return argv
    if not seatbelt_available():
        if not _warned:
            logging.getLogger(__name__).warning(
                "sandbox-exec is missing: sessions and evaluations run uncontained"
            )
            _warned = True
        return argv
    roots = [str(root) for root in _outside_home(read_only)]
    singles = sorted({str(path.resolve()) for path in read_files})
    rules = "\n  ".join(
        [f'(subpath (param "READ_ONLY_{i}"))' for i in range(len(roots))]
        + [f'(literal (param "READ_FILE_{i}"))' for i in range(len(singles))]
    )
    profile = (
        files(__package__).joinpath("macos.sb").read_text().replace("; READ_ONLY_ROOTS", rules)
    )
    parameters = {
        "WORKSPACE": str(workspace.resolve()),
        "SCRATCH": str(scratch.resolve()),
        "USER_CACHE": _user_cache_dir(),
    }
    parameters.update({f"READ_ONLY_{i}": root for i, root in enumerate(roots)})
    parameters.update({f"READ_FILE_{i}": path for i, path in enumerate(singles)})
    flags = [flag for key, value in parameters.items() for flag in ("-D", f"{key}={value}")]
    # the profile travels in argv, so no file the sandboxed process can write
    return [str(SANDBOX_EXEC), *flags, "-p", profile, *argv]


def session_wrap(
    argv: list[str],
    workspace: Path,
    scratch: Path,
    env: dict[str, str],
    read_only: Iterable[Path] = (),
    read_files: Iterable[Path] = (),
) -> list[str]:
    if sys.platform == "darwin":
        env["TMPDIR"] = str(scratch.resolve())
        env["CLAUDE_CODE_TMPDIR"] = str(scratch.resolve())
    return seatbelt_wrap(
        argv,
        workspace=workspace,
        scratch=scratch,
        read_only=[*executable_roots(argv[0]), *read_only],
        read_files=read_files,
    )
