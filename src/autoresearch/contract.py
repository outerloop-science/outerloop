"""Contract schema and loader for a target repo's `.autoresearch.yaml`.

The contract is the opt-in declaration: benchmarks, budgets, scope. The loader
enforces invariants no YAML can override (see the threat model in
docs/design/architecture.md): autoresearch is never a target of itself, and the
contract file, the target's roadmap, and `.github/` are always forbidden write
paths, regardless of what `scope.allowed` says.
"""

from __future__ import annotations

from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

SELF_REPO = "agentic-learning-ai-lab/autoresearch"
ALWAYS_FORBIDDEN: tuple[str, ...] = (".github/", ".autoresearch.yaml")


class SelfTargetError(ValueError):
    """Raised when a contract names autoresearch itself as the target."""


class ScopeError(ValueError):
    """Raised when an allowed path overlaps an always-forbidden path."""


class _StrictModel(BaseModel):
    # Typos in a contract must fail loudly, never be silently ignored.
    model_config = ConfigDict(extra="forbid")


class Benchmark(_StrictModel):
    name: str = Field(min_length=1)
    command: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    direction: Literal["min", "max"]


class SuiteAggregate(_StrictModel):
    """Unified-benchmark targets: one change is evaluated on every benchmark
    and reported per-env plus this aggregate (no cherry-picking)."""

    metric: str = Field(min_length=1)
    direction: Literal["min", "max"]


class Budgets(_StrictModel):
    gpu_hours_per_run: float = Field(ge=0)
    runs_per_week: int = Field(gt=0)


class Scope(_StrictModel):
    allowed: list[str] = Field(min_length=1)


class Contract(_StrictModel):
    benchmarks: list[Benchmark] = Field(min_length=1)
    budgets: Budgets
    scope: Scope
    roadmap: str = Field(min_length=1)
    suite: SuiteAggregate | None = None


def forbidden_paths(contract: Contract) -> tuple[str, ...]:
    """The write paths forbidden for this contract — the hard-coded set plus
    the target's roadmap."""
    return (*ALWAYS_FORBIDDEN, contract.roadmap)


def _overlaps(allowed: str, forbidden: str) -> bool:
    a = allowed.strip().lstrip("./")
    f = forbidden.strip().lstrip("./")
    if a == "":
        return True  # allowing the repo root allows everything
    return a == f or a.startswith(f) or f.startswith(a)


def load_contract(text: str, target_repo: str) -> Contract:
    """Parse and validate a contract for `target_repo` (owner/name)."""
    if target_repo.strip().lower() == SELF_REPO:
        raise SelfTargetError("autoresearch is never a valid target of itself")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("contract must be a YAML mapping")
    contract = Contract.model_validate(data)
    for allowed in contract.scope.allowed:
        for forbidden in forbidden_paths(contract):
            if _overlaps(allowed, forbidden):
                raise ScopeError(f"allowed path {allowed!r} overlaps forbidden {forbidden!r}")
    return contract
