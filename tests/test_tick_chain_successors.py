"""The chain's successor submission: singleton-serialized, on the cadence grid,
and never with a Slurm --deadline (the scheduler cancels deadline jobs on its
own start estimate under congestion — it killed every successor on
2026-09-02, see the #235 revert)."""

from __future__ import annotations

import re
from pathlib import Path

CHAIN = (Path(__file__).resolve().parents[1] / "scripts" / "tick_chain.sbatch").read_text()


def _successor_sbatch() -> str:
    m = re.search(r"sbatch --dependency=singleton[^\n]*(?:\\\n[^\n]*)*", CHAIN)
    assert m, "the successor sbatch line must be present"
    return m.group(0)


def test_successors_are_singleton_on_the_grid_without_a_deadline() -> None:
    line = _successor_sbatch()
    assert "--dependency=singleton" in line
    assert '--begin="$begin"' in line
    assert "--deadline" not in line and "--deadline" not in CHAIN
    assert '--partition="${AUTORESEARCH_PARTITION:-}"' in line
