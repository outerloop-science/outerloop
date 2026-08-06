"""Tests for the two CLIs: contract validation and the review entry point."""

from __future__ import annotations

from pathlib import Path

from autoresearch.contract_cli import main as contract_main
from autoresearch.review_cli import main as review_main

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


def test_review_cli_fails_closed_without_bot_login(monkeypatch, caplog) -> None:
    """Without a bot login the reviewer cannot honor 'never review bot PRs'."""
    monkeypatch.setenv("PR_REPO", "org/repo")
    monkeypatch.setenv("PR_NUMBER", "1")
    monkeypatch.delenv("REVIEW_BOT_LOGIN", raising=False)
    assert review_main() == 0
    assert "REVIEW_BOT_LOGIN is unset" in caplog.text


def test_review_cli_never_fails_the_build_on_api_errors(monkeypatch, caplog) -> None:
    monkeypatch.setenv("PR_REPO", "org/repo")
    monkeypatch.setenv("PR_NUMBER", "1")
    monkeypatch.setenv("REVIEW_BOT_LOGIN", "some-bot")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)  # -> ValueError from the provider
    assert review_main() == 0
    assert "did not complete" in caplog.text
