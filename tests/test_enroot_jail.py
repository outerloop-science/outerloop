"""The enroot launcher (`enroot_jail.sh`, driven by EnrootRuntime), run for
real against a stand-in enroot that records what it is handed: the checks
before anything is unpacked, the environment enroot starts with, the settings
and mounts it is given, the isolation level each enroot offers, the command's
status, and the cleanup on every exit."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fakes import fake_enroot
from outerloop.container import _ENROOT_JAIL

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="enroot and the launcher's tools are Linux-only"
)


@pytest.fixture
def jail(tmp_path: Path):
    """Run the launcher with the stand-in enroot; returns a runner and the
    directories a test inspects. `fake` sets the stand-in's FAKE_LEVEL,
    FAKE_VERSION and FAKE_CREATE_FAILS."""
    fake_dir = tmp_path / "fake"
    enroot = fake_enroot(fake_dir)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    image = tmp_path / "agent.sqsh"
    image.write_text("")

    def argv(*args: str) -> list[str]:
        return ["bash", "-c", _ENROOT_JAIL, "outerloop-enroot", "--enroot", str(enroot), *args]

    def run(
        *args: str,
        env: dict[str, str] | None = None,
        fake: dict[str, str] | None = None,
        command: tuple[str, ...] = ("true",),
        options: tuple[str, ...] | None = None,
        **kw,
    ) -> subprocess.CompletedProcess[str]:
        fake_enroot(fake_dir, **(fake or {}))
        if options is None:
            options = ("--home", str(home), "--pwd", str(ws), str(image))
        return subprocess.run(
            argv(*args, *options, *command),
            env={"PATH": os.environ["PATH"], "TMPDIR": str(scratch), **(env or {})},
            capture_output=True,
            text=True,
            **kw,
        )

    run.argv = argv  # type: ignore[attr-defined]
    run.fake_dir = fake_dir  # type: ignore[attr-defined]
    run.scratch = scratch  # type: ignore[attr-defined]
    run.home = home  # type: ignore[attr-defined]
    run.ws = ws  # type: ignore[attr-defined]
    run.image = image  # type: ignore[attr-defined]
    return run


def _calls(jail) -> list[str]:
    path = jail.fake_dir / "calls"
    return path.read_text().splitlines() if path.exists() else []


def _left(jail) -> list[Path]:
    return list(jail.scratch.glob("outerloop-enroot.*"))


def test_the_jail_sees_only_forwarded_variables_and_its_home(jail) -> None:
    host = {
        "ENROOTENV_OPENAI_API_KEY": "sk-key",
        "ENROOTENV_SPACED": "a b  c",
        "SLURM_JOB_ID": "7",
        "SECRET_OF_THE_HOST": "x",
    }
    proc = jail(env=host, command=("sh", "-c", "pwd; env | sort"))
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert lines[0] == str(jail.ws)
    env = dict(line.split("=", 1) for line in lines[1:] if "=" in line)
    assert env["OPENAI_API_KEY"] == "sk-key" and env["SPACED"] == "a b  c"
    assert env["HOME"] == str(jail.home)
    assert not any(k.startswith(("SLURM_", "SECRET_", "ENROOTENV_")) for k in env)


def test_enroot_starts_from_an_environment_the_launcher_sets(jail) -> None:
    """Hooks read SLURM_*, NVIDIA_* and the like from enroot's own environment,
    and ENROOT_* would reconfigure enroot: none of the caller's reaches it."""
    host = {
        "ENROOTENV_KEY": "v",
        "SLURM_JOB_ID": "7",
        "SLURM_STEP_ID": "0",
        "ENROOT_MOUNT_HOME": "yes",
        "ENROOT_CONFIG_PATH": "/home/me/.config/enroot",
        "NVIDIA_VISIBLE_DEVICES": "all",
        "HOME": "/home/me",
    }
    proc = jail(env=host)
    assert proc.returncode == 0, proc.stderr
    env = dict(
        line.split("=", 1) for line in (jail.fake_dir / "env.start").read_text().splitlines()
    )
    allowed = {"PATH", "TMPDIR", "PWD", "SHLVL", "_", "ENROOTENV_KEY"}
    extra = {k for k in env if k not in allowed and not k.startswith("ENROOT_")}
    assert extra == {"NVIDIA_VISIBLE_DEVICES", "MELLANOX_VISIBLE_DEVICES"}
    assert env["NVIDIA_VISIBLE_DEVICES"] == "void" and env["MELLANOX_VISIBLE_DEVICES"] == "void"
    paths = {k: v for k, v in env.items() if k.startswith("ENROOT_")}
    assert set(paths) == {
        "ENROOT_RUNTIME_PATH",
        "ENROOT_DATA_PATH",
        "ENROOT_CACHE_PATH",
        "ENROOT_CONFIG_PATH",
        "ENROOT_TEMP_PATH",
    }
    assert all(v.startswith(str(jail.scratch / "outerloop-enroot.")) for v in paths.values())
    assert env["TMPDIR"] == paths["ENROOT_TEMP_PATH"]


def test_the_settings_pin_isolation_and_order_the_mounts(jail) -> None:
    source = jail.ws / "tool"
    source.write_text("")
    proc = jail(
        options=(
            "--home",
            str(jail.home),
            "--bind",
            str(jail.ws),
            str(jail.ws),
            "rw",
            "--bind",
            str(source),
            "/opt/agent/tool",
            "ro",
            "--pwd",
            str(jail.ws),
            str(jail.image),
        )
    )
    assert proc.returncode == 0, proc.stderr
    conf = (jail.fake_dir / "jail.conf").read_text()
    for pin in (
        "REMAP_ROOT=no",
        "ROOTFS_WRITABLE=no",
        "MOUNT_HOME=no",
        "LOGIN_SHELL=no",
        "ALLOW_SUPERUSER=no",
        "UNSHARE_NET=no",
    ):
        assert f"\n#ENROOT_{pin}\n" in f"\n{conf}"
    # nothing else in the file may read as a directive
    directives = [line for line in conf.splitlines() if line.lstrip("# ").startswith("ENROOT_")]
    assert all(line.startswith("#ENROOT_") and line.endswith("=no") for line in directives)
    # a minimal /dev with its own /dev/shm, and no GPU device without --gpus
    assert "none /dev none x-detach" in conf and "tmpfs /dev/shm tmpfs" in conf
    assert "/dev/log" not in conf and "<<'GPU'" not in conf
    mounts = conf.split("cat <<'FSTAB'\n", 1)[1].split("\nFSTAB", 1)[0].splitlines()
    assert mounts == [
        "tmpfs /tmp tmpfs x-create=dir,rw,nosuid,nodev,mode=1777",
        "tmpfs /var/tmp tmpfs x-create=dir,rw,nosuid,nodev,mode=1777",
        f"{jail.home} {jail.home} none x-create=dir,bind,rw,nosuid,nodev",
        f"{jail.ws} {jail.ws} none x-create=dir,bind,rw,nosuid,nodev",
        f"{source} /opt/agent/tool none x-create=file,bind,ro,nosuid,nodev",
    ]


def test_a_workdir_holds_tmp_and_the_unpacked_image(jail, tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    proc = jail(
        options=("--home", str(jail.home), "--workdir", str(work), str(jail.image)),
        command=("sh", "-c", "env | sort"),
    )
    assert proc.returncode == 0, proc.stderr
    conf = (jail.fake_dir / "jail.conf").read_text()
    mounts = conf.split("cat <<'FSTAB'\n", 1)[1].split("\nFSTAB", 1)[0].splitlines()
    assert mounts[:2] == [
        f"{work}/tmp /tmp none x-create=dir,bind,rw,nosuid,nodev",
        f"{work}/var_tmp /var/tmp none x-create=dir,bind,rw,nosuid,nodev",
    ]
    data = (jail.fake_dir / "env.start").read_text()
    assert f"ENROOT_DATA_PATH={work}/outerloop-enroot." in data
    assert sorted(p.name for p in work.iterdir()) == ["tmp", "var_tmp"]


def test_a_gpu_jail_turns_enroot_gpu_hook_on(jail) -> None:
    proc = jail("--gpus")
    assert proc.returncode == 0, proc.stderr
    env = (jail.fake_dir / "env.start").read_text()
    assert "NVIDIA_VISIBLE_DEVICES=all\n" in env
    assert "NVIDIA_DRIVER_CAPABILITIES=compute,utility\n" in env
    conf = (jail.fake_dir / "jail.conf").read_text()
    # the device files enroot's hook mounted come back over the fresh /dev
    assert "<<'GPU'\n" in conf and "\nGPU\n" in conf


@pytest.mark.parametrize(
    ("level", "options", "flags", "notice"),
    [
        ("full", (), "--pid --ipc", ""),
        ("full", ("--shared-processes",), "--pid --ipc", ""),
        ("pid", ("--shared-ipc",), "--pid", "shares the host's IPC namespace"),
        ("pid", ("--shared-processes",), "--pid", "shares the host's IPC namespace"),
        ("none", ("--shared-processes",), "", "shares the host's process and IPC"),
    ],
)
def test_the_strongest_allowed_isolation_runs(jail, level, options, flags, notice) -> None:
    proc = jail(*options, fake={"FAKE_LEVEL": level})
    assert proc.returncode == 0, proc.stderr
    assert f"start {flags}".strip() in [c.strip() for c in _calls(jail)]
    assert (notice in proc.stderr) if notice else "outerloop:" not in proc.stderr


@pytest.mark.parametrize(
    ("level", "options"),
    [("pid", ()), ("none", ()), ("none", ("--shared-ipc",))],
)
def test_a_weaker_jail_than_accepted_is_refused(jail, level, options) -> None:
    proc = jail(*options, fake={"FAKE_LEVEL": level})
    assert proc.returncode == 255
    assert "cannot give this jail its own process and IPC namespaces" in proc.stderr
    assert "OUTERLOOP_ENROOT_SHARED_IPC=1" in proc.stderr
    assert not any(c.startswith("start") for c in _calls(jail))
    assert _left(jail) == []


def test_enroot_before_4_2_is_named_in_the_refusal(jail) -> None:
    proc = jail(fake={"FAKE_LEVEL": "none", "FAKE_VERSION": "4.0.1"})
    assert proc.returncode == 255 and "predates --pid and --ipc" in proc.stderr


def test_enroot_older_than_4_is_refused(jail) -> None:
    proc = jail(fake={"FAKE_VERSION": "3.4.0"})
    assert proc.returncode == 255 and "older than 4.0" in proc.stderr
    assert _calls(jail) == []


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("ENROOTENV_A", "x\nB=injected", "holds a newline"),
        ("ENROOTENV_A", '"quoted"', "wrapped in quotes"),
        ("ENROOTENV_A", "'quoted' ", "wrapped in quotes"),
        ("ENROOTENV_HOME", "/elsewhere", "refusing to forward HOME"),
        ("ENROOTENV_ENROOT_ROOTFS_WRITABLE", "y", "refusing to forward ENROOT_"),
        ("ENROOTENV_NVIDIA_VISIBLE_DEVICES", "all", "refusing to forward NVIDIA_"),
        ("ENROOTENV_MELLANOX_VISIBLE_DEVICES", "all", "refusing to forward MELLANOX_"),
    ],
)
def test_values_enroot_would_change_are_refused(jail, name, value, message) -> None:
    proc = jail(env={name: value})
    assert proc.returncode == 255 and message in proc.stderr
    assert _calls(jail) == [] and _left(jail) == []


def test_quotes_inside_a_value_cross_unchanged(jail) -> None:
    value = 'it\'s "fine" here'
    proc = jail(env={"ENROOTENV_A": value}, command=("sh", "-c", 'printf %s "$A"'))
    assert proc.returncode == 0 and proc.stdout == value


def test_paths_enroot_cannot_mount_are_refused(jail, tmp_path: Path) -> None:
    spaced = tmp_path / "a b"
    spaced.mkdir()
    cases = {
        ("--home", str(spaced), str(jail.image)): "holds whitespace",
        ("--home", "relative", str(jail.image)): "must be absolute",
        ("--home", str(tmp_path / "missing"), str(jail.image)): "no home directory",
        ("--home", str(jail.home), "--bind", str(jail.ws), "rel", "rw", str(jail.image)): (
            "must be absolute"
        ),
        ("--home", str(jail.home), "--bind", str(jail.ws), "/ws", "rx", str(jail.image)): (
            "ro or rw"
        ),
        ("--home", str(jail.home), "--bind", str(tmp_path / "gone"), "/g", "ro", str(jail.image)): (
            "no bind source"
        ),
        ("--home", str(jail.home), str(tmp_path / "missing.sqsh")): "no image",
    }
    for options, message in cases.items():
        proc = jail(options=options)
        assert proc.returncode == 255 and message in proc.stderr, (options, proc.stderr)
    assert _calls(jail) == [] and _left(jail) == []


def test_the_command_status_and_stdin_pass_through(jail) -> None:
    assert jail(command=("sh", "-c", "exit 7")).returncode == 7
    assert jail(command=("sh", "-c", "kill -TERM $$")).returncode == 143
    proc = jail(command=("cat",), input="the brief")
    assert proc.returncode == 0 and proc.stdout == "the brief"
    assert _left(jail) == []


def test_an_unpack_failure_stops_before_any_start(jail) -> None:
    proc = jail(fake={"FAKE_CREATE_FAILS": "1"})
    assert proc.returncode == 255
    assert "bad image" in proc.stderr and "could not unpack" in proc.stderr
    assert _calls(jail) == [] and _left(jail) == []


def test_termination_reaches_the_command_and_the_directory_goes(jail) -> None:
    proc = subprocess.Popen(
        jail.argv("--home", str(jail.home), str(jail.image), "sleep", "30"),
        env={"PATH": os.environ["PATH"], "TMPDIR": str(jail.scratch)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 20
    while not any(c.startswith("start") for c in _calls(jail)) and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=20) == 143
    assert _left(jail) == []


def test_a_jail_killed_outright_still_leaves_nothing(jail) -> None:
    """The harness kills a timed-out session's process group with SIGKILL; the
    launcher's helper, in a session of its own, removes the directory."""
    proc = subprocess.Popen(
        jail.argv("--home", str(jail.home), str(jail.image), "sleep", "30"),
        env={"PATH": os.environ["PATH"], "TMPDIR": str(jail.scratch)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 20
    while not any(c.startswith("start") for c in _calls(jail)) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(_left(jail)) == 1
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)
    deadline = time.monotonic() + 20
    while _left(jail) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _left(jail) == []


def test_the_jailed_process_does_not_inherit_the_cleanup_pipe(jail) -> None:
    """The SIGKILL-cleanup helper fires when the cleanup pipe reaches EOF, i.e.
    when every write end is closed. The {holder} fd is not close-on-exec, so if
    the jailed, untrusted command inherited it, it could hold it open past a
    SIGKILL and leak the unpacked tree, or read a host fd. The launcher must
    close it before exec'ing enroot: the stand-in records the fds it inherited,
    and none beyond stdin/stdout/stderr may be a pipe."""
    proc = jail(command=("true",))
    assert proc.returncode == 0, proc.stderr
    fds = (jail.fake_dir / "fds.start").read_text().splitlines()
    pipes = [line for line in fds if int(line.split()[0]) >= 3 and "pipe:" in line]
    assert pipes == [], f"enroot inherited a pipe fd (the cleanup holder?): {pipes}"


@pytest.mark.parametrize(
    ("level", "options", "out"),
    [
        ("full", (), "full"),
        ("pid", ("--shared-ipc",), "shared-ipc"),
        ("none", ("--shared-processes",), "shared-processes"),
    ],
)
def test_check_reports_the_jail_this_machine_builds(jail, level, options, out) -> None:
    proc = jail("--check", *options, fake={"FAKE_LEVEL": level}, options=(), command=())
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == out
    assert _left(jail) == []


def test_check_refuses_a_weaker_jail_than_accepted(jail) -> None:
    proc = jail("--check", fake={"FAKE_LEVEL": "pid"}, options=(), command=())
    assert proc.returncode == 255
    assert "OUTERLOOP_ENROOT_SHARED_IPC=1 accepts a shared IPC namespace" in proc.stderr
