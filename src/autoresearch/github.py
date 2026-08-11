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
import re
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

    def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        path = f"/repos/{urllib.parse.quote(repo)}/issues/{number}"
        return self._expect_dict(self._request("GET", path), path)

    def list_open_issues(self, repo: str, max_pages: int = 3) -> list[dict[str, Any]]:
        """Open issues (PRs excluded — the issues API mixes them in)."""
        items = self._paginate(f"/repos/{urllib.parse.quote(repo)}/issues", max_pages)
        return [i for i in items if "pull_request" not in i]

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

    BODY_EDIT_MARKER = "<!-- autoresearch:body-edit -->"
    # the orchestrator-owned candidate row in pr_body's results table
    # \r-tolerant: a human web-UI edit can normalize the body to CRLF
    _CANDIDATE_ROW = re.compile(r"^\| candidate \| .* \|(\r?)$", re.MULTILINE)

    def update_candidate_row(
        self, repo: str, number: int, candidate: float, digits: int | None = None
    ) -> bool:
        """Rewrite the results table's candidate row in place (PATCH).

        The row is orchestrator-owned and mechanical — the ONE part of the
        body that must never go stale when a follow-up push re-measures
        (maintainer decision 2026-08-09: rewrite the measured numbers,
        never the narrative). Returns False when the row is not found
        (older body formats), in which case the Edit addendum still
        carries the number.
        """
        if self.dry_run:
            log.info("[dry-run] update candidate row %s#%d -> %s", repo, number, candidate)
            return True
        path = f"/repos/{urllib.parse.quote(repo)}/pulls/{number}"
        current = str(self._expect_dict(self._request("GET", path), path).get("body") or "")
        # only the FIRST match, and only in the orchestrator's preamble
        # (before the report section) — agent report text could contain a
        # lookalike row, and it must stay untouched
        head, sep, tail = current.partition("## Research report")
        if not sep:
            # No report heading -> the preamble boundary is gone (human
            # body edit?): fail CLOSED rather than rewrite report text —
            # the Edit addendum already carries the number.
            return False
        if not self._CANDIDATE_ROW.search(head):
            return False
        from autoresearch.progress import fmt_metric

        head = self._CANDIDATE_ROW.sub(
            rf"| candidate | {fmt_metric(candidate, digits)} |\1", head, count=1
        )
        self._request("PATCH", path, {"body": f"{head}{sep}{tail}"})
        return True

    def append_pull_body(self, repo: str, number: int, addendum: str) -> None:
        """Upsert an EDIT addendum onto a PR body (read-modify-write PATCH).

        Follow-up commits desync the report frozen into the body at publish
        (found live on the first verifier exchange: round 2 reviewed a body
        describing code the follow-up had already replaced). The addendum
        marks the body EDITED rather than rewriting history in place — and
        REPLACES any previous addendum (marker-delimited) instead of
        stacking one per round, so the body stays bounded and always points
        at the latest state.
        """
        if self.dry_run:
            log.info("[dry-run] upsert PR body edit %s#%d (%d chars)", repo, number, len(addendum))
            return
        path = f"/repos/{urllib.parse.quote(repo)}/pulls/{number}"
        current = str(self._expect_dict(self._request("GET", path), path).get("body") or "")
        # Strip ONLY a previous addendum of ours: marker followed by our
        # exact format. Agent-authored report text could contain the marker
        # string (it is public), and splitting on it blindly would truncate
        # the frozen report — the history this method exists to preserve.
        base = current
        idx = current.rfind(self.BODY_EDIT_MARKER)
        if idx != -1 and current[idx + len(self.BODY_EDIT_MARKER) :].lstrip().startswith("---"):
            base = current[:idx].rstrip()
        self._request("PATCH", path, {"body": f"{base}\n\n{self.BODY_EDIT_MARKER}\n{addendum}"})

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        data = self._request("POST", "/graphql", {"query": query, "variables": variables})
        if not isinstance(data, dict):
            raise GitHubError(0, "/graphql", f"expected an object, got {type(data).__name__}")
        # GraphQL reports failures inside a 200; status 0 keeps callers'
        # retry/permanence classification honest (this is not an HTTP 200
        # success and not an HTTP error either).
        if data.get("errors"):
            raise GitHubError(0, "/graphql", str(data["errors"])[:300])
        inner = data.get("data")
        return inner if isinstance(inner, dict) else {}

    def review_decision(self, repo: str, number: int) -> str:
        """The PR's reviewDecision ("REVIEW_REQUIRED", "APPROVED",
        "CHANGES_REQUESTED", or "" when the base branch requires no
        reviews). GraphQL-only; readable with normal read permissions."""
        owner, _, name = repo.partition("/")
        query = (
            "query($owner: String!, $name: String!, $number: Int!) {"
            " repository(owner: $owner, name: $name) {"
            " pullRequest(number: $number) { reviewDecision } } }"
        )
        data = self._graphql(query, {"owner": owner, "name": name, "number": number})
        pr = (data.get("repository") or {}).get("pullRequest") or {}
        return str(pr.get("reviewDecision") or "")

    def allowed_merge_methods(self, repo: str) -> list[str]:
        """Merge methods the repo permits, in our preference order (merge
        commits first — trailer/provenance-preserving)."""
        path = f"/repos/{urllib.parse.quote(repo)}"
        settings = self._expect_dict(self._request("GET", path), path)
        order = [
            ("MERGE", "allow_merge_commit"),
            ("SQUASH", "allow_squash_merge"),
            ("REBASE", "allow_rebase_merge"),
        ]
        return [m for m, key in order if settings.get(key)]

    def enable_auto_merge(self, repo: str, number: int, method: str = "MERGE") -> None:
        """Arm GitHub's auto-merge on a PR (a GraphQL-only capability).

        Arming does not merge anything: it hands the merge to whatever
        branch protection still requires. Callers that must preserve the
        bot-never-merges rule should use arm_auto_merge_when_review_required
        instead of calling this directly. Repos that also run the follow-up
        lane should have dismiss-stale-reviews enabled, so a bot push after
        arming re-requires a human look instead of merging unseen code.
        """
        if self.dry_run:
            log.info("[dry-run] arm auto-merge on %s#%d", repo, number)
            return
        pr_path = f"/repos/{urllib.parse.quote(repo)}/pulls/{number}"
        node_id = self.get_pull_request(repo, number).get("node_id")
        if not node_id:
            raise GitHubError(0, pr_path, "no node_id in PR payload")
        mutation = (
            "mutation($pr: ID!, $method: PullRequestMergeMethod!) {"
            " enablePullRequestAutoMerge(input: {pullRequestId: $pr, mergeMethod: $method})"
            " { pullRequest { number } } }"
        )
        self._graphql(mutation, {"pr": str(node_id), "method": method})

    def arm_auto_merge_when_review_required(self, repo: str, number: int) -> bool:
        """Arm auto-merge ONLY when branch protection makes a human review
        the missing condition (reviewDecision == REVIEW_REQUIRED).

        On a repo without required reviews, arming would merge the bot's own
        PR the moment CI is green — no human ever acts. That would break the
        bot-never-merges rule via nothing but per-repo config drift, so the
        guard lives here, in code. The merge method falls back through what
        the repo allows (merge-commit preferred: it preserves the Agent
        trailers and commit sequence as provenance)."""
        decision = self.review_decision(repo, number)
        if decision != "REVIEW_REQUIRED":
            log.warning(
                "not arming auto-merge on %s#%d: reviewDecision=%r "
                "(no required human review would stand between arming and merging)",
                repo,
                number,
                decision,
            )
            return False
        methods = self.allowed_merge_methods(repo) or ["MERGE"]
        self.enable_auto_merge(repo, number, method=methods[0])
        return True

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

    def list_directory(self, repo: str, path: str, ref: str) -> list[dict[str, Any]]:
        """Entries of a directory at a ref ([] when absent or not a dir).

        ONE request, deliberately: the contents API returns the whole
        listing for a directory (capped ~1,000 entries) and IGNORES
        page/per_page — a pagination loop would re-fetch the same list and
        accumulate duplicates. Trees larger than the cap need the git
        trees API; callers here read a handful of entries.
        """
        query = urllib.parse.urlencode({"ref": ref})
        api_path = f"/repos/{urllib.parse.quote(repo)}/contents/{urllib.parse.quote(path)}?{query}"
        try:
            data = self._request("GET", api_path)
        except GitHubError as exc:
            if exc.status == 404:
                return []
            raise
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

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

    def create_pr_review(
        self,
        repo: str,
        number: int,
        body: str,
        comments: list[dict[str, Any]] | None = None,
    ) -> None:
        """Post a PR review with optional inline comments.

        The event is hard-coded to COMMENT: this client must never be able
        to approve or request changes — a Write-role bot review that blocks
        or endorses would hand a model role the exact powers the
        constitution denies it.
        """
        if self.dry_run:
            log.info(
                "[dry-run] review on %s#%s (%d chars, %d inline)",
                repo,
                number,
                len(body),
                len(comments or []),
            )
            return
        payload: dict[str, Any] = {"body": body, "event": "COMMENT"}
        if comments:
            payload["comments"] = comments
        self._request(
            "POST",
            f"/repos/{urllib.parse.quote(repo)}/pulls/{number}/reviews",
            payload,
        )

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
