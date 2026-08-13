"""Concrete RoleSpecs — the per-role manifests the role-runner consumes.

Each is data: instructions, skills, tools, key, scope, execution, budget, and
(for judges) an output_schema. Adding a role is a new factory here plus a
result-policy — no kernel change (docs/design/consolidation.md).
"""

from __future__ import annotations

from autoresearch.review import FINDINGS_SCHEMA
from autoresearch.rolespec import Environment, Execution, RoleSpec, SessionBudget

# Read-only investigation: repo-read plus the harness-provided pr-context and
# retriever. No Write/Edit/Bash — a judge never executes untrusted PR code.
_JUDGE_TOOLS = ("Read", "Grep", "Glob", "pr-context-read", "retriever")


def reviewer_spec(
    *, environment: Environment = "gh-runner", max_turns: int = 40, walltime_s: int = 1800
) -> RoleSpec:
    """The advisory reviewer as a read-only agent session.

    Investigates the PR head with the read tools and returns findings validated
    against `review.FINDINGS_SCHEMA`. Read-only by construction — the RoleSpec
    invariant rejects any mutating tool or write scope.
    """
    return RoleSpec(
        name="reviewer",
        instructions=(
            "Review the pull request for correctness and clarity. Investigate "
            "beyond the diff with the read tools; do not execute code. Return the "
            "findings as the required JSON object."
        ),
        key="reviewer",
        tools=_JUDGE_TOOLS,
        execution=Execution(environment=environment, can_execute=False),
        budget=SessionBudget(max_turns=max_turns, walltime_s=walltime_s),
        skills=("kernel-primer", "plain-style", "review-rubric", "read-only-investigation"),
        output_schema=FINDINGS_SCHEMA,
    )
