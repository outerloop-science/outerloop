"""RoleSpec: the manifest that turns the generic harness into one role.

Data, not code. Adding a role is a new RoleSpec plus its skills and a
result-policy — no kernel change (docs/design/consolidation.md). The kernel
reads a RoleSpec to decide what a session sees (skills, tools), how it is
constrained (key, scope, execution), and how its output is checked
(`output_schema`).

The one hard invariant here is the read-only judge boundary: a reviewer or
verifier investigates a PR but never executes untrusted code, so it may not
hold a mutating tool or a write scope (docs/design/reviewer-infra.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

RoleName = Literal["author", "reviewer", "verifier", "steward", "followup"]
KeyFamily = Literal["author", "reviewer", "verifier", "steward"]
Environment = Literal["apptainer", "gh-runner", "local"]

# Tools that change files or run code. A read-only judge is granted none of
# these: the security boundary is the tool set, not a prompt asking nicely.
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
    # Judges only: the final artifact must validate against this JSON schema.
    # None for editing roles, whose artifact is a workspace diff, not a verdict.
    output_schema: dict[str, Any] | None = None
    # Editing roles only: repo-relative write allowlist. None for read-only
    # judges — declared here for legibility, enforced by the tool set too.
    scope: tuple[str, ...] | None = None
    # Judges only: emit the verdict through the installed syscall tool (the
    # `finding` / `conclude` syscalls) instead of a final JSON message
    # (docs/design/role-cli.md). Requires `output_schema` (a verdict IS that
    # schema). The role runs the tool with a shell in the jail, same as an
    # author — the deployment builds the harness to match (`run_role` gates on
    # this flag alone). A spec without it uses the parse-and-repair path.
    verdict_tool: bool = False

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
        if self.verdict_tool and self.output_schema is None:
            raise RoleSpecError(f"role {self.name!r}: verdict_tool needs an output_schema")
