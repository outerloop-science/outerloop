"""Named, file-authenticated endpoints shared by every role and backend."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from pathlib import Path
from urllib.parse import urlsplit

from outerloop.github import FileTokenProvider

PROFILE_KEY = re.compile(r"OUTERLOOP_ENDPOINT_[A-Z][A-Z0-9_]*_(URL|URL_FILE|KEY_FILE|MODEL|API)\Z")
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


class EndpointUnavailable(Exception):
    """A configured server address is temporarily unavailable."""


def validate_url(value: str, name: str) -> str:
    url = urlsplit(value)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or any(c.isspace() for c in value)
    ):
        raise ValueError(
            f"endpoint {name!r}: URL must be an HTTP(S) base URL "
            "without credentials, query or fragment"
        )
    try:
        url.port  # noqa: B018 -- raises on a malformed or out-of-range port
    except ValueError as exc:
        raise ValueError(f"endpoint {name!r}: URL has an invalid port") from exc
    return value


@dataclass(frozen=True)
class EndpointProfile:
    name: str
    fixed_url: str
    key_file: Path
    model: str
    apis: tuple[str, ...]

    url_file: Path | None = None

    @property
    def codex_bridge(self) -> bool:
        return "chat" in self.apis and "responses" not in self.apis

    @property
    def url(self) -> str:
        if self.url_file is None:
            return self.fixed_url
        try:
            value = self.url_file.read_text().strip()
        except OSError as exc:
            raise EndpointUnavailable(
                f"endpoint {self.name!r}: URL_FILE {self.url_file} unavailable; "
                "waiting for server address"
            ) from exc
        if value.startswith("{"):
            try:
                value = json.loads(value)["url"]
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(
                    f"endpoint {self.name!r}: URL_FILE must contain a URL or JSON with a url key"
                ) from exc
        if not isinstance(value, str):
            raise ValueError(f"endpoint {self.name!r}: URL_FILE url must be a string")
        return validate_url(value, self.name)

    def session_url(self) -> str:
        """Resolve once and probe a dynamic server once, without session retries."""
        from outerloop.attempt import Terminated

        connection: HTTPConnection | None = None
        try:
            value = self.url
            if self.url_file is None:
                return value
            url = urlsplit(value)
            connection_type = HTTPSConnection if url.scheme == "https" else HTTPConnection
            connection = connection_type(url.hostname or "", url.port, timeout=3)
            path = url.path.rstrip("/")
            if not path.endswith("/v1"):
                path += "/v1"
            connection.request(
                "GET", path + "/models", headers={"Authorization": f"Bearer {self.key()}"}
            )
            response = connection.getresponse()
            if response.status != 200:
                raise EndpointUnavailable(f"endpoint {self.name!r}: models request failed")
            return value
        except (OSError, HTTPException, Terminated, KeyboardInterrupt) as exc:
            raise EndpointUnavailable(f"endpoint {self.name!r}: server unavailable") from exc
        finally:
            if connection is not None:
                connection.close()

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
        for suffix in ("URL", "URL_FILE", "KEY_FILE", "MODEL", "API")
    }
    if not any(values.values()):
        raise ValueError(f"unknown endpoint profile {name!r}")
    if bool(values["URL"]) == bool(values["URL_FILE"]):
        raise ValueError(
            f"endpoint {name!r}: missing or conflicting URL; "
            f"exactly one of {prefix}URL and {prefix}URL_FILE is required"
        )
    url_file = Path(values["URL_FILE"]) if values["URL_FILE"] else None
    if url_file is not None and not url_file.is_absolute():
        raise ValueError(f"endpoint {name!r}: URL_FILE must be absolute")
    for suffix, value in values.items():
        if suffix not in ("URL", "URL_FILE") and not value:
            raise ValueError(f"endpoint {name!r}: missing {prefix}{suffix}")
    apis = tuple(part.strip() for part in values["API"].split(","))
    required = {"claude": "anthropic", "codex": "responses", "hermes": "chat"}[backend]
    if any(api not in ("anthropic", "responses", "chat") for api in apis):
        raise ValueError(f"endpoint {name!r}: API must list anthropic, responses, or chat")
    if required not in apis and not (backend == "codex" and "chat" in apis):
        raise ValueError(f"endpoint {name!r}: {backend} requires API {required}")
    if values["URL"]:
        validate_url(values["URL"], name)
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
    profile = EndpointProfile(name.lower(), values["URL"], path, values["MODEL"], apis, url_file)
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
