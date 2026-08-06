"""GitHub operations for the orchestrator: REST and git, behind a token provider.

Auth flows through :class:`TokenProvider` so a GitHub App installation-token
provider can replace the PAT file without touching callers
(docs/design/external.md). Every mutating operation respects ``dry_run``: log
the intent, touch nothing.

Credential rules enforced here:
- Tokens never appear in process arguments or in ``.git/config``; git auth is
  injected per-invocation via ``GIT_CONFIG_*`` environment variables.
- Only network git subcommands get that environment. Local subcommands run
  token-free with hooks disabled, so repo content written by an agent session
  (hooks, filters) can never read the credential.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

API = "https://api.github.com"
NETWORK_GIT_COMMANDS = frozenset({"clone", "fetch", "pull", "push", "ls-remote"})
Transport = Callable[[urllib.request.Request], Any]


class GitHubError(RuntimeError):
    """A GitHub API call failed."""

    def __init__(self, status: int, path: str, message: str) -> None:
        super().__init__(f"{status} on {path}: {message}")
        self.status = status
        self.path = path


class GitError(RuntimeError):
    """A git subcommand failed; carries git's own explanation."""


class NothingToCommit(GitError):
    """Nothing staged — an ordinary outcome, not a crash."""


class ForbiddenPathError(GitError):
    """A commit tried to stage a path the contract forbids."""


class TokenProvider(Protocol):
    def token(self) -> str: ...


@dataclass(frozen=True)
class FileTokenProvider:
    """Reads a credential file (the bot PAT on the orchestrator host)."""

    path: Path

    def token(self) -> str:
        mode = self.path.stat().st_mode & 0o077
        if mode:
            raise PermissionError(f"{self.path} is group/world accessible; chmod 600 it")
        token = self.path.read_text().strip()
        if not token:
            raise ValueError(f"{self.path} is empty")
        return token


class _NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Drop the Authorization header when a redirect changes host."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if (
            new is not None
            and urllib.parse.urlparse(newurl).netloc != urllib.parse.urlparse(req.full_url).netloc
        ):
            for header in ("Authorization", "authorization"):
                new.headers.pop(header, None)
                new.unredirected_hdrs.pop(header, None)
        return new


_opener = urllib.request.build_opener(_NoAuthRedirect)


def _default_transport(request: urllib.request.Request) -> Any:
    try:
        with _opener.open(request, timeout=30) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:500]
        raise GitHubError(exc.code, urllib.parse.urlparse(request.full_url).path, body) from None
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

    @staticmethod
    def _expect_dict(data: Any, path: str) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise GitHubError(200, path, f"expected an object, got {type(data).__name__}")
        return data

    def default_branch(self, repo: str) -> str:
        path = f"/repos/{urllib.parse.quote(repo)}"
        return str(self._expect_dict(self._request("GET", path), path)["default_branch"])

    def get_file(self, repo: str, path: str, ref: str) -> str:
        """Fetch a file's text at a ref — used to read contracts from the
        default branch, never from PR branches."""
        query = urllib.parse.urlencode({"ref": ref})
        api_path = f"/repos/{urllib.parse.quote(repo)}/contents/{urllib.parse.quote(path)}?{query}"
        data = self._expect_dict(self._request("GET", api_path), api_path)
        if data.get("type") != "file":
            raise GitHubError(200, api_path, f"not a file (type={data.get('type')!r})")
        if data.get("encoding") != "base64":
            raise GitHubError(200, api_path, f"unreadable encoding {data.get('encoding')!r}")
        return base64.b64decode(data["content"]).decode()

    def create_pr(self, repo: str, head: str, base: str, title: str, body: str) -> int | None:
        if self.dry_run:
            log.info("[dry-run] create PR %s: %s <- %s (%r)", repo, base, head, title)
            return None
        path = f"/repos/{urllib.parse.quote(repo)}/pulls"
        data = self._expect_dict(
            self._request("POST", path, {"title": title, "head": head, "base": base, "body": body}),
            path,
        )
        if "number" not in data:
            raise GitHubError(200, path, f"no PR number in response: {data.get('message')}")
        return int(data["number"])

    def comment(self, repo: str, issue_number: int, body: str) -> None:
        if self.dry_run:
            log.info("[dry-run] comment on %s#%s (%d chars)", repo, issue_number, len(body))
            return
        path = f"/repos/{urllib.parse.quote(repo)}/issues/{issue_number}/comments"
        self._request("POST", path, {"body": body})


def _git_env(token: str | None) -> dict[str, str]:
    """Environment for a git invocation. The token is present only for network
    subcommands; local ones also get hooks disabled."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token is None:
        env["GIT_CONFIG_COUNT"] = "0"
        return env
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env |= {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }
    return env


def _run_git(args: list[str], env: dict[str, str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, env=env, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise GitError(f"git {' '.join(args[1:3])} failed: {detail}")
    return result.stdout.strip()


@dataclass
class Workspace:
    """A working clone the agent session edits; pushes happen orchestrator-side."""

    root: Path
    auth: TokenProvider | None = None
    dry_run: bool = False

    def git(self, *args: str) -> str:
        """Run a local git subcommand: no credential, hooks disabled."""
        return _run_git(
            ["git", "-C", str(self.root), "-c", "core.hooksPath=/dev/null", *args],
            _git_env(None),
        )

    def git_network(self, *args: str) -> str:
        """Run a git subcommand that talks to the remote, with credentials."""
        if args and args[0] not in NETWORK_GIT_COMMANDS:
            raise ValueError(f"{args[0]!r} is not a network git command")
        token = self.auth.token() if self.auth is not None else None
        return _run_git(
            ["git", "-C", str(self.root), "-c", "core.hooksPath=/dev/null", *args],
            _git_env(token),
        )

    @classmethod
    def clone(
        cls,
        url: str,
        dest: Path,
        auth: TokenProvider | None = None,
        dry_run: bool = False,
    ) -> Workspace:
        token = auth.token() if auth is not None else None
        _run_git(
            ["git", "clone", "--quiet", "-c", "core.hooksPath=/dev/null", url, str(dest)],
            _git_env(token),
        )
        return cls(root=dest, auth=auth, dry_run=dry_run)

    def branch(self, name: str) -> None:
        self.git("switch", "-c", name)

    def staged_paths(self) -> list[str]:
        output = self.git("diff", "--cached", "--name-only")
        return [line for line in output.splitlines() if line]

    def commit_all(
        self,
        message: str,
        author: str,
        forbidden: Callable[[str], bool] | None = None,
    ) -> None:
        """Stage everything and commit.

        `forbidden(path)` — normally `partial(path_is_forbidden, contract=...)`
        — vetoes the commit if the session touched a path the contract puts
        off-limits, so the invariant is enforced against the diff, not only
        against the contract's own scope list.
        """
        self.git("add", "-A")
        staged = self.staged_paths()
        if not staged:
            raise NothingToCommit("nothing to commit; working tree clean")
        if forbidden is not None:
            violations = [p for p in staged if forbidden(p)]
            if violations:
                self.git("reset")
                raise ForbiddenPathError(f"commit touches forbidden paths: {sorted(violations)}")
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
        self.git_network("push", "-u", "origin", "--", branch)
