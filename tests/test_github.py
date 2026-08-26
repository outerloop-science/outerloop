import base64
import json
import subprocess
import threading
import urllib.request
from pathlib import Path

import pytest

from autoresearch.github import FileTokenProvider, GitHubClient, GitHubError, Workspace


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


def test_get_file_decodes_base64(provider: FileTokenProvider) -> None:
    content = base64.b64encode(b"benchmarks: []\n").decode()
    transport = FakeTransport([{"type": "file", "encoding": "base64", "content": content}])
    client = GitHubClient(auth=provider, transport=transport)
    assert client.get_file("org/repo", ".autoresearch.yaml", "main") == "benchmarks: []\n"
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
    from autoresearch.github import EnvTokenProvider

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

    from autoresearch.github import _raw_transport

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
    from autoresearch.github import GitHubError

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
