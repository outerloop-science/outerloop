import base64
import json
import subprocess
import threading
import urllib.request
from pathlib import Path
from typing import cast

import pytest

from outerloop.github import FileTokenProvider, GitHubClient, GitHubError, Workspace


class FakeTransport:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request) -> object:
        self.requests.append(request)
        return self.responses.pop(0)


@pytest.fixture
def provider(tmp_path: Path) -> FileTokenProvider:
    pat = tmp_path / "pat"
    pat.write_text("github_pat_test123\n")
    pat.chmod(0o600)
    return FileTokenProvider(pat)


def test_token_provider_strips(provider: FileTokenProvider) -> None:
    assert provider.token() == "github_pat_test123"


def test_default_branch_and_headers(provider: FileTokenProvider) -> None:
    transport = FakeTransport([{"default_branch": "main"}])
    client = GitHubClient(auth=provider, transport=transport)
    assert client.default_branch("org/repo") == "main"
    request = transport.requests[0]
    assert request.full_url == "https://api.github.com/repos/org/repo"
    assert request.get_header("Authorization") == "Bearer github_pat_test123"


def test_branch_sha_reads_current_ref(provider: FileTokenProvider) -> None:
    transport = FakeTransport([{"object": {"sha": "new-tip"}}])
    client = GitHubClient(auth=provider, transport=transport)
    assert client.branch_sha("org/repo", "release/next") == "new-tip"
    assert transport.requests[0].get_method() == "GET"
    assert transport.requests[0].full_url == (
        "https://api.github.com/repos/org/repo/git/ref/heads/release%2Fnext"
    )


def test_commit_tree_reads_commit_tree(provider):
    transport = FakeTransport([{"sha": "commit", "tree": {"sha": "root-tree"}}])
    client = GitHubClient(auth=provider, transport=transport)
    assert client.commit_tree("org/repo", "feature/head") == "root-tree"
    assert transport.requests[0].get_method() == "GET"
    assert transport.requests[0].full_url == (
        "https://api.github.com/repos/org/repo/git/commits/feature%2Fhead"
    )


def test_commit_tree_missing_commit(provider, monkeypatch):
    from email.message import Message

    def missing(request, **kwargs):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", Message(), None)

    monkeypatch.setattr("outerloop.github.AUTH_SAFE_OPENER.open", missing)
    client = GitHubClient(auth=provider)
    with pytest.raises(GitHubError) as error:
        client.commit_tree("org/repo", "missing")
    assert error.value.status == 404


@pytest.mark.parametrize("response", [None, {}, {"tree": {}}, {"tree": {"sha": ""}}])
def test_commit_tree_rejects_missing_tree(provider, response):
    client = GitHubClient(auth=provider, transport=FakeTransport([response]))
    with pytest.raises(GitHubError):
        client.commit_tree("org/repo", "commit")


def test_public_tree_and_create_ref(provider, caplog):
    transport = FakeTransport([{"sha": "tree"}, {}])
    client = GitHubClient(auth=provider, transport=transport)
    assert client.get_tree("org/repo", "feature/head") == {"sha": "tree"}
    assert transport.requests[-1].full_url.endswith("/git/trees/feature%2Fhead?recursive=1")
    client.create_ref("org/repo", "refs/heads/research-log", "pin")
    request = transport.requests[-1]
    assert request.get_method() == "POST"
    assert isinstance(request.data, bytes)
    assert json.loads(request.data) == {"ref": "refs/heads/research-log", "sha": "pin"}
    client.dry_run = True
    client.create_ref("org/repo", "refs/heads/research-log", "pin")
    assert len(transport.requests) == 2
    assert provider.token() not in caplog.text


@pytest.mark.parametrize("response", [None, [], {}, {"object": {}}, {"object": {"sha": ""}}])
def test_branch_sha_rejects_missing_tip(provider: FileTokenProvider, response) -> None:
    client = GitHubClient(auth=provider, transport=FakeTransport([response]))
    with pytest.raises(GitHubError):
        client.branch_sha("org/repo", "main")


@pytest.mark.parametrize("status", ["ahead", "behind", "diverged", "identical"])
def test_compare_and_head_contains(provider: FileTokenProvider, status) -> None:
    response = {"status": status, "ahead_by": 2, "behind_by": 3, "commits": []}
    transport = FakeTransport([response, response])
    client = GitHubClient(auth=provider, transport=transport)
    assert client.compare("org/repo", "release/next", "head") == {
        "status": status,
        "ahead_by": 2,
        "behind_by": 3,
    }
    assert client.head_contains("org/repo", "release/next", "head") == (
        status in ("ahead", "identical")
    )
    assert transport.requests[0].get_method() == "GET"
    assert transport.requests[0].full_url == (
        "https://api.github.com/repos/org/repo/compare/release%2Fnext...head"
    )


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        {},
        {"status": 1, "ahead_by": 0, "behind_by": 0},
        {"status": "unknown", "ahead_by": 0, "behind_by": 0},
        {"status": "ahead", "ahead_by": "2", "behind_by": 0},
        {"status": "ahead", "ahead_by": True, "behind_by": 0},
        {"status": "ahead", "ahead_by": 2, "behind_by": None},
        {"status": "ahead", "ahead_by": 2, "behind_by": False},
    ],
)
def test_compare_rejects_malformed_body(provider: FileTokenProvider, response) -> None:
    client = GitHubClient(auth=provider, transport=FakeTransport([response]))
    with pytest.raises(GitHubError):
        client.head_contains("org/repo", "base", "head")


def test_compare_surfaces_unavailable(provider: FileTokenProvider, monkeypatch) -> None:
    import io
    import urllib.error
    from email.message import Message

    def unavailable(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 500, "unavailable", Message(), io.BytesIO(b"temporarily unavailable")
        )

    from outerloop.github import AUTH_SAFE_OPENER

    monkeypatch.setattr(AUTH_SAFE_OPENER, "open", unavailable)
    with pytest.raises(GitHubError) as error:
        GitHubClient(auth=provider).head_contains("org/repo", "base", "head")
    assert error.value.status == 500


def test_get_file_decodes_base64(provider: FileTokenProvider) -> None:
    content = base64.b64encode(b"benchmarks: []\n").decode()
    transport = FakeTransport([{"type": "file", "encoding": "base64", "content": content}])
    client = GitHubClient(auth=provider, transport=transport)
    assert client.get_file("org/repo", ".outerloop.yaml", "main") == "benchmarks: []\n"
    assert "ref=main" in transport.requests[0].full_url


def test_create_pr_posts_body(provider: FileTokenProvider) -> None:
    transport = FakeTransport([{"number": 7}])
    client = GitHubClient(auth=provider, transport=transport)
    assert client.create_pr("org/repo", "feat/x", "main", "Title", "Body") == 7
    request = transport.requests[0]
    assert request.get_method() == "POST"
    assert isinstance(request.data, bytes)
    assert json.loads(request.data.decode()) == {
        "title": "Title",
        "head": "feat/x",
        "base": "main",
        "body": "Body",
    }


def test_update_issue_sets_the_title_only_when_given(provider: FileTokenProvider) -> None:
    transport = FakeTransport([{}, {}])
    client = GitHubClient(auth=provider, transport=transport)
    client.update_issue("org/repo", 9, "new body", title="Maintainer digest — 2026-09-08")
    req = transport.requests[0]
    assert req.get_method() == "PATCH"
    assert req.full_url == "https://api.github.com/repos/org/repo/issues/9"
    assert isinstance(req.data, bytes)
    assert json.loads(req.data.decode()) == {
        "body": "new body",
        "title": "Maintainer digest — 2026-09-08",
    }
    client.update_issue("org/repo", 9, "body only")  # no title → body only, title untouched
    assert isinstance(transport.requests[1].data, bytes)
    assert json.loads(transport.requests[1].data.decode()) == {"body": "body only"}


def test_dry_run_mutations_touch_nothing(provider: FileTokenProvider) -> None:
    transport = FakeTransport([])
    client = GitHubClient(auth=provider, transport=transport, dry_run=True)
    assert client.create_pr("org/repo", "h", "b", "t", "b") is None
    client.comment("org/repo", 1, "hello")
    assert transport.requests == []


def _make_origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True)
    (seed / "README.md").write_text("seed\n")
    for cmd in (
        ["add", "-A"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed"],
        ["push", "-q", "origin", "main"],
    ):
        subprocess.run(["git", "-C", str(seed), *cmd], check=True)
    return origin


def test_workspace_clone_branch_commit_push(tmp_path: Path) -> None:
    origin = _make_origin(tmp_path)
    ws = Workspace.clone(f"file://{origin}", tmp_path / "work")
    ws.branch("feat/auto/test")
    (ws.root / "new.txt").write_text("x\n")
    ws.commit_all("Test commit", author="agentic-learning-bot")
    ws.push("feat/auto/test")
    log = subprocess.run(
        ["git", "-C", str(origin), "log", "feat/auto/test", "--format=%s <%ae>"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "Test commit <agentic-learning-bot@users.noreply.github.com>" in log


def test_workspace_dry_run_push_stays_local(tmp_path: Path) -> None:
    origin = _make_origin(tmp_path)
    ws = Workspace.clone(f"file://{origin}", tmp_path / "work", dry_run=True)
    ws.branch("feat/auto/nope")
    (ws.root / "new.txt").write_text("x\n")
    ws.commit_all("Local only", author="bot")
    ws.push("feat/auto/nope")
    branches = subprocess.run(
        ["git", "-C", str(origin), "branch"], capture_output=True, text=True, check=True
    ).stdout
    assert "feat/auto/nope" not in branches


def test_env_token_provider(monkeypatch) -> None:
    from outerloop.github import EnvTokenProvider

    monkeypatch.setenv("SOME_TOKEN", " tok \n")
    assert EnvTokenProvider("SOME_TOKEN").token() == "tok"
    monkeypatch.setenv("SOME_TOKEN", "")
    with pytest.raises(ValueError, match="unset or empty"):
        EnvTokenProvider("SOME_TOKEN").token()


def test_upsert_comment_edits_existing_marked_comment(provider: FileTokenProvider) -> None:
    marker = "<!-- m -->"
    transport = FakeTransport([[{"id": 42, "body": f"{marker} old", "user": {"type": "Bot"}}], {}])
    client = GitHubClient(auth=provider, transport=transport)
    client.upsert_comment("org/repo", 7, marker, f"{marker} new")
    edit = transport.requests[-1]
    assert edit.get_method() == "PATCH"
    assert "/issues/comments/42" in edit.full_url


def test_upsert_comment_creates_when_absent(provider: FileTokenProvider) -> None:
    transport = FakeTransport([[{"id": 1, "body": "unrelated", "user": {"type": "Bot"}}], {}])
    client = GitHubClient(auth=provider, transport=transport)
    client.upsert_comment("org/repo", 7, "<!-- m -->", "body")
    create = transport.requests[-1]
    assert create.get_method() == "POST"
    assert create.full_url.endswith("/issues/7/comments")


def test_diff_uses_raw_transport_and_diff_media_type(provider: FileTokenProvider) -> None:
    """The diff is text/plain — a JSON-decoding transport would break it."""
    seen: list[urllib.request.Request] = []

    def raw(request: urllib.request.Request) -> str:
        seen.append(request)
        return "--- a/x\n+++ b/x\n"

    client = GitHubClient(auth=provider, transport=FakeTransport([]), raw_transport=raw)
    assert client.get_pull_request_diff("org/repo", 3).startswith("--- a/x")
    assert seen[0].get_header("Accept") == "application/vnd.github.v3.diff"


def test_default_raw_transport_returns_text_not_json() -> None:
    """Regression: the diff path must not go through json.loads."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from outerloop.github import _raw_transport

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"--- a/x\n+++ b/x\n")

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/d")
        assert _raw_transport(request).startswith("--- a/x")
    finally:
        server.shutdown()


def test_upsert_never_overwrites_a_human_comment(provider: FileTokenProvider) -> None:
    """A human who quote-replies copies the marker; editing their comment is worse
    than posting a second one."""
    marker = "<!-- m -->"
    transport = FakeTransport(
        [[{"id": 9, "body": f"quoting: {marker}", "user": {"type": "User"}}], {}]
    )
    client = GitHubClient(auth=provider, transport=transport)
    client.upsert_comment("org/repo", 7, marker, "body")
    assert transport.requests[-1].get_method() == "POST"


def test_list_comments_paginates(provider: FileTokenProvider) -> None:
    page1 = [{"id": i, "body": "x", "user": {"type": "Bot"}} for i in range(100)]
    page2 = [{"id": 100, "body": "marked", "user": {"type": "Bot"}}]
    transport = FakeTransport([page1, page2])
    client = GitHubClient(auth=provider, transport=transport)
    assert len(client.list_comments("org/repo", 7)) == 101


def test_pull_request_files_paginate(provider: FileTokenProvider) -> None:
    page1 = [{"filename": f"f{i}.py", "status": "modified"} for i in range(100)]
    page2 = [{"filename": "last.py", "status": "added"}]
    transport = FakeTransport([page1, page2])
    client = GitHubClient(auth=provider, transport=transport)
    assert len(client.get_pull_request_files("org/repo", 7)) == 101


def test_file_content_decodes_base64(provider: FileTokenProvider) -> None:
    payload = {"encoding": "base64", "content": base64.b64encode(b"hello\nworld").decode()}
    client = GitHubClient(auth=provider, transport=FakeTransport([payload]))
    assert client.get_file_content("org/repo", "a/b.py", "sha") == "hello\nworld"


def test_file_content_is_none_on_error_or_nonfile(provider: FileTokenProvider) -> None:
    from outerloop.github import GitHubError

    def failing(request: urllib.request.Request) -> object:
        raise GitHubError(404, "/contents", "not found")

    client = GitHubClient(auth=provider, transport=failing)
    assert client.get_file_content("org/repo", "gone.py", "sha") is None

    directory = GitHubClient(auth=provider, transport=FakeTransport([[{"name": "sub"}]]))
    assert directory.get_file_content("org/repo", "dir", "sha") is None


def test_enable_auto_merge_arms_via_graphql(provider: FileTokenProvider) -> None:
    transport = FakeTransport(
        [
            {"node_id": "PR_node123", "number": 7},
            {"data": {"enablePullRequestAutoMerge": {"pullRequest": {"number": 7}}}},
        ]
    )
    client = GitHubClient(auth=provider, transport=transport)
    client.enable_auto_merge("org/repo", 7)
    graphql = transport.requests[-1]
    assert graphql.full_url.endswith("/graphql")
    assert isinstance(graphql.data, bytes)
    body = json.loads(graphql.data.decode())
    assert body["variables"] == {"pr": "PR_node123", "method": "MERGE"}
    assert "enablePullRequestAutoMerge" in body["query"]


def test_enable_auto_merge_surfaces_graphql_errors_as_status_zero(
    provider: FileTokenProvider,
) -> None:
    transport = FakeTransport(
        [
            {"node_id": "PR_node123", "number": 7},
            {"errors": [{"message": "Pull request Auto merge is not allowed"}]},
        ]
    )
    client = GitHubClient(auth=provider, transport=transport)
    with pytest.raises(GitHubError, match="not allowed") as exc_info:
        client.enable_auto_merge("org/repo", 7)
    assert exc_info.value.status == 0  # not an HTTP failure; not a success either


def test_enable_auto_merge_missing_node_id_is_typed(provider: FileTokenProvider) -> None:
    transport = FakeTransport([{"number": 7}])
    with pytest.raises(GitHubError, match="node_id"):
        GitHubClient(auth=provider, transport=transport).enable_auto_merge("org/repo", 7)


def test_enable_auto_merge_dry_run_touches_nothing(provider: FileTokenProvider) -> None:
    transport = FakeTransport([])
    GitHubClient(auth=provider, transport=transport, dry_run=True).enable_auto_merge("o/r", 1)
    assert transport.requests == []


def test_arming_guard_requires_a_required_review(provider: FileTokenProvider) -> None:
    """No required human review between arming and merging -> refuse to arm:
    the bot-never-merges rule must hold in code, not per-repo config."""
    transport = FakeTransport([{"data": {"repository": {"pullRequest": {"reviewDecision": None}}}}])
    client = GitHubClient(auth=provider, transport=transport)
    assert client.arm_auto_merge_when_review_required("org/repo", 7) is False
    assert len(transport.requests) == 1  # decision query only; no mutation sent


def test_arming_guard_arms_with_repo_allowed_method(provider: FileTokenProvider) -> None:
    """REVIEW_REQUIRED -> arm, falling back to a merge method the repo
    actually allows (squash-only self-hosters still get arming)."""
    transport = FakeTransport(
        [
            {"data": {"repository": {"pullRequest": {"reviewDecision": "REVIEW_REQUIRED"}}}},
            {"allow_merge_commit": False, "allow_squash_merge": True, "allow_rebase_merge": True},
            {"node_id": "PR_n", "number": 7},
            {"data": {"enablePullRequestAutoMerge": {"pullRequest": {"number": 7}}}},
        ]
    )
    client = GitHubClient(auth=provider, transport=transport)
    assert client.arm_auto_merge_when_review_required("org/repo", 7) is True
    assert isinstance(transport.requests[-1].data, bytes)
    body = json.loads(transport.requests[-1].data.decode())
    assert body["variables"]["method"] == "SQUASH"


def test_candidate_row_rewrite_touches_only_the_preamble(provider: FileTokenProvider) -> None:
    """The report can contain a lookalike row; only the orchestrator's
    table row (before the report section) is rewritten."""
    body = (
        "intro\n| | value |\n| --- | --- |\n| baseline (tsp) | 13.88 |\n"
        "| candidate | 13.1 |\n\n## Research report\n\nprose "
        "with a lookalike:\n| candidate | 999 |\n"
    )
    transport = FakeTransport([{"body": body}, None])
    client = GitHubClient(auth=provider, transport=transport)
    assert client.update_candidate_row("org/repo", 9, 10.2) is True
    payload = transport.requests[-1].data
    assert isinstance(payload, bytes)
    patched = json.loads(payload.decode())["body"]
    assert "| candidate | 10.2 |" in patched
    assert "| candidate | 999 |" in patched  # the report's lookalike untouched
    assert "| candidate | 13.1 |" not in patched


def test_candidate_row_rewrite_fails_closed_without_report_heading(
    provider: FileTokenProvider,
) -> None:
    """No report heading -> no preamble boundary -> no rewrite (the row
    found could be inside agent text)."""
    transport = FakeTransport([{"body": "| candidate | 13.1 |\nno heading here"}])
    client = GitHubClient(auth=provider, transport=transport)
    assert client.update_candidate_row("org/repo", 9, 10.2) is False
    assert len(transport.requests) == 1


def test_candidate_row_rewrite_reports_missing_row(provider: FileTokenProvider) -> None:
    transport = FakeTransport([{"body": "no table here\n\n## Research report\nx"}])
    client = GitHubClient(auth=provider, transport=transport)
    assert client.update_candidate_row("org/repo", 9, 10.2) is False
    assert len(transport.requests) == 1  # GET only, no PATCH


def test_session_planted_smudge_filter_never_executes(tmp_path: Path) -> None:
    # A session can write .git/config and .gitattributes in its workspace: a
    # filter driver planted there must not run with the orchestrator's
    # permissions when Workspace.git checks files out (the same neutralization
    # the dispatched job script applies).
    import subprocess as sp

    root = tmp_path / "ws"
    root.mkdir()

    def g(*args: str) -> None:
        sp.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    g("init", "-q", "-b", "main")
    (root / "data.txt").write_text("payload\n")
    (root / ".gitattributes").write_text("*.txt filter=evil\n")
    g("add", "-A")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    marker = tmp_path / "PWNED"
    # the "session" plants the driver in repo-local config
    g("config", "filter.evil.smudge", f"touch {marker} && cat")
    g("config", "filter.evil.clean", f"touch {marker} && cat")

    ws = Workspace(root=root)
    # force a fresh checkout of every file — with the driver live this would
    # run the smudge command
    ws.git("checkout", "-f", "HEAD", "--", ".")
    ws.git("status", "--porcelain")
    assert not marker.exists()


def test_non_utf8_filter_config_neither_crashes_nor_executes(tmp_path: Path) -> None:
    # a session can write raw bytes into .git/config: the discovery must not
    # crash the git call (availability), and the weird-byte driver must still
    # be neutralized (the surrogate-escaped override key matches exactly)
    import subprocess as sp

    root = tmp_path / "ws"
    root.mkdir()

    def g(*args: str) -> None:
        sp.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    g("init", "-q", "-b", "main")
    (root / "data.txt").write_text("payload\n")
    (root / ".gitattributes").write_bytes(b"*.txt filter=ev\xffil\n")
    g("add", "-A")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    marker = tmp_path / "PWNED"
    with (root / ".git" / "config").open("ab") as fh:
        fh.write(b'[filter "ev\xffil"]\n\tsmudge = touch ' + str(marker).encode() + b" && cat\n")

    ws = Workspace(root=root)
    ws.git("status", "--porcelain")  # must not raise on the non-UTF-8 config
    ws.git("checkout", "-f", "HEAD", "--", ".")
    assert not marker.exists()


def test_redirect_off_host_loses_the_authorization_header() -> None:
    # the shared opener forwards Authorization on a same-host redirect but
    # strips it when the redirect changes host (or scheme)
    from outerloop.github import _NoAuthRedirect

    handler = _NoAuthRedirect()
    request = urllib.request.Request(
        "https://api.github.com/app/installations/1/access_tokens",
        headers={"Authorization": "Bearer jwt"},
        method="POST",
    )
    hijacked = handler.redirect_request(
        request, None, 302, "Found", {}, "https://attacker.example/steal"
    )
    assert hijacked is not None
    assert "Authorization" not in hijacked.headers
    same_host = handler.redirect_request(
        request, None, 302, "Found", {}, "https://api.github.com/elsewhere"
    )
    assert same_host is not None
    assert same_host.headers.get("Authorization") == "Bearer jwt"


def test_update_issue_patches_the_body(provider: FileTokenProvider) -> None:
    transport = FakeTransport([{}])
    client = GitHubClient(auth=provider, transport=transport)
    client.update_issue("o/r", 9, "new body")
    request = transport.requests[0]
    assert request.get_method() == "PATCH"
    assert request.full_url.endswith("/repos/o/r/issues/9")
    assert json.loads(cast(bytes, request.data)) == {"body": "new body"}


def test_list_open_issues_can_filter_by_creator(provider: FileTokenProvider) -> None:
    transport = FakeTransport([[{"number": 1, "user": {"login": "github-actions[bot]"}}]])
    client = GitHubClient(auth=provider, transport=transport)
    assert client.list_open_issues("o/r", creator="github-actions[bot]") == [
        {"number": 1, "user": {"login": "github-actions[bot]"}}
    ]
    url = transport.requests[0].full_url
    assert "/repos/o/r/issues?per_page=100&page=1&creator=github-actions%5Bbot%5D" in url


def test_check_runs_pagination(provider):
    check = {
        "id": 1,
        "name": "test",
        "status": "completed",
        "conclusion": "failure",
        "html_url": "https://github.com/check",
        "app": {"slug": "github-actions"},
    }
    transport = FakeTransport(
        [{"check_runs": [check] * 100}, {"check_runs": [{**check, "id": 101}]}]
    )
    client = GitHubClient(auth=provider, transport=transport)
    runs = client.list_check_runs("org/repo", "branch/name")
    assert len(runs) == 101 and runs[-1]["id"] == 101
    assert runs[0] == check
    assert "/commits/branch%2Fname/check-runs" in transport.requests[0].full_url
    assert "page=2" in transport.requests[1].full_url
    assert "filter=all" in transport.requests[0].full_url


def test_job_log_tail_is_bounded_stripped_and_optional(provider, monkeypatch, caplog):
    import io

    from outerloop.github import AUTH_SAFE_OPENER

    def raw(request, timeout):
        assert request.full_url.endswith("/repos/org/repo/actions/jobs/123/logs")
        return io.BytesIO(
            (
                "old line\n" * 100 + "\x1b[31merror\x1b[0m\n"
                "\x1b]8;;https://example.com\x1b\\last line\x1b]8;;\x1b\\\n"
            ).encode()
        )

    monkeypatch.setattr(AUTH_SAFE_OPENER, "open", raw)
    client = GitHubClient(auth=provider)
    assert client.job_log_tail("org/repo", 123, 21) == "error\nlast line\n"
    assert client.job_log_tail("org/repo", 123, 0) == ""

    def fail(request, timeout):
        raise RuntimeError("log unavailable")

    monkeypatch.setattr(AUTH_SAFE_OPENER, "open", fail)
    caplog.set_level("INFO")
    assert client.job_log_tail("org/repo", 123, 100) == ""
    assert "job log for org/repo job 123 unavailable: log unavailable" in caplog.text


def test_log_redirect_strips_credentials_and_refuses_downgrade():
    import urllib.error

    from outerloop.github import _NoAuthRedirect

    request = urllib.request.Request(
        "https://api.github.com/repos/o/r/actions/jobs/1/logs",
        headers={"Authorization": "Bearer secret"},
    )
    redirect = _NoAuthRedirect()
    redirected = redirect.redirect_request(
        request, None, 302, "", {}, "https://storage.blob.core.windows.net/log?signature=x"
    )
    assert redirected.get_header("Authorization") is None
    assert "Authorization" not in redirected.unredirected_hdrs
    with pytest.raises(urllib.error.URLError, match="downgrade"):
        redirect.redirect_request(request, None, 302, "", {}, "http://storage.example/log")


def test_direct_merge_binds_expected_head(provider):
    transport = FakeTransport([{"merged": True}])
    client = GitHubClient(auth=provider, transport=transport)
    assert client.merge_pull("org/repo", 9, "squash", expected_head="blessed")
    request = transport.requests[0]
    assert request.get_method() == "PUT"
    assert isinstance(request.data, bytes)
    assert json.loads(request.data) == {"merge_method": "squash", "sha": "blessed"}


def test_job_log_stream_stops_at_byte_cap(provider, monkeypatch):
    from outerloop.github import AUTH_SAFE_OPENER, MAX_LOG_BYTES

    class Response:
        total = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            assert 0 < size <= 65536
            assert self.total + size <= MAX_LOG_BYTES
            self.total += size
            return b"old\n" * (size // 4)

    response = Response()
    monkeypatch.setattr(AUTH_SAFE_OPENER, "open", lambda *a, **k: response)
    assert GitHubClient(auth=provider).job_log_tail("org/repo", 1, 15) == "old\nold\nold\n"
    assert response.total == MAX_LOG_BYTES


def test_network_git_failure_never_carries_the_credential(monkeypatch, tmp_path):
    """git's error text is re-raised without the token or its Basic form, and
    without the original exception chained behind it."""
    from outerloop import github as github_mod

    token = "ghs_secret_token_value"
    basic = github_mod._basic(token)

    def fail(args, env, timeout=None):
        raise github_mod.GitError(f"git push failed: remote: {token} and {basic} refused")

    monkeypatch.setattr(github_mod, "_run_git", fail)
    with pytest.raises(github_mod.GitError) as caught:
        github_mod._run_git_with_credential(["git", "push"], token, tmp_path)
    text = str(caught.value)
    assert token not in text and basic not in text
    assert text.count("[redacted]") == 2
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_network_git_refuses_an_empty_token_before_any_call(monkeypatch, tmp_path):
    from outerloop import github as github_mod

    monkeypatch.setattr(github_mod, "_run_git", lambda *a, **k: pytest.fail("no network call"))
    with pytest.raises(github_mod.GitError, match="token is empty"):
        github_mod._run_git_with_credential(["git", "fetch"], "", tmp_path)
    # no token at all is a different case: an anonymous call is allowed
    monkeypatch.setattr(github_mod, "_run_git", lambda *a, **k: "ok")
    assert github_mod._run_git_with_credential(["git", "fetch"], None, tmp_path) == "ok"


def _ledger_tree(*paths):
    return {"truncated": False, "tree": [{"path": p, "type": "blob"} for p in paths]}


def _ledger_content(content):
    return {
        "type": "file",
        "encoding": "base64",
        "content": base64.b64encode(content.encode()).decode(),
    }


def test_ledger_pinned_read_and_removed_pending(provider):
    from outerloop.ledger_branch import read_ledger
    from outerloop.progress import LEADER_FILE

    transport = FakeTransport(
        [
            {"object": {"sha": "pinned"}},
            _ledger_tree(LEADER_FILE, "results/submissions/run/head.json"),
            _ledger_content("{}"),
            _ledger_content("null"),
        ]
    )
    client = GitHubClient(auth=provider, transport=transport)
    assert read_ledger(client, "org/repo") == ("pinned", {}, {})
    assert all("ref=pinned" in r.full_url for r in transport.requests[2:])


@pytest.mark.parametrize(
    "tree", [{"truncated": True, "tree": []}, {}, {"truncated": False, "tree": None}]
)
def test_ledger_refuses_incomplete_tree(provider, tree):
    from outerloop.ledger_branch import read_ledger
    from outerloop.progress import LedgerReadError

    client = GitHubClient(
        auth=provider, transport=FakeTransport([{"object": {"sha": "pin"}}, tree])
    )
    with pytest.raises(LedgerReadError):
        read_ledger(client, "org/repo")


def test_ledger_corrupt_read_never_writes(provider, monkeypatch):
    from outerloop.ledger_branch import write_ledger
    from outerloop.progress import LEADER_FILE, LedgerReadError

    client = GitHubClient(
        auth=provider, transport=FakeTransport([_ledger_tree(LEADER_FILE), _ledger_content("{")])
    )

    def forbidden(*args, **kwargs):
        pytest.fail("must not write corrupt ledger")

    monkeypatch.setattr(client, "put_files", forbidden)
    with pytest.raises(LedgerReadError):
        write_ledger(client, "org/repo", "pin", lambda leader, pendings: {})


@pytest.mark.parametrize("conflicts", [1, 3])
def test_ledger_cas_recomputes_and_bounds_retries(provider, monkeypatch, conflicts):
    from dataclasses import asdict

    from outerloop.ledger_branch import LedgerWriteError, write_ledger
    from outerloop.progress import LEADER_FILE, PROGRESS_FILE, LeaderEntry, render_markdown

    concurrent = LeaderEntry("other", "score", "max", 1.0, 2.0, "other-run", "today")
    encoded = json.dumps({"other": asdict(concurrent)})
    responses: list[object] = [_ledger_tree()]
    for index in range(conflicts):
        responses.append({"object": {"sha": f"head-{index + 1}"}})
        if index < 2:
            responses.extend([_ledger_tree(LEADER_FILE), _ledger_content(encoded)])
    client = GitHubClient(auth=provider, transport=FakeTransport(responses))
    seen = []
    writes = []

    def edit(leader, pendings):
        seen.append(dict(leader))
        return {LEADER_FILE: json.dumps({k: asdict(v) for k, v in leader.items()})}

    def put(repo, files, branch, message, expected_head=None):
        writes.append((expected_head, files))
        assert branch == "research-log"
        return len(writes) > conflicts

    monkeypatch.setattr(client, "put_files", put)
    if conflicts == 3:
        with pytest.raises(LedgerWriteError, match="three"):
            write_ledger(client, "org/repo", "head-0", edit)
        assert len(writes) == 3
    else:
        write_ledger(client, "org/repo", "head-0", edit)
        assert len(writes) == 2
    assert seen[0] == {} and seen[1] == {"other": concurrent}
    assert writes[1][0] == "head-1"
    assert writes[1][1][PROGRESS_FILE] == render_markdown({"other": concurrent}, "org/repo")
    assert json.loads(writes[1][1][LEADER_FILE])["other"] == asdict(concurrent)


@pytest.mark.parametrize("head", ["existing", ""])
def test_ledger_branch_creation_uses_only_callers_pin(provider, head):
    from outerloop.ledger_branch import ensure_ledger_branch

    transport = FakeTransport([{}])
    client = GitHubClient(auth=provider, transport=transport)
    from unittest.mock import patch

    with patch.object(client, "branch_head", return_value=head):
        ensure_ledger_branch(client, "org/repo", "pinned-main")
    assert len(transport.requests) == (0 if head else 1)
    if not head:
        payload = transport.requests[0].data
        assert isinstance(payload, bytes)
        assert json.loads(payload) == {
            "ref": "refs/heads/research-log",
            "sha": "pinned-main",
        }


def test_shared_research_branch_constant():
    import outerloop.attempt as attempt
    import outerloop.climbboard as board
    import outerloop.tick as tick
    from outerloop.ledger_branch import RESEARCH_LOG_BRANCH

    assert (
        attempt.RESEARCH_LOG_BRANCH
        == tick.RESEARCH_LOG_BRANCH
        == board.BOARD_BRANCH
        == RESEARCH_LOG_BRANCH
    )
    for module in (attempt, tick, board):
        assert module.__file__ is not None
        text = Path(module.__file__).read_text()
        assert 'BRANCH = "research-log"' not in text
        assert "from outerloop.ledger_branch import RESEARCH_LOG_BRANCH" in text


def test_ledger_pending_publish_and_reject_are_atomic(provider, monkeypatch):
    from outerloop.ledger_branch import read_ledger, write_ledger
    from outerloop.progress import (
        LEADER_FILE,
        PROGRESS_FILE,
        PendingSubmission,
        record_pending,
        reject,
    )

    pending = PendingSubmission(
        "bench",
        "score",
        "max",
        0.123456789012345,
        0.234567890123456,
        "run",
        123456789,
        "ruler",
        "signature",
        "sealed",
        12,
        "head",
        "today",
    )
    transport = FakeTransport(
        [
            {"object": {"sha": "pin"}},
            _ledger_tree(pending.path),
            _ledger_content(record_pending(pending)[pending.path]),
            _ledger_tree(pending.path),
            _ledger_content(record_pending(pending)[pending.path]),
            _ledger_tree(pending.path),
            _ledger_content("null"),
        ]
    )
    client = GitHubClient(auth=provider, transport=transport)
    head, leader, pendings = read_ledger(client, "org/repo")
    assert leader == {} and pendings == {pending.path: pending}
    writes = []

    def put(repo, files, branch, message, expected_head=None):
        writes.append(files)
        assert expected_head == "pin"
        return True

    monkeypatch.setattr(client, "put_files", put)
    write_ledger(client, "org/repo", head, lambda leader, pendings: reject(pending))
    # Replaying after a successful write remains safe.
    write_ledger(client, "org/repo", head, lambda leader, pendings: reject(pending))
    for files in writes:
        assert files[pending.path] == "null\n"
        assert json.loads(files[LEADER_FILE]) == {}
        assert PROGRESS_FILE in files


@pytest.mark.parametrize("head", [None, ""])
def test_ledger_unavailable_head_refuses_read(provider, monkeypatch, head):
    from outerloop.ledger_branch import read_ledger
    from outerloop.progress import LedgerReadError

    client = GitHubClient(auth=provider, transport=FakeTransport([]))
    monkeypatch.setattr(client, "branch_head", lambda *args: head)
    with pytest.raises(LedgerReadError):
        read_ledger(client, "org/repo")


@pytest.mark.parametrize("dry_run", [False, True])
def test_mark_ready_for_review(provider, dry_run):
    transport = FakeTransport([{"node_id": "PR_node"}, {"data": {}}])
    client = GitHubClient(auth=provider, transport=transport, dry_run=dry_run)
    client.mark_ready_for_review("org/repo", 29)
    if dry_run:
        assert not transport.requests
    else:
        assert transport.requests[0].full_url.endswith("/repos/org/repo/pulls/29")
        data = transport.requests[1].data
        assert isinstance(data, bytes)
        mutation = json.loads(data)
        assert "markPullRequestReadyForReview" in mutation["query"]
        assert mutation["variables"] == {"pr": "PR_node"}


@pytest.mark.parametrize(
    "responses",
    [
        [{"node_id": "PR_node"}, {"errors": [{"message": "denied"}]}],
        [{}],
    ],
)
def test_mark_ready_for_review_reports_failure(provider, responses):
    client = GitHubClient(auth=provider, transport=FakeTransport(responses))
    with pytest.raises(GitHubError):
        client.mark_ready_for_review("org/repo", 29)
