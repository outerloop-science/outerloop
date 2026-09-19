"""`outerloop start`: one command, Slurm or local, settings from flags,
environment, then .env."""

from __future__ import annotations

import os
import re
import shlex
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from outerloop import cli
from outerloop.cli import (
    DEFAULT_LOCAL_ROOT,
    DEFAULT_RESIDENT_MINUTES,
    RESIDENT_JOB_NAME,
    START_KEYS,
    TICK_ENV_KEYS,
    StartError,
    StartPlan,
    env_file_values,
    main,
    plan_start,
)

REPO = Path(__file__).resolve().parents[1]


def checkout(tmp_path: Path) -> Path:
    home = tmp_path / "checkout"
    (home / "scripts").mkdir(parents=True, exist_ok=True)
    (home / "scripts" / "tick_chain.sbatch").write_text("#!/bin/bash\n")
    return home


def env_file(tmp_path: Path, text: str, mode: int = 0o600) -> Path:
    path = tmp_path / ".env"
    path.write_text(text)
    path.chmod(mode)
    return path


# ---------------------------------------------------------------- .env


def test_env_file_values_reads_only_start_keys_last_wins_and_unquotes(tmp_path: Path) -> None:
    path = env_file(
        tmp_path,
        "# comment\nAUTORESEARCH_PARTITION=old\nOUTERLOOP_ROOT=/first\n"
        "OUTERLOOP_ROOT='/scratch/me/ar'\r\n"
        'OUTERLOOP_ACCOUNT="acct"\nOUTERLOOP_PANEL=\nOTHER=x\n'
        "OUTERLOOP_CADENCE_MIN = 20\n",
    )
    got = env_file_values(path)
    assert got == {
        "OUTERLOOP_ROOT": "/scratch/me/ar",
        "OUTERLOOP_ACCOUNT": "acct",
        "OUTERLOOP_CADENCE_MIN": "20",
    }
    # the author-knob view of the same file: an empty value is PRESENT
    assert env_file_values(path, TICK_ENV_KEYS) == {"OUTERLOOP_PANEL": ""}


def test_env_file_values_missing_file_is_empty(tmp_path: Path) -> None:
    assert env_file_values(tmp_path / "absent") == {}


def test_env_file_values_unreadable_is_a_start_error(tmp_path: Path) -> None:
    (tmp_path / ".env").mkdir()  # a directory where the file should be
    with pytest.raises(StartError, match="cannot read"):
        env_file_values(tmp_path / ".env")


@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666])
def test_env_file_values_refuses_a_writable_file(tmp_path: Path, mode: int) -> None:
    path = env_file(tmp_path, "OUTERLOOP_ROOT=/x\n", mode)
    if not path.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        pytest.skip("filesystem drops group/other write bits")
    with pytest.raises(StartError, match="refusing to read"):
        env_file_values(path)


def test_tick_env_keys_match_the_deploy_allowlist() -> None:
    """The local loop exports the same author knobs the chain's deploy step
    does; the two lists must not drift."""
    sh = (REPO / "scripts" / "tick_deploy.sh").read_text()
    m = re.search(r"for _k in (.*?); do", sh, re.S)
    assert m is not None
    keys = tuple(m.group(1).replace("\\\n", " ").split())
    assert keys == TICK_ENV_KEYS
    assert not set(START_KEYS) & set(TICK_ENV_KEYS)  # start's own keys are not per-tick knobs


# ---------------------------------------------------------------- planning


def plan(tmp_path: Path, **kw: Any) -> StartPlan:
    args: dict[str, Any] = dict(
        root="",
        account="",
        partition="",
        local=False,
        environ={},
        from_file={},
        sbatch_on_path=True,
        cwd=checkout(tmp_path),
    )
    args.update(kw)
    return plan_start(**args)


def test_default_local_root(tmp_path: Path) -> None:
    """The root uses HOME when supplied and the plain default otherwise."""
    from outerloop.cli import default_local_root

    env = {"HOME": str(tmp_path)}
    (tmp_path / ".autoresearch").mkdir()  # an old root is not looked for
    assert default_local_root(env) == tmp_path / ".outerloop"
    (tmp_path / ".outerloop").mkdir()
    assert default_local_root(env) == tmp_path / ".outerloop"
    assert default_local_root({}) == DEFAULT_LOCAL_ROOT


def test_resident_lookup_asks_for_resident_name(monkeypatch: Any) -> None:
    """A queued resident blocks another submission."""
    import subprocess

    from outerloop import cli as cli_mod

    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kw: Any) -> Any:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="900\n777\n", stderr="")

    monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
    assert cli_mod._resident_jobs() == ["777", "900"]
    assert "--name=outerloop-resident" in seen[0]


def test_default_image_path(monkeypatch: Any, tmp_path: Path) -> None:
    from outerloop.tick import _default_image

    monkeypatch.setenv("HOME", str(tmp_path))
    old = tmp_path / "autoresearch-images" / "agent-py312.sif"
    old.parent.mkdir()
    old.write_text("")  # an old image dir is not looked for
    assert _default_image() == str(tmp_path / "outerloop-images" / "agent-py312.sif")


def test_local_without_sbatch_defaults_the_root(tmp_path: Path) -> None:
    p = plan(tmp_path, sbatch_on_path=False)
    assert p.mode == "local"
    assert p.root == DEFAULT_LOCAL_ROOT
    assert p.home == tmp_path / "checkout"  # the loop needs the checkout too
    assert p.command() == [
        sys.executable,
        "-m",
        "outerloop.tick",
        "--root",
        str(DEFAULT_LOCAL_ROOT),
        "--loop",
    ]


def test_local_by_flag_or_env_or_file_even_with_sbatch(tmp_path: Path) -> None:
    assert plan(tmp_path, local=True).mode == "local"
    assert plan(tmp_path, environ={"OUTERLOOP_COMPUTE": "Local"}).mode == "local"
    assert plan(tmp_path, from_file={"OUTERLOOP_COMPUTE": "local"}).mode == "local"
    p = plan(tmp_path, local=True, root="~/state")
    assert p.root == Path("~/state").expanduser()


def test_slurm_composes_the_resident_submit(tmp_path: Path) -> None:
    home = checkout(tmp_path)
    p = plan(
        tmp_path,
        cwd=home,
        from_file={
            "OUTERLOOP_ROOT": "/scratch/me/ar",
            "OUTERLOOP_ACCOUNT": "pr_1_general",
            "OUTERLOOP_PARTITION": "cpu_short",
            "OUTERLOOP_CADENCE_MIN": "20",
            "OUTERLOOP_PAT_FILE": "/home/me/.config/autoresearch/bot_pat",
        },
    )
    assert p.mode == "slurm"
    assert p.command() == [
        "sbatch",
        "--parsable",
        "--dependency=singleton",
        f"--time={DEFAULT_RESIDENT_MINUTES}",
        f"--job-name={RESIDENT_JOB_NAME}",
        "--account=pr_1_general",
        "--partition=cpu_short",
        "--export=ALL",
        str(home / "scripts" / "tick_chain.sbatch"),
    ]
    # The knobs ride the inherited environment, not a comma-joined --export list.
    assert p.export_env() == {
        "OUTERLOOP_RESIDENT": "1",
        "OUTERLOOP_HOME": str(home),
        "OUTERLOOP_ROOT": "/scratch/me/ar",
        "OUTERLOOP_ACCOUNT": "pr_1_general",
        "OUTERLOOP_RESIDENT_MINUTES": str(DEFAULT_RESIDENT_MINUTES),
        "OUTERLOOP_PARTITION": "cpu_short",
        "OUTERLOOP_CADENCE_MIN": "20",
        "OUTERLOOP_PAT_FILE": "/home/me/.config/autoresearch/bot_pat",
    }


def test_precedence_is_flag_then_environment_then_file(tmp_path: Path) -> None:
    p = plan(
        tmp_path,
        partition="flagged",
        environ={
            "OUTERLOOP_ROOT": "/env/root",
            "OUTERLOOP_ACCOUNT": "envacct",
            "OUTERLOOP_PARTITION": "envpart",
        },
        from_file={
            "OUTERLOOP_ROOT": "/file/root",
            "OUTERLOOP_ACCOUNT": "fileacct",
            "OUTERLOOP_PARTITION": "filepart",
        },
    )
    assert (str(p.root), p.account, p.partition) == ("/env/root", "envacct", "flagged")


def test_home_is_a_checkout_on_slurm_and_optional_for_the_local_loop(tmp_path: Path) -> None:
    home = checkout(tmp_path)
    base = {"OUTERLOOP_ROOT": "/r", "OUTERLOOP_ACCOUNT": "a", "OUTERLOOP_PARTITION": "p"}
    with_home = {**base, "OUTERLOOP_HOME": str(home)}
    assert plan(tmp_path, cwd=tmp_path, environ=with_home).home == home
    assert plan(tmp_path, cwd=tmp_path, local=True, environ=with_home).home == home
    # Slurm deploys from the checkout: none at hand is an error
    with pytest.raises(StartError, match="source checkout"):
        plan(tmp_path, cwd=tmp_path, environ=base)
    # the local loop runs the installed package: home falls back under the root
    assert plan(tmp_path, cwd=tmp_path, local=True, root="/r").home == Path("/r/home")
    # a NAMED home that is not a checkout is still an error, in either mode
    with pytest.raises(StartError, match="source checkout"):
        plan(tmp_path, cwd=tmp_path, local=True, environ={"OUTERLOOP_HOME": str(tmp_path)})


def test_slurm_requires_root_but_account_and_partition_are_optional(tmp_path: Path) -> None:
    with pytest.raises(StartError, match="state root"):
        plan(tmp_path)
    # account and partition unset are fine (#300): Slurm bills the default
    # association and places on its default partition, and the sbatch command
    # carries neither flag.
    p = plan(tmp_path, root="/r")
    assert p.account == "" and p.partition == ""
    assert not any(a.startswith(("--account=", "--partition=")) for a in p.command())
    assert "OUTERLOOP_ACCOUNT" not in p.export_env()
    assert "OUTERLOOP_PARTITION" not in p.export_env()
    p = plan(tmp_path, root="/r", account="a")
    assert "--account=a" in p.command()
    assert not any(a.startswith("--partition=") for a in p.command())


def test_slurm_accepts_a_comma_list_partition(tmp_path: Path) -> None:
    # a multi-partition "a,b" now rides the inherited env, not the --export
    # delimiter, so it is accepted and passed through verbatim.
    p = plan(tmp_path, root="/r", account="a", partition="cpu_short,cpu_long")
    assert p.export_env()["OUTERLOOP_PARTITION"] == "cpu_short,cpu_long"
    assert "--partition=cpu_short,cpu_long" in p.command()
    # a newline would still corrupt the environment / sbatch argv.
    with pytest.raises(StartError, match="newline"):
        plan(tmp_path, root="/r", account="a", partition="cpu\nlong")


@pytest.mark.parametrize("bad", ["0", "-5", "30m", ""])
def test_cadence_must_be_a_positive_number_in_both_modes(tmp_path: Path, bad: str) -> None:
    if bad == "":
        assert plan(tmp_path, local=True, environ={"OUTERLOOP_CADENCE_MIN": ""}).cadence_min == ""
        return
    with pytest.raises(StartError, match="positive number of minutes"):
        plan(tmp_path, local=True, environ={"OUTERLOOP_CADENCE_MIN": bad})
    with pytest.raises(StartError, match="positive number of minutes"):
        plan(
            tmp_path,
            root="/r",
            account="a",
            partition="p",
            from_file={"OUTERLOOP_CADENCE_MIN": bad},
        )


def test_slurm_resident_minutes_must_be_a_positive_integer(tmp_path: Path) -> None:
    base = dict(root="/r", account="a", partition="p")
    p = plan(tmp_path, **base, environ={"OUTERLOOP_RESIDENT_MINUTES": "240"})
    assert p.resident_minutes == 240
    assert "--time=240" in p.command()
    assert p.export_env()["OUTERLOOP_RESIDENT_MINUTES"] == "240"  # successors keep it
    with pytest.raises(StartError, match="whole number"):
        plan(tmp_path, **base, environ={"OUTERLOOP_RESIDENT_MINUTES": "4h"})
    with pytest.raises(StartError, match="positive"):
        plan(tmp_path, **base, environ={"OUTERLOOP_RESIDENT_MINUTES": "0"})


# ---------------------------------------------------------------- main


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for key in (*START_KEYS, *TICK_ENV_KEYS, "OUTERLOOP_HOME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(cli, "ENV_FILE", tmp_path / "absent.env")
    monkeypatch.setattr(cli, "find_uv", lambda: ("/usr/bin/uv", ""))
    monkeypatch.setenv("OUTERLOOP_CLAUDE_BIN", sys.executable)
    monkeypatch.setenv("OUTERLOOP_CODEX_BIN", sys.executable)
    monkeypatch.setenv("OUTERLOOP_CLAUDE_MODEL", "claude-test-model")  # a configured deployment
    return tmp_path


def shim(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)


def test_dry_run_prints_the_command(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.chdir(checkout(clean_env))
    assert main(["start", "--dry-run", "--root", str(clean_env / "s")]) == 0
    out = capsys.readouterr().out
    assert "outerloop.tick" in out and "--loop" in out and str(clean_env / "s") in out


def test_local_start_execs_the_loop_with_env_knobs(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        cli,
        "ENV_FILE",
        env_file(
            clean_env,
            "OUTERLOOP_TARGET=o/r\nOUTERLOOP_PANEL=\nOUTERLOOP_CADENCE_MIN=15\n"
            "OUTERLOOP_PAT_FILE=/home/me/pat\n",
        ),
    )
    monkeypatch.setenv("OUTERLOOP_TARGET", "shell/wins")
    seen: dict[str, object] = {}

    def fake_exec(cmd: list[str], env: dict[str, str]) -> int:
        seen["cmd"], seen["env"] = cmd, env
        return 0

    monkeypatch.setattr(cli, "_exec", fake_exec)
    home = checkout(clean_env)
    monkeypatch.chdir(home)
    assert main(["start", "--root", str(clean_env / "state")]) == 0
    assert seen["cmd"] == [
        sys.executable,
        "-m",
        "outerloop.tick",
        "--root",
        str(clean_env / "state"),
        "--loop",
    ]
    env = seen["env"]
    assert isinstance(env, dict)
    assert env["OUTERLOOP_COMPUTE"] == "local"
    assert env["OUTERLOOP_ROOT"] == str(clean_env / "state")
    assert env["OUTERLOOP_HOME"] == str(home)  # the tick's lanes need the checkout
    assert env["OUTERLOOP_TARGET"] == "shell/wins"  # the shell beats the file at launch
    assert env["OUTERLOOP_PANEL"] == ""  # an off-switch in the file still lands
    assert env["OUTERLOOP_CADENCE_MIN"] == "15"  # the loop's cadence comes from .env too
    assert env["OUTERLOOP_PAT_FILE"] == "/home/me/pat"


def test_local_start_needs_no_checkout(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pip-installed adopter has no source checkout. The local loop runs the
    installed package, so start must not demand one: home becomes a directory
    under the state root, where flights and logs land (#287)."""
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli, "ENV_FILE", env_file(clean_env, "OUTERLOOP_TARGET=o/r\n"))
    seen: dict[str, object] = {}

    def fake_exec(cmd: list[str], env: dict[str, str]) -> int:
        seen["env"] = env
        return 0

    monkeypatch.setattr(cli, "_exec", fake_exec)
    plain = clean_env / "somewhere"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert main(["start", "--root", str(clean_env / "state")]) == 0
    env = seen["env"]
    assert isinstance(env, dict)
    assert env["OUTERLOOP_HOME"] == str(clean_env / "state" / "home")
    assert (clean_env / "state" / "home").is_dir()  # jobs cd into it


def test_outerloop_home_is_read_from_the_environment(clean_env: Path) -> None:
    home = checkout(clean_env)
    p = plan(clean_env, cwd=clean_env, local=True, root="/r", environ={"OUTERLOOP_HOME": str(home)})
    assert p.home == home


def test_slurm_start_still_needs_a_checkout(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/sbatch")
    plain = clean_env / "somewhere"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert main(["start", "--root", str(clean_env / "state"), "--account", "a"]) == 2
    assert "source checkout" in capsys.readouterr().err


def test_slurm_start_submits_once_and_reports(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = checkout(clean_env)
    bin_dir = clean_env / "bin"
    bin_dir.mkdir()
    log = clean_env / "sbatch.log"
    envlog = clean_env / "sbatch.env"
    shim(
        bin_dir,
        "sbatch",
        f'printf "%s\\n" "$@" > {log}\n'
        f'printf "%s:%s" "$OUTERLOOP_RESIDENT" "$OUTERLOOP_HOME" > {envlog}\n'
        'echo "4242;torch"\n',
    )
    shim(bin_dir, "squeue", "exit 0\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.chdir(home)
    rc = main(
        ["start", "--root", "/scratch/me/ar", "--account", "acct", "--partition", "cpu_short"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "job 4242" in out and "cpu_short" in out and "PAUSE" in out
    argv = log.read_text().split("\n")
    assert argv[0] == "--parsable" and f"--job-name={RESIDENT_JOB_NAME}" in argv
    assert "--dependency=singleton" in argv
    assert "--export=ALL" in argv  # the knobs ride the inherited env, asserted next
    assert envlog.read_text() == f"1:{home}"  # export_env reached sbatch's environment
    assert argv[-2] == str(home / "scripts" / "tick_chain.sbatch")


def test_slurm_start_does_not_submit_beside_a_live_resident(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = checkout(clean_env)
    bin_dir = clean_env / "bin"
    bin_dir.mkdir()
    shim(bin_dir, "sbatch", "echo SUBMITTED > " + str(clean_env / "submitted") + "\necho 1\n")
    shim(bin_dir, "squeue", "echo 777\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.chdir(home)
    assert main(["start", "--root", "/r", "--account", "a", "--partition", "p"]) == 0
    assert "already queued or running (job 777)" in capsys.readouterr().err
    assert not (clean_env / "submitted").exists()


def test_slurm_start_reports_a_failed_submit(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = checkout(clean_env)
    bin_dir = clean_env / "bin"
    bin_dir.mkdir()
    shim(bin_dir, "sbatch", "echo 'sbatch: error: invalid partition' >&2\nexit 1\n")
    shim(bin_dir, "squeue", "exit 0\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.chdir(home)
    assert main(["start", "--root", "/r", "--account", "a", "--partition", "nope"]) == 1
    assert "invalid partition" in capsys.readouterr().err


def test_slurm_start_fails_closed_when_the_scheduler_cannot_be_asked(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = checkout(clean_env)
    bin_dir = clean_env / "bin"
    bin_dir.mkdir()
    shim(bin_dir, "sbatch", "echo SUBMITTED > " + str(clean_env / "submitted") + "\necho 1\n")
    shim(bin_dir, "squeue", "echo 'squeue: error: slurm_load_jobs' >&2\nexit 1\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.chdir(home)
    assert main(["start", "--root", "/r", "--account", "a", "--partition", "p"]) == 1
    assert "could not ask the scheduler" in capsys.readouterr().err
    assert not (clean_env / "submitted").exists()


def test_slurm_start_withdraws_when_another_start_won_the_race(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both starts see no resident, both submit; the later job id withdraws."""
    home = checkout(clean_env)
    bin_dir = clean_env / "bin"
    bin_dir.mkdir()
    calls = clean_env / "squeue.calls"
    shim(bin_dir, "sbatch", "echo 4242\n")
    # first lookup: nothing; after the submit: the other start's job and ours
    shim(
        bin_dir,
        "squeue",
        f"echo x >> {calls}\n[ $(wc -l < {calls}) -gt 1 ] && printf '4242\\n4100\\n'\nexit 0\n",
    )
    shim(bin_dir, "scancel", 'echo "$1" > ' + str(clean_env / "cancelled") + "\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.chdir(home)
    assert main(["start", "--root", "/r", "--account", "a", "--partition", "p"]) == 0
    assert (clean_env / "cancelled").read_text().strip() == "4242"
    assert "job 4100" in capsys.readouterr().err


def test_slurm_start_reports_a_race_loser_it_could_not_cancel(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = checkout(clean_env)
    bin_dir = clean_env / "bin"
    bin_dir.mkdir()
    calls = clean_env / "squeue.calls"
    shim(bin_dir, "sbatch", "echo 4242\n")
    shim(
        bin_dir,
        "squeue",
        f"echo x >> {calls}\n[ $(wc -l < {calls}) -gt 1 ] && printf '4242\\n4100\\n'\nexit 0\n",
    )
    shim(bin_dir, "scancel", "echo 'scancel: error: Kill job error' >&2\nexit 1\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.chdir(home)
    assert main(["start", "--root", "/r", "--account", "a", "--partition", "p"]) == 1
    err = capsys.readouterr().err
    assert "could not be cancelled" in err and "scancel 4242" in err


def test_local_start_reads_the_env_file_once(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli, "ENV_FILE", env_file(clean_env, "OUTERLOOP_TARGET=o/r\n"))
    reads: list[tuple[str, ...]] = []
    real = cli.env_file_values

    def counting(path: Path, keys: tuple[str, ...] = cli.START_KEYS) -> dict[str, str]:
        reads.append(keys)
        return real(path, keys)

    monkeypatch.setattr(cli, "env_file_values", counting)
    seen: dict[str, dict[str, str]] = {}
    monkeypatch.setattr(cli, "_exec", lambda cmd, env: seen.setdefault("env", env) and 0)
    monkeypatch.chdir(checkout(clean_env))
    assert main(["start", "--root", str(clean_env / "s")]) == 0
    assert len(reads) == 1
    assert seen["env"]["OUTERLOOP_TARGET"] == "o/r"


def test_start_errors_are_exit_2_with_the_diagnosis(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/sbatch")
    monkeypatch.chdir(checkout(clean_env))
    assert main(["start"]) == 2
    assert "state root" in capsys.readouterr().err


def test_tick_subcommand_forwards_to_the_tick_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    from outerloop import tick

    seen: list[str] = []

    def fake_main() -> int:
        seen.extend(sys.argv)
        return 7

    monkeypatch.setattr(tick, "main", fake_main)
    assert main(["tick", "--root", "/r", "--loop"]) == 7
    assert seen[1:] == ["--root", "/r", "--loop"]


@pytest.mark.parametrize("backend", ["claude", "codex"])
def test_missing_harness_binary_resolution(tmp_path: Path, backend: str) -> None:
    from outerloop.cli import missing_harness_binary

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    present = bin_dir / backend
    key = f"OUTERLOOP_{backend.upper()}_BIN"
    other = "codex" if backend == "claude" else "claude"
    values = {
        "OUTERLOOP_AUTHOR_BACKEND": backend,
        f"OUTERLOOP_{other.upper()}_BIN": str(tmp_path / "stale"),
        "REVIEW_HERMES_REPO": str(tmp_path / "stale-hermes"),
    }
    env = {"PATH": str(bin_dir)}
    problem = missing_harness_binary(values, env)
    assert f"`{backend}` on PATH" in problem
    assert "init --force" in problem
    assert f"scripts/install_{backend}.sh" in problem
    present.write_text("#!/bin/sh\nexit 0\n")
    present.chmod(0o755)
    assert missing_harness_binary(values, env) == ""  # stale other backends do not block
    values[key] = str(present)
    assert missing_harness_binary(values, {"PATH": ""}) == ""
    values[key] = str(tmp_path / "gone")
    assert f"{key}={values[key]}" in missing_harness_binary(values, env)  # no PATH fallback
    assert missing_harness_binary(values, {**env, key: str(present)}) == ""
    assert missing_harness_binary(values, {**env, key: ""}) == ""
    present.chmod(0o644)
    assert missing_harness_binary(values, {**env, key: str(present)})
    present.unlink()
    present.mkdir()
    assert missing_harness_binary(values, {**env, key: str(present)})


@pytest.mark.parametrize("backend", ["claude", "codex"])
@pytest.mark.parametrize("recorded", [False, True])
def test_start_refuses_missing_harness_before_any_execution(
    clean_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backend: str,
    recorded: bool,
) -> None:
    monkeypatch.setenv("OUTERLOOP_AUTHOR_BACKEND", backend)
    monkeypatch.setenv(
        f"OUTERLOOP_{backend.upper()}_BIN", str(clean_env / "gone") if recorded else ""
    )
    monkeypatch.setenv("PATH", str(clean_env))
    monkeypatch.setenv("HOME", str(clean_env))  # not the runner's own ~/.local/bin
    monkeypatch.setattr(cli, "plan_start", lambda **kw: pytest.fail("must refuse before planning"))
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: pytest.fail("must not execute"))
    monkeypatch.setattr(cli, "_exec", lambda *a: pytest.fail("must not exec"))
    assert main(["start"]) == 2
    err = capsys.readouterr().err
    assert backend in err and "not an executable file" in err
    assert (str(clean_env / "gone") if recorded else "on PATH") in err


def test_start_dry_run_allows_missing_recorded_binary(clean_env, monkeypatch, capsys):
    monkeypatch.setenv("OUTERLOOP_CLAUDE_BIN", str(clean_env / "gone"))
    assert main(["start", "--local", "--dry-run"]) == 0
    assert "outerloop.tick" in capsys.readouterr().out


def test_missing_claude_model_resolution() -> None:
    """The Claude model is required by a claude author without its own model, by
    a claude panel lens without one, and by a provisioned steward lane; a
    deployment with none of those, or with the setting, passes."""
    from outerloop.cli import missing_claude_model

    unset = "OUTERLOOP_CLAUDE_MODEL is not set"
    assert missing_claude_model({}, {"OUTERLOOP_CLAUDE_MODEL": "claude-x"}) == ""
    assert missing_claude_model({"OUTERLOOP_CLAUDE_MODEL": "claude-x"}, {}) == ""
    # the defaults alone (claude author, verify,review panel) need it
    problem = missing_claude_model({}, {})
    assert unset in problem and "OUTERLOOP_CLAUDE_MODEL=<model>" in problem
    assert "claude author" in problem and "panel judge" in problem and "steward" not in problem
    # the author's own model covers the author; explicit lens models cover the panel
    covered = {
        "OUTERLOOP_AUTHOR_MODEL": "claude-author",
        "OUTERLOOP_PANEL": "verify:claude:claude-v,review:codex:gpt-x",
    }
    assert missing_claude_model(covered, {}) == ""
    # a bare claude lens under a claude author inherits the author's model
    assert missing_claude_model({**covered, "OUTERLOOP_PANEL": "review"}, {}) == ""
    # ... but under a codex author it must name its own model
    codex_bare = {"OUTERLOOP_AUTHOR_BACKEND": "codex", "OUTERLOOP_PANEL": "review:claude"}
    from outerloop.cli import missing_panel_model

    assert "review:claude:<model>" in missing_panel_model(codex_bare, {})
    # a codex author with the panel off needs nothing; a provisioned steward does
    codex = {"OUTERLOOP_AUTHOR_BACKEND": "codex", "OUTERLOOP_PANEL": ""}
    assert missing_claude_model(codex, {}) == ""
    assert "the steward" in missing_claude_model(codex, {"OUTERLOOP_STEWARD_KEY_FILE": "/k"})
    # the shell wins over .env, including an explicit empty value
    shell_cleared = {"OUTERLOOP_CLAUDE_MODEL": ""}
    assert unset in missing_claude_model({"OUTERLOOP_CLAUDE_MODEL": "claude-x"}, shell_cleared)
    # a malformed panel is the tick's diagnosis, not this one's
    assert missing_claude_model({**codex, "OUTERLOOP_PANEL": "bogus"}, {}) == ""


@pytest.mark.parametrize("dry_run", [False, True])
def test_start_refuses_without_the_claude_model(clean_env, monkeypatch, capsys, dry_run):
    monkeypatch.delenv("OUTERLOOP_CLAUDE_MODEL", raising=False)
    monkeypatch.setattr(cli, "plan_start", lambda **kw: pytest.fail("must refuse before planning"))
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: pytest.fail("must not execute"))
    monkeypatch.setattr(cli, "_exec", lambda *a: pytest.fail("must not exec"))
    argv = ["start", "--local", *(["--dry-run"] if dry_run else [])]
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert "OUTERLOOP_CLAUDE_MODEL is not set" in err
    assert f"OUTERLOOP_CLAUDE_MODEL=<model> to {clean_env / 'absent.env'}" in err
    # covered roles pass: the author names its model and every claude lens does too
    monkeypatch.setenv("OUTERLOOP_AUTHOR_MODEL", "claude-author")
    monkeypatch.setenv("OUTERLOOP_PANEL", "verify:claude:claude-v,review:claude:claude-r")
    monkeypatch.setattr(cli, "plan_start", plan_start)
    monkeypatch.setattr(cli, "_exec", lambda *a: 0)
    assert main(argv) == 0


def test_hermes_is_a_review_backend_not_an_author(tmp_path):
    problem = cli.missing_harness_binary({"OUTERLOOP_AUTHOR_BACKEND": "hermes"}, {})
    assert "unsupported author backend 'hermes'" in problem
    assert "scripts/install_hermes.sh" in problem


# ---------------------------------------------------------------- uv


def test_find_uv_takes_path_first_then_the_installer_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/opt/bin/uv")
    assert cli.find_uv() == ("/opt/bin/uv", "")
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.find_uv() == ("", "")  # nowhere
    installed = tmp_path / ".local" / "bin" / "uv"
    installed.parent.mkdir(parents=True)
    installed.mkdir()  # a searchable DIRECTORY of that name is not uv
    assert cli.find_uv() == ("", "")
    installed.rmdir()
    installed.write_text("#!/bin/sh\n")
    installed.chmod(0o755)
    assert cli.find_uv() == (str(installed), str(installed.parent))


def test_start_stops_when_uv_is_nowhere(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A loop without uv marks every run unmeasured; start says so and stops.
    The dry run still prints the command."""
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli, "find_uv", lambda: ("", ""))
    monkeypatch.setattr(cli, "_exec", lambda cmd, env: pytest.fail("must not exec"))
    monkeypatch.chdir(checkout(clean_env))
    assert main(["start", "--root", str(clean_env / "s")]) == 2
    assert "uv is not on PATH" in capsys.readouterr().err
    assert main(["start", "--dry-run", "--root", str(clean_env / "s")]) == 0


def test_start_adds_the_installer_directory_to_the_loops_path(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli, "find_uv", lambda: ("/h/.local/bin/uv", "/h/.local/bin"))
    seen: dict[str, object] = {}

    def fake_exec(cmd: list[str], env: dict[str, str]) -> int:
        seen["env"] = env
        return 0

    monkeypatch.setattr(cli, "_exec", fake_exec)
    monkeypatch.chdir(checkout(clean_env))
    assert main(["start", "--root", str(clean_env / "s")]) == 0
    env = seen["env"]
    assert isinstance(env, dict)
    assert env["PATH"].startswith("/h/.local/bin" + os.pathsep)


def test_slurm_start_hands_the_installer_directory_to_the_resident_job(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resident job inherits start's environment through --export=ALL, so
    the fallback directory must reach sbatch's PATH, not only the local loop."""
    home = checkout(clean_env)
    bin_dir = clean_env / "bin"
    bin_dir.mkdir()
    pathlog = clean_env / "sbatch.path"
    shim(bin_dir, "sbatch", f'printf "%s" "$PATH" > {pathlog}\necho "4242;torch"\n')
    shim(bin_dir, "squeue", "exit 0\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(cli, "find_uv", lambda: ("/h/.local/bin/uv", "/h/.local/bin"))
    monkeypatch.chdir(home)
    assert main(["start", "--root", "/scratch/me/ar", "--account", "a", "--partition", "p"]) == 0
    assert pathlog.read_text().startswith("/h/.local/bin" + os.pathsep)


# ---------------------------------------------------------------- upgrade


def test_upgrade_dry_run_prints_the_pip_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "subprocess", pytest.fail)  # must not run pip
    assert main(["upgrade", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "-m pip install --upgrade outerloop-science" in out
    assert "--pre" not in out


def test_upgrade_pre_dry_run_adds_the_pre_flag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "subprocess", pytest.fail)
    assert main(["upgrade", "--pre", "--dry-run"]) == 0
    assert "install --upgrade outerloop-science --pre" in capsys.readouterr().out


def test_upgrade_runs_pip_and_reports_the_version_change(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[list[str]] = []
    versions = iter(["0.1.0.dev3", "0.1.0.dev4"])

    def fake_run(argv: list[str], **kw: Any) -> Any:
        import subprocess

        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "_installed_version", lambda _python: next(versions))
    assert main(["upgrade"]) == 0
    assert calls[0][1:] == ["-m", "pip", "install", "--upgrade", "outerloop-science"]
    out = capsys.readouterr().out
    assert "0.1.0.dev3 -> 0.1.0.dev4" in out
    assert "outerloop start" in out


def test_upgrade_reports_when_already_current(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_run(argv: list[str], **kw: Any) -> Any:
        import subprocess

        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "_installed_version", lambda _python: "0.1.0.dev3")
    assert main(["upgrade"]) == 0
    assert "already up to date: outerloop 0.1.0.dev3" in capsys.readouterr().out


def test_upgrade_surfaces_a_failed_pip_and_its_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_run(argv: list[str], **kw: Any) -> Any:
        import subprocess

        return subprocess.CompletedProcess(argv, 3)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "_installed_version", lambda _python: "0.1.0.dev3")
    assert main(["upgrade"]) == 3
    assert "upgrade failed (pip exited 3)" in capsys.readouterr().err


def test_installed_version_reads_a_fresh_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kw: Any) -> Any:
        import subprocess

        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="0.1.0.dev9\n", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli._installed_version("/usr/bin/python3") == "0.1.0.dev9"
    # a fresh interpreter is what reflects a just-written upgrade, not this process
    assert seen[0][0] == "/usr/bin/python3"
    assert seen[0][1] == "-c"
    assert "outerloop-science" in seen[0][2]


def test_installed_version_is_unknown_when_the_lookup_says_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(argv: list[str], **kw: Any) -> Any:
        import subprocess

        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli._installed_version(sys.executable) == "unknown"


@pytest.mark.parametrize(
    "flag,env,file,mode",
    [
        ("", {}, {"OUTERLOOP_TICK_HOST": "login"}, "login"),
        ("", {"OUTERLOOP_TICK_HOST": "login"}, {"OUTERLOOP_TICK_HOST": "resident"}, "login"),
        ("login", {"OUTERLOOP_TICK_HOST": "resident"}, {}, "login"),
        ("resident", {"OUTERLOOP_TICK_HOST": "login"}, {}, "slurm"),
        ("login", {"OUTERLOOP_COMPUTE": "local"}, {}, "local"),
    ],
)
def test_tick_host_precedence(tmp_path, flag, env, file, mode):
    p = plan(tmp_path, root="/r", tick_host=flag, environ=env, from_file=file)
    assert p.mode == mode
    if mode == "login":
        assert p.command() == [sys.executable, "-m", "outerloop.tick", "--root", "/r", "--loop"]


def test_login_requires_sbatch(tmp_path):
    with pytest.raises(StartError, match="sbatch on PATH"):
        plan(tmp_path, root="/r", tick_host="login", sbatch_on_path=False)


def test_login_exec_exports_slurm_settings(clean_env, monkeypatch):
    from outerloop.compute import SlurmCompute, compute_from_env

    monkeypatch.chdir(checkout(clean_env))
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(cli, "_resident_jobs", lambda: [])
    niced: list[int] = []
    monkeypatch.setattr(cli.os, "nice", niced.append)
    settings = {key: "" for key in TICK_ENV_KEYS}
    settings.update(OUTERLOOP_QOS="priority", OUTERLOOP_APPTAINER_BIN="/apps/apptainer")
    settings.update(OUTERLOOP_CLAUDE_BIN=sys.executable, OUTERLOOP_CODEX_BIN=sys.executable)
    settings.update(OUTERLOOP_CLAUDE_MODEL="claude-test-model")  # clean_env's shell value
    settings.update(
        OUTERLOOP_TICK_HOST="login", OUTERLOOP_ACCOUNT="acct", OUTERLOOP_PARTITION="cpu"
    )
    monkeypatch.setattr(
        cli, "ENV_FILE", env_file(clean_env, "\n".join(f"{k}={v}" for k, v in settings.items()))
    )
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli, "_exec", lambda cmd, env: seen.update(cmd=cmd, env=env) or 0)
    assert main(["start", "--root", str(clean_env / "state")]) == 0
    env = seen["env"]
    assert set(TICK_ENV_KEYS) <= env.keys()
    assert all(env[key] == value for key, value in settings.items() if key != "OUTERLOOP_BOT_LOGIN")
    assert env["OUTERLOOP_COMPUTE"] == "slurm"
    with monkeypatch.context() as m:
        m.setenv("OUTERLOOP_COMPUTE", env["OUTERLOOP_COMPUTE"])
        assert isinstance(compute_from_env(), SlurmCompute)
    assert "OUTERLOOP_RESIDENT" not in env
    assert niced == [10]


@pytest.mark.parametrize("block", ["lease", "resident", "scheduler"])
def test_login_refuses_other_owner(clean_env, monkeypatch, capsys, block):
    import time

    from outerloop.runstate import acquire_tick_lease, release_tick_lease

    monkeypatch.chdir(checkout(clean_env))
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(
        cli,
        "_resident_jobs",
        lambda: ["777"] if block == "resident" else None if block == "scheduler" else [],
    )
    monkeypatch.setattr(cli, "_exec", lambda *a: pytest.fail("must refuse"))
    root = clean_env / "state"
    lease = acquire_tick_lease(root, "alpha1:123", time.time(), 5400) if block == "lease" else None
    try:
        assert main(["start", "--tick-host", "login", "--root", str(root)]) == 2
        assert {"lease": "alpha1:123", "resident": "777", "scheduler": "squeue failed"}[
            block
        ] in capsys.readouterr().err
    finally:
        if lease is not None:
            release_tick_lease(lease)


def test_resident_qos_from_file_and_environment(tmp_path):
    p = plan(tmp_path, root="/r", from_file={"OUTERLOOP_QOS": "priority"})
    assert "--qos=priority" in p.command()
    assert p.export_env()["OUTERLOOP_QOS"] == "priority"
    p = plan(
        tmp_path, root="/r", from_file={"OUTERLOOP_QOS": "priority"}, environ={"OUTERLOOP_QOS": ""}
    )
    assert not any(arg.startswith("--qos") for arg in p.command())


def test_login_dry_run_prints_exec_without_starting(clean_env, monkeypatch, capsys):
    monkeypatch.chdir(checkout(clean_env))
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(cli, "_resident_jobs", lambda: pytest.fail("dry run must not query Slurm"))
    monkeypatch.setattr(cli.os, "nice", lambda value: pytest.fail("dry run must not nice"))
    assert main(["start", "--tick-host", "login", "--root", "/shared/state", "--dry-run"]) == 0
    assert capsys.readouterr().out.strip() == shlex.join(
        [sys.executable, "-m", "outerloop.tick", "--root", "/shared/state", "--loop"]
    )


def test_login_uses_foreground_root_and_home_defaults(tmp_path):
    p = plan(
        tmp_path,
        tick_host="login",
        cwd=tmp_path,
        environ={"HOME": str(tmp_path), "OUTERLOOP_RESIDENT_MINUTES": "unused"},
        from_file={"OUTERLOOP_ACCOUNT": "a", "OUTERLOOP_PARTITION": "p"},
    )
    assert p.root == tmp_path / ".outerloop"
    assert p.home == p.root / "home"
    assert p.account == "a" and p.partition == "p"


def test_resident_start_refuses_a_live_login_loop(clean_env, monkeypatch, capsys):
    import time

    from outerloop.runstate import acquire_tick_lease, release_tick_lease

    monkeypatch.chdir(checkout(clean_env))
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(cli, "_resident_jobs", lambda: pytest.fail("the lease is checked first"))
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: pytest.fail("must not submit"))
    root = clean_env / "state"
    lease = acquire_tick_lease(root, "alpha1:123", time.time(), 5400)
    try:
        assert main(["start", "--root", str(root)]) == 2
        assert "alpha1:123" in capsys.readouterr().err
    finally:
        release_tick_lease(lease)


def test_resident_tick_takes_no_root_lease(tmp_path, monkeypatch):
    import sys

    from outerloop import tick as mod

    monkeypatch.setenv("OUTERLOOP_COMPUTE", "slurm")
    monkeypatch.delenv("OUTERLOOP_TICK_HOST", raising=False)
    monkeypatch.setattr(sys, "argv", ["tick", "--root", str(tmp_path)])
    monkeypatch.setattr(mod, "_service_spec_from_env", lambda root: (None, None))
    monkeypatch.setattr(mod, "tick", lambda *a, **k: mod.TickReport())
    assert mod.main() == 0
    assert not (tmp_path / "TICK").exists()


def test_local_start_refuses_a_held_lease(clean_env, monkeypatch, capsys):
    import time

    from outerloop.runstate import acquire_tick_lease, release_tick_lease

    monkeypatch.chdir(checkout(clean_env))
    monkeypatch.setattr(cli, "_exec", lambda *a: pytest.fail("must refuse"))
    root = clean_env / "state"
    lease = acquire_tick_lease(root, "alpha1:123", time.time(), 5400)
    try:
        assert main(["start", "--local", "--root", str(root)]) == 2
        assert "alpha1:123" in capsys.readouterr().err
    finally:
        release_tick_lease(lease)


def test_resident_submission_withdraws_when_a_loop_took_the_lease(clean_env, monkeypatch, capsys):
    from types import SimpleNamespace

    from outerloop import runstate

    monkeypatch.chdir(checkout(clean_env))
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/" + name)
    jobs = iter([[], ["4242"]])
    monkeypatch.setattr(cli, "_resident_jobs", lambda: next(jobs))
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="4242;cluster", stderr=""),
    )
    holders = iter(["", "alpha1:5"])  # free at the check, taken after the submission
    monkeypatch.setattr(runstate, "tick_lease_holder", lambda *a, **k: next(holders))
    cancelled: list[str] = []

    def cancel(job: str) -> bool:
        cancelled.append(job)
        return True

    monkeypatch.setattr(cli, "_cancel", cancel)
    assert main(["start", "--root", str(clean_env / "state")]) == 0
    assert cancelled == ["4242"]
    assert "alpha1:5" in capsys.readouterr().err


@pytest.mark.parametrize("backend", ["claude", "codex"])
@pytest.mark.parametrize("recorded", [False, True])
def test_start_launches_with_resolved_author(clean_env, monkeypatch, backend, recorded):
    from outerloop.harness import default_binary

    binary = clean_env / backend
    shim(clean_env, backend, "exit 0\n")
    key = f"OUTERLOOP_{backend.upper()}_BIN"
    monkeypatch.delenv(key)
    monkeypatch.setenv("PATH", str(clean_env) if not recorded else "")
    values = {"OUTERLOOP_AUTHOR_BACKEND": backend}
    if recorded:
        values[key] = str(binary)
    monkeypatch.setattr(
        cli, "ENV_FILE", env_file(clean_env, "\n".join(f"{k}={v}" for k, v in values.items()))
    )
    seen = []

    def launch(cmd, env):
        seen.append(default_binary(backend, env))
        return 0

    monkeypatch.setattr(cli, "_exec", launch)
    assert main(["start", "--local", "--root", str(clean_env / "state")]) == 0
    assert seen == [str(binary.resolve())]


def test_missing_path_does_not_accept_a_cli_only_in_cwd(tmp_path, monkeypatch):
    shim(tmp_path, "claude", "exit 0\n")
    monkeypatch.chdir(tmp_path)
    assert cli.missing_harness_binary({}, {"PATH": str(tmp_path / "absent")})


def test_start_sees_steward_key_from_env_file(clean_env, monkeypatch, capsys):
    monkeypatch.delenv("OUTERLOOP_CLAUDE_MODEL", raising=False)
    monkeypatch.delenv("OUTERLOOP_STEWARD_KEY_FILE", raising=False)
    path = env_file(
        clean_env,
        "OUTERLOOP_AUTHOR_BACKEND=codex\nOUTERLOOP_AUTHOR_MODEL=gpt-x\n"
        "OUTERLOOP_PANEL=\nOUTERLOOP_STEWARD_KEY_FILE=/keys/steward\n",
    )
    monkeypatch.setattr(cli, "ENV_FILE", path)
    exports = []

    def capture_exec(cmd, env):
        exports.append(env)
        return 0

    monkeypatch.setattr(cli, "_exec", capture_exec)
    assert main(["start", "--local"]) == 2
    assert "the steward" in capsys.readouterr().err
    path.write_text(path.read_text() + "OUTERLOOP_CLAUDE_MODEL=claude-x\n")
    assert main(["start", "--local"]) == 0
    assert exports[-1]["OUTERLOOP_STEWARD_KEY_FILE"] == "/keys/steward"
