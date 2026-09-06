"""`outerloop start`: one command, Slurm or local, settings from flags,
environment, then .env."""

from __future__ import annotations

import os
import re
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
        "# comment\nAUTORESEARCH_ROOT=/old\nOUTERLOOP_ROOT='/scratch/me/ar'\r\n"
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


def test_default_local_root_honors_a_pre_rename_state_dir(tmp_path: Path) -> None:
    """~/.outerloop is the default; an existing ~/.autoresearch is used only while
    no ~/.outerloop exists (state is never moved); no HOME means the plain
    default."""
    from outerloop.cli import default_local_root

    env = {"HOME": str(tmp_path)}
    assert default_local_root(env) == tmp_path / ".outerloop"
    (tmp_path / ".autoresearch").mkdir()
    assert default_local_root(env) == tmp_path / ".autoresearch"
    (tmp_path / ".outerloop").mkdir()
    assert default_local_root(env) == tmp_path / ".outerloop"
    assert default_local_root({}) == DEFAULT_LOCAL_ROOT


def test_resident_lookup_asks_for_both_names(monkeypatch: Any) -> None:
    """A resident submitted before the rename must still block `start`: the
    singleton serializes by name, so two names would mean two residents."""
    import subprocess

    from outerloop import cli as cli_mod

    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kw: Any) -> Any:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="900\n777\n", stderr="")

    monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
    assert cli_mod._resident_jobs() == ["777", "900"]
    assert "--name=outerloop-resident,autoresearch-resident" in seen[0]


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
