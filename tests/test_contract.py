import pytest
from pydantic import ValidationError

from autoresearch.contract import (
    Contract,
    ScopeError,
    SelfTargetError,
    forbidden_paths,
    load_contract,
)

# Mirror of autoresearch-pilot/.autoresearch.yaml (abridged) — the first real contract.
PILOT_CONTRACT = """
benchmarks:
  - name: tsp
    command: uv run python -m pilot.eval --env tsp --json
    metric: mean_tour_length
    direction: min
  - name: sokoban
    command: uv run python -m pilot.eval --env sokoban --json
    metric: solve_rate
    direction: max
budgets:
  gpu_hours_per_run: 0
  runs_per_week: 20
scope:
  allowed: [src/pilot/solvers/]
roadmap: README.md
"""

SUITE_CONTRACT = """
benchmarks:
  - name: pusht
    command: uv run python -m jepa_agent.eval --env pusht --json
    metric: success_rate
    direction: max
  - name: maze
    command: uv run python -m jepa_agent.eval --env maze --json
    metric: success_rate
    direction: max
suite:
  metric: mean_success_rate
  direction: max
budgets:
  gpu_hours_per_run: 8
  runs_per_week: 10
scope:
  allowed: [src/, tests/]
roadmap: docs/roadmap.md
"""


def test_pilot_contract_parses() -> None:
    contract = load_contract(PILOT_CONTRACT, "agentic-learning-ai-lab/autoresearch-pilot")
    assert [b.name for b in contract.benchmarks] == ["tsp", "sokoban"]
    assert contract.suite is None
    assert contract.budgets.gpu_hours_per_run == 0


def test_suite_contract_parses() -> None:
    contract = load_contract(SUITE_CONTRACT, "agentic-learning-ai-lab/jepa-agent")
    assert contract.suite is not None
    assert contract.suite.metric == "mean_success_rate"


def test_self_target_refused_case_insensitive() -> None:
    with pytest.raises(SelfTargetError):
        load_contract(PILOT_CONTRACT, "Agentic-Learning-AI-Lab/AutoResearch")


def test_forbidden_paths_include_roadmap() -> None:
    contract = load_contract(PILOT_CONTRACT, "x/y")
    assert set(forbidden_paths(contract)) == {".github/", ".autoresearch.yaml", "README.md"}


@pytest.mark.parametrize(
    "allowed",
    [".github/workflows/", ".autoresearch.yaml", "README.md", ".", "", "./"],
)
def test_scope_overlap_refused(allowed: str) -> None:
    text = PILOT_CONTRACT.replace("allowed: [src/pilot/solvers/]", f"allowed: ['{allowed}']")
    with pytest.raises(ScopeError):
        load_contract(text, "x/y")


def test_allowed_dir_containing_roadmap_refused() -> None:
    text = SUITE_CONTRACT.replace("allowed: [src/, tests/]", "allowed: [docs/]")
    with pytest.raises(ScopeError):
        load_contract(text, "x/y")


def test_unknown_keys_fail_loudly() -> None:
    with pytest.raises(ValidationError):
        load_contract(PILOT_CONTRACT + "\nextra_key: 1\n", "x/y")


def test_bad_direction_refused() -> None:
    with pytest.raises(ValidationError):
        load_contract(PILOT_CONTRACT.replace("direction: min", "direction: lower"), "x/y")


def test_negative_budget_refused() -> None:
    with pytest.raises(ValidationError):
        load_contract(
            PILOT_CONTRACT.replace("gpu_hours_per_run: 0", "gpu_hours_per_run: -1"), "x/y"
        )


def test_empty_benchmarks_refused() -> None:
    with pytest.raises(ValidationError):
        Contract.model_validate(
            {
                "benchmarks": [],
                "budgets": {"gpu_hours_per_run": 0, "runs_per_week": 1},
                "scope": {"allowed": ["src/"]},
                "roadmap": "README.md",
            }
        )
