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
- Credentialed invocations additionally neutralize every git setting that can
  spawn a child process (ssh command, credential helpers, alternate
  protocols), because the session owns the clone's ``.git/config``.
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
# Settings a session could add to .git/config that make git spawn a child
# process; neutralized on every credentialed invocation.
SAFE_GIT_FLAGS = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.sshCommand=false",
    "-c",
    "credential.helper=",
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.https.allow=always",
    "-c",
    "protocol.file.allow=always",
    "-c",
    "core.fsmonitor=",
    "-c",
    "core.quotePath=false",
)
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
class EnvTokenProvider:
    """Reads a token from an environment variable (CI-supplied credentials)."""

    variable: str

    def token(self) -> str:
        token = os.environ.get(self.variable, "").strip()
        if not token:
            raise ValueError(f"{self.variable} is unset or empty")
        return token


@dataclass(frozen=True)
class FileTokenProvider:
    """Reads a credential file (the bot PAT on the orchestrator host)."""

    path: Path

    def token(self) -> str:
        if not self.path.is_file():
            raise ValueError(f"{self.path} is not a readable credential file")
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
        old_parts = urllib.parse.urlparse(req.full_url)
        new_parts = urllib.parse.urlparse(newurl)
        if new is not None and (
            new_parts.netloc != old_parts.netloc or new_parts.scheme != old_parts.scheme
        ):
            for header in ("Authorization", "authorization"):
                new.headers.pop(header, None)
                new.unredirected_hdrs.pop(header, None)
        return new


_opener = urllib.request.build_opener(_NoAuthRedirect)


def _raw_transport(request: urllib.request.Request) -> str:
    """Fetch a non-JSON body (the diff media type returns text/plain)."""
    try:
        with _opener.open(request, timeout=30) as response:
            return str(response.read().decode(errors="replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:500]
        raise GitHubError(exc.code, urllib.parse.urlparse(request.full_url).path, body) from None
    except urllib.error.URLError as exc:
        raise GitHubError(
            0, urllib.parse.urlparse(request.full_url).path, str(exc.reason)
        ) from None


def _default_transport(request: urllib.request.Request) -> Any:
    try:
        with _opener.open(request, timeout=30) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:500]
        raise GitHubError(exc.code, urllib.parse.urlparse(request.full_url).path, body) from None
    except urllib.error.URLError as exc:
        raise GitHubError(
            0, urllib.parse.urlparse(request.full_url).path, str(exc.reason)
        ) from None
    return json.loads(payload) if payload else None


@dataclass
class GitHubClient:
    """Minimal REST surface the orchestrator needs. Mutations honor dry_run."""

    auth: TokenProvider
    transport: Transport = field(default=_default_transport)
    raw_transport: Callable[[urllib.request.Request], str] = field(default=_raw_transport)
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

    def create_pull(
        self, repo: str, title: str, head: str, base: str, body: str, draft: bool = False
    ) -> str:
        """Open a pull request; returns its html url."""
        if self.dry_run:
            log.info("[dry-run] PR on %s: %s (%s -> %s)", repo, title, head, base)
            return f"https://github.com/{repo}/pull/dry-run"
        path = f"/repos/{urllib.parse.quote(repo)}/pulls"
        data = self._request(
            "POST",
            path,
            {"title": title, "head": head, "base": base, "body": body, "draft": draft},
        )
        if not isinstance(data, dict) or "html_url" not in data:
            raise GitHubError(0, path, f"unexpected create_pull response: {str(data)[:200]}")
        return str(data["html_url"])

    def get_pull_request(self, repo: str, number: int) -> dict[str, Any]:
        path = f"/repos/{urllib.parse.quote(repo)}/pulls/{number}"
        return self._expect_dict(self._request("GET", path), path)

    def get_pull_request_diff(self, repo: str, number: int) -> str:
        """Fetch a PR's unified diff (uses the diff media type)."""
        path = f"/repos/{urllib.parse.quote(repo)}/pulls/{number}"
        request = urllib.request.Request(
            f"{API}{path}",
            method="GET",
            headers={
                "Authorization": f"Bearer {self.auth.token()}",
                "Accept": "application/vnd.github.v3.diff",
            },
        )
        return self.raw_transport(request)

    def get_pull_request_files(
        self, repo: str, number: int, max_pages: int = 5
    ) -> list[dict[str, Any]]:
        """Changed files of a PR (filename, status), following pagination."""
        out: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            path = (
                f"/repos/{urllib.parse.quote(repo)}/pulls/{number}/files?per_page=100&page={page}"
            )
            data = self._request("GET", path)
            if not isinstance(data, list) or not data:
                break
            out.extend(item for item in data if isinstance(item, dict))
            if len(data) < 100:
                break
        return out

    def get_file_content(self, repo: str, path: str, ref: str) -> str | None:
        """A file's text at `ref`, or None when it can't be provided.

        Best-effort by design (reviewer context): directories, submodules,
        files over the API's inline limit, and missing paths all return None
        rather than raising.
        """
        api_path = (
            f"/repos/{urllib.parse.quote(repo)}/contents/"
            f"{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref)}"
        )
        try:
            data = self._request("GET", api_path)
        except GitHubError as exc:
            if exc.status != 404:  # 404 = expected (path gone); the rest deserve a trace
                log.warning("context fetch failed for %s@%s: %s", path, ref, exc)
            return None
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            return None
        try:
            raw = base64.b64decode(data.get("content", ""))
            if b"\x00" in raw:
                return None  # binary
            return raw.decode("utf-8")
        except (ValueError, TypeError, UnicodeDecodeError):
            return None

    def list_comments(
        self, repo: str, issue_number: int, max_pages: int = 20
    ) -> list[dict[str, Any]]:
        """All comments on an issue/PR, following pagination."""
        comments: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            path = (
                f"/repos/{urllib.parse.quote(repo)}/issues/{issue_number}"
                f"/comments?per_page=100&page={page}"
            )
            data = self._request("GET", path)
            if not isinstance(data, list) or not data:
                break
            comments.extend(item for item in data if isinstance(item, dict))
            if len(data) < 100:
                break
        return comments

    def list_pr_reviews(self, repo: str, number: int, max_pages: int = 10) -> list[dict[str, Any]]:
        """Top-level PR reviews (the 'Review changes' submissions)."""
        return self._paginate(
            f"/repos/{urllib.parse.quote(repo)}/pulls/{number}/reviews", max_pages
        )

    def list_pr_review_comments(
        self, repo: str, number: int, max_pages: int = 10
    ) -> list[dict[str, Any]]:
        """Inline (Files changed) review comments."""
        return self._paginate(
            f"/repos/{urllib.parse.quote(repo)}/pulls/{number}/comments", max_pages
        )

    def _paginate(self, base_path: str, max_pages: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            data = self._request("GET", f"{base_path}?per_page=100&page={page}")
            if not isinstance(data, list) or not data:
                break
            items.extend(item for item in data if isinstance(item, dict))
            if len(data) < 100:
                break
        return items

    def upsert_comment(self, repo: str, issue_number: int, marker: str, body: str) -> None:
        """Post the comment, or edit the existing one carrying `marker`.

        Keeps the reviewer to one thread per PR however many times it runs.
        """
        if self.dry_run:
            log.info("[dry-run] upsert comment on %s#%s (%d chars)", repo, issue_number, len(body))
            return
        for comment in self.list_comments(repo, issue_number):
            # Only ever edit a bot's own comment: a human who quote-replies
            # copies the marker, and overwriting their text would be worse
            # than posting a second thread.
            author_type = str((comment.get("user") or {}).get("type", ""))
            if author_type.casefold() != "bot":
                continue
            if marker in str(comment.get("body", "")):
                path = f"/repos/{urllib.parse.quote(repo)}/issues/comments/{int(comment['id'])}"
                self._request("PATCH", path, {"body": body})
                return
        self.comment(repo, issue_number, body)

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
        skip = {"-C", "-c"}
        subcommand = next(
            (a for i, a in enumerate(args[1:], 1) if a not in skip and args[i - 1] not in skip),
            "?",
        )
        raise GitError(f"git {subcommand} failed: {detail}")
    return result.stdout.strip()


@dataclass
class Workspace:
    """A working clone the agent session edits; pushes happen orchestrator-side."""

    root: Path
    auth: TokenProvider | None = None
    dry_run: bool = False
    url: str | None = None

    def git(self, *args: str) -> str:
        """Run a local git subcommand: no credential, no child-spawning config."""
        return _run_git(["git", "-C", str(self.root), *SAFE_GIT_FLAGS, *args], _git_env(None))

    def git_network(self, *args: str) -> str:
        """Run a git subcommand that talks to the remote, with credentials."""
        if args and args[0] not in NETWORK_GIT_COMMANDS:
            raise ValueError(f"{args[0]!r} is not a network git command")
        token = self.auth.token() if self.auth is not None else None
        return _run_git(["git", "-C", str(self.root), *SAFE_GIT_FLAGS, *args], _git_env(token))

    def remote_url(self) -> str:
        """The remote URL as recorded at clone time, read token-free."""
        return self.git("config", "--get", "remote.origin.url")

    @classmethod
    def clone(
        cls,
        url: str,
        dest: Path,
        auth: TokenProvider | None = None,
        dry_run: bool = False,
    ) -> Workspace:
        token = auth.token() if auth is not None else None
        _run_git(["git", "clone", "--quiet", *SAFE_GIT_FLAGS, url, str(dest)], _git_env(token))
        return cls(root=dest, auth=auth, dry_run=dry_run, url=url)

    def branch(self, name: str) -> None:
        self.git("switch", "-c", name)

    def staged_paths(self) -> list[str]:
        """Staged paths, NUL-delimited so unicode/space/newline names survive."""
        output = self.git("diff", "--cached", "--name-only", "-z")
        return [entry for entry in output.split("\0") if entry]

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
        # Push to the URL captured at clone time: the session can rewrite
        # remote.origin.url, and "origin" would follow it.
        target = self.url or self.remote_url()
        self.git_network("push", target, "--", f"{branch}:{branch}")
