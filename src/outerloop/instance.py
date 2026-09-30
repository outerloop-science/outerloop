"""Stable scheduler identity, also runnable by the batch shim before deploy.

Only the standard library is used: the checkout need not have been synced yet.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def job_name(base: str = "outerloop-resident") -> str:
    """Key scheduler identity on the resolved settings path, never the state root."""
    default = Path.home() / ".config/outerloop/.env"
    selected = os.environ.get("OUTERLOOP_ENV_FILE", "").strip()
    if not selected:  # unset or empty: the default instance
        return base
    path = Path(selected)
    if not path.is_absolute():
        raise ValueError("OUTERLOOP_ENV_FILE must be an absolute path")
    canonical = path.resolve()
    if canonical == default.resolve():
        return base
    digest = hashlib.sha256(os.fsencode(canonical)).hexdigest()[:12]
    return f"{base}-{digest}"


if __name__ == "__main__":
    print(job_name())
