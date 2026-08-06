"""Regression tests for the findings of the 2026-08-05 adversarial review."""

from __future__ import annotations

import subprocess
import urllib.error
import urllib.request
from functools import partial
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


def test_session_planted_hook_cannot_read_token(
    tmp_path: Path, provider: FileTokenProvider
) -> None:
    origin = _origin(tmp_path)
    ws = Workspace.clone(f"file://{origin}", tmp_path / "work", auth=provider)
    hooks = ws.root / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    stolen = tmp_path / "stolen.txt"
    hook = hooks / "pre-commit"
    hook.write_text(f'#!/bin/sh\nenv > "{stolen}"\n')
    hook.chmod(0o755)
    ws.branch("feat/auto/x")
    (ws.root / "f.txt").write_text("x\n")
    ws.commit_all("msg", author="bot")
    assert not stolen.exists(), "hook ran despite core.hooksPath=/dev/null"


def test_local_git_env_has_no_credential(tmp_path: Path, provider: FileTokenProvider) -> None:
    origin = _origin(tmp_path)
    ws = Workspace.clone(f"file://{origin}", tmp_path / "work", auth=provider)
    assert "SUPERSECRET" not in ws.git("config", "--list")


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
def test_http_error_becomes_typed_without_url_token(provider: FileTokenProvider) -> None:
    def transport(request: urllib.request.Request) -> object:
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    client = GitHubClient(auth=provider, transport=transport)
    with pytest.raises(urllib.error.HTTPError):
        client.default_branch("org/repo")  # raw transport passes through; wrapper is in default


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
