"""Single reader for packaged harness pins; also usable directly from a checkout."""

from __future__ import annotations

import os
import re
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path

OVERRIDE_KEYS = (
    "OUTERLOOP_CLAUDE_VERSION",
    "OUTERLOOP_CLAUDE_SHA256",
    "OUTERLOOP_CODEX_VERSION",
    "OUTERLOOP_CODEX_SHA256",
    "OUTERLOOP_HERMES_REF",
    "OUTERLOOP_HERMES_SHA",
)

NAMES = ("claude", "codex", "hermes", "bridge")


def pins(name: str) -> dict[str, str]:
    if name not in NAMES:
        raise ValueError(f"unknown harness: {name}")
    with Path(__file__).with_name("harnesses.toml").open("rb") as file:
        result = tomllib.load(file)[name]
    if not isinstance(result, dict) or not all(isinstance(v, str) for v in result.values()):
        raise ValueError(f"invalid pins for {name}")
    return result


def effective(name: str, env: Mapping[str, str] | None = None) -> dict[str, str]:
    env = os.environ if env is None else env
    result = pins(name)
    if name == "bridge":
        return result
    for field in ("ref", "sha") if name == "hermes" else ("version",):
        value = env.get(f"OUTERLOOP_{name.upper()}_{field.upper()}")
        if value:
            result[field] = value
    if name == "hermes":
        if not re.fullmatch(r"[0-9a-f]{40}", result["sha"]):
            raise ValueError("Hermes SHA must be a full commit SHA")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", result["ref"]):
            raise ValueError("invalid Hermes ref")
        if env.get("OUTERLOOP_HERMES_REF") and not env.get("OUTERLOOP_HERMES_SHA"):
            raise ValueError("a Hermes ref override requires OUTERLOOP_HERMES_SHA")
        if env.get("OUTERLOOP_HERMES_SHA") and not env.get("OUTERLOOP_HERMES_REF"):
            raise ValueError("a Hermes SHA override requires OUTERLOOP_HERMES_REF")
    elif not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", result["version"]):
        raise ValueError(f"invalid {name} version")
    if name != "hermes":
        key = f"OUTERLOOP_{name.upper()}_SHA256"
        checksum = env.get(key, "")
        if env.get(f"OUTERLOOP_{name.upper()}_VERSION") and not checksum:
            raise ValueError(f"a {name} version override requires {key}")
        if checksum:
            if not re.fullmatch(r"[0-9a-f]{64}", checksum):
                raise ValueError(f"invalid {key}")
            if name == "claude":
                for field in result.keys() - {"version"}:
                    result[field] = checksum
            else:
                result["sha256"] = checksum
    return result


if __name__ == "__main__":
    try:
        name, field = sys.argv[1:]
        print(effective(name)[field])
    except (ValueError, KeyError) as exc:
        sys.exit(str(exc))
