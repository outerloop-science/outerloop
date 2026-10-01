"""Deployment-owned author selection; contracts only allocate agent identities."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType

SETTING = "OUTERLOOP_AUTHOR_OVERRIDES"


@dataclass(frozen=True)
class AuthorOverride:
    backend: str
    model: str
    slots: tuple[str, ...] | None = None

    def resolved_model(self) -> str:
        """Bind profile model defaults without inheriting the fleet endpoint."""
        from outerloop.endpoints import resolve_endpoint

        model, profile = resolve_endpoint(self.model, self.backend)
        return f"{model}[endpoint={profile.name}]" if profile else model

    def matches(self, agent_id: str) -> bool:
        return self.slots is None or agent_id in self.slots


@lru_cache(maxsize=16)
def parse_overrides(raw: str) -> Mapping[str, tuple[AuthorOverride, ...]]:
    """Parse once per setting value, including in long-lived tick processes."""
    try:
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            raise ValueError("must be a JSON object")
        result = {}
        for target, entries in data.items():
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", target):
                raise ValueError(f"invalid target {target!r}; expected owner/repo")
            # one override, or a list of them for different slots of the same target
            listed = isinstance(entries, list)
            if listed and not entries:
                raise ValueError(f"{target}: expected at least one override")
            parsed: list[AuthorOverride] = []
            for value in entries if listed else [entries]:
                parsed.append(_parse_one(target, value, listed))
            claimed = [s for o in parsed for s in (o.slots or ())]
            if len(set(claimed)) != len(claimed):
                raise ValueError(f"{target}: a slot is listed in more than one override")
            result[target] = tuple(parsed)
        return MappingProxyType(result)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{SETTING}: {exc}") from exc


def _parse_one(target: str, value: object, listed: bool) -> AuthorOverride:
    if not isinstance(value, dict) or set(value) - {"backend", "model", "slots"}:
        raise ValueError(f"{target}: expected backend, model and optional slots")
    backend, model, slots = value.get("backend"), value.get("model"), value.get("slots")
    if backend not in ("claude", "codex", "hermes"):
        raise ValueError(f"{target}: backend must be claude, codex or hermes")
    if (
        not isinstance(model, str)
        or not model.strip()
        or model != model.strip()
        or any(c in model for c in "\r\n\x00")
    ):
        raise ValueError(f"{target}: model must be a nonempty string without outer whitespace")
    from outerloop.endpoints import split_endpoint

    split_endpoint(model)
    if "slots" in value and (
        not isinstance(slots, list)
        or not slots
        or any(
            not isinstance(s, str)
            or not re.fullmatch(r"agent-\d{2,}", s)
            or int(s[6:]) < 1
            or s != f"agent-{int(s[6:]):02d}"
            for s in slots
        )
        or len(set(slots)) != len(slots)
    ):
        raise ValueError(f"{target}: slots must be distinct agent identities (agent-01, ...)")
    if listed and slots is None:
        raise ValueError(f"{target}: each override in a list must name its slots")
    return AuthorOverride(backend, model, None if slots is None else tuple(slots))


def overrides(
    environ: Mapping[str, str] | None = None,
) -> Mapping[str, tuple[AuthorOverride, ...]]:
    return parse_overrides((os.environ if environ is None else environ).get(SETTING, ""))


def override_entries(
    environ: Mapping[str, str] | None = None,
) -> tuple[tuple[str, AuthorOverride], ...]:
    """Every (target, override) pair, for callers that check or install each backend."""
    return tuple((t, o) for t, group in overrides(environ).items() for o in group)


def select_override(target: str, agent_id: str) -> AuthorOverride | None:
    return next((o for o in overrides().get(target, ()) if o.matches(agent_id)), None)


def validate_overrides(environ: Mapping[str, str], image: str) -> None:
    from outerloop.attempt import author_config_error

    for target, selected in override_entries(environ):
        error = author_config_error(selected.backend, selected.model, image, environ=environ)
        if error:
            raise ValueError(f"{SETTING}: {target}: {error}")
