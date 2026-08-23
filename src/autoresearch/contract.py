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
import re
from pathlib import PurePosixPath
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    # Slug shape only: the name reaches branch names, ledger keys, and log
    # labels — contract text must not shape refs or paths beyond a slug.
    name: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    command: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    direction: Literal["min", "max"]
    # Significant digits for HUMAN surfaces (PR tables, BENCHMARKS.md,
    # replies) per the benchmark's community convention. Full precision
    # lives only in results/leader.json, the machine ledger (maintainer
    # decision 2026-08-09). Display-only: comparisons always use full
    # floats, so this can never hide or fake an improvement.
    display_digits: int | None = Field(default=None, ge=2, le=12)
    # Resampled-pool benchmarks: the env var the eval reads its run seed
    # from (e.g. PILOT_REACH_SEED). When set, the orchestrator draws ONE
    # fresh seed per measurement pass and pins BOTH sides of a comparison
    # to it (paired, common random numbers), then records it in the ledger
    # row — the number becomes re-derivable instead of pool luck. Strict
    # env-var shape: this string reaches a subprocess environment. RULER
    # INVARIANT the eval must uphold: emit the seed to stdout only, never
    # persist it into the tree — a seed artifact in the workspace would be
    # readable by the solver session that runs between the paired evals
    # (the verifier's ruler read covers this).
    seed_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    # Expected eval duration, minutes — a HINT that turns on dispatched
    # evals (docs/design/dispatcher.md): above the in-job threshold the
    # orchestrator runs this benchmark's evals as their own jobs. Clamped
    # by dispatch.EVAL_JOB_MINUTES_CEILING (our spend cap); it governs only
    # the first eval once measured durations exist. None = in-job (today's
    # behavior).
    eval_minutes: int | None = Field(default=None, ge=1)
    # Depth dial (docs/design/research-loop-buildout.md, Phase 2a): how many times
    # the author may iterate on its OWN measured result within one climb, keeping
    # the best gated candidate across iterations. 1 = today's single pass (no
    # depth). A per-benchmark dial so "does depth pay here?" is answerable per
    # benchmark; the fixed-k trigger (always run k) is what reads this, with
    # stall/agent-judged triggers as future configs. The depth LOOP that consumes
    # it lands in part 2; this is the declared knob (a contract can set it now).
    depth_k: int = Field(default=1, ge=1, le=8)

    @field_validator("seed_env")
    @classmethod
    def _seed_env_never_managed(cls, value: str | None) -> str | None:
        from autoresearch.orchestrator import managed_eval_env

        if value is not None and managed_eval_env(value):
            raise ValueError(
                f"seed_env must not name or prefix the evaluator's managed environment ({value!r})"
            )
        return value

    # Cross-seed noise floor. A comparison against the RECORDED best was
    # measured under a different seed, so a delta inside the floor is noise,
    # not progress; same-seed paired comparisons are exempt by construction.
    # Two forms. min_delta is absolute metric units, right for a bounded
    # metric (a success rate). min_delta_rel is a fraction of the recorded
    # level, right for an unbounded metric (wall-clock timing) whose level
    # drifts with hardware, so an absolute number goes stale. Set either or
    # both; both means the more conservative of the two applies.
    min_delta: float | None = Field(default=None, ge=0)
    # capped at 1.0: a noise floor is a small fraction (e.g. 0.13), so a
    # value above the level itself is a typo (13 meaning 13%), and a floor
    # of 13x would freeze the benchmark. A relative floor assumes a
    # non-zero level; pair it with a small absolute min_delta as a backstop
    # for a metric that can reach 0.
    min_delta_rel: float | None = Field(default=None, ge=0, le=1.0)


class SuiteAggregate(_StrictModel):
    """Unified-benchmark targets: one change is evaluated on every benchmark
    and reported per-env plus this aggregate (no cherry-picking)."""

    metric: str = Field(min_length=1)
    direction: Literal["min", "max"]


class Budgets(_StrictModel):
    gpu_hours_per_run: float = Field(ge=0)
    runs_per_week: int = Field(gt=0)
    # Optional per-repo shaping of the orchestrator's session/job limits.
    # These are WISHES, not grants: limits.effective_limits clamps every
    # value into orchestrator-side [floor, ceiling] bounds, so a target can
    # spend less of us, never more. Absent = orchestrator defaults.
    session_max_turns: int | None = Field(default=None, gt=0)
    session_minutes: int | None = Field(default=None, gt=0)
    climb_job_minutes: int | None = Field(default=None, gt=0)
    followup_job_minutes: int | None = Field(default=None, gt=0)


class Scope(_StrictModel):
    allowed: list[str] = Field(min_length=1)
    # Shared code paths (encoder / world model / training loop — code every
    # benchmark exercises, as opposed to env-specific solver code). A solver
    # diff touching any of these is suite-gated: the orchestrator re-measures
    # EVERY sibling benchmark on both sides and refuses credit if one
    # regresses beyond its own floor. Env-specific diffs stay cheap — only
    # their benchmark is measured.
    shared: list[str] = Field(default_factory=list)


# The orchestrator's ledger: no agent scope — solver OR steward — may
# contain these; their numbers carry orchestrator provenance only.
RECORD_PATHS = ("BENCHMARKS.md", "results/leader.json")


class StewardScope(_StrictModel):
    """Paths the BENCHMARK STEWARD may edit: env generators, the eval
    harness, tests, and reference data — NEVER the record ledger
    (BENCHMARKS.md, results/leader.json; the orchestrator writes those
    with its own measurements, and load_contract rejects a steward scope
    that includes them). The solver's `scope.allowed` is implicitly
    forbidden to the steward — the roles' territories must not overlap
    (collusion structure, design/meta.md) — and the always-forbidden set
    (this contract, `.github/`, the roadmap) binds the steward too."""

    allowed: list[str] = Field(min_length=1)


class Contract(_StrictModel):
    benchmarks: list[Benchmark] = Field(min_length=1)
    budgets: Budgets
    scope: Scope
    roadmap: str = Field(min_length=1)
    suite: SuiteAggregate | None = None
    steward: StewardScope | None = None

    @field_validator("benchmarks")
    @classmethod
    def _unique_names(cls, benchmarks: list[Benchmark]) -> list[Benchmark]:
        # Benchmark names are IDENTITIES: they key branch names, ledger rows,
        # and dispatched measure/job names. A duplicate silently collides all
        # three (two measures share an eval dir; one result is lost).
        names = [b.name for b in benchmarks]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate benchmark name(s): {', '.join(dupes)}")
        return benchmarks


_HOST_PREFIX = re.compile(r"^(?:[a-z+]+://)?(?:[^@/]*@)?(?:www\.)?github\.com[:/]+")


def normalize_repo(target_repo: str) -> str:
    """Reduce a repo reference to `owner/name`, casefolded.

    Accepts bare `owner/name`, trailing `/` or `.git`, and any scheme/userinfo
    URL spelling, so the self-target check can't be dodged by spelling.
    """
    ref = target_repo.strip().casefold()
    ref = _HOST_PREFIX.sub("", ref)
    ref = re.sub(r"/{2,}", "/", ref).strip("/")
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
    if any(part.casefold() == ".git" for part in path.parts):
        raise ScopeError(f"the git directory is never writable: {entry!r}")
    return path


def forbidden_paths(contract: Contract) -> tuple[str, ...]:
    """Write paths forbidden for this contract: the hard-coded set plus the
    target's roadmap."""
    return (*ALWAYS_FORBIDDEN, str(normalize_path(contract.roadmap)))


def _fold(path: PurePosixPath) -> PurePosixPath:
    """Casefold components: `.GITHUB/x` must be as forbidden as `.github/x`."""
    return PurePosixPath(*[part.casefold() for part in path.parts])


def path_is_forbidden(candidate: str, contract: Contract) -> bool:
    """True if `candidate` (a repo-relative file path) may not be written.

    Component-wise and case-insensitive, so `README.mdx` is not shadowed by
    roadmap `README.md` but `.GITHUB/` is still blocked. Anything
    unnormalizable counts as forbidden.
    """
    try:
        path = _fold(normalize_path(candidate))
    except ScopeError:
        return True
    if any(part == ".git" for part in path.parts):
        return True  # never write into the git directory itself
    for forbidden in forbidden_paths(contract):
        f = _fold(PurePosixPath(forbidden))
        if path == f or f in path.parents:
            return True
    return False


def _overlaps(allowed: PurePosixPath, forbidden: PurePosixPath) -> bool:
    a, f = _fold(allowed), _fold(forbidden)
    return a == f or f in a.parents or a in f.parents


def load_contract(text: str, target_repo: str) -> Contract:
    """Parse and validate a contract for `target_repo`."""
    if normalize_repo(target_repo) == SELF_REPO:
        raise SelfTargetError("autoresearch is never a valid target of itself")
    if len(text.encode()) > MAX_CONTRACT_BYTES:
        raise ContractError(f"contract exceeds {MAX_CONTRACT_BYTES} bytes")
    try:
        data = yaml.load(text, Loader=_SafeLoader)
    except ContractError:
        raise
    except (yaml.YAMLError, TypeError, ValueError) as exc:
        raise ContractError(f"unparseable contract: {type(exc).__name__}") from None
    if not isinstance(data, dict):
        raise ContractError("contract must be a YAML mapping")
    contract = Contract.model_validate(data)
    forbidden = [PurePosixPath(p) for p in forbidden_paths(contract)]
    for entry in contract.scope.allowed:
        allowed = normalize_path(entry)
        for path in forbidden:
            if _overlaps(allowed, path):
                raise ScopeError(f"allowed path {entry!r} overlaps forbidden {str(path)!r}")
    # Shared paths route the suite gate off changed paths, which are already
    # scope-checked — but a malformed entry would silently never match, so
    # the same load-time rigor applies. A shared path the agent can never
    # touch (no overlap with any allowed path) is dead config that reads as
    # protection: refused loudly, like any contract typo.
    solver_allowed = [normalize_path(entry) for entry in contract.scope.allowed]
    for entry in contract.scope.shared:
        shared = normalize_path(entry)
        for path in forbidden:
            if _overlaps(shared, path):
                raise ScopeError(f"shared path {entry!r} overlaps forbidden {str(path)!r}")
        if not any(_overlaps(shared, a) for a in solver_allowed):
            raise ScopeError(
                f"shared path {entry!r} overlaps no allowed path — the suite "
                f"gate could never trigger on it"
            )
    if contract.steward is not None:
        # same rigor as the solver scope, plus the role-separation invariant:
        # steward and solver territories must not overlap AT LOAD TIME — a
        # malformed or colliding entry is a contract error, never a
        # mid-run surprise
        solver = [normalize_path(entry) for entry in contract.scope.allowed]
        records = [PurePosixPath(p) for p in RECORD_PATHS]
        for entry in contract.steward.allowed:
            allowed = normalize_path(entry)
            for path in forbidden:
                if _overlaps(allowed, path):
                    raise ScopeError(f"steward path {entry!r} overlaps forbidden {str(path)!r}")
            for sp in solver:
                if _overlaps(allowed, sp):
                    raise ScopeError(f"steward path {entry!r} overlaps solver scope {str(sp)!r}")
            for rp in records:
                if _overlaps(allowed, rp):
                    raise ScopeError(
                        f"steward path {entry!r} overlaps the record ledger "
                        f"{str(rp)!r} (orchestrator-owned)"
                    )
    return contract
