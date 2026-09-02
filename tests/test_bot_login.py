"""The kernel's own login follows one env knob across every role."""

from __future__ import annotations

from autoresearch.github import DEFAULT_BOT_LOGIN, bot_login_from_env


def test_bot_login_defaults_to_the_account_and_follows_the_knob(monkeypatch) -> None:
    monkeypatch.delenv("AUTORESEARCH_BOT_LOGIN", raising=False)
    assert bot_login_from_env() == DEFAULT_BOT_LOGIN
    monkeypatch.setenv("AUTORESEARCH_BOT_LOGIN", "   ")
    assert bot_login_from_env() == DEFAULT_BOT_LOGIN  # blank is unset
    monkeypatch.setenv("AUTORESEARCH_BOT_LOGIN", "outerloop-autoresearch[bot]")
    assert bot_login_from_env() == "outerloop-autoresearch[bot]"


def test_every_role_config_reads_the_login_at_construction(monkeypatch, tmp_path) -> None:
    """The defaults are call-time factories: a tick, climb, or steward built
    after the env is set sees the App's login; an explicit value still wins."""
    from autoresearch.orchestrator import RunConfig
    from autoresearch.steward import StewardConfig
    from autoresearch.tick import FollowupSpec

    monkeypatch.setenv("AUTORESEARCH_BOT_LOGIN", "outerloop-autoresearch[bot]")
    spec = FollowupSpec(account="a", partition="p", run_root=tmp_path, image="", home=tmp_path)
    assert spec.bot_login == "outerloop-autoresearch[bot]"
    assert RunConfig(target="o/r", benchmark="b").bot_login == "outerloop-autoresearch[bot]"
    assert StewardConfig(target="o/r", benchmark="b").bot_login == "outerloop-autoresearch[bot]"
    assert RunConfig(target="o/r", benchmark="b", bot_login="x").bot_login == "x"
    monkeypatch.delenv("AUTORESEARCH_BOT_LOGIN")
    assert RunConfig(target="o/r", benchmark="b").bot_login == DEFAULT_BOT_LOGIN
