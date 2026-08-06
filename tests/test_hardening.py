"""Regression tests for the findings of the 2026-08-05 adversarial review."""

from __future__ import annotations

import base64
import subprocess
import threading
import urllib.error
import urllib.request
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from autoresearch.contract import (
    ContractError,
    ScopeError,
    SelfTargetError,
    load_contract,
    normalize_repo,
    path_is_forbidden,
)
from autoresearch.github import (
    FileTokenProvider,
    ForbiddenPathError,
    GitHubClient,
    GitHubError,
    NothingToCommit,
    Workspace,
    _default_transport,
)
from test_contract import PILOT_CONTRACT


# --- finding 2: path normalization -------------------------------------------------
@pytest.mark.parametrize(
    "allowed",
    ["src/../.github/", "src//../.github", "../../etc", "/etc/passwd", "*", "src/**", ".", "./"],
)
def test_unsafe_scope_entries_refused(allowed: str) -> None:
    text = PILOT_CONTRACT.replace("allowed: [src/pilot/solvers/]", f"allowed: ['{allowed}']")
    with pytest.raises(ScopeError):
        load_contract(text, "x/y")


def test_sibling_name_not_shadowed_by_roadmap() -> None:
    # roadmap README.md must not forbid README.mdx (prefix vs component match)
    text = PILOT_CONTRACT.replace("allowed: [src/pilot/solvers/]", "allowed: [README.mdx]")
    contract = load_contract(text, "x/y")
    assert not path_is_forbidden("README.mdx", contract)
    assert path_is_forbidden("README.md", contract)


def test_forbidden_matches_are_component_wise() -> None:
    contract = load_contract(PILOT_CONTRACT, "x/y")
    assert path_is_forbidden(".github/workflows/ci.yml", contract)
    assert path_is_forbidden(".autoresearch.yaml", contract)
    assert path_is_forbidden("src/../.github/x.yml", contract)  # unnormalizable → forbidden
    assert not path_is_forbidden("src/pilot/solvers/tsp.py", contract)


# --- finding 3: self-target spellings ----------------------------------------------
@pytest.mark.parametrize(
    "spelling",
    [
        "agentic-learning-ai-lab/autoresearch",
        "Agentic-Learning-AI-Lab/AutoResearch",
        "agentic-learning-ai-lab/autoresearch.git",
        "agentic-learning-ai-lab/autoresearch/",
        "https://github.com/agentic-learning-ai-lab/autoresearch",
        "git@github.com:agentic-learning-ai-lab/autoresearch.git",
    ],
)
def test_self_target_spellings_refused(spelling: str) -> None:
    with pytest.raises(SelfTargetError):
        load_contract(PILOT_CONTRACT, spelling)


def test_normalize_repo_keeps_other_repos() -> None:
    assert normalize_repo("https://github.com/org/Other.git") == "org/other"


# --- finding 4: untrusted YAML ------------------------------------------------------
def test_alias_bomb_refused_fast() -> None:
    bomb = "a: &a [x, x, x, x, x, x, x, x, x]\n"
    for i in range(1, 10):
        bomb += f"{'b' * i}: &{'b' * i} [*{'b' * (i - 1) if i > 1 else 'a'}] * 9\n"
    with pytest.raises(ContractError, match="alias"):
        load_contract("benchmarks: []\n" + bomb, "x/y")


def test_duplicate_keys_refused() -> None:
    text = PILOT_CONTRACT + "\nbudgets:\n  gpu_hours_per_run: 999\n  runs_per_week: 999\n"
    with pytest.raises(ContractError, match="duplicate"):
        load_contract(text, "x/y")


def test_oversized_contract_refused() -> None:
    with pytest.raises(ContractError, match="exceeds"):
        load_contract("x: 1\n" * 20_000, "x/y")


# --- finding 1: no credential for local git -----------------------------------------
def _origin(tmp_path: Path) -> Path:
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


@pytest.fixture
def provider(tmp_path: Path) -> FileTokenProvider:
    pat = tmp_path / "pat"
    pat.write_text("github_pat_SUPERSECRET\n")
    pat.chmod(0o600)
    return FileTokenProvider(pat)


def _plant_hook(ws: Workspace, name: str, sink: Path) -> None:
    """Plant a hook the way a compromised session would, defeating the
    clone-time core.hooksPath so the per-invocation flag is what's tested."""
    subprocess.run(["git", "-C", str(ws.root), "config", "--unset", "core.hooksPath"], check=False)
    hooks = ws.root / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / name
    hook.write_text(f'#!/bin/sh\nenv > "{sink}"\nexit 0\n')
    hook.chmod(0o755)


def _basic_header(token: str) -> str:
    return base64.b64encode(f"x-access-token:{token}".encode()).decode()


def test_session_planted_commit_hook_cannot_read_token(
    tmp_path: Path, provider: FileTokenProvider
) -> None:
    origin = _origin(tmp_path)
    ws = Workspace.clone(f"file://{origin}", tmp_path / "work", auth=provider)
    stolen = tmp_path / "stolen_commit.txt"
    _plant_hook(ws, "pre-commit", stolen)
    ws.branch("feat/auto/x")
    (ws.root / "f.txt").write_text("x\n")
    ws.commit_all("msg", author="bot")
    assert not stolen.exists(), "hook ran during commit"


def test_session_planted_push_hook_cannot_read_token(
    tmp_path: Path, provider: FileTokenProvider
) -> None:
    """The push path carries the credential — hooks must be dead there too."""
    origin = _origin(tmp_path)
    ws = Workspace.clone(f"file://{origin}", tmp_path / "work", auth=provider)
    ws.branch("feat/auto/x")
    (ws.root / "f.txt").write_text("x\n")
    ws.commit_all("msg", author="bot")
    stolen = tmp_path / "stolen_push.txt"
    _plant_hook(ws, "pre-push", stolen)
    ws.push("feat/auto/x")
    assert not stolen.exists(), "pre-push hook ran with the credential in env"
    if stolen.exists():  # pragma: no cover - defensive
        assert _basic_header(provider.token()) not in stolen.read_text()


def test_session_cannot_redirect_push_to_its_own_remote(
    tmp_path: Path, provider: FileTokenProvider
) -> None:
    """A session rewriting remote.origin.url must not capture the token."""
    origin = _origin(tmp_path)
    evil = tmp_path / "evil.git"
    subprocess.run(["git", "init", "--bare", "-q", str(evil)], check=True)
    ws = Workspace.clone(f"file://{origin}", tmp_path / "work", auth=provider)
    ws.branch("feat/auto/x")
    (ws.root / "f.txt").write_text("x\n")
    ws.commit_all("msg", author="bot")
    subprocess.run(
        ["git", "-C", str(ws.root), "config", "remote.origin.url", f"file://{evil}"], check=True
    )
    ws.push("feat/auto/x")
    landed = subprocess.run(
        ["git", "-C", str(origin), "branch"], capture_output=True, text=True, check=True
    ).stdout
    hijacked = subprocess.run(
        ["git", "-C", str(evil), "branch"], capture_output=True, text=True, check=True
    ).stdout
    assert "feat/auto/x" in landed, "push did not reach the real origin"
    assert "feat/auto/x" not in hijacked, "push followed the session's rewritten remote"


def test_local_git_env_has_no_credential(tmp_path: Path, provider: FileTokenProvider) -> None:
    origin = _origin(tmp_path)
    ws = Workspace.clone(f"file://{origin}", tmp_path / "work", auth=provider)
    config = ws.git("config", "--list")
    assert "extraheader" not in config.lower()
    assert _basic_header(provider.token()) not in config


def test_file_token_provider_rejects_loose_permissions(tmp_path: Path) -> None:
    pat = tmp_path / "loose"
    pat.write_text("tok\n")
    pat.chmod(0o644)
    with pytest.raises(PermissionError):
        FileTokenProvider(pat).token()


def test_file_token_provider_rejects_empty(tmp_path: Path) -> None:
    pat = tmp_path / "empty"
    pat.write_text("\n")
    pat.chmod(0o600)
    with pytest.raises(ValueError, match="empty"):
        FileTokenProvider(pat).token()


# --- findings 5 and 8: commit semantics ---------------------------------------------
def test_commit_all_on_clean_tree_raises_typed(tmp_path: Path) -> None:
    origin = _origin(tmp_path)
    ws = Workspace.clone(f"file://{origin}", tmp_path / "work")
    ws.branch("feat/auto/x")
    with pytest.raises(NothingToCommit):
        ws.commit_all("nothing", author="bot")


def test_commit_all_refuses_forbidden_paths(tmp_path: Path) -> None:
    origin = _origin(tmp_path)
    ws = Workspace.clone(f"file://{origin}", tmp_path / "work")
    ws.branch("feat/auto/x")
    (ws.root / ".github" / "workflows").mkdir(parents=True)
    (ws.root / ".github" / "workflows" / "evil.yml").write_text("on: push\n")
    contract = load_contract(PILOT_CONTRACT, "x/y")
    with pytest.raises(ForbiddenPathError, match=r"\.github"):
        ws.commit_all("evil", author="bot", forbidden=partial(path_is_forbidden, contract=contract))
    assert ws.git("log", "--oneline").count("\n") == 0  # nothing new committed


# --- findings 6 and 7: HTTP errors and response shapes ------------------------------
def test_default_transport_types_http_errors(provider: FileTokenProvider) -> None:
    """Exercise the real transport, not a stub: 404 must become GitHubError."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"message": "Not Found"}')

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/x",
            headers={"Authorization": "Bearer github_pat_SUPERSECRET"},
        )
        with pytest.raises(GitHubError) as excinfo:
            _default_transport(request)
        assert excinfo.value.status == 404
        assert "SUPERSECRET" not in str(excinfo.value)
    finally:
        server.shutdown()


def test_cross_host_redirect_drops_authorization() -> None:
    """The PAT must not follow a redirect to another host."""
    seen: list[str | None] = []

    class Final(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args: object) -> None:
            pass

    final = HTTPServer(("127.0.0.1", 0), Final)
    threading.Thread(target=final.serve_forever, daemon=True).start()
    final_url = f"http://localhost:{final.server_port}/dest"

    class Redirector(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", final_url)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    hop = HTTPServer(("127.0.0.1", 0), Redirector)
    threading.Thread(target=hop.serve_forever, daemon=True).start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{hop.server_port}/start",
            headers={"Authorization": "Bearer github_pat_SUPERSECRET"},
        )
        _default_transport(request)
        assert seen == [None], f"credential followed the redirect: {seen}"
    finally:
        hop.shutdown()
        final.shutdown()


def test_get_file_on_directory_listing_errors(provider: FileTokenProvider) -> None:
    client = GitHubClient(auth=provider, transport=lambda _r: [{"type": "file"}])
    with pytest.raises(GitHubError, match="expected an object"):
        client.get_file("org/repo", "src", "main")


def test_get_file_on_large_file_errors(provider: FileTokenProvider) -> None:
    client = GitHubClient(
        auth=provider, transport=lambda _r: {"type": "file", "encoding": "none", "content": ""}
    )
    with pytest.raises(GitHubError, match="encoding"):
        client.get_file("org/repo", "big.bin", "main")


def test_create_pr_without_number_errors(provider: FileTokenProvider) -> None:
    client = GitHubClient(auth=provider, transport=lambda _r: {"message": "Validation Failed"})
    with pytest.raises(GitHubError, match="no PR number"):
        client.create_pr("org/repo", "h", "main", "t", "b")


def test_ref_is_url_encoded(provider: FileTokenProvider) -> None:
    seen: list[str] = []

    def transport(request: urllib.request.Request) -> object:
        seen.append(request.full_url)
        return {"type": "file", "encoding": "base64", "content": "eA=="}

    GitHubClient(auth=provider, transport=transport).get_file("org/repo", "a.yaml", "we#ird")
    assert "we%23ird" in seen[0]


# --- verification round: remaining bypasses -----------------------------------------
@pytest.mark.parametrize(
    "spelling",
    [
        "ssh://git@github.com/agentic-learning-ai-lab/autoresearch.git",
        "github.com/agentic-learning-ai-lab/autoresearch",
        "www.github.com/agentic-learning-ai-lab/autoresearch",
        "git://github.com/agentic-learning-ai-lab/autoresearch",
        "https://x-access-token:tok@github.com/agentic-learning-ai-lab/autoresearch",
        "https://github.com//agentic-learning-ai-lab/autoresearch",
        "  agentic-learning-ai-lab/autoresearch\n",
    ],
)
def test_self_target_url_spellings_refused(spelling: str) -> None:
    with pytest.raises(SelfTargetError):
        load_contract(PILOT_CONTRACT, spelling)


@pytest.mark.parametrize(
    "candidate", [".GITHUB/workflows/evil.yml", ".GitHub/x", ".Autoresearch.YAML"]
)
def test_forbidden_paths_are_case_insensitive(candidate: str) -> None:
    contract = load_contract(PILOT_CONTRACT, "x/y")
    assert path_is_forbidden(candidate, contract)


def test_git_directory_never_writable() -> None:
    contract = load_contract(PILOT_CONTRACT, "x/y")
    assert path_is_forbidden(".git/config", contract)
    text = PILOT_CONTRACT.replace("allowed: [src/pilot/solvers/]", "allowed: ['.git/hooks']")
    with pytest.raises(ScopeError):
        load_contract(text, "x/y")


@pytest.mark.parametrize(
    "hostile",
    ["benchmarks: [", "\tbad: indent", "? {a: 1}\n: v", "x: !!python/object/apply:os.system ['x']"],
)
def test_hostile_yaml_raises_contract_error(hostile: str) -> None:
    with pytest.raises(ContractError):
        load_contract(hostile, "x/y")


def test_unicode_filenames_do_not_trip_the_forbidden_check(tmp_path: Path) -> None:
    """git C-quotes exotic names; the veto must see real paths, not quoted ones."""
    origin = _origin(tmp_path)
    ws = Workspace.clone(f"file://{origin}", tmp_path / "work")
    ws.branch("feat/auto/x")
    (ws.root / "café.txt").write_text("x\n")
    (ws.root / "a b.txt").write_text("x\n")
    contract = load_contract(PILOT_CONTRACT, "x/y")
    ws.commit_all("unicode", author="bot", forbidden=partial(path_is_forbidden, contract=contract))
    assert "café.txt" in ws.git("show", "--stat", "--name-only", "HEAD")
