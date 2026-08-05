"""GitHub operations for the orchestrator: REST and git, behind a token provider.

Auth flows through :class:`TokenProvider` so a GitHub App installation-token
provider can replace the PAT file without touching callers
(docs/design/external.md). Every mutating operation respects ``dry_run``: log
the intent, touch nothing. Tokens never appear in process arguments or in
``.git/config`` — git auth is injected per-invocation via ``GIT_CONFIG_*``
environment variables.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

API = "https://api.github.com"
Transport = Callable[[urllib.request.Request], Any]


class TokenProvider(Protocol):
    def token(self) -> str: ...


@dataclass(frozen=True)
class FileTokenProvider:
    """Reads a 0600 credential file (the bot PAT on the orchestrator host)."""

    path: Path

    def token(self) -> str:
        return self.path.read_text().strip()


def _default_transport(request: urllib.request.Request) -> Any:
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read()
    return json.loads(payload) if payload else None


@dataclass
class GitHubClient:
    """Minimal REST surface the orchestrator needs. Mutations honor dry_run."""

    auth: TokenProvider
    transport: Transport = field(default=_default_transport)
    dry_run: bool = False

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        request = urllib.request.Request(
            f"{API}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.auth.token()}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
            },
        )
        return self.transport(request)

    def default_branch(self, repo: str) -> str:
        return str(self._request("GET", f"/repos/{repo}")["default_branch"])

    def get_file(self, repo: str, path: str, ref: str) -> str:
        """Fetch a file's text at a ref — used to read contracts from the
        default branch, never from PR branches."""
        data = self._request("GET", f"/repos/{repo}/contents/{path}?ref={ref}")
        if data.get("encoding") != "base64":
            raise ValueError(f"unexpected encoding for {repo}:{path}")
        return base64.b64decode(data["content"]).decode()

    def create_pr(self, repo: str, head: str, base: str, title: str, body: str) -> int | None:
        if self.dry_run:
            log.info("[dry-run] create PR %s: %s <- %s (%r)", repo, base, head, title)
            return None
        data = self._request(
            "POST",
            f"/repos/{repo}/pulls",
            {"title": title, "head": head, "base": base, "body": body},
        )
        return int(data["number"])

    def comment(self, repo: str, issue_number: int, body: str) -> None:
        if self.dry_run:
            log.info("[dry-run] comment on %s#%s (%d chars)", repo, issue_number, len(body))
            return
        self._request("POST", f"/repos/{repo}/issues/{issue_number}/comments", {"body": body})


def _git_auth_env(token: str | None) -> dict[str, str]:
    """Per-invocation git auth that never lands in argv or .git/config."""
    env = dict(os.environ)
    if token is not None:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env |= {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        }
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


@dataclass
class Workspace:
    """A working clone the agent session edits; pushes happen orchestrator-side."""

    root: Path
    auth: TokenProvider | None = None
    dry_run: bool = False

    def _token(self) -> str | None:
        return self.auth.token() if self.auth is not None else None

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            env=_git_auth_env(self._token()),
            check=True,
        )
        return result.stdout.strip()

    @classmethod
    def clone(
        cls,
        url: str,
        dest: Path,
        auth: TokenProvider | None = None,
        dry_run: bool = False,
    ) -> Workspace:
        token = auth.token() if auth is not None else None
        subprocess.run(
            ["git", "clone", "--quiet", url, str(dest)],
            capture_output=True,
            text=True,
            env=_git_auth_env(token),
            check=True,
        )
        return cls(root=dest, auth=auth, dry_run=dry_run)

    def branch(self, name: str) -> None:
        self.git("switch", "-c", name)

    def commit_all(self, message: str, author: str) -> None:
        self.git("add", "-A")
        self.git(
            "-c",
            f"user.name={author}",
            "-c",
            f"user.email={author}@users.noreply.github.com",
            "commit",
            "-m",
            message,
        )

    def push(self, branch: str) -> None:
        if self.dry_run:
            log.info("[dry-run] push %s from %s", branch, self.root)
            return
        self.git("push", "-u", "origin", branch)
