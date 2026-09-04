"""review_agent_cli.main: env reading, fail-closed skips, and the wired call."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import outerloop.review_agent_cli as cli


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
    calls: dict[str, Any] = {}
    # capture what the CLI passes to the harness builder (backend + key), so a
    # test can assert the backend is actually forwarded
    monkeypatch.setattr(
        cli,
        "build_harness",
        lambda api_key, spec, **k: calls.update(build_key=api_key, build_kwargs=k) or "harness",
    )
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
    # the codex backend AND its openai key are forwarded to the harness builder
    assert calls["build_kwargs"]["backend"] == "codex"
    assert calls["build_key"] == "sk-openai"


def test_hermes_backend_reads_openrouter_key(monkeypatch: Any, tmp_path: Path) -> None:
    env = _base_env()
    env["REVIEW_BACKEND"] = "hermes"
    env["OPENROUTER_API_KEY"] = "sk-or-test"
    env["REVIEW_HERMES_REPO"] = str(tmp_path)  # else hermes fail-closed skips
    del env["ANTHROPIC_REVIEWER_KEY"]  # wrong key for this backend; must be ignored
    calls = _patch(monkeypatch, env)
    assert cli.main() == 0
    assert calls["build_kwargs"]["backend"] == "hermes"
    assert calls["build_key"] == "sk-or-test"
    assert calls["build_kwargs"]["hermes_repo"] == tmp_path.resolve()
    assert calls["build_kwargs"]["hermes_provider"] == "openrouter"


def test_hermes_backend_without_repo_skips(monkeypatch: Any) -> None:
    env = _base_env()
    env["REVIEW_BACKEND"] = "hermes"
    env["OPENROUTER_API_KEY"] = "sk-or-test"  # key present, but no pinned clone
    calls = _patch(monkeypatch, env)
    assert cli.main() == 0
    assert calls == {}  # fail-closed: hermes needs REVIEW_HERMES_REPO


def test_unknown_backend_skips(monkeypatch: Any) -> None:
    env = _base_env()
    env["REVIEW_BACKEND"] = "gemini-cli"  # a genuinely unknown backend (hermes is now valid)
    calls = _patch(monkeypatch, env)
    assert cli.main() == 0
    assert calls == {}


def test_missing_checkout_fails_closed(monkeypatch: Any) -> None:
    env = _base_env()
    del env["REVIEW_CHECKOUT"]  # would otherwise default to cwd (wrong tree)
    calls = _patch(monkeypatch, env)
    assert cli.main() == 0
    assert calls == {}


def test_missing_key_in_emit_mode_writes_a_stub(monkeypatch: Any, tmp_path: Path) -> None:
    # a standing second opinion must not die silently when its key is
    # missing/expired: the emitted stub becomes the PR-visible warning
    import json

    env = _base_env()
    del env["ANTHROPIC_REVIEWER_KEY"]
    env["REVIEW_EMIT_FILE"] = str(tmp_path / "findings.json")
    calls = _patch(monkeypatch, env)
    assert cli.main() == 0
    assert "args" not in calls  # no session ran
    envelope = json.loads((tmp_path / "findings.json").read_text())
    assert envelope["kind"] == "skip-stub"
    assert "ANTHROPIC_REVIEWER_KEY" in envelope["detail"]
    assert envelope["reviewed_by"] == "claude"  # the stub names its backend


def test_unknown_backend_in_emit_mode_writes_a_stub(monkeypatch: Any, tmp_path: Path) -> None:
    import json

    env = _base_env()
    env["REVIEW_BACKEND"] = "gemeni"  # a typo'd caller input
    env["REVIEW_EMIT_FILE"] = str(tmp_path / "findings.json")
    calls = _patch(monkeypatch, env)
    assert cli.main() == 0
    assert "args" not in calls
    envelope = json.loads((tmp_path / "findings.json").read_text())
    assert envelope["kind"] == "skip-stub"
    assert "unknown REVIEW_BACKEND" in envelope["detail"]


def test_hermes_openai_provider_reads_the_openai_key(monkeypatch: Any, tmp_path: Path) -> None:
    env = _base_env()
    del env["ANTHROPIC_REVIEWER_KEY"]
    env.update(
        {
            "REVIEW_BACKEND": "hermes",
            "REVIEW_HERMES_PROVIDER": "openai",
            "REVIEW_HERMES_REPO": str(tmp_path),
            "OPENAI_REVIEWER_KEY": "sk-openai-test",
            "REVIEW_MODEL": "gpt-5.6-terra",  # openai-direct requires a native id
        }
    )
    calls = _patch(monkeypatch, env)
    assert cli.main() == 0
    assert calls["build_key"] == "sk-openai-test"
    assert calls["build_kwargs"]["hermes_provider"] == "openai"


def test_unknown_hermes_provider_in_emit_mode_writes_a_stub(
    monkeypatch: Any, tmp_path: Path
) -> None:
    # the provider input is free-form like the backend input: a typo must be
    # a PR-visible stub, never a traceback
    import json

    env = _base_env()
    env.update(
        {
            "REVIEW_BACKEND": "hermes",
            "REVIEW_HERMES_PROVIDER": "opnai",
            "REVIEW_EMIT_FILE": str(tmp_path / "findings.json"),
        }
    )
    calls = _patch(monkeypatch, env)
    assert cli.main() == 0
    assert "args" not in calls
    envelope = json.loads((tmp_path / "findings.json").read_text())
    assert envelope["kind"] == "skip-stub"
    assert "unknown REVIEW_HERMES_PROVIDER" in envelope["detail"]


def test_hermes_openai_without_model_writes_a_stub(monkeypatch: Any, tmp_path: Path) -> None:
    # hermes's default model id is OpenRouter-shaped; api.openai.com cannot
    # serve it, so an unset model must be a PR-visible stub, not a dead session
    import json

    env = _base_env()
    del env["ANTHROPIC_REVIEWER_KEY"]
    env.update(
        {
            "REVIEW_BACKEND": "hermes",
            "REVIEW_HERMES_PROVIDER": "openai",
            "REVIEW_HERMES_REPO": str(tmp_path),
            "OPENAI_REVIEWER_KEY": "sk-openai-test",
            "REVIEW_EMIT_FILE": str(tmp_path / "findings.json"),
        }
    )
    calls = _patch(monkeypatch, env)
    assert cli.main() == 0
    assert "args" not in calls
    envelope = json.loads((tmp_path / "findings.json").read_text())
    assert envelope["kind"] == "skip-stub"
    assert "provider-native REVIEW_MODEL" in envelope["detail"]
    # an OpenRouter-shaped id is just as unservable as an empty one
    env["REVIEW_MODEL"] = "openai/gpt-5.6-terra"
    calls = _patch(monkeypatch, env)
    assert cli.main() == 0
    assert "args" not in calls


def test_fail_closed_skips_emit_stubs(monkeypatch: Any, tmp_path: Path) -> None:
    # every fail-closed path leaves an envelope in emit mode: the post job
    # requires the artifact, so a silent return would fail it for the wrong
    # reason (and a misconfigured standing reviewer must be PR-visible)
    import json

    for missing, fragment in [
        ("REVIEW_BOT_LOGIN", "REVIEW_BOT_LOGIN is unset"),
        ("REVIEW_CHECKOUT", "REVIEW_CHECKOUT is unset"),
    ]:
        env = _base_env()
        del env[missing]
        env["REVIEW_EMIT_FILE"] = str(tmp_path / f"{missing}.json")
        calls = _patch(monkeypatch, env)
        assert cli.main() == 0
        assert "args" not in calls
        envelope = json.loads((tmp_path / f"{missing}.json").read_text())
        assert envelope["kind"] == "skip-stub"
        assert fragment in envelope["detail"]


def test_openrouter_native_shaped_model_writes_a_stub(monkeypatch: Any, tmp_path: Path) -> None:
    # the mirror of the openai-direct gate: OpenRouter cannot resolve a
    # provider-native id, so the session would burn its budget for nothing
    import json

    env = _base_env()
    del env["ANTHROPIC_REVIEWER_KEY"]
    env.update(
        {
            "REVIEW_BACKEND": "hermes",
            "REVIEW_HERMES_REPO": str(tmp_path),
            "OPENROUTER_API_KEY": "sk-or-test",
            "REVIEW_MODEL": "gpt-5.6-terra",  # missing the openai/ prefix
            "REVIEW_EMIT_FILE": str(tmp_path / "findings.json"),
        }
    )
    calls = _patch(monkeypatch, env)
    assert cli.main() == 0
    assert "args" not in calls
    envelope = json.loads((tmp_path / "findings.json").read_text())
    assert envelope["kind"] == "skip-stub"
    assert "OpenRouter-shaped" in envelope["detail"]


def test_padded_provider_matches_github_expression_semantics(
    monkeypatch: Any, tmp_path: Path
) -> None:
    # the workflow's key-injection expressions compare untrimmed input; the
    # CLI must apply the same rule so the two layers cannot disagree — a
    # padded value lands in the unknown-provider stub, never a wrong-key skip
    import json

    env = _base_env()
    env.update(
        {
            "REVIEW_BACKEND": "hermes",
            "REVIEW_HERMES_PROVIDER": " openai ",
            "REVIEW_EMIT_FILE": str(tmp_path / "findings.json"),
        }
    )
    calls = _patch(monkeypatch, env)
    assert cli.main() == 0
    assert "args" not in calls
    envelope = json.loads((tmp_path / "findings.json").read_text())
    assert envelope["kind"] == "skip-stub"
    assert "unknown REVIEW_HERMES_PROVIDER" in envelope["detail"]


def test_explicitly_empty_hermes_provider_means_the_default(
    monkeypatch: Any, tmp_path: Path
) -> None:
    # workflow env is always SET; empty must behave exactly like omitted
    env = _base_env()
    del env["ANTHROPIC_REVIEWER_KEY"]
    env.update(
        {
            "REVIEW_BACKEND": "hermes",
            "REVIEW_HERMES_PROVIDER": "",
            "REVIEW_HERMES_REPO": str(tmp_path),
            "OPENROUTER_API_KEY": "sk-or-test",
        }
    )
    calls = _patch(monkeypatch, env)
    assert cli.main() == 0
    assert calls["build_key"] == "sk-or-test"
    assert calls["build_kwargs"]["hermes_provider"] == "openrouter"
