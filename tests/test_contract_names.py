"""All contract readers use `.outerloop.yaml`."""

from __future__ import annotations

from pathlib import Path

import pytest

from outerloop.contract import (
    ALWAYS_FORBIDDEN,
    CONTRACT_NAME,
    contract_in_tree,
    contract_text_in_tree,
    find_contract,
)
from outerloop.github import GitError, contract_at


def test_contract_and_github_are_forbidden() -> None:
    assert CONTRACT_NAME == ".outerloop.yaml"
    assert ALWAYS_FORBIDDEN == (".github", ".outerloop.yaml")


def test_find_contract() -> None:
    files = {".outerloop.yaml": "new"}
    assert find_contract(lambda n: files.get(n)) == (".outerloop.yaml", "new")
    assert find_contract(lambda n: None) is None


def test_contract_in_tree_and_text(tmp_path: Path) -> None:
    assert contract_in_tree(tmp_path) is None
    (tmp_path / ".outerloop.yaml").write_text("new: 1\n")
    assert contract_in_tree(tmp_path) == (".outerloop.yaml", "new: 1\n")
    assert contract_text_in_tree(tmp_path) == "new: 1\n"


def test_contract_text_in_tree_raises_naming_contract(tmp_path: Path) -> None:
    (tmp_path / "unknown.yaml").write_text("benchmarks: []\n")
    with pytest.raises(FileNotFoundError, match=r"\.outerloop\.yaml"):
        contract_text_in_tree(tmp_path)


class _Ws:
    """A Workspace stand-in: `git show <sha>:<path>` fails for a missing path."""

    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    def git(self, *args: str) -> str:
        assert args[0] == "show"
        sha, _, name = args[1].partition(":")
        if name not in self.files:
            raise GitError(f"fatal: path '{name}' does not exist in '{sha}'")
        return self.files[name]


def test_contract_at_over_git_show() -> None:
    assert contract_at(_Ws({".outerloop.yaml": "new"}), "abc") == "new"
    with pytest.raises(GitError, match="no contract at abc"):
        contract_at(_Ws({}), "abc")


def test_tick_contract_text_via_the_api() -> None:
    from outerloop.tick import _contract_text

    class GH:
        def __init__(self, files: dict[str, str]) -> None:
            self.files = files

        def get_file_content(self, repo: str, path: str, ref: str) -> str | None:
            return self.files.get(path)  # None = missing, like the real client

    assert _contract_text(GH({".outerloop.yaml": "n"}), "o/r", "x") == "n"
    assert _contract_text(GH({}), "o/r", "main") is None
