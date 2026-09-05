"""The GitHub App Manifest flow's pure pieces: manifest, conversion, creds."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from outerloop import appmanifest
from outerloop.appmanifest import (
    SETUP_URL,
    build_manifest,
    build_setup_url,
    capture_installation_id,
    convert_manifest,
    install_url,
    request_manifest_code,
    save_app_creds,
    set_installation_id,
)

CONVERSION = {
    "id": 4830667,
    "slug": "my-outerloop",
    "pem": "-----BEGIN KEY-----\nx\n-----END KEY-----",
}


def test_build_manifest_declares_least_privilege() -> None:
    m = build_manifest("my-app", "https://example.com")
    assert m["name"] == "my-app"
    assert m["public"] is False
    assert m["redirect_url"] == SETUP_URL  # GitHub returns the code to the hosted page
    assert m["default_permissions"] == {
        "contents": "write",
        "issues": "write",
        "metadata": "read",
        "pull_requests": "write",
    }
    assert m["hook_attributes"]["active"] is False


def test_convert_manifest_returns_creds() -> None:
    got = convert_manifest("thecode", transport=lambda req: CONVERSION)
    assert got["id"] == 4830667 and got["slug"] == "my-outerloop"


def test_convert_manifest_rejects_incomplete() -> None:
    with pytest.raises(ValueError, match="missing"):
        convert_manifest("c", transport=lambda req: {"id": 1})  # no pem/slug
    with pytest.raises(ValueError, match="no object"):
        convert_manifest("c", transport=lambda req: None)


def test_save_app_creds_writes_pem_and_json_0600(tmp_path: Path) -> None:
    pem_path, app_json = save_app_creds(CONVERSION, tmp_path, installation_id=0)
    assert pem_path == tmp_path / "my-outerloop-app.pem"
    assert app_json == tmp_path / "github_app.my-outerloop.json"
    assert pem_path.read_text() == CONVERSION["pem"]
    assert stat.S_IMODE(pem_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(app_json.stat().st_mode) == 0o600
    data = json.loads(app_json.read_text())
    assert data == {"app_id": 4830667, "installation_id": 0, "private_key": str(pem_path)}


def test_install_url() -> None:
    assert install_url(CONVERSION) == "https://github.com/apps/my-outerloop/installations/new"


def test_set_installation_id(tmp_path: Path) -> None:
    _, app_json = save_app_creds(CONVERSION, tmp_path)
    set_installation_id(app_json, 159164320)
    assert json.loads(app_json.read_text())["installation_id"] == 159164320
    assert stat.S_IMODE(app_json.stat().st_mode) == 0o600


def test_capture_installation_id_matches_owner(tmp_path: Path, monkeypatch) -> None:
    pem = tmp_path / "k.pem"
    pem.write_text("x")
    monkeypatch.setattr(appmanifest, "signer_from_private_key", lambda p: lambda m: b"s")
    monkeypatch.setattr(appmanifest, "build_app_jwt", lambda *a, **k: "jwt")
    installs = [
        {"id": 111, "account": {"login": "someone-else"}},
        {"id": 222, "account": {"login": "Agentic-Learning-AI-Lab"}},
    ]
    got = capture_installation_id(1, pem, "agentic-learning-ai-lab", transport=lambda req: installs)
    assert got == 222  # case-insensitive owner match
    # sole-installation fallback when no owner is given
    assert capture_installation_id(1, pem, "", transport=lambda req: [installs[0]]) == 111
    # nothing installed yet
    assert capture_installation_id(1, pem, "x", transport=lambda req: []) == 0


def test_build_setup_url_packs_the_manifest_in_the_fragment() -> None:
    import base64

    url = build_setup_url(build_manifest("n", "u"), "st8", "my-org")
    assert url.startswith(SETUP_URL + "#")
    payload = json.loads(base64.urlsafe_b64decode(url.split("#", 1)[1]))
    assert payload["state"] == "st8" and payload["org"] == "my-org"
    assert payload["manifest"]["default_permissions"]["contents"] == "write"


def test_request_manifest_code_prints_url_and_reads_code() -> None:
    printed: list[str] = []
    code = request_manifest_code(
        "n", "https://h", "", print_fn=printed.append, input_fn=lambda _: "  code42  "
    )
    assert code == "code42"  # stripped
    assert any(SETUP_URL in line for line in printed)  # the adopter is shown the URL
