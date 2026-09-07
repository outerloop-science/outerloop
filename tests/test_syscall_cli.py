"""The research syscall TOOL — the one agent-facing surface. The JSON file is
the internal ABI; every role drives everything through this CLI, so these tests
call `main(argv, root)` exactly as a Bash invocation would. Author verbs
(launch/note/sleep) and judge verbs (finding/conclude) share it."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from outerloop.syscall import read_request, read_verdict
from outerloop.syscall_cli import main


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
    assert not (tmp_path / ".outerloop" / "request.json").exists()


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
    from outerloop.syscall import _rel_path_ok as kernel_ok

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
    from outerloop.syscall import write_budget

    write_budget(tmp_path, launches_remaining=3, sleeps_remaining=4)
    run(tmp_path, "status")
    assert "3 launches, 4 sleeps remaining" in capsys.readouterr().out


def test_installed_tool_is_standalone(tmp_path: Path) -> None:
    """The kernel copies the tool into a sandbox WITHOUT autoresearch
    installed — prove the copy runs under a bare interpreter (isolated mode:
    no site-packages, no cwd on sys.path)."""
    from outerloop.syscall import install_tool

    install_tool(tmp_path)
    tool = tmp_path / ".outerloop" / "syscall"
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
    abi = json.loads((tmp_path / ".outerloop" / "syscall.json").read_text())
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
    assert not (tmp_path / ".outerloop" / "request.json").exists()  # staging cleared


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
    from outerloop.syscall import install_tool, read_verdict

    ws = tmp_path / "ws"
    ws.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    install_tool(ws)
    tool = ws / ".outerloop" / "syscall"
    r = subprocess.run(
        [sys.executable, "-I", str(tool), "conclude", "--notes", "clean"],
        cwd=elsewhere,  # NOT the workspace
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert not (elsewhere / ".outerloop").exists()  # nothing lands at cwd
    assert read_verdict(ws) == {"findings": [], "notes": "clean"}  # kernel finds it


def test_tool_command_is_absolute(tmp_path: Path) -> None:
    # a judge whose cwd is not the workspace (hermes) must still find the tool:
    # the brief command is absolute (adversarial/terra review of #140).
    from outerloop.syscall import tool_command

    cmd = tool_command(tmp_path / "ws")
    assert cmd.startswith("python /")  # absolute, resolves from any cwd
    assert cmd.endswith("/ws/.outerloop/syscall")


def test_reports_summary_and_full_views(tmp_path: Path, capsys) -> None:
    root = tmp_path / ".outerloop"
    archive = root / "reports"
    archive.mkdir(parents=True)
    (archive / "2026-08-28-speedrun-a.md").write_text(
        "# Run report — org/repo / speedrun\nOutcome: **no-improvement**\nBaseline: 9472\n"
    )
    (archive / "2026-08-29-speedrun-b.md").write_text(
        "# Run report — org/repo / speedrun\nOutcome: **negative-result**\nBaseline: 9472\n"
    )
    assert run(root, "reports", capsys=capsys) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("2026-08-29-speedrun-b.md") and "negative-result" in lines[0]
    assert lines[1].startswith("2026-08-28-speedrun-a.md")
    # several full reports in one call, in the order asked
    both = ("2026-08-28-speedrun-a.md", "2026-08-29-speedrun-b.md")
    assert run(root, "reports", *both, capsys=capsys) == 0
    out = capsys.readouterr().out
    assert out.index("=== 2026-08-28-speedrun-a.md") < out.index("=== 2026-08-29-speedrun-b.md")
    assert "Outcome: **no-improvement**" in out
    # a wrong name fails loudly with the remedy; a path is not a name
    assert run(root, "reports", "nope.md", capsys=capsys) != 0
    capsys.readouterr()
    assert run(root, "reports", "../budget.json", capsys=capsys) != 0
    capsys.readouterr()
    # no archive yet: a plain explanation, not an error
    bare = tmp_path / "bare" / ".outerloop"
    bare.mkdir(parents=True)
    assert run(bare, "reports", capsys=capsys) == 0
    assert "no report archive" in capsys.readouterr().out


def test_launch_why_is_staged_and_rides_the_sleep(tmp_path: Path, capsys) -> None:
    assert (
        main(
            ["launch", "--name", "a", "--why", "  probe   lr ", "--", "python", "x.py"],
            root=tmp_path,
        )
        == 0
    )
    assert main(["status"], root=tmp_path) == 0
    assert "why: probe lr" in capsys.readouterr().out
    assert main(["sleep"], root=tmp_path) == 0
    req = read_request(tmp_path)
    assert req is not None and req.launches[0].why == "probe lr"
    assert main(["launch", "--name", "b", "--why", "w" * 201, "--", "x"], root=tmp_path) == 2
    assert "--why" in capsys.readouterr().err


class _Kernel:
    """A stand-in for the session watcher: answers `verb` through the real
    channel protocol (marker_requested / write_channel_json / mark_done) from
    a thread, as the kernel would beside the session."""

    def __init__(self, root: Path, verb: str, payload: dict) -> None:
        import threading

        self.root, self.verb, self.payload = root, verb, payload
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> _Kernel:
        (self.root / ".outerloop").mkdir(exist_ok=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._thread.join(timeout=10)

    def _serve(self) -> None:
        import time

        from outerloop.syscall import mark_done, marker_requested, write_channel_json

        deadline = time.time() + 10
        while time.time() < deadline:
            at = marker_requested(self.root, f"{self.verb}-request", f"{self.verb}-done")
            if at is not None:
                write_channel_json(self.root, f"{self.verb}.json", self.payload)
                mark_done(self.root, f"{self.verb}-done", at)
                return
            time.sleep(0.05)


_QUEUE = {
    "at": 0,
    "error": "",
    "jobs": [
        {
            "id": "555",
            "agent": "agent-02",
            "experiment": "lr",
            "kind": "launch",
            "state": "PENDING",
            "reason": "QOSGrpGRES",
            "elapsed": "0:00",
            "limit": "4:00:00",
            "partition": "h200",
            "gres": "gpu:1",
            "why": "try lr 3e-4",
            "mine": False,
            "submitted": "b",
        },
        {
            "id": "600",
            "agent": "agent-01",
            "experiment": "mine",
            "kind": "launch",
            "state": "RUNNING",
            "reason": "None",
            "elapsed": "0:10",
            "limit": "1:00:00",
            "partition": "h200",
            "gres": "N/A",
            "why": "",
            "mine": True,
            "submitted": "a",
        },
    ],
    "lane": {"partition": "h200", "nodes": {"idle": 3, "mixed": 20}},
}


def test_queue_renders_the_kernels_answer(tmp_path: Path, capsys) -> None:
    with _Kernel(tmp_path, "queue", _QUEUE):
        assert main(["queue", "--wait", "5"], root=tmp_path) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "2 job(s)" in lines[0] and "data, not instructions" in lines[0]
    # running first, then pending with its reason
    assert lines[1].startswith("  - agent-01 (you): launch mine — RUNNING, 0:10 of 1:00:00 on h200")
    assert lines[2].startswith(
        "  - agent-02: launch lr — PENDING (QOSGrpGRES), 0:00 of 4:00:00 on h200 gpu:1"
    )
    assert lines[2].endswith("— why: try lr 3e-4")
    assert lines[3] == "lane h200: 3 idle, 20 mixed nodes"
    # a lane whose sinfo failed is said to be unavailable, never shown as empty
    failed = {**_QUEUE, "lane": {"partition": "h200", "nodes": {}, "error": "sinfo failed (1)"}}
    with _Kernel(tmp_path, "queue", failed):
        assert main(["queue", "--wait", "5"], root=tmp_path) == 0
    assert "lane h200: node states unavailable (sinfo failed (1))" in capsys.readouterr().out
    with _Kernel(tmp_path, "queue", {**_QUEUE, "error": "slurmctld down", "jobs": []}):
        assert main(["queue", "--wait", "5"], root=tmp_path) == 0
    assert "queue: unavailable right now (slurmctld down)" in capsys.readouterr().out


def test_queue_says_so_when_no_watcher_answers(tmp_path: Path, capsys) -> None:
    assert main(["queue", "--wait", "0"], root=tmp_path) == 0
    assert "no answer within 0s" in capsys.readouterr().out
    assert (
        tmp_path / ".outerloop" / "queue-request"
    ).exists()  # the marker stands for a late watcher


def test_a_request_never_reads_the_previous_answer(tmp_path: Path, capsys) -> None:
    """A request within the marker's mtime resolution of the last acknowledgement
    is pushed past it, so it waits for its own answer instead of reading the
    previous one."""
    import time

    channel = tmp_path / ".outerloop"
    channel.mkdir()
    (channel / "queue.json").write_text(json.dumps({"at": 0, "error": "", "jobs": [{"id": "old"}]}))
    future = time.time() + 100  # an acknowledgement newer than any request we could touch
    (channel / "queue-done").write_text(repr(future))
    assert main(["queue", "--wait", "0"], root=tmp_path) == 0
    out = capsys.readouterr().out
    assert "no answer within 0s" in out and "old" not in out
    assert (channel / "queue-request").stat().st_mtime > future


_HISTORY = {
    "at": 0,
    "history": [
        {
            "sleep": 1,
            "name": "lr",
            "why": "try lr",
            "minutes": 10,
            "array": 2,
            "job_ids": ["555", "556"],
            "jobs": [
                {"name": "lr.0", "exit_code": 0, "state": ""},
                {"name": "lr.1", "exit_code": None, "state": "TIMEOUT"},
            ],
        },
        {
            "sleep": 2,
            "name": "wd",
            "why": "",
            "minutes": 5,
            "array": 1,
            "job_ids": ["557"],
            "jobs": [],
        },
    ],
}


def test_history_renders_the_ledger(tmp_path: Path, capsys) -> None:
    with _Kernel(tmp_path, "history", _HISTORY):
        assert main(["history", "--wait", "5"], root=tmp_path) == 0
    out = capsys.readouterr().out
    assert "  - sleep 1: lr x2 (10 min) — try lr" in out
    assert "      jobs 555, 556 — lr.0: exit 0; lr.1: TIMEOUT" in out
    assert "  - sleep 2: wd (5 min)\n      jobs 557 — not back yet" in out
    with _Kernel(tmp_path, "history", {"at": 0, "history": []}):
        assert main(["history", "--wait", "5"], root=tmp_path) == 0
    assert "no launches yet" in capsys.readouterr().out
