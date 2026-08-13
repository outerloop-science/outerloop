"""review_agent_cli.main: env reading, fail-closed skips, and the wired call."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import autoresearch.review_agent_cli as cli


def _base_env() -> dict[str, str]:
    return {
        "PR_REPO": "org/repo",
        "PR_NUMBER": "7",
        "REVIEW_BOT_LOGIN": "autoresearch-bot",
        "ANTHROPIC_REVIEWER_KEY": "sk-test",
        "REVIEW_CHECKOUT": "/tmp/pr-head",
    }


def _patch(monkeypatch: Any, env: dict[str, str]) -> dict[str, Any]:
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "GitHubClient", lambda auth: "client")
    monkeypatch.setattr(cli, "build_reviewer_harness", lambda *a, **k: "harness")
    calls: dict[str, Any] = {}
    monkeypatch.setattr(
        cli,
        "run_agent_review",
        lambda *a, **k: calls.update(args=a, kwargs=k) or "Round 1",
    )
    return calls


def test_configured_run_calls_through(monkeypatch: Any) -> None:
    calls = _patch(monkeypatch, _base_env())
    assert cli.main() == 0
    # (client, repo, number, harness, workspace)
    assert calls["args"][1] == "org/repo"
    assert calls["args"][2] == 7
    assert calls["args"][4] == Path("/tmp/pr-head").resolve()
    assert calls["kwargs"]["bot_login"] == "autoresearch-bot"


def test_missing_bot_login_skips(monkeypatch: Any) -> None:
    env = _base_env()
    del env["REVIEW_BOT_LOGIN"]
    calls = _patch(monkeypatch, env)
    assert cli.main() == 0
    assert calls == {}  # never reached run_agent_review


def test_missing_key_skips(monkeypatch: Any) -> None:
    env = _base_env()
    env["ANTHROPIC_REVIEWER_KEY"] = "  "
    calls = _patch(monkeypatch, env)
    assert cli.main() == 0
    assert calls == {}


def test_codex_backend_reads_openai_key(monkeypatch: Any) -> None:
    env = _base_env()
    env["REVIEW_BACKEND"] = "codex"
    env["OPENAI_REVIEWER_KEY"] = "sk-openai"
    del env["ANTHROPIC_REVIEWER_KEY"]  # wrong key for this backend; must be ignored
    calls = _patch(monkeypatch, env)
    assert cli.main() == 0
    assert calls.get("args")  # reached run_agent_review with the openai key


def test_unknown_backend_skips(monkeypatch: Any) -> None:
    env = _base_env()
    env["REVIEW_BACKEND"] = "hermes"
    calls = _patch(monkeypatch, env)
    assert cli.main() == 0
    assert calls == {}


def test_missing_checkout_fails_closed(monkeypatch: Any) -> None:
    env = _base_env()
    del env["REVIEW_CHECKOUT"]  # would otherwise default to cwd (wrong tree)
    calls = _patch(monkeypatch, env)
    assert cli.main() == 0
    assert calls == {}
