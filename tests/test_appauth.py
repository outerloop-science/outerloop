"""The GitHub App installation-token provider — exercised with an injected
signer, transport, and clock, so no key, network, or cryptography is needed."""

from __future__ import annotations

import base64
import json

import pytest

from autoresearch.appauth import (
    AppInstallationTokenProvider,
    build_app_jwt,
    signer_from_private_key,
)


def _decode_segment(segment: str) -> dict:
    pad = "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment + pad))


def test_build_app_jwt_shape() -> None:
    jwt = build_app_jwt(app_id=12345, now=1_000_000.0, sign=lambda m: b"sig")
    header_seg, payload_seg, sig_seg = jwt.split(".")
    assert _decode_segment(header_seg) == {"alg": "RS256", "typ": "JWT"}
    payload = _decode_segment(payload_seg)
    assert payload["iss"] == "12345"
    assert payload["iat"] == 1_000_000 - 60  # backdated for clock skew
    assert payload["exp"] == 1_000_000 + 9 * 60  # under GitHub's 10-min cap
    assert payload["exp"] - payload["iat"] <= 600
    # the signature is base64url of the injected signer's output
    assert base64.urlsafe_b64decode(sig_seg + "=" * (-len(sig_seg) % 4)) == b"sig"
    # RS256 signs exactly "header.payload" (the first two segments)
    signed = {}

    def capture(message: bytes) -> bytes:
        signed["input"] = message
        return b"x"

    jwt2 = build_app_jwt(1, 1000.0, capture)
    assert signed["input"] == jwt2.rsplit(".", 1)[0].encode()


def test_token_mints_caches_and_refreshes() -> None:
    calls = []
    clock = {"t": 1_000_000.0}

    def transport(request):
        calls.append(request.full_url)
        # a token valid for one hour from the current clock
        return {"token": f"ghs_{len(calls)}", "expires_at": _iso(clock["t"] + 3600)}

    p = AppInstallationTokenProvider(
        app_id=42,
        installation_id=99,
        sign=lambda m: b"sig",
        transport=transport,
        now=lambda: clock["t"],
    )
    # first call mints and hits the installation endpoint
    assert p.token() == "ghs_1"
    assert calls == ["https://api.github.com/app/installations/99/access_tokens"]
    # within the validity window (minus refresh margin) the cache serves it
    clock["t"] += 3000  # still inside 3600 - 300 margin
    assert p.token() == "ghs_1"
    assert len(calls) == 1  # no second exchange
    # past the refresh margin, a fresh token is minted
    clock["t"] += 400  # now within 5 min of expiry
    assert p.token() == "ghs_2"
    assert len(calls) == 2


def test_token_rejects_a_bodyless_response() -> None:
    p = AppInstallationTokenProvider(
        app_id=1, installation_id=1, sign=lambda m: b"s", transport=lambda r: {}
    )
    with pytest.raises(ValueError, match="missing a token"):
        p.token()


def test_garbled_expiry_forces_a_re_mint_every_call() -> None:
    calls = []

    def transport(request):
        calls.append(1)
        return {"token": "ghs_x", "expires_at": "not-a-date"}

    p = AppInstallationTokenProvider(
        app_id=1, installation_id=1, sign=lambda m: b"s", transport=transport, now=lambda: 100.0
    )
    p.token()
    p.token()
    assert len(calls) == 2  # an unparsable expiry is treated as immediate


def test_signer_refuses_a_missing_or_open_key(tmp_path) -> None:
    with pytest.raises(ValueError, match="not a readable"):
        signer_from_private_key(tmp_path / "nope.pem")
    key = tmp_path / "key.pem"
    key.write_text("-----BEGIN PRIVATE KEY-----\n...\n")
    key.chmod(0o644)
    with pytest.raises(PermissionError, match="group/world accessible"):
        signer_from_private_key(key)


def _iso(epoch: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
