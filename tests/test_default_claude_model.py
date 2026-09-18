"""Claude role defaults read deployment configuration at construction time."""

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

from outerloop import attempt, steward
from outerloop.harness import ClaudeCodeHarness, default_claude_model


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "claude-opus-5"),
        ("", "claude-opus-5"),
        (" \t", "claude-opus-5"),
        (" claude-opus-4-8 ", "claude-opus-4-8"),
    ],
)
def test_default_claude_model(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("OUTERLOOP_CLAUDE_MODEL", raising=False)
    else:
        monkeypatch.setenv("OUTERLOOP_CLAUDE_MODEL", value)
    assert default_claude_model() == expected
    assert ClaudeCodeHarness(api_key="test").model == expected
    # a legacy claude record under a codex fleet takes the claude default ...
    assert attempt.resume_author(SimpleNamespace(), "gpt-fleet", "codex")[1] == expected
    # ... and under a claude fleet it takes the fleet's configured author model
    assert attempt.resume_author(SimpleNamespace(), "claude-fleet", "claude")[1] == "claude-fleet"


@pytest.mark.parametrize("author_override", ["", "claude-author-override"])
def test_author_cli_default_claude_model(monkeypatch, author_override):
    """The fresh-author CLI's --model default follows OUTERLOOP_CLAUDE_MODEL,
    and OUTERLOOP_AUTHOR_MODEL still wins when set."""
    monkeypatch.setenv("OUTERLOOP_CLAUDE_MODEL", "claude-opus-4-8")
    if author_override:
        monkeypatch.setenv("OUTERLOOP_AUTHOR_MODEL", author_override)
    else:
        monkeypatch.delenv("OUTERLOOP_AUTHOR_MODEL", raising=False)
    monkeypatch.setattr(attempt, "arm_sigterm_containment", lambda: None)
    captured: dict[str, argparse.ArgumentParser] = {}

    class Parsed(Exception):
        pass

    def capture(parser, *args, **kwargs):
        captured["parser"] = parser
        raise Parsed

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", capture)
    with pytest.raises(Parsed):
        attempt.main()
    assert captured["parser"].get_default("model") == (author_override or "claude-opus-4-8")


@pytest.mark.parametrize("explicit", ["", "claude-explicit"])
def test_panel_default_claude_model(monkeypatch, tmp_path: Path, explicit):
    monkeypatch.setenv("OUTERLOOP_CLAUDE_MODEL", "claude-opus-4-8")
    monkeypatch.setattr(attempt, "role_key", lambda *args: "test-key")
    args = SimpleNamespace(
        panel=f"review:claude:{explicit}",
        panel_key_file=str(tmp_path / "key"),
        claude_bin="claude",
        codex_bin="codex",
        image="",
    )
    lenses, _ = attempt._panel_lenses_from_args(args)
    assert isinstance(lenses[0].harness, ClaudeCodeHarness)
    assert lenses[0].harness.model == (explicit or "claude-opus-4-8")


@pytest.mark.parametrize("explicit", ["", "claude-explicit"])
def test_steward_default_claude_model(monkeypatch, explicit):
    monkeypatch.setenv("OUTERLOOP_CLAUDE_MODEL", "claude-opus-4-8")
    monkeypatch.setattr(steward, "arm_sigterm_containment", lambda: None)
    parse_args = argparse.ArgumentParser.parse_args

    class Parsed(Exception):
        pass

    def capture_args(parser):
        argv = ["--target", "org/repo", "--benchmark", "test", "--run-root", "/tmp/test"]
        if explicit:
            argv += ["--model", explicit]
        args = parse_args(parser, argv)
        assert args.model == (explicit or "claude-opus-4-8")
        raise Parsed

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", capture_args)
    with pytest.raises(Parsed):
        steward.main()
