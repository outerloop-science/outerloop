"""The author's launch/sleep TOOL — the agent-facing surface. The JSON file is
the internal ABI; the author drives everything through this CLI, so these tests
call `main(argv, root)` exactly as a Bash invocation would."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from autoresearch.syscall import read_request
from autoresearch.syscall_cli import main


def run(tmp: Path, *argv: str, capsys=None) -> int:
    return main(list(argv), root=tmp)


def test_launch_then_sleep_commits_the_abi(tmp_path: Path, capsys) -> None:
    assert (
        run(
            tmp_path,
            "launch",
            "--name",
            "train-lr3",
            "--minutes",
            "90",
            "--artifact",
            "results/curve.json",
            "--",
            "uv",
            "run",
            "python",
            "train.py",
            "--lr",
            "3e-4",
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "staged launch 'train-lr3'" in out and "1 staged" in out
    assert run(tmp_path, "sleep") == 0
    assert "END YOUR TURN" in capsys.readouterr().out
    # the committed ABI parses through the KERNEL's authoritative reader
    req = read_request(tmp_path)
    assert req is not None
    assert req.launches[0].name == "train-lr3"
    assert req.launches[0].command == "uv run python train.py --lr 3e-4"
    assert req.launches[0].minutes == 90
    assert req.launches[0].artifacts == ("results/curve.json",)
    # staging is cleared by the commit
    assert not (tmp_path / ".autoresearch" / "request.json").exists()


def test_quoted_command_args_survive_to_the_abi(tmp_path: Path, capsys) -> None:
    # a shell that invoked the CLI split `--label "a b"` into tokens; the tool
    # must re-quote so the eventual sh -c re-parses the SAME tokens, not two
    # (terra #133 r1). Round-trips through the kernel's authoritative reader.
    run(
        tmp_path,
        "launch",
        "--name",
        "q",
        "--",
        "python",
        "t.py",
        "--label",
        "a b",
        "--flag=x y",
    )
    run(tmp_path, "sleep")
    req = read_request(tmp_path)
    assert req is not None
    # shlex round-trip: the command re-splits into exactly the original tokens
    import shlex as _shlex

    assert _shlex.split(req.launches[0].command) == [
        "python",
        "t.py",
        "--label",
        "a b",
        "--flag=x y",
    ]


def test_note_and_status_and_cancel(tmp_path: Path, capsys) -> None:
    run(tmp_path, "launch", "--name", "probe", "--", "echo", "hi")
    run(tmp_path, "note", "check the tails first")
    capsys.readouterr()
    assert run(tmp_path, "status") == 0
    out = capsys.readouterr().out
    assert "1 launch(es) staged" in out and "probe" in out and "check the tails" in out
    assert run(tmp_path, "cancel") == 0
    capsys.readouterr()
    run(tmp_path, "status")
    assert "0 launch(es) staged" in capsys.readouterr().out


def test_validation_fails_fast_in_session(tmp_path: Path, capsys) -> None:
    # the whole point of the tool: bad input fails IMMEDIATELY with a message,
    # instead of becoming a burned post-sleep session-error.
    cases = [
        (["launch", "--name", "UPPER", "--", "x"], "--name"),
        (["launch", "--name", "ok"], "needs a command"),
        (["launch", "--name", "ok", "--artifact", "../pw", "--", "x"], "repo-relative"),
        (["launch", "--name", "ok", "--minutes", "0", "--", "x"], "positive"),
        (["note", "x" * 2001], "exceeds"),
    ]
    for argv, needle in cases:
        assert run(tmp_path, *argv) == 2
        err = capsys.readouterr().err
        assert needle in err, (argv, err)
    # duplicate staged name
    assert run(tmp_path, "launch", "--name", "a", "--", "x") == 0
    assert run(tmp_path, "launch", "--name", "a", "--", "y") == 2
    assert "already staged" in capsys.readouterr().err


def test_sleep_with_nothing_staged_is_a_checkpoint(tmp_path: Path, capsys) -> None:
    assert run(tmp_path, "sleep") == 0
    assert "checkpoint" in capsys.readouterr().out
    req = read_request(tmp_path)
    assert req is not None and req.launches == ()


def test_status_shows_the_kernel_written_budget(tmp_path: Path, capsys) -> None:
    from autoresearch.syscall import write_budget

    write_budget(tmp_path, launches_remaining=3, sleeps_remaining=4)
    run(tmp_path, "status")
    assert "3 launches, 4 sleeps remaining" in capsys.readouterr().out


def test_installed_tool_is_standalone(tmp_path: Path) -> None:
    """The kernel copies the tool into a sandbox WITHOUT autoresearch
    installed — prove the copy runs under a bare interpreter (isolated mode:
    no site-packages, no cwd on sys.path)."""
    from autoresearch.syscall import install_tool

    install_tool(tmp_path)
    tool = tmp_path / ".autoresearch" / "syscall"
    assert tool.exists()
    r = subprocess.run(
        [sys.executable, "-I", str(tool), "launch", "--name", "solo", "--", "echo", "ok"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "staged launch 'solo'" in r.stdout
    r = subprocess.run(
        [sys.executable, "-I", str(tool), "sleep"], cwd=tmp_path, capture_output=True, text=True
    )
    assert r.returncode == 0 and "END YOUR TURN" in r.stdout
    abi = json.loads((tmp_path / ".autoresearch" / "syscall.json").read_text())
    assert abi["launches"][0]["name"] == "solo"
