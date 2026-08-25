"""The research syscall TOOL — the one agent-facing surface. The JSON file is
the internal ABI; every role drives everything through this CLI, so these tests
call `main(argv, root)` exactly as a Bash invocation would. Author verbs
(launch/note/sleep) and judge verbs (finding/conclude) share it."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from autoresearch.syscall import read_request, read_verdict
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
    assert "nothing staged" in capsys.readouterr().out


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


def test_artifact_path_check_matches_the_kernel(tmp_path: Path, capsys) -> None:
    # the tool's fast check must accept EXACTLY what the kernel accepts, or the
    # author burns a sleep on a post-session error (terra #133 r2). Cross-check
    # each tricky path against both validators.
    from autoresearch.syscall import _rel_path_ok as kernel_ok

    cases = ["out/x.json", "", ".", "out/./x", "out//x", "../x", "/abs", "~/x", "a\\b"]
    for path in cases:
        rc = run(tmp_path, "launch", "--name", "a", "--artifact", path, "--", "echo", "hi")
        run(tmp_path, "cancel")
        capsys.readouterr()
        tool_ok = rc == 0
        assert tool_ok == kernel_ok(path), (path, tool_ok, kernel_ok(path))


def test_submit_stages_and_rides_the_sleep(tmp_path: Path, capsys) -> None:
    assert run(tmp_path, "submit") == 0
    assert "sealed" in capsys.readouterr().out.lower()
    assert run(tmp_path, "status") == 0
    assert "submit staged" in capsys.readouterr().out
    assert run(tmp_path, "sleep") == 0
    assert "submit" in capsys.readouterr().out
    req = read_request(tmp_path)
    assert req is not None and req.submit and req.launches == ()


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
    assert abi["type"] == "sleep"
    assert abi["launches"][0]["name"] == "solo"


# --- judge verbs: finding / conclude ---------------------------------------


def test_findings_then_conclude_round_trip_through_the_reader(tmp_path: Path, capsys) -> None:
    assert (
        run(
            tmp_path,
            "finding",
            "--file",
            "src/solver.py",
            "--line",
            "42",
            "--confidence",
            "high",
            "--summary",
            "off-by-one",
            "--detail",
            "the loop skips the last index",
            "--blocking",
            "--kind",
            "change",
        )
        == 0
    )
    assert "BLOCKING" in capsys.readouterr().out
    # a second, non-local, non-blocking finding
    run(
        tmp_path,
        "finding",
        "--file",
        "README.md",
        "--confidence",
        "low",
        "--summary",
        "typo",
        "--detail",
        "spelling",
        "--kind",
        "note",
    )
    assert run(tmp_path, "conclude", "--notes", "one real defect") == 0
    assert "final answer" in capsys.readouterr().out

    verdict = read_verdict(tmp_path)  # the KERNEL's authoritative parse
    assert verdict is not None
    assert verdict["notes"] == "one real defect"
    assert len(verdict["findings"]) == 2
    assert verdict["findings"][0] == {
        "file": "src/solver.py",
        "line": 42,
        "confidence": "high",
        "summary": "off-by-one",
        "detail": "the loop skips the last index",
        "blocking": True,
        "kind": "change",
    }
    assert verdict["findings"][1]["line"] is None  # --line omitted -> null
    assert not (tmp_path / ".autoresearch" / "request.json").exists()  # staging cleared


def test_conclude_with_no_findings_is_a_clean_verdict(tmp_path: Path) -> None:
    run(tmp_path, "conclude", "--notes", "materially sound")
    assert read_verdict(tmp_path) == {"findings": [], "notes": "materially sound"}


def test_verifier_category_is_carried(tmp_path: Path) -> None:
    run(
        tmp_path,
        "finding",
        "--file",
        "x.py",
        "--confidence",
        "high",
        "--summary",
        "s",
        "--detail",
        "d",
        "--blocking",
        "--category",
        "ruler-fishing",
    )
    run(tmp_path, "conclude")
    verdict = read_verdict(tmp_path)
    assert verdict is not None and verdict["findings"][0]["category"] == "ruler-fishing"


def test_finding_validation_fails_fast(tmp_path: Path, capsys) -> None:
    cases = [
        (
            ["finding", "--file", "", "--confidence", "high", "--summary", "s", "--detail", "d"],
            "file",
        ),
        (
            ["finding", "--file", "x", "--confidence", "wat", "--summary", "s", "--detail", "d"],
            "confidence",
        ),
        (
            ["finding", "--file", "x", "--confidence", "high", "--summary", "", "--detail", "d"],
            "summary",
        ),
        (
            [
                "finding",
                "--file",
                "x",
                "--line",
                "0",
                "--confidence",
                "high",
                "--summary",
                "s",
                "--detail",
                "d",
            ],
            "1-indexed",
        ),
        (["conclude", "--notes", "x" * 6001], "exceeds"),
    ]
    for argv, needle in cases:
        assert run(tmp_path, *argv) == 2
        assert needle in capsys.readouterr().err


def test_status_shows_staged_findings(tmp_path: Path, capsys) -> None:
    run(
        tmp_path,
        "finding",
        "--file",
        "a.py",
        "--line",
        "3",
        "--confidence",
        "high",
        "--summary",
        "leak",
        "--detail",
        "d",
        "--blocking",
    )
    capsys.readouterr()
    run(tmp_path, "status")
    out = capsys.readouterr().out
    assert "1 finding(s) staged" in out and "BLOCKING" in out and "a.py:3" in out


def test_installed_tool_roots_at_its_install_location_not_cwd(tmp_path: Path) -> None:
    """An agent may invoke the tool from a subdirectory or another working dir
    entirely (hermes starts in its per-run home): the syscall must land in the
    tool's OWN workspace channel, never a cwd-relative one the kernel never
    reads (adversarial review of the interchangeable-backend refactor)."""
    from autoresearch.syscall import install_tool, read_verdict

    ws = tmp_path / "ws"
    ws.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    install_tool(ws)
    tool = ws / ".autoresearch" / "syscall"
    r = subprocess.run(
        [sys.executable, "-I", str(tool), "conclude", "--notes", "clean"],
        cwd=elsewhere,  # NOT the workspace
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert not (elsewhere / ".autoresearch").exists()  # nothing lands at cwd
    assert read_verdict(ws) == {"findings": [], "notes": "clean"}  # kernel finds it


def test_tool_command_is_absolute(tmp_path: Path) -> None:
    # a judge whose cwd is not the workspace (hermes) must still find the tool:
    # the brief command is absolute (adversarial/terra review of #140).
    from autoresearch.syscall import tool_command

    cmd = tool_command(tmp_path / "ws")
    assert cmd.startswith("python /")  # absolute, resolves from any cwd
    assert cmd.endswith("/ws/.autoresearch/syscall")
