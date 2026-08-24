"""The judge verdict tool + its kernel-side reader (docs/design/role-cli.md
Phase 2). The tool is the agent surface; verdict.json is the ABI; read_verdict
is the authoritative validator that replaces role_runner's parse-and-repair."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from autoresearch.verdict import VerdictError, install_tool, read_verdict
from autoresearch.verdict_cli import main


def run(tmp: Path, *argv: str) -> int:
    return main(list(argv), root=tmp)


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
    f0 = verdict["findings"][0]
    assert f0 == {
        "file": "src/solver.py",
        "line": 42,
        "confidence": "high",
        "summary": "off-by-one",
        "detail": "the loop skips the last index",
        "blocking": True,
        "kind": "change",
    }
    assert verdict["findings"][1]["line"] is None  # --line omitted -> null
    assert not (tmp_path / ".verdict" / "findings.json").exists()  # staging cleared


def test_no_verdict_file_is_none(tmp_path: Path) -> None:
    assert read_verdict(tmp_path) is None  # judge never concluded


def test_conclude_with_no_findings_is_a_clean_verdict(tmp_path: Path) -> None:
    run(tmp_path, "conclude", "--notes", "materially sound")
    verdict = read_verdict(tmp_path)
    assert verdict == {"findings": [], "notes": "materially sound"}


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
    assert verdict is not None
    assert verdict["findings"][0]["category"] == "ruler-fishing"  # a real taxonomy value


def test_tool_validation_fails_fast(tmp_path: Path, capsys) -> None:
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
        (["conclude", "--notes", "x" * 6001], "exceeds"),
    ]
    for argv, needle in cases:
        rc = run(tmp_path, *argv)
        assert rc == 2
        assert needle in (capsys.readouterr().err)


def test_line_must_be_positive_tool_and_reader(tmp_path: Path, capsys) -> None:
    # 1-indexed: 0/negative are meaningless -> rejected both in-session and by
    # the authoritative reader (terra #136 r1).
    assert (
        run(
            tmp_path,
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
        )
        == 2
    )
    assert "1-indexed" in capsys.readouterr().err
    d = tmp_path / ".verdict"
    d.mkdir(exist_ok=True)
    (d / "verdict.json").write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "file": "x",
                        "line": -3,
                        "confidence": "high",
                        "summary": "s",
                        "detail": "d",
                        "blocking": False,
                        "kind": "note",
                    }
                ],
                "notes": "",
            }
        )
    )
    with pytest.raises(VerdictError, match="positive"):
        read_verdict(tmp_path)


def test_reader_clamps_unknown_category_to_other(tmp_path: Path) -> None:
    # a taxonomy typo normalizes to "other" rather than nuking the verdict —
    # same stance as verifier.py's clamp (terra #136 r1).
    d = tmp_path / ".verdict"
    d.mkdir(exist_ok=True)
    (d / "verdict.json").write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "file": "x",
                        "line": 1,
                        "confidence": "high",
                        "summary": "s",
                        "detail": "d",
                        "blocking": True,
                        "kind": "note",
                        "category": "made-up-thing",
                    }
                ],
                "notes": "",
            }
        )
    )
    verdict = read_verdict(tmp_path)
    assert verdict is not None and verdict["findings"][0]["category"] == "other"


def test_reader_rejects_malformed_verdict_loudly(tmp_path: Path) -> None:
    d = tmp_path / ".verdict"
    d.mkdir()
    # a bad confidence the tool would have blocked, but a hand-written ABI must
    # still be caught by the kernel (the tool is not the trust boundary)
    (d / "verdict.json").write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "file": "x",
                        "line": 1,
                        "confidence": "SURE",
                        "summary": "s",
                        "detail": "d",
                        "blocking": True,
                        "kind": "note",
                    }
                ],
                "notes": "",
            }
        )
    )
    with pytest.raises(VerdictError, match="confidence"):
        read_verdict(tmp_path)


def test_reader_rejects_a_finding_missing_blocking(tmp_path: Path) -> None:
    # fail-open guard: a finding that omits blocking must be REJECTED, never
    # defaulted to non-gating ("silence is never endorsement" — terra #136 r3).
    d = tmp_path / ".verdict"
    d.mkdir(exist_ok=True)
    (d / "verdict.json").write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "file": "x",
                        "line": 1,
                        "confidence": "high",
                        "summary": "s",
                        "detail": "d",
                        "kind": "note",
                    }
                ],
                "notes": "",
            }
        )
    )
    with pytest.raises(VerdictError, match="missing required keys"):
        read_verdict(tmp_path)


def test_reader_handles_unhashable_enum_values(tmp_path: Path) -> None:
    # a hand-written finding with a list where a string enum belongs must raise
    # VerdictError, not crash the reader with TypeError (terra #136 r4).
    d = tmp_path / ".verdict"
    d.mkdir(exist_ok=True)
    for bad in ("confidence", "kind"):
        f = {
            "file": "x",
            "line": 1,
            "confidence": "high",
            "summary": "s",
            "detail": "d",
            "blocking": True,
            "kind": "note",
        }
        f[bad] = []  # unhashable
        (d / "verdict.json").write_text(json.dumps({"findings": [f], "notes": ""}))
        with pytest.raises(VerdictError, match=bad):
            read_verdict(tmp_path)


def test_reader_size_caps_a_giant_verdict(tmp_path: Path) -> None:
    from autoresearch.verdict import MAX_VERDICT_BYTES

    d = tmp_path / ".verdict"
    d.mkdir()
    (d / "verdict.json").write_text("x" * (MAX_VERDICT_BYTES + 1))
    with pytest.raises(VerdictError, match="exceeds"):
        read_verdict(tmp_path)


def test_install_tool_refuses_a_symlinked_channel(tmp_path: Path) -> None:
    # the judge's checkout is author-authored: a .verdict symlink to a host dir
    # must not let install write through it (terra #136 r2).
    escape = tmp_path / "ESCAPE"
    escape.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".verdict").symlink_to(escape, target_is_directory=True)
    install_tool(ws)
    # .verdict is now a real kernel-owned dir, not the symlink; nothing written
    # through to the escape target
    assert not (ws / ".verdict").is_symlink()
    assert (ws / ".verdict" / "verdict").exists()
    assert list(escape.iterdir()) == []


def test_installed_tool_is_standalone(tmp_path: Path) -> None:
    # the kernel installs the tool into a prepared checkout without autoresearch;
    # prove the copy runs under a bare interpreter (isolated mode)
    install_tool(tmp_path)
    tool = tmp_path / ".verdict" / "verdict"
    assert tool.exists()
    r = subprocess.run(
        [
            sys.executable,
            "-I",
            str(tool),
            "finding",
            "--file",
            "a.py",
            "--confidence",
            "low",
            "--summary",
            "s",
            "--detail",
            "d",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    r = subprocess.run(
        [sys.executable, "-I", str(tool), "conclude"], cwd=tmp_path, capture_output=True, text=True
    )
    assert r.returncode == 0
    verdict = read_verdict(tmp_path)
    assert verdict is not None and verdict["findings"][0]["file"] == "a.py"
