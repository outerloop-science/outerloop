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
    assert attempt.resume_author(SimpleNamespace(), "fleet-model")[1] == expected


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
