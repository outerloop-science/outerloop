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
import contextlib
import json
import logging
import os
import re
import stat
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

DEFAULT_BOT_LOGIN = "agentic-learning-bot"


def bot_login_from_env(default: str = DEFAULT_BOT_LOGIN) -> str:
    """The login the kernel posts and pushes as — the bot ACCOUNT under a PAT,
    the App's `<slug>[bot]` under App auth (docs/design/github-app-auth.md).
    `AUTORESEARCH_BOT_LOGIN` sets it for every role at once (the tick exports
    it, jobs inherit it); every own-comment filter, alarm-issue lookup and
    intake-claim scan keys on this string, so it must follow the credential."""
    return os.environ.get("AUTORESEARCH_BOT_LOGIN", "").strip() or default


def bot_aliases_from_env() -> tuple[str, ...]:
    """Former logins the kernel posted as (comma-separated
    `AUTORESEARCH_BOT_ALIASES`). Every issue, claim, alarm and PR created
    before an identity flip carries the OLD login; recognition stays keyed on
    login — never on the public markers, which anyone can paste — so the set
    of our logins is what a flip must widen (live: after the App flip the
    intake lane claimed the kernel's own research-log issue, authored by the
    PAT account)."""
    raw = os.environ.get("AUTORESEARCH_BOT_ALIASES", "")
    return tuple(dict.fromkeys(a.strip() for a in raw.split(",") if a.strip()))


def is_own_login(login: str, bot_login: str) -> bool:
    """Whether `login` is one of the kernel's identities: the login it posts
    as now, or a former one. Blank never matches (a blank identity must fail
    closed at every gate, as before)."""
    who = login.strip().casefold()
    if not who or not bot_login.strip():
        return False  # no identity of our own = no identity at all (aliases included)
    ours = {bot_login.strip().casefold()} | {a.casefold() for a in bot_aliases_from_env()}
    ours.discard("")
    return who in ours


log = logging.getLogger(__name__)

API = "https://api.github.com"
NETWORK_GIT_COMMANDS = frozenset({"clone", "fetch", "pull", "push", "ls-remote"})
# a remote that accepts the connection but never finishes must not hang the
# wake forever (the run would sit blocked until Slurm's walltime kill)
NETWORK_GIT_TIMEOUT_S = 900
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


# The one opener every API call shares — a redirect that changes host loses
# the Authorization header instead of forwarding the credential.
AUTH_SAFE_OPENER = urllib.request.build_opener(_NoAuthRedirect)


def _raw_transport(request: urllib.request.Request) -> str:
    """Fetch a non-JSON body (the diff media type returns text/plain)."""
    try:
        with AUTH_SAFE_OPENER.open(request, timeout=30) as response:
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
        with AUTH_SAFE_OPENER.open(request, timeout=30) as response:
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

    def find_open_pull_for_head(
        self, repo: str, head_branch: str, base: str
    ) -> dict[str, Any] | None:
        """The OPEN PR from `head_branch` INTO `base` (owner:branch, base-scoped
        so a same-branch PR to a different base is never matched), or None.
        Returns the raw PR dict (`html_url`, `number`, `draft`, …). Used for
        idempotency: a wake that died after opening the PR but before recording
        it reconciles to that PR instead of re-pushing (non-fast-forward) and
        opening a duplicate."""
        owner = repo.split("/")[0]
        query = urllib.parse.urlencode(
            {"head": f"{owner}:{head_branch}", "base": base, "state": "open"}
        )
        path = f"/repos/{urllib.parse.quote(repo)}/pulls?{query}"
        data = self._request("GET", path)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("html_url"):
                    return item
        return None

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
        (rewrite the measured numbers,
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

        Follow-up commits desync the report frozen into the body at publish.
        The addendum marks the body EDITED rather than rewriting history in
        place — and
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

    def enable_auto_merge(
        self, repo: str, number: int, method: str = "MERGE", expected_head: str = ""
    ) -> None:
        """Arm GitHub's auto-merge on a PR (a GraphQL-only capability).
        `expected_head` binds the arm to that head oid: GitHub refuses when
        the PR head has moved, so a push racing the caller's check can never
        be the thing that merges.

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
        variables: dict[str, Any] = {"pr": str(node_id), "method": method}
        if expected_head:
            mutation = (
                "mutation($pr: ID!, $method: PullRequestMergeMethod!, $head: GitObjectID!) {"
                " enablePullRequestAutoMerge(input: {pullRequestId: $pr, mergeMethod: $method,"
                " expectedHeadOid: $head}) { pullRequest { number } } }"
            )
            variables["head"] = expected_head
        else:
            mutation = (
                "mutation($pr: ID!, $method: PullRequestMergeMethod!) {"
                " enablePullRequestAutoMerge(input: {pullRequestId: $pr, mergeMethod: $method})"
                " { pullRequest { number } } }"
            )
        self._graphql(mutation, variables)

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

    def disable_auto_merge(self, repo: str, number: int) -> bool:
        """Disarm GitHub auto-merge (GraphQL). The follow-up lane calls this
        before pushing new commits to an auto-mode PR: an armed PR would
        merge the NEW head on green CI without a fresh gate/suite/panel
        (terra #171). Returns False when nothing was armed or on error."""
        if self.dry_run:
            log.info("[dry-run] disarm auto-merge on %s#%d", repo, number)
            return True
        try:
            node_id = self.get_pull_request(repo, number).get("node_id")
            if not node_id:
                return False
            mutation = (
                "mutation($pr: ID!) {"
                " disablePullRequestAutoMerge(input: {pullRequestId: $pr})"
                " { pullRequest { number } } }"
            )
            self._graphql(mutation, {"pr": str(node_id)})
            return True
        except GitHubError as exc:
            if "not enabled" in str(exc).casefold():
                # nothing was armed — the state we wanted; pushing is safe
                return True
            log.warning("auto-merge disarm on %s#%s failed: %s", repo, number, exc)
            return False

    def merge_pull(
        self, repo: str, number: int, method: str = "merge", expected_head: str = ""
    ) -> bool:
        """Directly merge a pull request (REST). Used only by AUTO merge mode
        when nothing is pending for auto-merge to arm against. `expected_head`
        rides the API's `sha` guard: GitHub refuses (409) when the head moved
        since the caller checked it."""
        if self.dry_run:
            log.info("[dry-run] merge %s#%s", repo, number)
            return True
        body: dict[str, Any] = {"merge_method": method}
        if expected_head:
            body["sha"] = expected_head
        try:
            self._request(
                "PUT",
                f"/repos/{urllib.parse.quote(repo)}/pulls/{number}/merge",
                body,
            )
            return True
        except GitHubError as exc:
            log.warning("direct merge of %s#%s failed: %s", repo, number, exc)
            return False

    def arm_auto_merge_auto_mode(self, repo: str, number: int, expected_head: str = "") -> bool:
        """AUTO merge mode (the contract's `merge: auto` dial): arm
        auto-merge so the PR merges when its required checks pass; when
        GitHub declines ONLY because nothing is pending (clean status),
        merge directly. Any other decline — auto-merge disabled in repo
        settings, missing permission — is a repo-owner control and STOPS
        here (terra #171: the broad fallback would have bulldozed a
        deliberately disabled auto-merge setting). The manual-mode
        review-required guard deliberately does not apply — the owner
        opted this repo in, and the gate/panel bound before publish."""
        methods = self.allowed_merge_methods(repo) or ["MERGE"]
        try:
            self.enable_auto_merge(repo, number, method=methods[0], expected_head=expected_head)
            return True
        except GitHubError as exc:
            if "clean status" not in str(exc).casefold():
                log.warning(
                    "auto-merge arming on %s#%s failed (%s); NOT merging "
                    "directly — the decline may be a repo-owner control",
                    repo,
                    number,
                    exc,
                )
                return False
            log.info(
                "auto-merge arming on %s#%s: PR already clean; merging directly",
                repo,
                number,
            )
        return self.merge_pull(repo, number, method=methods[0].lower(), expected_head=expected_head)

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

    def create_issue(self, repo: str, title: str, body: str) -> int:
        """Open an issue; returns its number (0 in dry-run)."""
        if self.dry_run:
            log.info("[dry-run] issue on %s: %s", repo, title)
            return 0
        data = self._request(
            "POST",
            f"/repos/{urllib.parse.quote(repo)}/issues",
            {"title": title, "body": body},
        )
        return int(data.get("number", 0)) if isinstance(data, dict) else 0

    def close_issue(self, repo: str, number: int) -> None:
        if self.dry_run:
            log.info("[dry-run] close issue %s#%s", repo, number)
            return
        self._request(
            "PATCH",
            f"/repos/{urllib.parse.quote(repo)}/issues/{number}",
            {"state": "closed"},
        )

    def ensure_branch(self, repo: str, branch: str) -> bool:
        """The branch exists (created from the default branch's head if not).
        Returns False only when creation failed."""
        quoted = urllib.parse.quote(repo)
        try:
            self._request("GET", f"/repos/{quoted}/git/ref/heads/{urllib.parse.quote(branch)}")
            return True
        except GitHubError:
            pass
        if self.dry_run:
            log.info("[dry-run] create branch %s on %s", branch, repo)
            return True
        try:
            default = self.default_branch(repo)
            ref = self._request(
                "GET", f"/repos/{quoted}/git/ref/heads/{urllib.parse.quote(default)}"
            )
            sha = ref["object"]["sha"] if isinstance(ref, dict) else ""
            self._request(
                "POST", f"/repos/{quoted}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha}
            )
            return True
        except (GitHubError, KeyError, TypeError) as exc:
            log.warning("could not create branch %s on %s: %s", branch, repo, exc)
            return False

    def put_file(self, repo: str, path: str, content: str, branch: str, message: str) -> str:
        """Create or update one file on `branch` via the contents API.
        Returns "created" | "updated" ("" on failure) — callers use the
        distinction for idempotency (an update means this artifact was
        already published once)."""
        if self.dry_run:
            log.info("[dry-run] put %s on %s@%s", path, repo, branch)
            return "created"
        quoted = urllib.parse.quote(repo)
        api = f"/repos/{quoted}/contents/{urllib.parse.quote(path)}"
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        try:
            existing = self._request("GET", f"{api}?ref={urllib.parse.quote(branch)}")
            if isinstance(existing, dict) and existing.get("sha"):
                body["sha"] = existing["sha"]
        except GitHubError:
            pass  # new file
        try:
            self._request("PUT", api, body)
            return "updated" if "sha" in body else "created"
        except GitHubError as exc:
            log.warning("could not put %s on %s@%s: %s", path, repo, branch, exc)
            return ""

    def branch_head(self, repo: str, branch: str) -> str | None:
        """The branch's current commit sha; "" when the branch does not
        exist (nothing to protect), None on an outage or malformed reply
        (the caller must not write unguarded)."""
        quoted = urllib.parse.quote(repo)
        try:
            ref = self._request(
                "GET", f"/repos/{quoted}/git/ref/heads/{urllib.parse.quote(branch)}"
            )
            return str(ref["object"]["sha"])
        except GitHubError as exc:
            return "" if getattr(exc, "status", None) == 404 else None
        except (KeyError, TypeError):
            return None

    def put_files(
        self,
        repo: str,
        files: dict[str, str],
        branch: str,
        message: str,
        expected_head: str | None = None,
    ) -> bool:
        """Create or update SEVERAL files on `branch` as ONE commit (git data
        API: blobs -> tree -> commit -> ref). All-or-nothing: True only when
        the ref moved; on failure nothing changed and the caller retries the
        whole batch next pass.

        Concurrency: pass `expected_head` (the head the caller SNAPSHOTTED
        before reading the files it compared against) and the batch refuses
        when the branch has moved since — a write that landed mid-pass is
        never buried under stale content. A write landing after this check
        still cannot be clobbered: the unforced ref update rejects any
        non-fast-forward."""
        if not files:
            return True
        if self.dry_run:
            log.info("[dry-run] batch put %d file(s) on %s@%s", len(files), repo, branch)
            return True
        quoted = urllib.parse.quote(repo)
        ref_path = f"/repos/{quoted}/git/refs/heads/{urllib.parse.quote(branch)}"
        try:
            ref = self._request(
                "GET", f"/repos/{quoted}/git/ref/heads/{urllib.parse.quote(branch)}"
            )
            base_sha = ref["object"]["sha"]
            if expected_head and base_sha != expected_head:
                log.warning(
                    "batch on %s@%s refused: head moved %s -> %s (retry next pass)",
                    repo,
                    branch,
                    expected_head[:9],
                    str(base_sha)[:9],
                )
                return False
            base_tree = self._request("GET", f"/repos/{quoted}/git/commits/{base_sha}")["tree"][
                "sha"
            ]
            entries = []
            for path, content in sorted(files.items()):
                blob = self._request(
                    "POST",
                    f"/repos/{quoted}/git/blobs",
                    {
                        "content": base64.b64encode(content.encode()).decode(),
                        "encoding": "base64",
                    },
                )
                entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
            tree = self._request(
                "POST", f"/repos/{quoted}/git/trees", {"base_tree": base_tree, "tree": entries}
            )
            commit = self._request(
                "POST",
                f"/repos/{quoted}/git/commits",
                {"message": message, "tree": tree["sha"], "parents": [base_sha]},
            )
            self._request("PATCH", ref_path, {"sha": commit["sha"]})
            return True
        except (GitHubError, KeyError, TypeError) as exc:
            log.warning(
                "batched put of %d file(s) on %s@%s failed: %s", len(files), repo, branch, exc
            )
            return False

    def comment(self, repo: str, issue_number: int, body: str) -> None:
        if self.dry_run:
            log.info("[dry-run] comment on %s#%s (%d chars)", repo, issue_number, len(body))
            return
        path = f"/repos/{urllib.parse.quote(repo)}/issues/{issue_number}/comments"
        self._request("POST", path, {"body": body})


_MINIMAL_GIT_CONFIG = b"[core]\n\trepositoryformatversion = 0\n"
# config sections that can REDIRECT a git operation to attacker-chosen refs
# or files: url.*.insteadOf rewrites, and include/includeIf that pull in more
# config (possibly a FIFO that would block git's own parse forever). The
# session-writable .git/config is the only config source under credential
# (global/system are /dev/null), so stripping these here disarms the whole
# class before any git subcommand — local or network — reads the file.
_REDIRECT_SECTIONS = ("url", "include", "includeif")
_SECTION_RE = re.compile(r"^\s*\[\s*([A-Za-z0-9.-]+)")


# The parts of a workspace's .git the kernel relies on after a session ran.
# 2026-09-03 an author copied the pack files into the container's private
# /tmp and symlinked objects/pack there, so the kernel's git on the host
# found refs with no objects behind them. The guard runs inside every kernel
# git call (Workspace.git / git_network), so no path can skip it.
# config and config.worktree are NOT here: _ensure_regular_config sanitizes
# them in place (a FIFO or symlink config is replaced, not fatal) and runs
# first, so the guard's own git subprocess never reads a tampered config.
_GIT_REGULAR_FILES = ("HEAD",)  # must exist and be regular files
_GIT_REGULAR_IF_PRESENT = (
    "index",
    "packed-refs",
    "MERGE_HEAD",
    "FETCH_HEAD",
    "info/exclude",  # the wake writes it; a symlink would carry the write elsewhere
)
_GIT_DIRS = ("objects", "refs")  # must exist and be directories
_GIT_DIRS_IF_PRESENT = ("objects/pack", "objects/info", "hooks", "info", "logs", "logs/refs")
# No symlink may appear among .git's own entries, nor ANYWHERE under the
# small control trees git writes to or reads from (refs, reflogs, hooks,
# info): a link there carries a ref update, a reflog append, or a hook read
# outside the workspace. The object store is checked one level deep plus
# its pack/info dirs (loose-object trees are large; a missing object is
# caught by the HEAD readability check below).
_GIT_NO_SYMLINK_TREES = ("refs", "logs", "hooks", "info")
_GIT_NO_SYMLINK_ENTRIES = ("", "objects", "objects/info", "objects/pack")


def _altered(what: str) -> GitError:
    return GitError(f"workspace .git altered by the session: {what}")


def ensure_regular_git_dir(root: Path | None) -> None:
    """Refuse a workspace whose .git a session reshaped. `.git` must be a
    real directory (no symlink, no gitdir file); HEAD and config must be
    regular files and the other control files regular when present (a FIFO
    would hang the next git call); objects and refs must be directories, as
    must objects/pack when present; object alternates are never ours. A
    root with no .git at all is left to git's own error."""
    if root is None:
        return
    git_dir = Path(root) / ".git"
    try:
        st = os.lstat(git_dir)
    except OSError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise _altered(".git is a symlink")
    if not stat.S_ISDIR(st.st_mode):
        raise _altered(".git is not a directory (a gitdir file)")
    _ensure_regular_config(root)  # before any git subprocess below reads it

    def check(rel: str, want_dir: bool, required: bool) -> None:
        try:
            st = os.lstat(git_dir / rel)
        except OSError:
            if required:
                raise _altered(f".git/{rel} is missing") from None
            return
        if stat.S_ISLNK(st.st_mode):
            raise _altered(f".git/{rel} is a symlink")
        if want_dir and not stat.S_ISDIR(st.st_mode):
            raise _altered(f".git/{rel} is not a directory")
        if not want_dir and not stat.S_ISREG(st.st_mode):
            raise _altered(f".git/{rel} is not a regular file")

    for rel in _GIT_REGULAR_FILES:
        check(rel, want_dir=False, required=True)
    for rel in _GIT_REGULAR_IF_PRESENT:
        check(rel, want_dir=False, required=False)
    for rel in _GIT_DIRS:
        check(rel, want_dir=True, required=True)
    for rel in _GIT_DIRS_IF_PRESENT:
        check(rel, want_dir=True, required=False)
    for rel in _GIT_NO_SYMLINK_ENTRIES:
        try:
            with os.scandir(git_dir / rel if rel else git_dir) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        shown = f"{rel}/{entry.name}" if rel else entry.name
                        raise _altered(f".git/{shown} is a symlink")
        except OSError:
            continue
    for rel in _GIT_NO_SYMLINK_TREES:
        top = git_dir / rel
        if not top.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
            for name in (*dirnames, *filenames):
                if os.path.islink(os.path.join(dirpath, name)):
                    shown = os.path.relpath(os.path.join(dirpath, name), git_dir)
                    raise _altered(f".git/{shown} is a symlink")
    if os.path.lexists(git_dir / "objects" / "info" / "alternates"):
        raise _altered("object alternates are present")
    # The structure can be intact with the objects gone (pack files deleted,
    # or moved and the link removed): HEAD's commit must still be readable.
    # An unborn HEAD (a fresh repository whose branch has no commit yet) has
    # nothing to check; a HEAD whose branch ref vanished from a repository
    # that has other refs is an erased ref, not an unborn one.
    sha = _head_commit_sha(git_dir)
    if sha is None and _has_any_ref(git_dir):
        raise _altered("HEAD names a branch whose ref is missing")
    if sha is not None:
        try:
            _run_git(
                ["git", "-C", str(root), *SAFE_GIT_FLAGS, "cat-file", "-e", f"{sha}^{{commit}}"],
                _git_env(None, Path(root)),
            )
        except GitError:
            raise _altered(
                "HEAD's commit is unreadable: the object store was emptied or moved"
            ) from None


def _has_any_ref(git_dir: Path) -> bool:
    """Does the repository hold any ref at all (loose or packed)? A clone
    always does; only a fresh `git init` with no commit has none."""
    if (git_dir / "packed-refs").is_file():
        return True
    for sub in ("heads", "remotes", "tags"):
        top = git_dir / "refs" / sub
        if top.is_dir() and any(p.is_file() for p in top.rglob("*")):
            return True
    return False


def _head_commit_sha(git_dir: Path) -> str | None:
    """The sha HEAD names, from the loose ref file or packed-refs, or None when
    HEAD is detached-invalid or its branch is unborn (nothing to verify)."""
    try:
        head = (git_dir / "HEAD").read_text().strip()
    except OSError:
        return None
    if not head.startswith("ref: "):
        return head if re.fullmatch(r"[0-9a-f]{40,64}", head) else None
    ref = head[5:].strip()
    try:
        return (git_dir / ref).read_text().strip() or None
    except OSError:
        pass
    try:
        for line in (git_dir / "packed-refs").read_text().splitlines():
            if line.endswith(" " + ref):
                return line.split(" ", 1)[0]
    except OSError:
        pass
    return None


def _ensure_regular_config(root: Path | None) -> None:
    """Sanitize .git/config before any git command reads it. A session can
    (a) replace the file with a FIFO/symlink/device — git's own parse, or a
    read, would BLOCK forever with no writer, hanging the wake — or (b) leave
    a regular file that INCLUDES a FIFO or rewrites URLs. Non-regular files are
    replaced with a minimal config; a regular file's redirect sections
    (url/include/includeIf) are stripped in place. A clean config is left
    untouched (a cheap lstat and, at most, one small read)."""
    if root is None:
        return
    # every session-writable config file git reads under credential: the
    # main local config and the per-worktree config (extensions.worktreeConfig).
    # global/system are /dev/null via the env, so this list is exhaustive.
    for cfg in (root / ".git" / "config", root / ".git" / "config.worktree"):
        _sanitize_one_config(cfg)


def _sanitize_one_config(cfg: Path) -> None:
    try:
        st = os.lstat(cfg)
    except OSError:
        return

    def _write(data: bytes) -> None:
        with contextlib.suppress(OSError):
            if cfg.exists():
                cfg.unlink()
            fd = os.open(cfg, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)

    if not stat.S_ISREG(st.st_mode) or st.st_size > 1_000_000:
        _write(_MINIMAL_GIT_CONFIG)  # FIFO/symlink/device/oversize: hostile
        return
    # a regular file never blocks a read (reading does NOT follow includes),
    # so this is safe; only the redirect sections are removed
    try:
        text = cfg.read_text(errors="replace")
    except OSError:
        return
    kept: list[str] = []
    dropping = False
    changed = False
    for line in text.splitlines(keepends=True):
        m = _SECTION_RE.match(line)
        if m is not None:  # a new section header decides the next block
            dropping = m.group(1).split(".", 1)[0].casefold() in _REDIRECT_SECTIONS
        if dropping:
            changed = True
            continue
        kept.append(line)
    if changed:
        _write("".join(kept).encode())


def _filter_override_pairs(root: Path | None) -> list[tuple[str, str]]:
    """GIT_CONFIG pairs that neutralize every filter driver the REPO config
    defines (each overridden to a passthrough) plus attribute files. The
    workspace's .git/config and .gitattributes are session-written, so a
    checkout there must never execute a configured smudge/clean/process
    command with the orchestrator's permissions — the same neutralization the
    dispatched job script applies. Env-var pairs, not `-c`: a driver name
    containing '=' or dots survives correctly."""
    pairs: list[tuple[str, str]] = [("core.attributesFile", "/dev/null")]
    if root is None:
        return pairs
    listing = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "config",
            "-z",
            "--get-regexp",
            r"^filter\..*\.(clean|smudge|process)$",
        ],
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_COUNT": "0",
            "GIT_TERMINAL_PROMPT": "0",
        },
        capture_output=True,
        timeout=30,
    )
    # BYTES + surrogateescape, never text=True: the config is session-written,
    # so a non-UTF-8 byte in a value must not crash the git call — and the
    # surrogates roundtrip through the env (os.fsencode), so an override key
    # still matches a weird-byte driver name EXACTLY (a lossy decode would
    # silently fail to neutralize that driver).
    # -z: NUL-separated records, each "key\nvalue" — a value containing a
    # newline can never masquerade as a second record.
    for record in listing.stdout.decode("utf-8", "surrogateescape").split("\0"):
        key = record.split("\n", 1)[0]
        if not key.startswith("filter.") or "." not in key[len("filter.") :]:
            continue
        driver = key[len("filter.") : key.rindex(".")]
        pairs += [
            (f"filter.{driver}.clean", "cat"),
            (f"filter.{driver}.smudge", "cat"),
            (f"filter.{driver}.process", ""),
        ]
    return pairs


def _git_env(token: str | None, root: Path | None = None) -> dict[str, str]:
    """Environment for a git invocation: host global/system config never
    loads (a host-configured filter driver must not be selectable by a
    session-written .gitattributes), repo-defined filter drivers are
    overridden to passthrough, and the token — present only for network
    subcommands — rides an env-injected header."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    # The session owns the clone and can write refs/replace/*, which git
    # honors in ancestry checks (merge-base) and object lookups
    # (rev-parse sha:path) — either would let a doctored object stand in
    # for a real one during post-session verification. The kernel never
    # uses replace refs; disable them on every invocation.
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    pairs = _filter_override_pairs(root)
    if token is not None:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        pairs.append(("http.https://github.com/.extraheader", f"Authorization: Basic {basic}"))
    env["GIT_CONFIG_COUNT"] = str(len(pairs))
    for i, (k, v) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"] = k
        env[f"GIT_CONFIG_VALUE_{i}"] = v
    return env


def _run_git(args: list[str], env: dict[str, str], timeout: float | None = None) -> str:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, env=env, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git timed out after {timeout:.0f}s: {' '.join(args[3:5])}") from exc
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
        """Run a local git subcommand: no credential, no child-spawning config,
        repo-defined filter drivers neutralized. A session-reshaped .git is
        refused before git runs (ensure_regular_git_dir, which also
        sanitizes the config first)."""
        ensure_regular_git_dir(self.root)
        return _run_git(
            ["git", "-C", str(self.root), *SAFE_GIT_FLAGS, *args], _git_env(None, self.root)
        )

    def git_network(self, *args: str) -> str:
        """Run a git subcommand that talks to the remote, with credentials.
        The config is sanitized first (redirect sections stripped), and the
        fetch/push pass an explicit URL + refspec, so no session-controlled
        remote or rewrite can steer a credentialed op."""
        if args and args[0] not in NETWORK_GIT_COMMANDS:
            raise ValueError(f"{args[0]!r} is not a network git command")
        ensure_regular_git_dir(self.root)  # sanitizes the config first, too
        token = self.auth.token() if self.auth is not None else None
        return _run_git(
            ["git", "-C", str(self.root), *SAFE_GIT_FLAGS, *args],
            _git_env(token, self.root),
            timeout=NETWORK_GIT_TIMEOUT_S,
        )

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

    def fetch_origin(self) -> None:
        """Refresh refs/remotes/origin/* from the URL captured at clone time —
        never the "origin" remote, whose url and uploadpack live in
        session-writable .git/config. The credential is host-scoped
        (extraheader), so a rewritten URL could not receive it, but the
        CONTENT must come from the canonical repo too: a poisoned fetch
        source would forge what origin/<base> means to every downstream
        comparison."""
        if self.dry_run:
            log.info("[dry-run] fetch into %s", self.root)
            return
        target = self.url or self.remote_url()
        self.git_network("fetch", "--prune", target, "--", "+refs/heads/*:refs/remotes/origin/*")

    def fetch_branch(self, branch: str) -> None:
        """Fetch one branch into FETCH_HEAD from the canonical URL (resolved
        before the clean-config window, never the mutable "origin" remote)."""
        target = self.url or self.remote_url()
        self.git_network("fetch", target, branch)

    def push(self, branch: str) -> None:
        if self.dry_run:
            log.info("[dry-run] push %s from %s", branch, self.root)
            return
        # Push to the URL captured at clone time: the session can rewrite
        # remote.origin.url, and "origin" would follow it.
        target = self.url or self.remote_url()
        self.git_network("push", target, "--", f"{branch}:{branch}")
