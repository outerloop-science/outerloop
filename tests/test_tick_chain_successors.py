"""The chain's successor submission: singleton-serialized, on the cadence grid,
and never with a Slurm --deadline (the scheduler cancels deadline jobs on its
own start estimate under congestion — it killed every successor on
2026-09-02, see the #235 revert)."""

from __future__ import annotations

import re
from pathlib import Path

CHAIN = (Path(__file__).resolve().parents[1] / "scripts" / "tick_chain.sbatch").read_text()


def _successor_sbatch() -> str:
    # the whole continued command, from sbatch to the script path
    m = re.search(r'sbatch --dependency=singleton.*?tick_chain\.sbatch"', CHAIN, re.S)
    assert m, "the successor sbatch line must be present"
    return m.group(0)


def test_successors_are_singleton_on_the_grid_without_a_deadline() -> None:
    line = _successor_sbatch()
    assert "--dependency=singleton" in line
    assert '--begin="$begin"' in line
    assert "--deadline" not in line and "--deadline" not in CHAIN
    # partition is optional now: passed through only when set (part_arg is
    # derived from AUTORESEARCH_PARTITION near the top of the chain).
    assert '${part_arg:+"$part_arg"}' in line
    assert 'part_arg="--partition=${AUTORESEARCH_PARTITION}"' in CHAIN


def _grid(epoch_now: int, cadence_s: int, pending: int, i: int) -> int:
    """Run the chain's OWN slot arithmetic (the exact lines from the script) in
    bash with the given inputs and return begin_epoch."""
    import subprocess

    lines = [
        line.strip()
        for line in CHAIN.splitlines()
        if line.strip().startswith(("next_slot=", "begin_epoch="))
    ]
    assert len(lines) == 2, lines
    script = (
        f"epoch_now={epoch_now}; cadence_s={cadence_s}; pending={pending}; i={i}\n"
        + "\n".join(lines)
        + "\necho $begin_epoch\n"
    )
    return int(
        subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True).stdout
    )


def test_successors_land_on_the_cadence_grid_after_the_queued_ones() -> None:
    cadence = 30 * 60
    now = 1_900_000_000 + 7 * 60 + 13  # 7m13s past a slot
    first = _grid(now, cadence, pending=0, i=1)
    assert first % cadence == 0 and first > now  # the NEXT slot, on the grid
    assert first - now < cadence
    # with one successor already queued, the new one takes the slot after it
    assert _grid(now, cadence, pending=1, i=1) == first + cadence
    # two top-ups in one tick occupy consecutive slots
    assert _grid(now, cadence, pending=0, i=2) == first + cadence
    # exactly on a slot boundary, the next slot is still strictly in the future
    on_slot = 1_900_000_000 - (1_900_000_000 % cadence)
    assert _grid(on_slot, cadence, pending=0, i=1) == on_slot + cadence
    # short cadences keep the same grid rule
    assert _grid(now, 6 * 60, pending=0, i=1) % (6 * 60) == 0
