"""Autonomous research agent that co-develops the lab's benchmark-bearing repos."""

import os

__version__ = "0.1.0.dev1"


def _bridge_legacy_env() -> None:
    """Accept the new `OUTERLOOP_*` names at the process boundary: copy each into
    its `AUTORESEARCH_*` twin when the twin is unset, so the kernel's internals —
    which still read `AUTORESEARCH_*` during the rename — see one name. The chain
    scripts do the same in shell. Removed with the final internal flip."""
    for key, value in list(os.environ.items()):
        if key.startswith("OUTERLOOP_"):
            os.environ.setdefault("AUTORESEARCH_" + key[len("OUTERLOOP_") :], value)


_bridge_legacy_env()
