"""A tick successor's scheduling window: start within one cadence of its slot,
deadline never inside the walltime (terra #235 r1)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "successor_window.sh"


def _window(slot: int, cadence_s: int, walltime_min: int) -> tuple[str, str, int]:
    out = subprocess.run(
        ["bash", str(SCRIPT), str(slot), str(cadence_s), str(walltime_min)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    return out[0], out[1], int(out[2])


@pytest.mark.parametrize("cadence_min", [6, 10, 30])
def test_deadline_is_slot_plus_cadence_plus_walltime(cadence_min: int) -> None:
    slot = 1_900_000_000
    begin, deadline, deadline_epoch = _window(slot, cadence_min * 60, 15)
    assert deadline_epoch - slot == cadence_min * 60 + 15 * 60
    # the window always leaves room for the walltime, whatever the cadence
    assert deadline_epoch - slot > 15 * 60
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", begin)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", deadline)


def test_the_chain_submits_successors_with_that_window() -> None:
    """The chain's sbatch line carries --begin, --deadline and --time from the
    one walltime constant; the helper is the deadline's only owner."""
    chain = (ROOT / "scripts" / "tick_chain.sbatch").read_text()
    assert "TICK_WALLTIME_MIN=15" in chain and "#SBATCH --time=15" in chain
    assert (
        'scripts/successor_window.sh" \\\n        "$begin_epoch" "$cadence_s" "$TICK_WALLTIME_MIN"'
        in chain
    )
    assert '--begin="$begin" --deadline="$deadline"' in chain
    assert '--time="$TICK_WALLTIME_MIN"' in chain
