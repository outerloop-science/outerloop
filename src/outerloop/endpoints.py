"""Named, file-authenticated endpoints shared by every role and backend."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from outerloop.github import FileTokenProvider

PROFILE_KEY = re.compile(r"OUTERLOOP_ENDPOINT_[A-Z][A-Z0-9_]*_(URL|KEY_FILE|MODEL|API)\Z")
PROFILE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")


def endpoint_config_key(key: str) -> bool:
    return PROFILE_KEY.fullmatch(key) is not None


def split_endpoint(model: str) -> tuple[str, str]:
    """Only an explicit bracketed selector changes a native model's route."""
    if "[endpoint=" not in model:
        return model, ""
    match = re.fullmatch(r"([^\[\]]*)\[endpoint=([A-Za-z][A-Za-z0-9_]*)\]", model)
    if match is None:
        raise ValueError("endpoint selector must be <model>[endpoint=<profile>]")
    return match[1], match[2].lower()


@dataclass(frozen=True)
class EndpointProfile:
    name: str
    url: str
    key_file: Path
    model: str
    apis: tuple[str, ...]

    def key(self) -> str:
        try:
            return FileTokenProvider(self.key_file).token()
        except (OSError, ValueError) as exc:
            raise ValueError(f"endpoint {self.name!r}: {exc}") from exc


def endpoint_profile(
    name: str, backend: str, model: str = "", environ: Mapping[str, str] | None = None
) -> EndpointProfile:
    env = os.environ if environ is None else environ
    if backend not in ("claude", "codex", "hermes"):
        raise ValueError(f"endpoint {name!r}: unsupported backend {backend!r}")
    if not PROFILE_NAME.fullmatch(name):
        raise ValueError(f"invalid endpoint profile name {name!r}")
    prefix = f"OUTERLOOP_ENDPOINT_{name.upper()}_"
    values = {
        suffix: env.get(prefix + suffix, "").strip()
        for suffix in ("URL", "KEY_FILE", "MODEL", "API")
    }
    if not any(values.values()):
        raise ValueError(f"unknown endpoint profile {name!r}")
    for suffix, value in values.items():
        if not value:
            raise ValueError(f"endpoint {name!r}: missing {prefix}{suffix}")
    apis = tuple(part.strip() for part in values["API"].split(","))
    required = {"claude": "anthropic", "codex": "responses", "hermes": "chat"}[backend]
    if any(api not in ("anthropic", "responses", "chat") for api in apis):
        raise ValueError(f"endpoint {name!r}: API must list anthropic, responses, or chat")
    if required not in apis:
        raise ValueError(f"endpoint {name!r}: {backend} requires API {required}")
    url = urlsplit(values["URL"])
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError(
            f"endpoint {name!r}: URL must be an HTTP(S) base URL "
            "without credentials, query or fragment"
        )
    path = Path(values["KEY_FILE"]).expanduser()
    if not path.is_absolute():
        raise ValueError(f"endpoint {name!r}: KEY_FILE must be absolute")
    if (
        "[" in values["MODEL"]
        or "]" in values["MODEL"]
        or any(c in values["MODEL"] for c in "\r\n")
    ):
        raise ValueError(f"endpoint {name!r}: invalid served model")
    if model and model != values["MODEL"]:
        raise ValueError(
            f"endpoint {name!r}: model {model!r} does not match served model {values['MODEL']!r}"
        )
    profile = EndpointProfile(name.lower(), values["URL"], path, values["MODEL"], apis)
    profile.key()  # Validate before any session or intake claim.
    return profile


def resolve_endpoint(
    model: str, backend: str, name: str = "", environ: Mapping[str, str] | None = None
) -> tuple[str, EndpointProfile | None]:
    served, selected = split_endpoint(model)
    if name and selected and name.lower() != selected:
        raise ValueError("conflicting endpoint selectors")
    selected = selected or name
    profile = endpoint_profile(selected, backend, served, environ) if selected else None
    return (profile.model if profile else served), profile


def author_model_setting(backend: str, model: str, environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    served, profile = resolve_endpoint(
        model, backend, env.get("OUTERLOOP_AUTHOR_ENDPOINT", "").strip(), env
    )
    return f"{served}[endpoint={profile.name}]" if profile else served


def model_key(key_file: str | Path, backend: str, model: str) -> str:
    from outerloop.role_runner import role_key

    _, profile = resolve_endpoint(model, backend)
    return profile.key() if profile else role_key(key_file, backend)


def validate_judge_key_file(
    profile: EndpointProfile, author_path: str | Path, claude_panel_path: str | Path
) -> None:
    """Profiles obey the same file isolation as conventional judge credentials."""
    for label, path in (("author", author_path), ("claude panel", claude_panel_path)):
        other = Path(path).expanduser()
        if profile.key_file.resolve() == other.resolve() or (
            other.exists() and profile.key_file.samefile(other)
        ):
            raise ValueError(f"endpoint judge key file is the {label} key file (role separation)")
