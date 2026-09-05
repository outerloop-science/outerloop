"""Validate a contract file (`.outerloop.yaml`) before you push it.

    uv run python -m outerloop.contract_cli .outerloop.yaml

Prints what the agent would be allowed to do, or exactly what is wrong.
"""

from __future__ import annotations

import sys
from pathlib import Path

from outerloop.contract import (
    CONTRACT_NAME,
    CONTRACT_NAMES,
    ContractError,
    forbidden_paths,
    load_contract,
)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    # no arg: whichever contract name the cwd has, new name first
    path = (
        Path(args[0])
        if args
        else next((Path(n) for n in CONTRACT_NAMES if Path(n).is_file()), Path(CONTRACT_NAME))
    )
    repo = args[1] if len(args) > 1 else "your-org/your-repo"

    if not path.is_file():
        print(f"✗ no contract at {path}")
        return 2
    try:
        contract = load_contract(path.read_text(), repo)
    except ContractError as exc:
        print(f"✗ {path}: {exc}")
        return 1

    print(f"✓ {path} is valid\n")
    print("Benchmarks the agent will try to improve:")
    for benchmark in contract.benchmarks:
        arrow = "↓ lower is better" if benchmark.direction == "min" else "↑ higher is better"
        print(f"  • {benchmark.name}: {benchmark.metric} ({arrow})")
        print(f"    $ {benchmark.command}")
    if contract.suite is not None:
        print(f"\nSuite aggregate: {contract.suite.metric} ({contract.suite.direction})")
    print("\nThe agent may write only to:")
    for allowed in contract.scope.allowed:
        print(f"  • {allowed}")
    print("\nAlways forbidden, whatever the contract says:")
    for forbidden in forbidden_paths(contract):
        print(f"  • {forbidden}")
    print(
        f"\nBudget: {contract.budgets.gpu_hours_per_run} GPU-hours per run, "
        f"{contract.budgets.runs_per_week} runs per week"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
