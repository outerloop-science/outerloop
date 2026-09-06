"""Autonomous research agent that co-develops the lab's benchmark-bearing repos."""

import os

__version__ = "0.1.0.dev1"


def _bridge_legacy_env() -> None:
    """Accept the pre-rename `AUTORESEARCH_*` names at the process boundary for
    one release: copy each into its `OUTERLOOP_*` twin when the twin is unset,
    so the kernel reads one name. The chain scripts do the same in shell.
    Removed in the release after 0.1."""
    for key, value in list(os.environ.items()):
        if key.startswith("AUTORESEARCH_"):
            os.environ.setdefault("OUTERLOOP_" + key[len("AUTORESEARCH_") :], value)


_bridge_legacy_env()
