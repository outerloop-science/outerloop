"""Stable scheduler names, with run attribution preserved within 128 characters."""

import hashlib

JOB_NAME_LIMIT = 128


def run_key(run_id: str) -> str:
    return "rk-" + hashlib.sha256(run_id.encode()).hexdigest()[:12]


def run_job_name(run_id: str, *, prefix: str = "", suffix: str = "") -> str:
    name = f"{prefix}{run_id}{suffix}"
    if len(name) <= JOB_NAME_LIMIT:
        return name
    name = f"{prefix}{run_key(run_id)}{suffix}"
    if len(name) <= JOB_NAME_LIMIT:
        return name
    # Keep the run key and distinguish even unusually long labels.
    digest = hashlib.sha256(name.encode()).hexdigest()[:12]
    key = run_key(run_id)
    room = JOB_NAME_LIMIT - len(key) - len(digest) - 2
    return f"{key}-{name[:room]}-{digest}"
