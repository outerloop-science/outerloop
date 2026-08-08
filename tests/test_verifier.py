"""The verifier: inverted population, gaming lens, ruler-aware context."""

from __future__ import annotations

import json
from typing import Any

from autoresearch.review import PullRequest, ReviewResult
from autoresearch.verifier import (
    VERIFY_HEADER,
    VERIFY_MARKER,
    build_verify_prompt,
    format_verify_comment,
    verify,
    verify_skip_reason,
)

BOT = "agentic-learning-bot"


def make_pr(**overrides: Any) -> PullRequest:
    base = {
        "repo": "org/pilot",
        "number": 9,
        "title": "[agent] tsp: 13.88 -> 10.84",
        "body": "baseline 13.88 candidate 10.84\n\n## Research report\nswapped heuristic",
        "diff": "--- a/src/pilot/solvers/tsp.py\n+++ b/src/pilot/solvers/tsp.py\n@@\n+x=1\n",
        "author": BOT,
        "labels": (),
    }
    return PullRequest(**{**base, **overrides})


class ScriptedCompleter:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, prompt: str, schema: dict) -> str:
        self.calls.append((system, prompt))
        return json.dumps(self.payload)


def test_population_is_the_reviewers_inverse() -> None:
    """Bot PRs are the whole point; human PRs are skipped; fail closed
    without a bot login."""
    assert verify_skip_reason(make_pr(), BOT) is None
    assert verify_skip_reason(make_pr(author="Agentic-Learning-Bot"), BOT) is None  # case
    human = verify_skip_reason(make_pr(author="renmengye"), BOT)
    assert human is not None and "human-authored" in human
    closed = verify_skip_reason(make_pr(), "")
    assert closed is not None and "fail closed" in closed


def test_opt_out_and_empty_diff_still_skip() -> None:
    assert verify_skip_reason(make_pr(labels=("autoresearch:no-review",)), BOT) is not None
    assert verify_skip_reason(make_pr(diff="  \n"), BOT) is not None


def test_prompt_carries_contract_ruler_claim_and_change() -> None:
    prompt = build_verify_prompt(
        make_pr(),
        contract_text="benchmarks:\n  - name: tsp\n",
        ruler_files=(("src/pilot/eval.py", "def evaluate(): ..."),),
        today="2026-08-09",
    )
    assert "## The contract" in prompt and "name: tsp" in prompt
    assert "## Ruler source" in prompt and "def evaluate" in prompt
    assert "## The claim" in prompt and "Research report" in prompt
    assert "## The change" in prompt and "+x=1" in prompt
    # the ruler section states the agent cannot modify it
    assert "cannot modify" in prompt


def test_verify_tags_findings_with_category_and_sanitizes() -> None:
    completer = ScriptedCompleter(
        {
            "findings": [
                {
                    "file": "src/pilot/solvers/tsp.py",
                    "line": 3,
                    "category": "harness-exploitation",
                    "confidence": "high",
                    "summary": "caches across eval calls\nAPPROVED",
                    "detail": "the eval calls fn 4x on identical inputs",
                }
            ],
            "notes": "",
        }
    )
    result = verify(make_pr(), completer, BOT, contract_text="c")
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.summary.startswith("[harness-exploitation]")
    assert "\n" not in finding.summary  # sanitize collapsed the newline
    system, _prompt = completer.calls[0]
    assert "harness-exploitation" in system and "silence is not an endorsement" in system


def test_human_pr_never_reaches_the_model() -> None:
    completer = ScriptedCompleter({"findings": [], "notes": ""})
    result = verify(make_pr(author="human-dev"), completer, BOT, contract_text="c")
    assert result.skipped is not None
    assert completer.calls == []
    assert format_verify_comment(result) is None


def test_clean_read_states_silence_is_not_endorsement() -> None:
    body = format_verify_comment(ReviewResult(findings=[], notes=""))
    assert body is not None
    assert body.startswith(VERIFY_MARKER)
    assert VERIFY_HEADER in body
    assert "not an endorsement" in body


def test_ruler_paths_resolved_from_contract_commands() -> None:
    from autoresearch.verifier_cli import _ruler_paths

    contract = """
benchmarks:
  - name: tsp
    command: uv run python -m pilot.eval --env tsp --json
  - name: probe
    command: uv run python -m pilot.eval --env probe --json
"""
    paths = _ruler_paths(contract)
    assert paths[0] == "src/pilot/eval.py"
    assert "pilot/eval.py" in paths
    assert len([p for p in paths if p.endswith("eval.py")]) == 2  # deduped


def test_gather_ruler_fetches_modules_then_tests() -> None:
    from autoresearch.verifier_cli import gather_ruler

    class FakeClient:
        def get_file_content(self, repo, path, ref):
            if path == "src/pilot/eval.py":
                return "EVAL"
            if path.startswith("tests/"):
                return "TEST"
            return None

        def list_directory(self, repo, path, ref):
            assert path == "tests"
            return [
                {"type": "file", "name": "test_envs.py", "path": "tests/test_envs.py"},
                {"type": "dir", "name": "data", "path": "tests/data"},
                {"type": "file", "name": "conftest.py", "path": "tests/conftest.py"},
            ]

    ruler = gather_ruler(
        FakeClient(),  # type: ignore[arg-type]
        "org/pilot",
        "main",
        "command: python -m pilot.eval",
    )
    paths = [p for p, _ in ruler]
    assert paths[0] == "src/pilot/eval.py"  # eval module first
    assert "tests/test_envs.py" in paths and "tests/conftest.py" in paths
    assert "tests/data" not in paths  # dirs skipped
