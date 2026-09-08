"""Small utilities shared across test modules."""

from __future__ import annotations

import time
from collections.abc import Callable


def wait_until(
    predicate: Callable[[], object], timeout: float = 5.0, interval: float = 0.01
) -> bool:
    """Poll `predicate` until it is truthy or `timeout` seconds pass; True if it
    became so, False on timeout. Callers assert the result, so a timeout fails
    the test rather than passing silently."""
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)
