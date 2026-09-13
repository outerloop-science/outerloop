"""The lifecycle's public names and removed command."""

import importlib.util

import pytest

from outerloop.cli import main
from outerloop.runstate import STATES


def test_three_states_and_board_labels():
    from outerloop.climbboard import _LIVE_STATES

    assert STATES == ("running", "parked", "ended")
    assert _LIVE_STATES == ("running", "parked")


def test_no_followup_cli(capsys):
    assert importlib.util.find_spec("outerloop.followup") is None
    with pytest.raises(SystemExit) as exc:
        main(["followup"])
    assert exc.value.code == 2
    assert "invalid choice: 'followup'" in capsys.readouterr().err
