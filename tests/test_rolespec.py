"""RoleSpec invariants — the read-only judge boundary especially."""

from __future__ import annotations

import pytest

from outerloop.rolespec import Execution, RoleSpec, RoleSpecError, SessionBudget


def _author() -> RoleSpec:
    return RoleSpec(
        name="author",
        instructions="Improve the method to raise the benchmark.",
        key="author",
        tools=("Read", "Grep", "Glob", "Edit", "Write", "Bash"),
        execution=Execution(environment="apptainer", can_execute=True),
        budget=SessionBudget(max_turns=120, walltime_s=5400),
        skills=("kernel-primer", "hypothesis-discipline"),
        scope=("models/",),
    )


def _reviewer() -> RoleSpec:
    return RoleSpec(
        name="reviewer",
        instructions="Review the PR and report findings.",
        key="reviewer",
        tools=("Read", "Grep", "Glob", "pr-context-read", "retriever"),
        execution=Execution(environment="gh-runner", can_execute=False),
        budget=SessionBudget(max_turns=40, walltime_s=1800),
        skills=("review-rubric", "read-only-investigation"),
        output_schema={"type": "object"},
    )


def test_author_and_reviewer_are_valid() -> None:
    assert _author().name == "author"
    reviewer = _reviewer()
    assert reviewer.scope is None
    assert reviewer.execution.can_execute is False


def test_read_only_role_rejects_mutating_tools() -> None:
    with pytest.raises(RoleSpecError, match="mutating tools"):
        RoleSpec(
            name="reviewer",
            instructions="x",
            key="reviewer",
            tools=("Read", "Bash"),
            execution=Execution(environment="gh-runner", can_execute=False),
            budget=SessionBudget(max_turns=40, walltime_s=1800),
        )


def test_read_only_role_rejects_scope() -> None:
    with pytest.raises(RoleSpecError, match="scope must be None"):
        RoleSpec(
            name="verifier",
            instructions="x",
            key="verifier",
            tools=("Read", "Grep"),
            execution=Execution(environment="gh-runner", can_execute=False),
            budget=SessionBudget(max_turns=40, walltime_s=1800),
            scope=("models/",),
        )


def test_empty_tools_rejected() -> None:
    with pytest.raises(RoleSpecError, match="no tools"):
        RoleSpec(
            name="author",
            instructions="x",
            key="author",
            tools=(),
            execution=Execution(environment="apptainer", can_execute=True),
            budget=SessionBudget(max_turns=10, walltime_s=60),
        )


def test_judge_may_not_hold_a_write_scope() -> None:
    import pytest

    from outerloop.rolespec import Execution, RoleSpec, RoleSpecError, SessionBudget

    with pytest.raises(RoleSpecError, match="never edits"):
        RoleSpec(
            name="reviewer",
            instructions="x",
            key="reviewer",
            tools=("Read", "Bash"),
            execution=Execution(environment="gh-runner", can_execute=True),
            budget=SessionBudget(max_turns=1, walltime_s=1),
            output_schema={"required": []},
            scope=("src/",),  # a judge records a verdict; it edits nothing
        )
