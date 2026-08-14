"""Tests for the contract-validation CLI (recovered from the sunset test_clis)."""

from __future__ import annotations

from pathlib import Path

from autoresearch.contract_cli import main as contract_main

GOOD = """
benchmarks:
  - name: demo
    command: run me
    metric: success_rate
    direction: max
budgets: {gpu_hours_per_run: 8, runs_per_week: 10}
scope: {allowed: [src/]}
roadmap: docs/roadmap.md
"""


def test_contract_cli_accepts_valid(tmp_path: Path, capsys) -> None:
    path = tmp_path / ".autoresearch.yaml"
    path.write_text(GOOD)
    assert contract_main([str(path), "org/repo"]) == 0
    out = capsys.readouterr().out
    assert "is valid" in out
    assert "src/" in out
    assert ".github" in out  # shows the always-forbidden set


def test_contract_cli_reports_the_error(tmp_path: Path, capsys) -> None:
    path = tmp_path / ".autoresearch.yaml"
    path.write_text(GOOD.replace("allowed: [src/]", "allowed: ['.github/workflows']"))
    assert contract_main([str(path), "org/repo"]) == 1
    assert "overlaps forbidden" in capsys.readouterr().out


def test_contract_cli_missing_file(tmp_path: Path, capsys) -> None:
    assert contract_main([str(tmp_path / "nope.yaml")]) == 2
    assert "no contract" in capsys.readouterr().out
