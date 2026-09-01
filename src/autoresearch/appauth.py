"""GitHub App installation-token auth (docs/design/github-app-auth.md).

`AppInstallationTokenProvider` satisfies the `TokenProvider` protocol used
throughout `github.py`, so it replaces `FileTokenProvider` at the one
constructor call without touching any caller. Each `token()` mints a
short-lived JWT (RS256, signed by the App private key), exchanges it for a
~1h installation token scoped to the installation's repos, and caches that
token until a refresh margin before it expires.

The RS256 signer and the HTTP transport are injected so the provider — and
its tests — build and run without the `cryptography` dependency or a network;
that dependency is added to the `app-auth` extra only at live cutover.
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
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
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
        expires_at = body.get("expires_at")
        # a missing/garbled expiry is treated as immediate — safe: we simply
        # re-mint on every call rather than trust an unknown lifetime
        try:
            self._expiry = _parse_expiry(str(expires_at)) if expires_at else now
        except ValueError:
            self._expiry = now
        return self._token


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
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "GitHub App auth needs the 'app-auth' extra (cryptography); "
            "install it before selecting the App token provider"
        ) from exc

    key = serialization.load_pem_private_key(pem_path.read_bytes(), password=None)

    def sign(message: bytes) -> bytes:
        return key.sign(message, padding.PKCS1v15(), hashes.SHA256())

    return sign
