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
    monkeypatch.setenv("ANTHROPIC_REVIEWER_KEY", "test-key")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)  # -> ValueError from the provider
    assert review_main() == 0
    assert "did not complete" in caplog.text


def test_review_cli_skips_without_api_key(monkeypatch, caplog) -> None:
    """An unset Actions secret arrives as an empty string; skip, don't crash."""
    monkeypatch.setenv("PR_REPO", "org/repo")
    monkeypatch.setenv("PR_NUMBER", "1")
    monkeypatch.setenv("REVIEW_BOT_LOGIN", "some-bot")
    monkeypatch.setenv("ANTHROPIC_REVIEWER_KEY", "")
    assert review_main() == 0
    assert "ANTHROPIC_REVIEWER_KEY is unset" in caplog.text


def test_completer_failures_are_expected_failures() -> None:
    """Operational LLM errors must never propagate out of the advisory CLI."""
    from autoresearch.llm import CompleterError, TruncatedError
    from autoresearch.review_cli import EXPECTED_FAILURES

    assert CompleterError in EXPECTED_FAILURES
    assert TruncatedError in EXPECTED_FAILURES


class FakeReviewClient:
    """Stands in for GitHubClient in review_cli tests; records content fetches."""

    def __init__(self, auth: object = None, author: str = "human-dev") -> None:
        self.author = author
        self.content_fetches: list[str] = []

    def get_pull_request(self, repo: str, number: int) -> dict:
        return {
            "title": "t",
            "body": "b",
            "user": {"login": self.author},
            "labels": [],
            "head": {"sha": "abc123"},
        }

    def get_pull_request_diff(self, repo: str, number: int) -> str:
        return "--- a/x.py\n+++ b/x.py\n"

    def get_pull_request_files(self, repo: str, number: int) -> list[dict]:
        return [{"filename": "x.py", "status": "modified"}]

    def get_file_content(self, repo: str, path: str, ref: str) -> str:
        self.content_fetches.append(path)
        return "def f(): pass"

    def upsert_comment(self, repo: str, number: int, marker: str, body: str) -> None:
        pass


def _cli_env(monkeypatch) -> None:
    monkeypatch.setenv("PR_REPO", "org/repo")
    monkeypatch.setenv("PR_NUMBER", "1")
    monkeypatch.setenv("REVIEW_BOT_LOGIN", "some-bot")
    monkeypatch.setenv("ANTHROPIC_REVIEWER_KEY", "k")
    monkeypatch.setenv("GITHUB_TOKEN", "t")


def test_review_cli_threads_date_and_context(monkeypatch) -> None:
    """main() must pass today= (the live false-positive fix) and the fetched
    file context through to review()."""
    import autoresearch.review_cli as cli

    captured: dict = {}
    fake_client = FakeReviewClient()

    def fake_review(pr, completer, bot_login, today=None):
        captured["today"] = today
        captured["context"] = tuple(pr.context_files)
        from autoresearch.review import ReviewResult

        return ReviewResult(findings=[], notes="")

    monkeypatch.setattr(cli, "GitHubClient", lambda auth: fake_client)
    monkeypatch.setattr(cli, "review", fake_review)
    monkeypatch.setattr(cli, "AnthropicCompleter", lambda **kw: object())
    _cli_env(monkeypatch)
    assert cli.main() == 0
    assert captured["today"] is not None and len(captured["today"]) == 10
    assert captured["context"] == (("x.py", "def f(): pass"),)


def test_review_cli_skips_context_fetch_for_bot_prs(monkeypatch) -> None:
    """Bot-authored PRs must not pay the contents-API fan-out."""
    import autoresearch.review_cli as cli

    fake_client = FakeReviewClient(author="Some-Bot")
    monkeypatch.setattr(cli, "GitHubClient", lambda auth: fake_client)
    monkeypatch.setattr(cli, "AnthropicCompleter", lambda **kw: object())
    _cli_env(monkeypatch)
    assert cli.main() == 0
    assert fake_client.content_fetches == []
