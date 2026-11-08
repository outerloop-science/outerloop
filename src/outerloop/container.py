"""The container seam: one interface for the runtime that jails untrusted code.

Every place the kernel runs agent-written code in a container (the author and
judge sessions of each harness, the dispatched eval and launch job, and the
in-job evaluator) describes the jail it wants as a `Jail` and asks a
`ContainerRuntime` for the command that builds it; `outerloop init` asks the
same runtime whether this machine can build jails, which image format it runs
and how to install it. This is the seam `docs/design/multi-cluster.md`
proposes. The call sites own what the jail holds: which host paths are visible,
which variables cross, where the command starts. The runtime owns how its tool
spells that, and the variable names a contract may not set because they would
steer it (`RESERVED_ENV_PREFIXES`).

`runtime_from_env` is the one factory every call site builds its runtime
through. `OUTERLOOP_CONTAINER_RUNTIME` picks `ApptainerRuntime` (`apptainer
exec --containall --cleanenv`, the default) or `EnrootRuntime` (enroot,
through the launcher in `enroot_jail.sh`).
"""

from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, ClassVar, Protocol

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
ENROOT_RELEASES = "https://github.com/NVIDIA/enroot/releases"
ENROOT_VERSION = "4.2.1"


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

    def __post_init__(self) -> None:
        # every jail runs as $HOME = the home mount; an empty one would render
        # `--home ":"` (apptainer) or an empty mount (enroot), silently wrong
        if not self.home:
            raise ValueError("a jail needs a home directory")


def _shell_expr(path: str) -> str:
    """Render a job-script path as a double-quoted shell word, so the job shell
    expands the variables it names (`$SCRATCH`, `$TREE`). Call sites pass shell
    expressions over job variables, never arbitrary literal paths; reject the
    metacharacters that would run code or break the quoting, so a literal path
    holding `$(...)`, a backtick, a double quote or a newline can never be
    injected into the job script here (such a path belongs in `argv`, which
    never invokes a shell)."""
    if '"' in path or "`" in path or "\n" in path or "$(" in path:
        raise ValueError(f"not a safe job-script path expression: {path!r}")
    return f'"{path}"'


class ContainerRuntime(Protocol):
    """The verbs every container runtime implements. Variables reach the jailed
    program only through the environment of the process that builds the jail,
    under the names `forward` gives, never through its argv (argv is readable by
    every user on the host in /proc)."""

    # the image file this runtime runs (`image.py` adds the name and processor)
    image_suffix: ClassVar[str]
    # variables that would steer the runtime or cross into the jail if a
    # contract set them (`managed_eval_env` refuses them)
    reserved_env_prefixes: ClassVar[tuple[str, ...]]

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

    def install_hint(self) -> str:
        """The steps that make `check` pass on this machine, for someone
        installing the runtime for the first time."""

    def accepted_gap(self) -> str:
        """The wall the operator allowed this runtime's jails to run without,
        said once when the loop starts, or "" when every jail keeps them all."""


@dataclass(frozen=True)
class ApptainerRuntime:
    """Apptainer: `--containall --cleanenv` drops the host's home, environment,
    /tmp, /dev and process view, so the jail sees only what `Jail` lists."""

    image_suffix: ClassVar[str] = ".sif"
    # APPTAINERENV_* is translated into the jail's environment (APPTAINERENV_HOME
    # becomes HOME inside); APPTAINER_* configures the host-side CLI (bind
    # paths, home, containment)
    reserved_env_prefixes: ClassVar[tuple[str, ...]] = ("APPTAINERENV_", "APPTAINER_")
    binary: str

    def argv(self, jail: Jail, command: Sequence[str]) -> list[str]:
        return [self.binary, *_apptainer_options(jail, lambda path: path), jail.image, *command]

    def shell(self, jail: Jail, command: str) -> str:
        options = _apptainer_options(jail, _shell_expr)
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

    def install_hint(self) -> str:
        return _apptainer_install_hint()

    def accepted_gap(self) -> str:
        return ""


def _apptainer_install_hint() -> str:
    """The exact steps for THIS machine, written for someone installing Apptainer
    for the first time. Every path is a root operation or an admin request, which
    is why init never runs it."""
    if sys.platform == "darwin":
        return (
            "macOS has no Apptainer; runs without an image use the kernel's Seatbelt profile\n"
            "  (sandbox-exec) instead. A Linux machine, or Slurm, runs in the image."
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


# The launcher every enroot jail goes through, read once: argv and job scripts
# carry it as text, so a job that runs after this checkout moved never needs it.
_ENROOT_JAIL = files(__package__).joinpath("enroot_jail.sh").read_text()


@dataclass(frozen=True)
class EnrootRuntime:
    """enroot, called directly: pyxis starts containers only as Slurm job steps
    and copies the job's environment into them, while sessions and the in-job
    evaluator run inside a job and local mode has no Slurm. Each jail goes
    through `enroot_jail.sh`, which unpacks the image for that jail alone into
    the jail's work directory or TMPDIR, starts it with a cleared environment,
    a read-only root, no host home and a minimal /dev, and removes the unpacked
    tree on every exit, a SIGKILL included. enroot 4.2 and later give the jail
    its own process and IPC namespaces (`--pid --ipc`). Where they cannot
    start, a jail runs only when the operator accepted the gap: `shared_ipc`
    (`OUTERLOOP_ENROOT_SHARED_IPC=1`) a shared IPC namespace, and
    `shared_processes` (`OUTERLOOP_ENROOT_SHARED_PROCESSES=1`) a shared
    process view as well."""

    image_suffix: ClassVar[str] = ".sqsh"
    # ENROOTENV_* crosses into the jail (`forward`); ENROOT_* configures enroot
    # itself, and NVIDIA_* and MELLANOX_* turn its GPU and network-device hooks on
    reserved_env_prefixes: ClassVar[tuple[str, ...]] = (
        "ENROOTENV_",
        "ENROOT_",
        "NVIDIA_",
        "MELLANOX_",
    )
    binary: str
    shared_ipc: bool = False
    shared_processes: bool = False

    def argv(self, jail: Jail, command: Sequence[str]) -> list[str]:
        options = self._options(jail, lambda path: path, self.binary)
        return ["bash", "-c", _ENROOT_JAIL, "outerloop-enroot", *options, jail.image, *command]

    def shell(self, jail: Jail, command: str) -> str:
        options = self._options(jail, _shell_expr, shlex.quote(self.binary))
        launcher = shlex.quote(_ENROOT_JAIL)
        return " ".join(
            ["bash", "-c", launcher, "outerloop-enroot", *options, shlex.quote(jail.image), command]
        )

    def forward(self, name: str) -> str:
        # enroot_jail.sh passes ENROOTENV_<name> into the jail as <name>
        return f"ENROOTENV_{name}"

    def check(self, *, runner: Callable[..., Any] = subprocess.run) -> str:
        """Why contained runs are NOT possible here, or "" when they are. Finds
        enroot and has the launcher start a jail of the host's own programs (no
        image needed), which also tells whether enroot gives it its own process
        and IPC namespaces here, and refuses a weaker jail the operator did not
        accept."""
        if sys.platform == "darwin":
            return "enroot does not exist on macOS"
        if not sys.platform.startswith("linux"):
            return f"enroot is not available on {sys.platform}"
        binary = shutil.which(self.binary)
        if binary is None:
            return "enroot is not installed (not on PATH)"
        argv = ["bash", "-c", _ENROOT_JAIL, "outerloop-enroot", "--check", *self._policy(binary)]
        try:
            proc = runner(argv, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"enroot failed to start: {exc}"
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()
            last = err.splitlines()[-1] if err else f"exit {proc.returncode}"
            return last.removeprefix("outerloop: ")
        return ""

    def install_hint(self) -> str:
        """The exact steps for THIS machine, from NVIDIA's release packages: a
        root operation or an admin request, so init never runs it."""
        if sys.platform != "linux":
            return "enroot runs only on Linux; macOS uses the kernel's Seatbelt profile instead."
        dist, _version = _linux_flavor()
        machine = platform.machine()
        deb = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine, machine)
        if dist in ("ubuntu", "debian", "linuxmint", "pop"):
            package = f"enroot_{ENROOT_VERSION}-1_{deb}.deb"
            steps = (
                f"    1. curl -fsSLO {ENROOT_RELEASES}/download/v{ENROOT_VERSION}/{package}\n"
                f"    2. sudo apt-get install -y ./{package}\n"
            )
        elif dist in ("rhel", "centos", "rocky", "almalinux", "fedora"):
            package = f"enroot-{ENROOT_VERSION}-1.el8.{machine}.rpm"
            steps = (
                f"    1. curl -fsSLO {ENROOT_RELEASES}/download/v{ENROOT_VERSION}/{package}\n"
                f"    2. sudo dnf install -y ./{package}\n"
            )
        else:
            steps = f"    {ENROOT_RELEASES} (Debian and RPM packages for x86-64 and ARM64)\n"
        return (
            f"Install enroot {ENROOT_VERSION} from NVIDIA's release packages:\n"
            + steps
            + "  enroot 4.2 and later give each jail its own process and IPC namespaces; where\n"
            "  they cannot start, OUTERLOOP_ENROOT_SHARED_IPC=1 or\n"
            "  OUTERLOOP_ENROOT_SHARED_PROCESSES=1 accepts the gap (docs/install.md, running on\n"
            "  enroot). On a Slurm cluster, ask the administrators; a cluster with pyxis already\n"
            "  has enroot. Then run `outerloop init --force` again: it starts a jail, then\n"
            "  downloads the image."
        )

    def accepted_gap(self) -> str:
        if self.shared_processes:
            return (
                "OUTERLOOP_ENROOT_SHARED_PROCESSES=1: where enroot cannot give a jail its own "
                "process and IPC namespaces, the jail shares the node's: it sees every process "
                "there, can signal this account's, and on a host that does not block it can read "
                "their environments, model keys included"
            )
        if self.shared_ipc:
            return (
                "OUTERLOOP_ENROOT_SHARED_IPC=1: where enroot cannot give a jail its own IPC "
                "namespace, the jail shares the node's System V IPC"
            )
        return ""

    def _policy(self, binary: str) -> list[str]:
        """The launcher options naming enroot and the gaps the operator accepted."""
        return [
            "--enroot",
            binary,
            *(["--shared-ipc"] if self.shared_ipc else []),
            *(["--shared-processes"] if self.shared_processes else []),
        ]

    def _options(self, jail: Jail, path: Callable[[str], str], binary: str) -> list[str]:
        """The launcher options that build `jail`, each path rendered by `path`."""
        options = [*self._policy(binary), *(["--gpus"] if jail.gpus else [])]
        options += ["--home", path(jail.home)]
        for bind in jail.binds:
            mode = "ro" if bind.read_only else "rw"
            options += ["--bind", path(bind.source), path(bind.target), mode]
        if jail.pwd:
            options += ["--pwd", path(jail.pwd)]
        if jail.workdir:
            options += ["--workdir", path(jail.workdir)]
        return options


# what a contract may never set, under any runtime: a contract stays valid when
# its deployment switches runtimes
RESERVED_ENV_PREFIXES = (
    *ApptainerRuntime.reserved_env_prefixes,
    *EnrootRuntime.reserved_env_prefixes,
)


def runtime_from_env(environ: Mapping[str, str] | None = None) -> ContainerRuntime:
    """The deployment's container runtime, resolved without running it:
    `OUTERLOOP_CONTAINER_RUNTIME` names it (`apptainer` by default), run as
    `OUTERLOOP_APPTAINER_BIN` or `OUTERLOOP_ENROOT_BIN` (default: the name on
    PATH). `environ` stands in for the process environment."""
    env = os.environ if environ is None else environ
    name = env.get("OUTERLOOP_CONTAINER_RUNTIME", "").strip().lower() or "apptainer"
    if name == "apptainer":
        return ApptainerRuntime(env.get("OUTERLOOP_APPTAINER_BIN", "").strip() or "apptainer")
    if name == "enroot":
        return EnrootRuntime(
            env.get("OUTERLOOP_ENROOT_BIN", "").strip() or "enroot",
            shared_ipc=env.get("OUTERLOOP_ENROOT_SHARED_IPC", "").strip() == "1",
            shared_processes=env.get("OUTERLOOP_ENROOT_SHARED_PROCESSES", "").strip() == "1",
        )
    raise ValueError(f"OUTERLOOP_CONTAINER_RUNTIME={name}: choose apptainer or enroot")


def _linux_flavor() -> tuple[str, str]:
    """(distribution id, version) from os-release, or ("", "")."""
    try:
        info = platform.freedesktop_os_release()
    except OSError:
        return "", ""
    return info.get("ID", ""), info.get("VERSION_ID", "")
