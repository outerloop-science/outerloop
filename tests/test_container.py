"""The container seam: what each runtime builds from a `Jail`, its probe, and
the factory."""

import shlex
from typing import Any

import pytest

from outerloop import container
from outerloop.container import ApptainerRuntime, Bind, ContainerRuntime, Jail, runtime_from_env


def test_apptainer_argv_spells_every_part_of_the_jail() -> None:
    runtime: ContainerRuntime = ApptainerRuntime("/apps/apptainer")
    jail = Jail(
        "/img/agent.sif",
        home="/run/home",
        binds=(Bind("/ws", "/ws"), Bind("/bin/tool", "/opt/agent/tool", read_only=True)),
        pwd="/ws",
        workdir="/scratch/work",
        gpus=True,
    )
    assert runtime.argv(jail, ["tool", "-p", "x"]) == [
        "/apps/apptainer",
        "exec",
        "--containall",
        "--cleanenv",
        "--nv",
        "--home",
        "/run/home:/run/home",
        "--bind",
        "/ws:/ws",
        "--bind",
        "/bin/tool:/opt/agent/tool:ro",
        "--pwd",
        "/ws",
        "--workdir",
        "/scratch/work",
        "/img/agent.sif",
        "tool",
        "-p",
        "x",
    ]


def test_apptainer_argv_omits_what_the_jail_leaves_unset() -> None:
    argv = ApptainerRuntime("apptainer").argv(Jail("/i.sif", home="/h", binds=()), ["true"])
    assert argv == [
        "apptainer",
        "exec",
        "--containall",
        "--cleanenv",
        "--home",
        "/h:/h",
        "/i.sif",
        "true",
    ]


def test_apptainer_shell_line_reads_the_scripts_variables() -> None:
    """Paths are shell expressions inside double quotes; the binary and the image
    are literal paths, quoted for the shell."""
    jail = Jail(
        "/images/agent image.sif",
        home="$SCRATCH/home",
        binds=(Bind("$TREE", "$TREE"), Bind("$SCRATCH/cache", "$SCRATCH/cache")),
        pwd="$TREE",
        workdir="$SCRATCH/work",
        gpus=True,
    )
    line = ApptainerRuntime("/apps/container tools/apptainer").shell(jail, 'sh -c "$(cat cmd)"')
    assert line == (
        "'/apps/container tools/apptainer' exec --containall --cleanenv --nv "
        '--home "$SCRATCH/home:$SCRATCH/home" --bind "$TREE:$TREE" '
        '--bind "$SCRATCH/cache:$SCRATCH/cache" --pwd "$TREE" --workdir "$SCRATCH/work" '
        "'/images/agent image.sif' sh -c \"$(cat cmd)\""
    )
    assert shlex.split(line)[:2] == ["/apps/container tools/apptainer", "exec"]


def test_apptainer_forwards_through_its_environment_prefix() -> None:
    """apptainer sets APPTAINERENV_<name> inside the jail as <name>."""
    assert ApptainerRuntime("apptainer").forward("OPENAI_API_KEY") == "APPTAINERENV_OPENAI_API_KEY"


class _Proc:
    def __init__(self, rc: int, err: str = "") -> None:
        self.returncode = rc
        self.stderr = err


def test_apptainer_check_runs_a_container_not_just_which(monkeypatch: pytest.MonkeyPatch) -> None:
    """Being on PATH is not enough (Ubuntu 24.04 strips capabilities from a
    hand-installed apptainer's user namespace): the probe execs a container
    and reads the failure."""
    runtime = ApptainerRuntime("apptainer")
    monkeypatch.setattr(container.sys, "platform", "linux")
    monkeypatch.setattr(container.shutil, "which", lambda name: None)
    assert "not installed" in runtime.check()
    monkeypatch.setattr(container.shutil, "which", lambda name: "/usr/bin/apptainer")
    seen: list[list[str]] = []

    def working(argv: list[str], **kw: Any) -> _Proc:
        seen.append(argv)
        return _Proc(0)

    assert runtime.check(runner=working) == ""
    assert seen and seen[0][0] == "/usr/bin/apptainer" and seen[0][1] == "exec"
    broken = _Proc(
        255,
        "ERROR  : Could not write info to setgroups: Permission denied\n"
        "ERROR  : Error while waiting event for user namespace mappings: no event received",
    )
    problem = runtime.check(runner=lambda argv, **kw: broken)
    assert "cannot create containers" in problem and "user namespace" in problem
    other = _Proc(255, "FATAL:   container creation failed: something else")
    assert "apptainer exec failed" in runtime.check(runner=lambda argv, **kw: other)
    monkeypatch.setattr(container.sys, "platform", "darwin")
    assert "macOS" in runtime.check()


def test_runtime_from_env_runs_the_configured_apptainer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OUTERLOOP_APPTAINER_BIN", "/apps/apptainer")
    assert runtime_from_env() == ApptainerRuntime("/apps/apptainer")
    monkeypatch.setenv("OUTERLOOP_APPTAINER_BIN", "")
    assert runtime_from_env() == ApptainerRuntime("apptainer")
