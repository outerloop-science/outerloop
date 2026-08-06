import base64
import json
import subprocess
import threading
import urllib.request
from pathlib import Path

import pytest

from autoresearch.github import FileTokenProvider, GitHubClient, Workspace


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
