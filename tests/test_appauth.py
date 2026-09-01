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
    # both minted tokens are remembered for write-time redaction sets
    assert p.issued() == ("ghs_1", "ghs_2")


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


def test_default_transport_routes_through_the_shared_opener(monkeypatch) -> None:
    # the real exchange must go through github.py's auth-stripping opener, so
    # a cross-host redirect can never forward the App JWT
    import io

    from autoresearch import appauth

    seen = {}

    class FakeOpener:
        def open(self, request, timeout=None):
            seen["url"] = request.full_url
            return io.BytesIO(b'{"token": "ghs_ok"}')

    monkeypatch.setattr(appauth, "AUTH_SAFE_OPENER", FakeOpener())
    import urllib.request

    body = appauth._default_transport(urllib.request.Request("https://api.github.com/x"))
    assert body == {"token": "ghs_ok"}
    assert seen["url"] == "https://api.github.com/x"


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


def test_resolve_bot_auth_defaults_to_the_pat(tmp_path) -> None:
    from autoresearch.appauth import resolve_bot_auth
    from autoresearch.github import FileTokenProvider

    pat = tmp_path / "pat"
    pat.write_text("github_pat_x\n")
    pat.chmod(0o600)
    provider = resolve_bot_auth(pat)
    assert isinstance(provider, FileTokenProvider)
    assert provider.token() == "github_pat_x"
    # an empty app_file is the same as none
    assert isinstance(resolve_bot_auth(pat, ""), FileTokenProvider)
    assert isinstance(resolve_bot_auth(pat, "  "), FileTokenProvider)


def _rsa_pem(path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)


def test_resolve_bot_auth_selects_the_app_provider(tmp_path) -> None:
    from autoresearch.appauth import resolve_bot_auth

    key = tmp_path / "app.pem"
    _rsa_pem(key)
    config = tmp_path / "github_app.json"
    config.write_text(
        json.dumps({"app_id": 4797847, "installation_id": 158334152, "private_key": str(key)})
    )
    provider = resolve_bot_auth(tmp_path / "pat-not-read", config)
    assert isinstance(provider, AppInstallationTokenProvider)
    assert provider.app_id == 4797847
    assert provider.installation_id == 158334152
    # the signer round-trips: the JWT signature verifies against the public key
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    from autoresearch.appauth import build_app_jwt, signer_from_private_key

    jwt = build_app_jwt(4797847, 1_000_000.0, signer_from_private_key(key))
    signing_input, _, sig_seg = jwt.rpartition(".")
    signature = base64.urlsafe_b64decode(sig_seg + "=" * (-len(sig_seg) % 4))
    private = serialization.load_pem_private_key(key.read_bytes(), password=None)
    assert isinstance(private, rsa.RSAPrivateKey)
    private.public_key().verify(
        signature, signing_input.encode(), padding.PKCS1v15(), hashes.SHA256()
    )


def test_app_config_failures_are_loud(tmp_path) -> None:
    from autoresearch.appauth import app_provider_from_file

    with pytest.raises(ValueError, match="cannot read App config"):
        app_provider_from_file(tmp_path / "absent.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ValueError, match="cannot read App config"):
        app_provider_from_file(bad)
    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"app_id": 1}))
    with pytest.raises(ValueError, match="missing installation_id, private_key"):
        app_provider_from_file(partial)
    # the key file's custody rules apply through the config path too
    key = tmp_path / "open.pem"
    key.write_text("k")
    key.chmod(0o644)
    lax = tmp_path / "lax.json"
    lax.write_text(json.dumps({"app_id": 1, "installation_id": 2, "private_key": str(key)}))
    with pytest.raises(PermissionError, match="group/world accessible"):
        app_provider_from_file(lax)


def test_redact_covers_tokens_minted_after_the_snapshot(monkeypatch) -> None:
    """The round-2 obligation: a secrets tuple captured at CLI start must not
    miss an installation token minted by a later refresh."""
    from autoresearch import appauth
    from autoresearch.harness import redact

    monkeypatch.setattr(appauth, "_ISSUED_TOKENS", [])
    clock = {"t": 1_000.0}
    serial = {"n": 0}

    def transport(request):
        serial["n"] += 1
        return {"token": f"ghs_mint_{serial['n']}", "expires_at": _iso(clock["t"] + 3600)}

    p = AppInstallationTokenProvider(
        1, 2, sign=lambda m: b"s", transport=transport, now=lambda: clock["t"]
    )
    snapshot = ("api_key_x", p.token())  # what call sites capture today
    clock["t"] += 3400  # into the refresh margin
    p.token()  # the mid-run refresh the snapshot cannot know about
    text = "a ghs_mint_1 b ghs_mint_2 c api_key_x"
    assert redact(text, snapshot) == "a [redacted] b [redacted] c [redacted]"
