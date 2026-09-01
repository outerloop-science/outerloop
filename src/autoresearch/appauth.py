"""GitHub App installation-token auth (docs/design/github-app-auth.md).

`AppInstallationTokenProvider` satisfies the `TokenProvider` protocol used
throughout `github.py`. Role CLIs construct bot auth through
`resolve_bot_auth`, which selects this provider when an App config file is
given (`--github-app-file` / `AUTORESEARCH_GITHUB_APP_FILE`) and falls back
to the PAT file otherwise — the cutover flag, revertible by unsetting the
env. Each `token()` mints a short-lived JWT (RS256, signed by the App
private key), exchanges it for a ~1h installation token scoped to the
installation's repos, and caches that token until a refresh margin before
it expires.

The RS256 signer and the HTTP transport are injected so the provider — and
its tests — build and run without the `cryptography` dependency or a
network; the production signer lives behind the `app-auth` extra.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from autoresearch.github import AUTH_SAFE_OPENER, FileTokenProvider, TokenProvider

# RS256-sign the JWT signing input, returning the raw signature bytes.
Signer = Callable[[bytes], bytes]
# Perform the token-exchange POST and return the parsed JSON body.
Transport = Callable[[urllib.request.Request], Any]

API = "https://api.github.com"
# GitHub caps App JWT lifetime at 10 minutes; stay comfortably under it.
_JWT_TTL_S = 9 * 60
# Backdate `iat` to tolerate clock skew between us and GitHub.
_JWT_BACKDATE_S = 60
# Re-mint the installation token this long before it actually expires, so a
# token is never handed out on the edge of expiry.
_REFRESH_MARGIN_S = 5 * 60

# Every installation token minted by this process, whichever provider
# instance minted it. `redact` appends these to its snapshotted secrets
# tuple at write time, so a token minted after a call site captured its
# tuple still never reaches a report, record, or log.
_ISSUED_TOKENS: list[str] = []


def issued_tokens() -> tuple[str, ...]:
    """All installation tokens minted this process, for write-time redaction."""
    return tuple(_ISSUED_TOKENS)


def _b64url(data: bytes) -> str:
    """URL-safe base64 without padding — the JWT wire encoding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def build_app_jwt(app_id: int, now: float, sign: Signer) -> str:
    """A GitHub App JWT: header.payload.signature, RS256 over the first two."""
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64url(
        json.dumps(
            {
                "iat": int(now) - _JWT_BACKDATE_S,
                "exp": int(now) + _JWT_TTL_S,
                "iss": str(app_id),
            }
        ).encode()
    )
    signing_input = f"{header}.{payload}"
    signature = _b64url(sign(signing_input.encode("ascii")))
    return f"{signing_input}.{signature}"


def _parse_expiry(expires_at: str) -> float:
    """`2026-09-01T12:00:00Z` (or with an offset) -> unix seconds."""
    text = expires_at.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).timestamp()


def _default_transport(request: urllib.request.Request) -> Any:
    # the shared opener: a cross-host redirect must not forward the App JWT
    try:
        with AUTH_SAFE_OPENER.open(request, timeout=30) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:500]
        raise ValueError(f"installation-token exchange failed ({exc.code}): {body}") from None
    except urllib.error.URLError as exc:
        raise ValueError(f"installation-token exchange failed: {exc.reason}") from None
    return json.loads(payload) if payload else None


class AppInstallationTokenProvider:
    """A `TokenProvider` minting cached, short-lived installation tokens."""

    def __init__(
        self,
        app_id: int,
        installation_id: int,
        sign: Signer,
        *,
        transport: Transport | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.app_id = app_id
        self.installation_id = installation_id
        self._sign = sign
        self._transport = transport or _default_transport
        self._now = now
        self._token = ""
        self._expiry = 0.0
        # every token ever minted this process — the PAT was one immortal
        # string, but these rotate ~hourly, so a redaction set snapshotted at
        # construction goes stale; redaction must pull issued() at write time
        self._issued: list[str] = []

    def token(self) -> str:
        now = self._now()
        if self._token and now < self._expiry - _REFRESH_MARGIN_S:
            return self._token
        jwt = build_app_jwt(self.app_id, now, self._sign)
        request = urllib.request.Request(
            f"{API}/app/installations/{self.installation_id}/access_tokens",
            method="POST",
            headers={
                "Authorization": f"Bearer {jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        body = self._transport(request)
        if not isinstance(body, dict) or not body.get("token"):
            raise ValueError("installation-token response missing a token")
        self._token = str(body["token"])
        self._issued.append(self._token)
        _ISSUED_TOKENS.append(self._token)
        expires_at = body.get("expires_at")
        # a missing/garbled expiry is treated as immediate — safe: we simply
        # re-mint on every call rather than trust an unknown lifetime
        try:
            self._expiry = _parse_expiry(str(expires_at)) if expires_at else now
        except ValueError:
            self._expiry = now
        return self._token

    def issued(self) -> tuple[str, ...]:
        """Every token this provider has minted, for redaction sets built at
        write time — a set snapshotted at construction misses refreshes."""
        return tuple(self._issued)


def signer_from_private_key(pem_path: Path) -> Signer:
    """The production RS256 signer, built from the App private-key PEM. Lazily
    imports `cryptography` (the `app-auth` extra) so the module and its tests
    do not depend on it; the key file must be owner-only (chmod 600), the same
    custody the PAT file has."""
    if not pem_path.is_file():
        raise ValueError(f"{pem_path} is not a readable private-key file")
    if pem_path.stat().st_mode & 0o077:
        raise PermissionError(f"{pem_path} is group/world accessible; chmod 600 it")
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "GitHub App auth needs the 'app-auth' extra (cryptography); "
            "install it before selecting the App token provider"
        ) from exc

    key = serialization.load_pem_private_key(pem_path.read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError(f"{pem_path} is not an RSA key; GitHub App keys are RSA")

    def sign(message: bytes) -> bytes:
        return key.sign(message, padding.PKCS1v15(), hashes.SHA256())

    return sign


def app_provider_from_file(app_file: Path) -> AppInstallationTokenProvider:
    """Build the provider from the App config file: JSON with `app_id`,
    `installation_id` (integers), and `private_key` (path to the PEM). The
    ids are not secrets; the file exists so the whole App identity travels
    as one path on the same rails the PAT path already rides."""
    try:
        config = json.loads(app_file.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read App config {app_file}: {exc}") from None
    missing = [k for k in ("app_id", "installation_id", "private_key") if not config.get(k)]
    if missing:
        raise ValueError(f"App config {app_file} is missing {', '.join(missing)}")
    return AppInstallationTokenProvider(
        int(config["app_id"]),
        int(config["installation_id"]),
        signer_from_private_key(Path(str(config["private_key"])).expanduser()),
    )


def resolve_bot_auth(pat_file: str | Path, app_file: str | Path = "") -> TokenProvider:
    """The one seam every role CLI constructs bot auth through: the App
    provider when an App config file is given, the PAT file otherwise."""
    if str(app_file).strip():
        return app_provider_from_file(Path(str(app_file)).expanduser())
    return FileTokenProvider(Path(str(pat_file)).expanduser())
