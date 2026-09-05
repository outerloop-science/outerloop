"""GitHub App Manifest flow for `outerloop init --github-app`.

Creates the adopter's OWN GitHub App in one click instead of a hand-rolled PAT:
init serves a one-shot local page that POSTs a manifest (our exact permissions)
to GitHub; the adopter clicks Create; GitHub redirects back with a code; init
exchanges it for the app id + private key, writes `github_app.<slug>.json` + the
PEM (both 0600), then points them at the install page and captures the
installation id.

This needs a browser on the same machine (the redirect lands on localhost). On a
headless cluster, run it on your laptop and copy the two written files to
`~/.config/autoresearch/` there. The written files are what `resolve_bot_auth`
reads via `AUTORESEARCH_GITHUB_APP_FILE` — the same path `outerloop start` uses.
"""

from __future__ import annotations

import html
import http.server
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

from outerloop.appauth import API, build_app_jwt, signer_from_private_key

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


def build_manifest(name: str, url: str, redirect_url: str) -> dict:
    """The manifest GitHub creates the App from: our permissions, no webhook,
    installable only on the creating account (`public=false`)."""
    return {
        "name": name,
        "url": url,
        "redirect_url": redirect_url,
        "public": False,
        "default_permissions": dict(DEFAULT_PERMISSIONS),
        "hook_attributes": {"active": False, "url": url},
    }


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


def _create_page(manifest: dict, state: str, org: str) -> bytes:
    """A one-shot page that auto-POSTs the manifest to GitHub's App-create form.
    GitHub reads the App settings from the `manifest` field and the redirect from
    inside it; `state` comes back on the redirect so we can reject a stray hit."""
    action = (
        f"https://github.com/organizations/{urllib.parse.quote(org)}/settings/apps/new"
        if org
        else "https://github.com/settings/apps/new"
    )
    action = f"{action}?state={urllib.parse.quote(state)}"
    field = html.escape(json.dumps(manifest), quote=True)
    return (
        "<!doctype html><meta charset=utf-8><body>"
        f'<form id=f method=post action="{html.escape(action, quote=True)}">'
        f'<input type=hidden name=manifest value="{field}"></form>'
        "<script>document.getElementById('f').submit()</script>"
        "Creating your GitHub App — if nothing happens, "
        f'<a href="{html.escape(action, quote=True)}">continue here</a>.</body>'
    ).encode()


def run_manifest_flow(
    name: str,
    homepage_url: str,
    org: str = "",
    *,
    open_browser: bool = True,
    timeout_s: int = 300,
) -> str:
    """Serve the create page on localhost, open a browser to it, and return the
    one-time manifest `code` GitHub redirects back with (or "" on timeout/state
    mismatch). Browser-and-localhost bound: run it where a browser can reach
    127.0.0.1. The caller converts the code and writes the creds."""
    state = secrets.token_urlsafe(16)

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # keep the terminal quiet
            pass

        def _reply(self, body: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            server: Any = self.server
            if parsed.path == "/callback":
                q = urllib.parse.parse_qs(parsed.query)
                if (q.get("state") or [""])[0] == state:
                    server.code = (q.get("code") or [""])[0]
                self._reply(
                    b"<!doctype html>App created \xe2\x80\x94 return to the terminal; "
                    b"you can close this tab."
                )
            else:
                self._reply(_create_page(server.manifest, state, org))

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    redirect = f"http://127.0.0.1:{port}/callback"
    server.manifest = build_manifest(name, homepage_url, redirect)  # type: ignore[attr-defined]
    server.code = ""  # type: ignore[attr-defined]
    server.timeout = timeout_s
    url = f"http://127.0.0.1:{port}/"
    print(f"Opening {url} to create your GitHub App (state {state[:6]}…).")
    if open_browser:
        webbrowser.open(url)
    deadline = time.time() + timeout_s
    while not server.code and time.time() < deadline:  # type: ignore[attr-defined]
        server.handle_request()  # one request per loop (the page load, then the redirect)
    return str(server.code)  # type: ignore[attr-defined]
