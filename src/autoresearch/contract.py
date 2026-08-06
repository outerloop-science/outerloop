"""Contract schema and loader for a target repo's `.autoresearch.yaml`.

The contract is the opt-in declaration: benchmarks, budgets, scope. The loader
enforces invariants no YAML can override (see the threat model in
docs/design/architecture.md): autoresearch is never a target of itself, and the
contract file, the target's roadmap, and `.github/` are always forbidden write
paths, regardless of what `scope.allowed` says.

Contracts live in target repos and are therefore untrusted input: parsing
rejects aliases, duplicate keys, and oversized documents.
"""

from __future__ import annotations

import posixpath
from pathlib import PurePosixPath
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

SELF_REPO = "agentic-learning-ai-lab/autoresearch"
ALWAYS_FORBIDDEN: tuple[str, ...] = (".github", ".autoresearch.yaml")
MAX_CONTRACT_BYTES = 64 * 1024
_GLOB_CHARS = set("*?[]!")


class ContractError(ValueError):
    """Base class for contract rejections."""


class SelfTargetError(ContractError):
    """Raised when a contract names autoresearch itself as the target."""


class ScopeError(ContractError):
    """Raised when an allowed path is unsafe or overlaps a forbidden path."""


class _SafeLoader(yaml.SafeLoader):
    """SafeLoader that refuses alias expansion and duplicate mapping keys."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.events.AliasEvent):
            raise ContractError("YAML aliases are not allowed in contracts")
        return super().compose_node(parent, index)

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise ContractError(f"duplicate key in contract: {key!r}")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


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


def normalize_repo(target_repo: str) -> str:
    """Reduce a repo reference to `owner/name`, casefolded.

    Accepts bare `owner/name`, trailing `/` or `.git`, and https/ssh URLs, so
    the self-target check can't be dodged by spelling.
    """
    ref = target_repo.strip().casefold()
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:", "ssh://"):
        if ref.startswith(prefix):
            ref = ref[len(prefix) :]
    ref = ref.rstrip("/")
    if ref.endswith(".git"):
        ref = ref[: -len(".git")]
    return ref


def normalize_path(entry: str) -> PurePosixPath:
    """Normalize a scope entry, rejecting anything that can escape the repo."""
    raw = entry.strip()
    if not raw or raw in {".", "./"}:
        raise ScopeError("allowing the repository root is never permitted")
    if raw.startswith("/") or ":" in raw or "\\" in raw:
        raise ScopeError(f"path must be repo-relative and POSIX: {entry!r}")
    if _GLOB_CHARS & set(raw):
        raise ScopeError(f"glob patterns are not allowed in scope: {entry!r}")
    normalized = posixpath.normpath(raw)
    path = PurePosixPath(normalized)
    if normalized in {".", ""} or any(part == ".." for part in path.parts):
        raise ScopeError(f"path escapes the repository: {entry!r}")
    return path


def forbidden_paths(contract: Contract) -> tuple[str, ...]:
    """Write paths forbidden for this contract: the hard-coded set plus the
    target's roadmap."""
    return (*ALWAYS_FORBIDDEN, str(normalize_path(contract.roadmap)))


def path_is_forbidden(candidate: str, contract: Contract) -> bool:
    """True if `candidate` (a repo-relative file path) may not be written.

    Component-wise, so `README.mdx` is not shadowed by roadmap `README.md`.
    Anything unnormalizable counts as forbidden.
    """
    try:
        path = normalize_path(candidate)
    except ScopeError:
        return True
    for forbidden in forbidden_paths(contract):
        f = PurePosixPath(forbidden)
        if path == f or f in path.parents:
            return True
    return False


def _overlaps(allowed: PurePosixPath, forbidden: PurePosixPath) -> bool:
    return allowed == forbidden or forbidden in allowed.parents or allowed in forbidden.parents


def load_contract(text: str, target_repo: str) -> Contract:
    """Parse and validate a contract for `target_repo`."""
    if normalize_repo(target_repo) == SELF_REPO:
        raise SelfTargetError("autoresearch is never a valid target of itself")
    if len(text.encode()) > MAX_CONTRACT_BYTES:
        raise ContractError(f"contract exceeds {MAX_CONTRACT_BYTES} bytes")
    data = yaml.load(text, Loader=_SafeLoader)
    if not isinstance(data, dict):
        raise ContractError("contract must be a YAML mapping")
    contract = Contract.model_validate(data)
    forbidden = [PurePosixPath(p) for p in forbidden_paths(contract)]
    for entry in contract.scope.allowed:
        allowed = normalize_path(entry)
        for path in forbidden:
            if _overlaps(allowed, path):
                raise ScopeError(f"allowed path {entry!r} overlaps forbidden {str(path)!r}")
    return contract
