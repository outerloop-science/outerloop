"""`autoresearch start`: one command, Slurm or local, settings from flags,
environment, then .env."""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from autoresearch import cli
from autoresearch.cli import (
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
        "# comment\nAUTORESEARCH_ROOT=/old\nAUTORESEARCH_ROOT='/scratch/me/ar'\r\n"
        'AUTORESEARCH_ACCOUNT="acct"\nAUTORESEARCH_PANEL=\nOTHER=x\n'
        "AUTORESEARCH_CADENCE_MIN = 20\n",
    )
    got = env_file_values(path)
    assert got == {
        "AUTORESEARCH_ROOT": "/scratch/me/ar",
        "AUTORESEARCH_ACCOUNT": "acct",
        "AUTORESEARCH_CADENCE_MIN": "20",
    }
    # the author-knob view of the same file: an empty value is PRESENT
    assert env_file_values(path, TICK_ENV_KEYS) == {"AUTORESEARCH_PANEL": ""}


def test_env_file_values_missing_file_is_empty(tmp_path: Path) -> None:
    assert env_file_values(tmp_path / "absent") == {}


def test_env_file_values_unreadable_is_a_start_error(tmp_path: Path) -> None:
    (tmp_path / ".env").mkdir()  # a directory where the file should be
    with pytest.raises(StartError, match="cannot read"):
        env_file_values(tmp_path / ".env")


@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666])
def test_env_file_values_refuses_a_writable_file(tmp_path: Path, mode: int) -> None:
    path = env_file(tmp_path, "AUTORESEARCH_ROOT=/x\n", mode)
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


def test_local_without_sbatch_defaults_the_root(tmp_path: Path) -> None:
    p = plan(tmp_path, sbatch_on_path=False)
    assert p.mode == "local"
    assert p.root == DEFAULT_LOCAL_ROOT
    assert p.command() == [
        sys.executable,
        "-m",
        "autoresearch.tick",
        "--root",
        str(DEFAULT_LOCAL_ROOT),
        "--loop",
    ]


def test_local_by_flag_or_env_or_file_even_with_sbatch(tmp_path: Path) -> None:
    assert plan(tmp_path, local=True).mode == "local"
    assert plan(tmp_path, environ={"AUTORESEARCH_COMPUTE": "Local"}).mode == "local"
    assert plan(tmp_path, from_file={"AUTORESEARCH_COMPUTE": "local"}).mode == "local"
    p = plan(tmp_path, local=True, root="~/state")
    assert p.root == Path("~/state").expanduser()


def test_slurm_composes_the_resident_submit(tmp_path: Path) -> None:
    home = checkout(tmp_path)
    p = plan(
        tmp_path,
        cwd=home,
        from_file={
            "AUTORESEARCH_ROOT": "/scratch/me/ar",
            "AUTORESEARCH_ACCOUNT": "pr_1_general",
            "AUTORESEARCH_PARTITION": "cpu_short",
            "AUTORESEARCH_CADENCE_MIN": "20",
            "AUTORESEARCH_PAT_FILE": "/home/me/.config/autoresearch/bot_pat",
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
        "--export=ALL,AUTORESEARCH_RESIDENT=1,"
        f"AUTORESEARCH_HOME={home},AUTORESEARCH_ROOT=/scratch/me/ar,"
        "AUTORESEARCH_ACCOUNT=pr_1_general,AUTORESEARCH_PARTITION=cpu_short,"
        f"AUTORESEARCH_RESIDENT_MINUTES={DEFAULT_RESIDENT_MINUTES},"
        "AUTORESEARCH_CADENCE_MIN=20,AUTORESEARCH_PAT_FILE=/home/me/.config/autoresearch/bot_pat",
        str(home / "scripts" / "tick_chain.sbatch"),
    ]


def test_precedence_is_flag_then_environment_then_file(tmp_path: Path) -> None:
    p = plan(
        tmp_path,
        partition="flagged",
        environ={
            "AUTORESEARCH_ROOT": "/env/root",
            "AUTORESEARCH_ACCOUNT": "envacct",
            "AUTORESEARCH_PARTITION": "envpart",
        },
        from_file={
            "AUTORESEARCH_ROOT": "/file/root",
            "AUTORESEARCH_ACCOUNT": "fileacct",
            "AUTORESEARCH_PARTITION": "filepart",
        },
    )
    assert (str(p.root), p.account, p.partition) == ("/env/root", "envacct", "flagged")


def test_slurm_home_from_env_or_cwd_and_must_be_a_checkout(tmp_path: Path) -> None:
    home = checkout(tmp_path)
    base = {"AUTORESEARCH_ROOT": "/r", "AUTORESEARCH_ACCOUNT": "a", "AUTORESEARCH_PARTITION": "p"}
    assert (
        plan(tmp_path, cwd=tmp_path, environ={**base, "AUTORESEARCH_HOME": str(home)}).home == home
    )
    with pytest.raises(StartError, match="not an autoresearch checkout"):
        plan(tmp_path, cwd=tmp_path, environ=base)


def test_slurm_requires_root_account_and_partition(tmp_path: Path) -> None:
    with pytest.raises(StartError, match="state root"):
        plan(tmp_path)
    with pytest.raises(
        StartError,
        match="--account / AUTORESEARCH_ACCOUNT and --partition / AUTORESEARCH_PARTITION",
    ):
        plan(tmp_path, root="/r")
    with pytest.raises(StartError, match="--partition / AUTORESEARCH_PARTITION"):
        plan(tmp_path, root="/r", account="a")


def test_slurm_rejects_values_that_would_break_export(tmp_path: Path) -> None:
    with pytest.raises(StartError, match="commas or whitespace"):
        plan(tmp_path, root="/r", account="a,b", partition="p")
    with pytest.raises(StartError, match="commas or whitespace"):
        plan(tmp_path, root="/my root", account="a", partition="p")


def test_slurm_resident_minutes_must_be_a_positive_integer(tmp_path: Path) -> None:
    base = dict(root="/r", account="a", partition="p")
    p = plan(tmp_path, **base, environ={"AUTORESEARCH_RESIDENT_MINUTES": "240"})
    assert p.resident_minutes == 240
    assert "--time=240" in p.command()
    assert any("AUTORESEARCH_RESIDENT_MINUTES=240" in a for a in p.command())  # successors keep it
    with pytest.raises(StartError, match="whole number"):
        plan(tmp_path, **base, environ={"AUTORESEARCH_RESIDENT_MINUTES": "4h"})
    with pytest.raises(StartError, match="positive"):
        plan(tmp_path, **base, environ={"AUTORESEARCH_RESIDENT_MINUTES": "0"})


# ---------------------------------------------------------------- main


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for key in (*START_KEYS, *TICK_ENV_KEYS, "AUTORESEARCH_HOME"):
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
    assert main(["start", "--dry-run", "--root", str(clean_env / "s")]) == 0
    out = capsys.readouterr().out
    assert "autoresearch.tick" in out and "--loop" in out and str(clean_env / "s") in out


def test_local_start_execs_the_loop_with_env_knobs(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        cli,
        "ENV_FILE",
        env_file(
            clean_env,
            "AUTORESEARCH_TARGET=o/r\nAUTORESEARCH_PANEL=\nAUTORESEARCH_CADENCE_MIN=15\n"
            "AUTORESEARCH_PAT_FILE=/home/me/pat\n",
        ),
    )
    monkeypatch.setenv("AUTORESEARCH_TARGET", "shell/wins")
    seen: dict[str, object] = {}

    def fake_exec(cmd: list[str], env: dict[str, str]) -> int:
        seen["cmd"], seen["env"] = cmd, env
        return 0

    monkeypatch.setattr(cli, "_exec", fake_exec)
    assert main(["start", "--root", str(clean_env / "state")]) == 0
    assert seen["cmd"] == [
        sys.executable,
        "-m",
        "autoresearch.tick",
        "--root",
        str(clean_env / "state"),
        "--loop",
    ]
    env = seen["env"]
    assert isinstance(env, dict)
    assert env["AUTORESEARCH_COMPUTE"] == "local"
    assert env["AUTORESEARCH_ROOT"] == str(clean_env / "state")
    assert env["AUTORESEARCH_TARGET"] == "shell/wins"  # the shell beats the file at launch
    assert env["AUTORESEARCH_PANEL"] == ""  # an off-switch in the file still lands
    assert env["AUTORESEARCH_CADENCE_MIN"] == "15"  # the loop's cadence comes from .env too
    assert env["AUTORESEARCH_PAT_FILE"] == "/home/me/pat"


def test_slurm_start_submits_once_and_reports(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = checkout(clean_env)
    bin_dir = clean_env / "bin"
    bin_dir.mkdir()
    log = clean_env / "sbatch.log"
    shim(bin_dir, "sbatch", f'printf "%s\\n" "$@" > {log}\necho "4242;torch"\n')
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
    assert any(
        a.startswith("--export=ALL,AUTORESEARCH_RESIDENT=1,") and f"AUTORESEARCH_HOME={home}" in a
        for a in argv
    )
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


def test_start_errors_are_exit_2_with_the_diagnosis(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/sbatch")
    monkeypatch.chdir(checkout(clean_env))
    assert main(["start"]) == 2
    assert "state root" in capsys.readouterr().err


def test_tick_subcommand_forwards_to_the_tick_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    from autoresearch import tick

    seen: list[str] = []

    def fake_main() -> int:
        seen.extend(sys.argv)
        return 7

    monkeypatch.setattr(tick, "main", fake_main)
    assert main(["tick", "--root", "/r", "--loop"]) == 7
    assert seen[1:] == ["--root", "/r", "--loop"]
