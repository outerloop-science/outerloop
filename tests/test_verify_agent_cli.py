"""verify_agent_cli.main: env reading and fail-closed skips."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import autoresearch.verify_agent_cli as cli


def _base_env() -> dict[str, str]:
    return {
        "PR_REPO": "org/repo",
        "PR_NUMBER": "9",
        "REVIEW_BOT_LOGIN": "agentic-learning-bot",
        "ANTHROPIC_VERIFIER_KEY": "sk-test",
        "VERIFY_CHECKOUT": "/tmp/two-trees",
    }


def _patch(monkeypatch: Any, env: dict[str, str]) -> dict[str, Any]:
    monkeypatch.setattr(cli.os, "environ", env)
    monkeypatch.setattr(cli, "GitHubClient", lambda auth: "client")
    calls: dict[str, Any] = {}
    monkeypatch.setattr(
        cli,
        "build_reviewer_harness",
        lambda api_key, spec, **k: calls.update(key=api_key, spec=spec) or "harness",
    )
    monkeypatch.setattr(
        cli,
        "run_agent_verify",
        lambda *a, **k: calls.update(args=a, kwargs=k) or "Round 1",
    )
    return calls


def test_configured_run_calls_through(monkeypatch: Any) -> None:
    calls = _patch(monkeypatch, _base_env())
    assert cli.main() == 0
    assert calls["args"][1] == "org/repo" and calls["args"][2] == 9
    assert calls["args"][4] == Path("/tmp/two-trees").resolve()
    assert calls["key"] == "sk-test"
    assert calls["spec"].name == "verifier"  # the verifier RoleSpec, not the reviewer


def test_missing_checkout_fails_closed(monkeypatch: Any) -> None:
    env = _base_env()
    del env["VERIFY_CHECKOUT"]
    calls = _patch(monkeypatch, env)
    assert cli.main() == 0
    assert "args" not in calls


def test_missing_key_skips(monkeypatch: Any) -> None:
    env = _base_env()
    env["ANTHROPIC_VERIFIER_KEY"] = " "
    calls = _patch(monkeypatch, env)
    assert cli.main() == 0
    assert "args" not in calls


def test_bad_pr_number_skips(monkeypatch: Any) -> None:
    env = _base_env()
    env["PR_NUMBER"] = "not-a-number"
    calls = _patch(monkeypatch, env)
    assert cli.main() == 0
    assert "args" not in calls
