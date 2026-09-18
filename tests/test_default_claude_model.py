"""OUTERLOOP_CLAUDE_MODEL is the model for every Claude role: a required
deployment setting with no code default, resolved at use time so an explicit
model never needs it and a missing one is a named error, not a traceback."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

import outerloop
from outerloop import attempt, steward
from outerloop.harness import ClaudeCodeHarness, ClaudeModelUnset, default_claude_model

UNSET = "OUTERLOOP_CLAUDE_MODEL is not set"
# bound once: the helpers below patch parse_args per call and must not re-wrap a wrapper
_PARSE_ARGS = argparse.ArgumentParser.parse_args


@pytest.mark.parametrize("value", [None, "", " \t"])
def test_default_claude_model_requires_the_setting(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("OUTERLOOP_CLAUDE_MODEL", raising=False)
    else:
        monkeypatch.setenv("OUTERLOOP_CLAUDE_MODEL", value)
    with pytest.raises(ClaudeModelUnset, match=UNSET) as info:
        default_claude_model()
    assert "OUTERLOOP_CLAUDE_MODEL=<model>" in str(info.value)  # the line to add
    assert isinstance(info.value, RuntimeError)
    # the harness default and a legacy claude record under a codex fleet resolve
    # through the same function, at construction / wake time
    with pytest.raises(ClaudeModelUnset):
        ClaudeCodeHarness(api_key="test")
    with pytest.raises(ClaudeModelUnset):
        attempt.resume_author(SimpleNamespace(), "gpt-fleet", "codex")
    # a claude fleet's configured author model needs no OUTERLOOP_CLAUDE_MODEL
    assert attempt.resume_author(SimpleNamespace(), "claude-fleet", "claude")[1] == "claude-fleet"


def test_default_claude_model_reads_the_setting_stripped(monkeypatch):
    monkeypatch.setenv("OUTERLOOP_CLAUDE_MODEL", " claude-opus-4-8 ")
    assert default_claude_model() == "claude-opus-4-8"
    assert ClaudeCodeHarness(api_key="test").model == "claude-opus-4-8"
    assert attempt.resume_author(SimpleNamespace(), "gpt-fleet", "codex")[1] == "claude-opus-4-8"


@pytest.mark.parametrize(
    ("backend", "author_model", "expected"),
    [
        ("claude", "", "claude-test-model"),
        ("claude", "claude-author-override", "claude-author-override"),
        ("codex", "gpt-5.6-terra", "gpt-5.6-terra"),
        ("codex", "", ""),  # codex_author_config_error names the fix, not ClaudeModelUnset
    ],
)
def test_fleet_author_model(monkeypatch, backend, author_model, expected):
    if author_model:
        monkeypatch.setenv("OUTERLOOP_AUTHOR_MODEL", author_model)
    else:
        monkeypatch.delenv("OUTERLOOP_AUTHOR_MODEL", raising=False)
    assert attempt.fleet_author_model(backend) == expected
    monkeypatch.delenv("OUTERLOOP_CLAUDE_MODEL", raising=False)
    if backend == "claude" and not author_model:
        with pytest.raises(ClaudeModelUnset):
            attempt.fleet_author_model(backend)
    else:
        assert attempt.fleet_author_model(backend) == expected


class _Stop(Exception):
    pass


def _stop(*args, **kwargs):
    raise _Stop


def _run_author_cli(monkeypatch, argv: list[str]) -> argparse.Namespace:
    """Run the author CLI on argv up to its first side effect (bot auth) and
    return the namespace as main resolved it."""
    monkeypatch.setattr(attempt, "arm_sigterm_containment", lambda: None)
    monkeypatch.setattr(attempt, "resolve_bot_auth", _stop)
    captured: dict[str, argparse.Namespace] = {}

    def capture(parser, *args, **kwargs):
        captured["args"] = _PARSE_ARGS(parser, argv)
        return captured["args"]

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", capture)
    with pytest.raises(_Stop):
        attempt.main()
    return captured["args"]


AUTHOR_ARGV = [
    "--uncontained",
    "--run-root",
    "/tmp/test",
    "--target",
    "org/repo",
    "--benchmark",
    "b",
]


def test_author_cli_model_is_resolved_after_parsing(monkeypatch):
    """The parser carries no model default (nothing is evaluated at build time);
    main resolves OUTERLOOP_AUTHOR_MODEL, else the Claude model, else fails with
    the fix named; an explicit --model needs neither."""
    monkeypatch.delenv("OUTERLOOP_CLAUDE_MODEL", raising=False)
    monkeypatch.delenv("OUTERLOOP_AUTHOR_MODEL", raising=False)
    monkeypatch.delenv("OUTERLOOP_AUTHOR_BACKEND", raising=False)
    args = _run_author_cli(monkeypatch, [*AUTHOR_ARGV, "--model", "claude-explicit"])
    assert args.model == "claude-explicit"
    monkeypatch.setenv("OUTERLOOP_AUTHOR_MODEL", "claude-author")
    assert _run_author_cli(monkeypatch, AUTHOR_ARGV).model == "claude-author"
    monkeypatch.delenv("OUTERLOOP_AUTHOR_MODEL", raising=False)
    monkeypatch.setenv("OUTERLOOP_CLAUDE_MODEL", "claude-deployment")
    assert _run_author_cli(monkeypatch, AUTHOR_ARGV).model == "claude-deployment"
    # a codex fleet never asks for the Claude model
    monkeypatch.delenv("OUTERLOOP_CLAUDE_MODEL", raising=False)
    monkeypatch.setenv("OUTERLOOP_AUTHOR_BACKEND", "codex")
    monkeypatch.setenv("OUTERLOOP_AUTHOR_MODEL", "gpt-5.6-terra")
    assert _run_author_cli(monkeypatch, AUTHOR_ARGV).model == "gpt-5.6-terra"


def test_author_cli_refuses_without_the_setting(monkeypatch, capsys):
    monkeypatch.delenv("OUTERLOOP_CLAUDE_MODEL", raising=False)
    monkeypatch.delenv("OUTERLOOP_AUTHOR_MODEL", raising=False)
    monkeypatch.delenv("OUTERLOOP_AUTHOR_BACKEND", raising=False)
    with pytest.raises(SystemExit) as info:
        _run_author_cli(monkeypatch, AUTHOR_ARGV)
    assert info.value.code == 2
    assert UNSET in capsys.readouterr().err


def _run_steward_cli(monkeypatch, argv: list[str]) -> argparse.Namespace:
    monkeypatch.setattr(steward, "arm_sigterm_containment", lambda: None)
    monkeypatch.setattr(steward, "role_key", _stop)
    captured: dict[str, argparse.Namespace] = {}

    def capture(parser, *args, **kwargs):
        captured["args"] = _PARSE_ARGS(parser, argv)
        return captured["args"]

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", capture)
    with pytest.raises(_Stop):
        steward.main()
    return captured["args"]


STEWARD_ARGV = [
    "--target",
    "org/repo",
    "--benchmark",
    "test",
    "--run-root",
    "/tmp/test",
    "--uncontained",
]


def test_steward_cli_model_is_resolved_after_parsing(monkeypatch, capsys):
    monkeypatch.delenv("OUTERLOOP_CLAUDE_MODEL", raising=False)
    args = _run_steward_cli(monkeypatch, [*STEWARD_ARGV, "--model", "claude-explicit"])
    assert args.model == "claude-explicit"
    with pytest.raises(SystemExit) as info:
        _run_steward_cli(monkeypatch, STEWARD_ARGV)
    assert info.value.code == 2
    assert UNSET in capsys.readouterr().err
    monkeypatch.setenv("OUTERLOOP_CLAUDE_MODEL", "claude-deployment")
    assert _run_steward_cli(monkeypatch, STEWARD_ARGV).model == "claude-deployment"


@pytest.mark.parametrize("explicit", ["", "claude-explicit"])
def test_panel_lens_model(monkeypatch, tmp_path: Path, explicit):
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
    assert lenses[0].harness.model == (explicit or "claude-test-model")
    monkeypatch.delenv("OUTERLOOP_CLAUDE_MODEL", raising=False)
    if explicit:
        assert attempt._panel_lenses_from_args(args)[0][0].harness.model == explicit
    else:
        # the climb's panel error path (parser.error upstream), never a traceback
        with pytest.raises(ValueError, match=f"panel entry review:claude: {UNSET}"):
            attempt._panel_lenses_from_args(args)


def test_review_and_verify_agents_skip_with_the_reason(monkeypatch, caplog, tmp_path: Path):
    """The GitHub-Actions judges on the claude backend without a model input
    skip with the message (a PR-visible stub / a warning), never a traceback."""
    from outerloop import review_agent_cli, verify_agent_cli
    from outerloop.roles import reviewer_spec

    monkeypatch.delenv("OUTERLOOP_CLAUDE_MODEL", raising=False)
    for key in ("REVIEW_BACKEND", "REVIEW_MODEL", "VERIFY_MODEL", "REVIEW_BINARY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ANTHROPIC_REVIEWER_KEY", "k")
    harness, why, backend = review_agent_cli.resolve_reviewer_harness(reviewer_spec())
    assert (harness, backend) == (None, "claude") and UNSET in why
    monkeypatch.setenv("REVIEW_MODEL", "claude-explicit")
    harness, why, _ = review_agent_cli.resolve_reviewer_harness(reviewer_spec())
    assert why == "" and isinstance(harness, ClaudeCodeHarness)
    assert harness.model == "claude-explicit"

    monkeypatch.setenv("PR_REPO", "o/r")
    monkeypatch.setenv("PR_NUMBER", "1")
    monkeypatch.setenv("REVIEW_BOT_LOGIN", "bot")
    monkeypatch.setenv("ANTHROPIC_VERIFIER_KEY", "k")
    # every earlier guard passes only with a real two-tree checkout
    (tmp_path / "pr-head").mkdir()
    (tmp_path / "base").mkdir()
    monkeypatch.setenv("VERIFY_CHECKOUT", str(tmp_path))
    monkeypatch.setattr(verify_agent_cli, "run_agent_verify", _stop)
    with caplog.at_level("WARNING"):
        assert verify_agent_cli.main() == 0
    assert UNSET in caplog.text and "skipping verification" in caplog.text


def test_tick_preflights_name_the_missing_setting(monkeypatch, tmp_path: Path):
    """The tick host catches the missing setting before an issue is claimed:
    author (claude fleet), panel (a claude lens without its own model), steward."""
    from outerloop.tick import ServiceSpec, _author_config_error, _panel_preflight_error

    panel_key = tmp_path / "verifier_key"
    panel_key.write_text("k")
    panel_key.chmod(0o600)

    def make(**kw):
        return ServiceSpec(
            target="org/pilot",
            account="a",
            partition="p",
            run_root=tmp_path,
            image="img.sif",
            home=tmp_path,
            panel_key_file=str(panel_key),
            **kw,
        )

    monkeypatch.delenv("OUTERLOOP_AUTHOR_MODEL", raising=False)
    monkeypatch.delenv("OUTERLOOP_AUTHOR_BACKEND", raising=False)
    assert _author_config_error(make()) == ""
    assert _panel_preflight_error(make()) == ""
    monkeypatch.delenv("OUTERLOOP_CLAUDE_MODEL", raising=False)
    assert UNSET in _author_config_error(make())
    assert UNSET in _panel_preflight_error(make())
    # covered by the author's own model / explicit lens models: no complaint
    monkeypatch.setenv("OUTERLOOP_AUTHOR_MODEL", "claude-author")
    assert _author_config_error(make()) == ""
    assert _panel_preflight_error(make(panel="verify:claude:claude-x,review:claude:claude-y")) == ""
    # a codex fleet without its model gets the codex diagnosis, not this one
    monkeypatch.delenv("OUTERLOOP_AUTHOR_MODEL", raising=False)
    monkeypatch.setenv("OUTERLOOP_AUTHOR_BACKEND", "codex")
    problem = _author_config_error(make())
    assert "codex/openai model" in problem and UNSET not in problem


def test_steward_lane_skips_without_the_setting(monkeypatch, tmp_path: Path, caplog):
    from outerloop.compute import CommandResult, SlurmCompute
    from outerloop.contract import load_contract
    from outerloop.limits import effective_limits
    from outerloop.tick import ServiceSpec, service_steward

    contract = load_contract(
        """
benchmarks:
  - {name: tsp, command: c, metric: m, direction: min}
budgets: {gpu_hours_per_run: 0, runs_per_week: 20}
scope: {allowed: [src/pilot/solvers/]}
steward: {allowed: [src/pilot/instances.py]}
roadmap: docs/roadmap.md
""",
        "org/pilot",
    )

    class G:
        comments_posted: ClassVar[list] = []

        def list_open_issues(self, repo, max_pages: int = 3):
            return [
                {
                    "number": 21,
                    "title": "re-base the tsp pool",
                    "body": "",
                    "user": {"login": "renmengye"},
                    "author_association": "OWNER",
                    "labels": [{"name": "autoresearch:steward"}],
                }
            ]

        def list_comments(self, repo, number, max_pages: int = 20):
            return []

        def comment(self, repo, number, body):
            self.comments_posted.append((number, body))

    submitted: list[list[str]] = []

    def runner(argv, timeout_s):
        submitted.append(list(argv))
        return CommandResult(0, "321\n", "")

    spec = ServiceSpec(
        target="org/pilot",
        account="a",
        partition="p",
        run_root=tmp_path,
        image="img.sif",
        home=tmp_path,
        steward_key_file="/k",
    )
    monkeypatch.delenv("OUTERLOOP_CLAUDE_MODEL", raising=False)
    limits = effective_limits(contract.budgets)
    with caplog.at_level("ERROR"):
        out = service_steward(
            tmp_path, G(), SlurmCompute(runner=runner), spec, 1_000_000.0, contract, limits
        )
    assert out is None and submitted == [] and G.comments_posted == []  # nothing claimed
    assert UNSET in caplog.text


def test_no_literal_claude_model_in_the_package():
    """Deployments name the model; the kernel never does."""
    pkg = Path(outerloop.__file__).parent
    literal = re.compile(r"claude-(?:opus|sonnet|haiku|fable|\d)", re.IGNORECASE)
    hits = [
        f"{path.relative_to(pkg)}:{number}"
        for path in sorted(pkg.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
        for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1)
        if literal.search(line)
    ]
    assert hits == []
