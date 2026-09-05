"""GitHub App Manifest flow for `outerloop init --github-app`.

Creates the adopter's OWN GitHub App in one click instead of a hand-rolled PAT.
Because the bot runs on the adopter's own compute (self-hosted), the App's
private key must live there — a shared App would mean either we run the fleet or
we hand out a master key — so each adopter owns their App. GitHub's manifest flow
makes that a click: a pre-filled create page, then a code we exchange for the key.

The flow is paste-based and hostless on the adopter's side: init prints ONE URL
to the hosted helper page (`outerloop.science/app-setup`), which carries the
manifest in its URL *fragment* (never sent to any server). The adopter opens it
in any browser — laptop or, for a headless cluster, anywhere — clicks Create,
and GitHub redirects back to that page with a one-time code the page displays.
The adopter pastes the code here; we exchange it for the app id + key, write
`github_app.<slug>.json` + the PEM (both 0600), help install the App, and capture
the installation id. The written files are what `resolve_bot_auth` reads via
`AUTORESEARCH_GITHUB_APP_FILE`, the same path `outerloop start` uses.
"""

from __future__ import annotations

import base64
import json
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from outerloop.appauth import API, build_app_jwt, signer_from_private_key

# The hosted helper page: it auto-POSTs the manifest to GitHub, then displays the
# returned code. It is also the manifest's redirect_url, so GitHub sends the code
# straight back to it. One static page under the org's domain.
SETUP_URL = "https://outerloop.science/app-setup"

# The App's fine-grained permissions — exactly what the fleet exercises: contents
# to push commits, pull_requests to open/label PRs, issues for the courtesy note
# on the requesting issue. Nothing else (least privilege; matches the live bot).
DEFAULT_PERMISSIONS = {
    "contents": "write",
    "issues": "write",
    "metadata": "read",
    "pull_requests": "write",
}

# The POST/GET transport returns parsed JSON; injected so the pure logic tests
# without a network.
Transport = Callable[[urllib.request.Request], Any]


def build_manifest(name: str, url: str, redirect_url: str = SETUP_URL) -> dict:
    """The manifest GitHub creates the App from: our permissions, no webhook,
    installable only on the creating account (`public=false`). `redirect_url` is
    where GitHub returns the code — the hosted helper page by default."""
    return {
        "name": name,
        "url": url,
        "redirect_url": redirect_url,
        "public": False,
        "default_permissions": dict(DEFAULT_PERMISSIONS),
        "hook_attributes": {"active": False, "url": url},
    }


def build_setup_url(manifest: dict, state: str, org: str = "") -> str:
    """The one URL the adopter opens: the hosted helper page with the manifest,
    state, and org packed into the URL *fragment* (base64) — a fragment stays in
    the browser and is never sent to any server, so the manifest isn't logged."""
    payload = json.dumps({"manifest": manifest, "state": state, "org": org})
    encoded = base64.urlsafe_b64encode(payload.encode()).decode()
    return f"{SETUP_URL}#{encoded}"


def request_manifest_code(
    name: str,
    homepage_url: str,
    org: str = "",
    *,
    print_fn: Callable[[str], None] = print,
    input_fn: Callable[[str], str] = input,
) -> str:
    """Print the setup URL and read back the code the hosted page shows. Works
    anywhere a browser can reach the internet — no localhost, no callback."""
    state = secrets.token_urlsafe(16)
    manifest = build_manifest(name, homepage_url)
    url = build_setup_url(manifest, state, org)
    print_fn("Create your GitHub App — open this URL in a browser (any machine):")
    print_fn(f"\n  {url}\n")
    print_fn("Click 'Create GitHub App', then copy the code the page shows.")
    return input_fn("Paste the code here: ").strip()


def _default_transport(request: urllib.request.Request) -> Any:
    with urllib.request.urlopen(request, timeout=30) as resp:
        payload = resp.read()
    return json.loads(payload) if payload else None


def convert_manifest(code: str, *, transport: Transport | None = None) -> dict:
    """Exchange the one-time manifest `code` for the App's credentials (id, pem,
    slug, ...). GitHub invalidates the code on first use and after ~1h."""
    transport = transport or _default_transport
    request = urllib.request.Request(
        f"{API}/app-manifests/{code}/conversions",
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        body = transport(request)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:200]
        raise ValueError(f"manifest conversion failed ({exc.code}): {detail}") from None
    if not isinstance(body, dict):
        raise ValueError("manifest conversion returned no object")
    missing = [k for k in ("id", "pem", "slug") if not body.get(k)]
    if missing:
        raise ValueError(f"manifest conversion missing {', '.join(missing)}")
    return body


def save_app_creds(
    conversion: dict, config_dir: Path, installation_id: int = 0
) -> tuple[Path, Path]:
    """Write the PEM (0600) and `github_app.<slug>.json` pointing at it, both
    owner-only. `installation_id` stays 0 until the App is installed and
    `capture_installation_id` fills it. Returns (pem_path, app_json_path)."""
    config_dir.mkdir(parents=True, exist_ok=True)
    slug = str(conversion["slug"])
    pem_path = config_dir / f"{slug}-app.pem"
    pem_path.write_text(str(conversion["pem"]))
    pem_path.chmod(0o600)
    app_json = config_dir / f"github_app.{slug}.json"
    app_json.write_text(
        json.dumps(
            {
                "app_id": int(conversion["id"]),
                "installation_id": int(installation_id),
                "private_key": str(pem_path),
            },
            indent=2,
        )
        + "\n"
    )
    app_json.chmod(0o600)
    return pem_path, app_json


def install_url(conversion: dict) -> str:
    """Where the adopter installs the freshly created App on their repos."""
    return f"https://github.com/apps/{conversion['slug']}/installations/new"


def capture_installation_id(
    app_id: int,
    pem_path: Path,
    owner: str = "",
    *,
    transport: Transport | None = None,
    now: Callable[[], float] = time.time,
) -> int:
    """After the App is installed, its installation id for `owner` (or the sole
    installation when `owner` is blank). App JWT → GET /app/installations. 0 when
    nothing is installed yet — the caller retries after the adopter installs."""
    transport = transport or _default_transport
    sign = signer_from_private_key(pem_path)
    jwt = build_app_jwt(app_id, now(), sign)
    request = urllib.request.Request(
        f"{API}/app/installations",
        headers={
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    installs = transport(request) or []
    if owner:
        for inst in installs:
            if str((inst.get("account") or {}).get("login", "")).lower() == owner.lower():
                return int(inst["id"])
        return 0
    return int(installs[0]["id"]) if installs else 0


def set_installation_id(app_json: Path, installation_id: int) -> None:
    """Fill the installation id into an already-written github_app.<slug>.json."""
    data = json.loads(app_json.read_text())
    data["installation_id"] = int(installation_id)
    app_json.write_text(json.dumps(data, indent=2) + "\n")
    app_json.chmod(0o600)
