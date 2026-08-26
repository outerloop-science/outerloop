"""Back-compat shim: `climb` was renamed to `attempt` (the author-role
activity — docs/design/research-loop-buildout.md). This module keeps
`python -m autoresearch.climb` and `from autoresearch.climb import ...`
working for one deprecation cycle, so a climb job SUBMITTED before the
rename deploy — pending in the Slurm queue, its argv already fixed —
still runs even if it lands on the post-rename tree (flight_checkout can
fall back to the shared checkout, which after deploy is the new code).

Delete once no pre-rename job can still be queued (a few tick cadences).
"""

from __future__ import annotations

from autoresearch.attempt import *  # noqa: F403 — re-export the public surface
from autoresearch.attempt import main


def _run() -> int:
    import logging

    logging.getLogger(__name__).warning(
        "autoresearch.climb is a compatibility shim; use autoresearch.attempt"
    )
    return main()


if __name__ == "__main__":
    import sys

    sys.exit(_run())
