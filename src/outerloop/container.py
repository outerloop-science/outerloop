"""The container seam: one interface for the runtime that jails untrusted code.

Every place the kernel runs agent-written code in a container (the author and
judge sessions of each harness, the dispatched eval and launch job, and the
in-job evaluator) describes the jail it wants as a `Jail` and asks a
`ContainerRuntime` for the command that builds it; `outerloop init` asks the
same runtime whether this machine can build jails at all. This is the seam
`docs/design/multi-cluster.md` proposes. The call sites own what the jail
holds: which host paths are visible, which variables cross, where the command
starts. The runtime owns how its tool spells that. Two places outside this
module still name Apptainer: the image `init` fetches and its install steps
(`image.py`), and the evaluator's guard against variables that would steer the
runtime (`PROTECTED_ENV_PREFIXES` in `orchestrator.py`).

`runtime_from_env` is the one factory every call site builds its runtime
through. `ApptainerRuntime` builds `apptainer exec --containall --cleanenv`
jails and is the only runtime for now.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

_PROBE_TIMEOUT_S = 60


@dataclass(frozen=True)
class Bind:
    """A host path made visible inside the jail at `target`."""

    source: str
    target: str
    read_only: bool = False


@dataclass(frozen=True)
class Jail:
    """What a jailed command sees beyond the image.

    `home` is a host directory mounted at its own path and set as $HOME, so
    state the jailed program keeps in its home outlives the jail. A runtime
    mounts the home before the binds; a bind whose target lies inside another
    bind's target comes after it in `binds`, so it shows over that one.
    `workdir` names a host directory for the jail's /tmp and /var/tmp, for
    commands that write more temporary data than a small in-memory /tmp holds.
    `gpus` makes the job's allocated GPUs visible; without it the jail has none.
    """

    image: str
    home: str
    binds: tuple[Bind, ...]
    pwd: str = ""
    workdir: str = ""
    gpus: bool = False


class ContainerRuntime(Protocol):
    """The verbs every container runtime implements. Variables reach the jailed
    program only through the environment of the process that builds the jail,
    under the names `forward` gives, never through its argv (argv is readable by
    every user on the host in /proc)."""

    def argv(self, jail: Jail, command: Sequence[str]) -> list[str]:
        """The argv that runs `command` inside `jail`."""

    def shell(self, jail: Jail, command: str) -> str:
        """`argv` as one simple command of a POSIX shell script, so the caller may
        append redirections. The home, bind paths, `pwd` and `workdir` are shell
        expressions the line places inside double quotes, so they may name the
        script's variables; the image is a literal path; `command` is shell
        text, inserted as given."""

    def forward(self, name: str) -> str:
        """The variable that, set in the environment of the process building a
        jail, appears inside it as `name`."""

    def check(self) -> str:
        """Why this machine cannot build jails, or "" when it can."""


@dataclass(frozen=True)
class ApptainerRuntime:
    """Apptainer: `--containall --cleanenv` drops the host's home, environment,
    /tmp, /dev and process view, so the jail sees only what `Jail` lists."""

    binary: str

    def argv(self, jail: Jail, command: Sequence[str]) -> list[str]:
        return [self.binary, *_apptainer_options(jail, lambda path: path), jail.image, *command]

    def shell(self, jail: Jail, command: str) -> str:
        options = _apptainer_options(jail, lambda path: f'"{path}"')
        return " ".join([shlex.quote(self.binary), *options, shlex.quote(jail.image), command])

    def forward(self, name: str) -> str:
        # apptainer sets APPTAINERENV_<name> inside as <name>, even under --cleanenv
        return f"APPTAINERENV_{name}"

    def check(self, *, runner: Callable[..., Any] = subprocess.run) -> str:
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
        binary = shutil.which(self.binary)
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


def _apptainer_options(jail: Jail, path: Callable[[str], str]) -> list[str]:
    """The `apptainer exec` options that build `jail`, each path argument
    rendered by `path`."""
    options = ["exec", "--containall", "--cleanenv"]
    if jail.gpus:
        options.append("--nv")
    # --home, never --env HOME=... (apptainer silently refuses that): one option
    # mounts the directory and sets $HOME to it
    options += ["--home", path(f"{jail.home}:{jail.home}")]
    for bind in jail.binds:
        ro = ":ro" if bind.read_only else ""
        options += ["--bind", path(f"{bind.source}:{bind.target}{ro}")]
    if jail.pwd:
        options += ["--pwd", path(jail.pwd)]
    if jail.workdir:
        # /tmp and /var/tmp move off apptainer's small tmpfs into this directory
        options += ["--workdir", path(jail.workdir)]
    return options


def runtime_from_env() -> ContainerRuntime:
    """The deployment's container runtime: Apptainer, run as
    `OUTERLOOP_APPTAINER_BIN` (default `apptainer` on PATH), resolved without
    running it."""
    return ApptainerRuntime(os.environ.get("OUTERLOOP_APPTAINER_BIN", "").strip() or "apptainer")
