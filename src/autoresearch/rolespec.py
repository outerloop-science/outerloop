"""RoleSpec: the manifest that turns the generic harness into one role.

Data, not code. Adding a role is a new RoleSpec plus its skills and a
result-policy — no kernel change (docs/design/consolidation.md). The kernel
reads a RoleSpec to decide what a session sees (skills, tools), how it is
constrained (key, scope, execution), and how its output is checked
(`output_schema`).

Every role runs the same way — a session inside the deployment's boundary (a
container where one exists, the ephemeral runner where one doesn't, plus the
tokenless split) — and roles differ by prompt, verbs, and output handling,
never by a bespoke containment posture. The invariant kept here is
consistency: a spec that declares itself non-executing may not hold a
mutating tool or a write scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

RoleName = Literal["author", "reviewer", "verifier", "steward", "followup"]
KeyFamily = Literal["author", "reviewer", "verifier", "steward"]
Environment = Literal["apptainer", "gh-runner", "local"]

# Tools that change files or run code. A spec that declares can_execute=False
# must not hold any of these — the declaration and the tool set must agree.
MUTATING_TOOLS: frozenset[str] = frozenset({"Write", "Edit", "Bash"})


class RoleSpecError(ValueError):
    """A RoleSpec violates a hard invariant."""


@dataclass(frozen=True)
class Execution:
    # `environment` is declarative deployment metadata: it names where the
    # role is meant to run, but binding it (the apptainer image, the runner)
    # is the harness builder's job at the deployment site. `can_execute` is
    # the enforced half — the RoleSpec invariant and the builders check it.
    environment: Environment
    can_execute: bool  # may the session run code (Bash)?


@dataclass(frozen=True)
class SessionBudget:
    max_turns: int
    walltime_s: int
    cost_cap_usd: float | None = None


@dataclass(frozen=True)
class RoleSpec:
    name: RoleName
    instructions: str  # standing role text; skills add know-how on top
    key: KeyFamily  # credential family (isolation)
    tools: tuple[str, ...]  # allowed tool ids (native + harness-provided)
    execution: Execution
    budget: SessionBudget
    skills: tuple[str, ...] = ()
    # A role WITH a schema is a judge: it records findings through the
    # installed syscall tool (`finding` / `conclude`, docs/design/role-cli.md)
    # and the kernel reads the committed verdict back authoritatively
    # (`syscall.read_verdict`, which owns the one canonical verdict shape; the
    # schema here marks the role and documents the downstream shape). None for
    # editing roles, whose artifact is a workspace diff, not a verdict.
    output_schema: dict[str, Any] | None = None
    # Editing roles only: repo-relative write allowlist. None for judges —
    # they investigate and record a verdict; they do not edit the tree.
    scope: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not self.tools:
            raise RoleSpecError(f"role {self.name!r} has no tools")
        if not self.execution.can_execute:
            mutating = MUTATING_TOOLS.intersection(self.tools)
            if mutating:
                raise RoleSpecError(
                    f"read-only role {self.name!r} may not hold mutating tools {sorted(mutating)}"
                )
            if self.scope is not None:
                raise RoleSpecError(
                    f"read-only role {self.name!r} edits nothing; scope must be None"
                )
        if self.output_schema is not None and self.scope is not None:
            raise RoleSpecError(
                f"judge {self.name!r} records a verdict, never edits; scope must be None"
            )
