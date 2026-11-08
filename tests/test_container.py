"""The container seam: what each runtime builds from a `Jail`, its probe, and
the factory."""

import shlex
from typing import Any

import pytest

from outerloop import container
from outerloop.container import (
    _ENROOT_JAIL,
    RESERVED_ENV_PREFIXES,
    ApptainerRuntime,
    Bind,
    ContainerRuntime,
    EnrootRuntime,
    Jail,
    runtime_from_env,
)


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


@pytest.mark.parametrize("runtime", [ApptainerRuntime("apptainer"), EnrootRuntime("enroot")])
@pytest.mark.parametrize(
    "evil", ["$TREE/$(touch pwned)", "$TREE/`id`", '$TREE/a"b', "$TREE/a\nb", "$TREE/a\\"]
)
def test_shell_rejects_a_path_that_would_inject_into_the_job_script(
    runtime: ContainerRuntime, evil: str
) -> None:
    """shell() renders paths as shell expressions in double quotes; a literal
    path with command substitution, a backtick, a quote or a newline would run
    code or break the quoting in the job script, so it is refused, not rendered
    (#605 review). argv(), which never invokes a shell, carries such a path fine."""
    jail = Jail("/i.img", home="$H", binds=(Bind(evil, evil),), pwd="$T", workdir="$W")
    with pytest.raises(ValueError, match="job-script path"):
        runtime.shell(jail, "cmd")
    runtime.argv(jail, ["true"])  # argv is safe: no shell, path is a literal argv element


def test_a_jail_requires_a_home() -> None:
    """Every jail runs as $HOME = the home mount; an empty home would silently
    render `--home ":"` (#605 review)."""
    with pytest.raises(ValueError, match="home"):
        Jail("/i.img", home="", binds=())


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
    # the EXACT probe flags, not just argv[0]/argv[1]: dropping --containall,
    # --cleanenv or --no-home from the probe must fail here (#605 review)
    assert seen and seen[0][:7] == [
        "/usr/bin/apptainer",
        "exec",
        "--containall",
        "--cleanenv",
        "--no-home",
        "--pwd",
        "/",
    ]
    assert seen[0][-1] == "/bin/true"
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


def test_runtime_from_env_picks_enroot_and_its_opt_ins() -> None:
    assert runtime_from_env({"OUTERLOOP_CONTAINER_RUNTIME": "enroot"}) == EnrootRuntime("enroot")
    settings = {
        "OUTERLOOP_CONTAINER_RUNTIME": " Enroot ",
        "OUTERLOOP_ENROOT_BIN": "/apps/enroot",
        "OUTERLOOP_ENROOT_SHARED_IPC": "1",
        "OUTERLOOP_ENROOT_SHARED_PROCESSES": "yes",  # only "1" opts in, as everywhere
    }
    assert runtime_from_env(settings) == EnrootRuntime("/apps/enroot", shared_ipc=True)
    assert runtime_from_env({"OUTERLOOP_CONTAINER_RUNTIME": ""}) == ApptainerRuntime("apptainer")
    with pytest.raises(ValueError, match="choose apptainer or enroot"):
        runtime_from_env({"OUTERLOOP_CONTAINER_RUNTIME": "docker"})


def test_enroot_argv_hands_the_jail_to_its_launcher() -> None:
    runtime: ContainerRuntime = EnrootRuntime(
        "/apps/enroot", shared_ipc=True, shared_processes=True
    )
    jail = Jail(
        "/img/agent.sqsh",
        home="/run/home",
        binds=(Bind("/ws", "/ws"), Bind("/bin/tool", "/opt/agent/tool", read_only=True)),
        pwd="/ws",
        workdir="/scratch/work",
        gpus=True,
    )
    assert runtime.argv(jail, ["tool", "-p", "x"]) == [
        "bash",
        "-c",
        _ENROOT_JAIL,
        "outerloop-enroot",
        "--enroot",
        "/apps/enroot",
        "--shared-ipc",
        "--shared-processes",
        "--gpus",
        "--home",
        "/run/home",
        "--bind",
        "/ws",
        "/ws",
        "rw",
        "--bind",
        "/bin/tool",
        "/opt/agent/tool",
        "ro",
        "--pwd",
        "/ws",
        "--workdir",
        "/scratch/work",
        "/img/agent.sqsh",
        "tool",
        "-p",
        "x",
    ]
    bare = EnrootRuntime("enroot").argv(Jail("/i.sqsh", home="/h", binds=()), ["true"])
    assert bare[3:] == ["outerloop-enroot", "--enroot", "enroot", "--home", "/h", "/i.sqsh", "true"]


def test_enroot_shell_line_carries_the_launcher_and_reads_the_scripts_variables() -> None:
    """A job script embeds the launcher itself, so it never depends on the
    checkout that wrote it; paths stay shell expressions inside double quotes."""
    jail = Jail(
        "/images/agent image.sqsh",
        home="$SCRATCH/home",
        binds=(Bind("$TREE", "$TREE"),),
        pwd="$TREE",
        workdir="$SCRATCH/work",
    )
    line = EnrootRuntime("/apps/my tools/enroot").shell(jail, 'sh -c "$(cat cmd)"')
    tail = (
        "outerloop-enroot --enroot '/apps/my tools/enroot' --home \"$SCRATCH/home\" "
        '--bind "$TREE" "$TREE" rw --pwd "$TREE" --workdir "$SCRATCH/work" '
        "'/images/agent image.sqsh' sh -c \"$(cat cmd)\""
    )
    assert line.startswith("bash -c '") and line.endswith(tail)
    assert shlex.split(line)[:4] == ["bash", "-c", _ENROOT_JAIL, "outerloop-enroot"]


def test_enroot_forwards_through_its_launchers_prefix() -> None:
    assert EnrootRuntime("enroot").forward("OPENAI_API_KEY") == "ENROOTENV_OPENAI_API_KEY"


def test_enroot_check_runs_the_launchers_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = EnrootRuntime("enroot", shared_ipc=True)
    monkeypatch.setattr(container.sys, "platform", "linux")
    monkeypatch.setattr(container.shutil, "which", lambda name: None)
    assert "not installed" in runtime.check()
    monkeypatch.setattr(container.shutil, "which", lambda name: "/usr/bin/enroot")
    seen: list[list[str]] = []

    def working(argv: list[str], **kw: Any) -> _Proc:
        seen.append(argv)
        return _Proc(0)

    assert runtime.check(runner=working) == ""
    assert seen[0][:3] == ["bash", "-c", _ENROOT_JAIL]
    assert seen[0][3:] == [
        "outerloop-enroot",
        "--check",
        "--enroot",
        "/usr/bin/enroot",
        "--shared-ipc",
    ]
    refused = _Proc(255, "outerloop: enroot cannot give this jail its own process namespace")
    assert runtime.check(runner=lambda argv, **kw: refused) == (
        "enroot cannot give this jail its own process namespace"
    )
    monkeypatch.setattr(container.sys, "platform", "darwin")
    assert "macOS" in runtime.check()


def test_every_runtime_reserves_its_variables() -> None:
    """A contract may not set what crosses into a jail or steers a runtime,
    whichever runtime its deployment uses."""
    assert set(RESERVED_ENV_PREFIXES) == {
        "APPTAINERENV_",
        "APPTAINER_",
        "ENROOTENV_",
        "ENROOT_",
        "NVIDIA_",
        "MELLANOX_",
    }
    assert ApptainerRuntime.image_suffix == ".sif" and EnrootRuntime.image_suffix == ".sqsh"


def test_only_an_enroot_opt_in_is_an_accepted_gap() -> None:
    assert ApptainerRuntime("apptainer").accepted_gap() == ""
    assert EnrootRuntime("enroot").accepted_gap() == ""
    assert (
        "OUTERLOOP_ENROOT_SHARED_IPC=1" in EnrootRuntime("enroot", shared_ipc=True).accepted_gap()
    )
    both = EnrootRuntime("enroot", shared_ipc=True, shared_processes=True).accepted_gap()
    # the serious gap names the worst case plainly: signalling AND reading env (keys)
    assert "OUTERLOOP_ENROOT_SHARED_PROCESSES=1" in both and "signal" in both
    assert "environments" in both and "keys" in both


def test_apptainer_install_hint_names_the_distribution(monkeypatch: pytest.MonkeyPatch) -> None:
    hint = ApptainerRuntime("apptainer").install_hint
    monkeypatch.setattr(container.sys, "platform", "linux")
    monkeypatch.setattr(container, "_linux_flavor", lambda: ("ubuntu", "24.04"))
    for machine in ("x86_64", "aarch64"):  # the PPA covers both (terra, #306)
        monkeypatch.setattr(container.platform, "machine", lambda m=machine: m)
        assert "ppa:apptainer/ppa" in hint() and "AppArmor" in hint() and "setgroups" in hint()
        assert "outerloop init --force" in hint() and ".deb" not in hint()
    monkeypatch.setattr(container, "_linux_flavor", lambda: ("debian", "12"))
    monkeypatch.setattr(container.platform, "machine", lambda: "x86_64")
    assert "apptainer_1.5.3_amd64.deb" in hint()
    monkeypatch.setattr(container.platform, "machine", lambda: "aarch64")
    odd = hint()
    assert "No Apptainer Debian package" in odd and "aarch64" in odd and ".deb" not in odd
    monkeypatch.setattr(container, "_linux_flavor", lambda: ("fedora", "40"))
    assert "dnf install -y apptainer" in hint() and "epel" not in hint()
    monkeypatch.setattr(container, "_linux_flavor", lambda: ("rocky", "9"))
    assert "epel-release" in hint()
    monkeypatch.setattr(container, "_linux_flavor", lambda: ("arch", ""))
    generic = hint()
    # the installer is pinned to the release tag and never piped into a shell
    assert "/v1.5.3/tools/install-unprivileged.sh" in generic and "| bash" not in generic
    monkeypatch.setattr(container.sys, "platform", "darwin")
    assert "macOS" in hint()


def test_enroot_install_hint_names_the_package_for_this_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hint = EnrootRuntime("enroot").install_hint
    monkeypatch.setattr(container.sys, "platform", "linux")
    monkeypatch.setattr(container, "_linux_flavor", lambda: ("ubuntu", "24.04"))
    monkeypatch.setattr(container.platform, "machine", lambda: "aarch64")
    assert "enroot_4.2.1-1_arm64.deb" in hint() and "apt-get install -y ./" in hint()
    monkeypatch.setattr(container.platform, "machine", lambda: "x86_64")
    assert "enroot_4.2.1-1_amd64.deb" in hint()
    monkeypatch.setattr(container, "_linux_flavor", lambda: ("rocky", "9"))
    assert "enroot-4.2.1-1.el8.x86_64.rpm" in hint() and "dnf install" in hint()
    monkeypatch.setattr(container, "_linux_flavor", lambda: ("arch", ""))
    assert "github.com/NVIDIA/enroot/releases" in hint()
    for text in (hint(),):
        assert "OUTERLOOP_ENROOT_SHARED_IPC=1" in text and "outerloop init --force" in text
    monkeypatch.setattr(container.sys, "platform", "darwin")
    assert "only on Linux" in hint()
