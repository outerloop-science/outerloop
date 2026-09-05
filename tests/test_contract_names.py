"""The contract file is `.outerloop.yaml`; `.autoresearch.yaml` is still read.

Every read site resolves the name through `find_contract` (new name first), so a
target written before the rename keeps working without touching anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from outerloop.contract import (
    ALWAYS_FORBIDDEN,
    CONTRACT_NAME,
    CONTRACT_NAMES,
    contract_in_tree,
    contract_text_in_tree,
    find_contract,
)
from outerloop.github import GitError, contract_at


def test_new_name_first_and_neither_is_writable() -> None:
    assert CONTRACT_NAMES == (".outerloop.yaml", ".autoresearch.yaml")
    assert CONTRACT_NAME == ".outerloop.yaml"
    assert set(CONTRACT_NAMES) <= set(ALWAYS_FORBIDDEN)


def test_find_contract_prefers_new_and_falls_back() -> None:
    both = {".outerloop.yaml": "new", ".autoresearch.yaml": "old"}
    assert find_contract(lambda n: both.get(n)) == (".outerloop.yaml", "new")
    legacy = {".autoresearch.yaml": "old"}
    assert find_contract(lambda n: legacy.get(n)) == (".autoresearch.yaml", "old")
    assert find_contract(lambda n: None) is None


def test_contract_in_tree_and_text(tmp_path: Path) -> None:
    assert contract_in_tree(tmp_path) is None
    (tmp_path / ".autoresearch.yaml").write_text("legacy: 1\n")
    assert contract_in_tree(tmp_path) == (".autoresearch.yaml", "legacy: 1\n")
    (tmp_path / ".outerloop.yaml").write_text("new: 1\n")
    assert contract_text_in_tree(tmp_path) == "new: 1\n"  # new name wins when both exist


def test_contract_text_in_tree_raises_naming_both(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"\.outerloop\.yaml or \.autoresearch\.yaml"):
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


def test_contract_at_falls_back_over_git_show() -> None:
    assert contract_at(_Ws({".autoresearch.yaml": "old"}), "abc") == "old"
    assert contract_at(_Ws({".outerloop.yaml": "new", ".autoresearch.yaml": "old"}), "abc") == "new"
    with pytest.raises(GitError, match="no contract at abc"):
        contract_at(_Ws({}), "abc")


def test_tick_contract_text_via_the_api() -> None:
    from outerloop.tick import _contract_text

    class GH:
        def __init__(self, files: dict[str, str]) -> None:
            self.files = files

        def get_file_content(self, repo: str, path: str, ref: str) -> str | None:
            return self.files.get(path)  # None = missing, like the real client

    assert _contract_text(GH({".autoresearch.yaml": "old"}), "o/r", "main") == "old"
    assert (
        _contract_text(GH({".outerloop.yaml": "n", ".autoresearch.yaml": "o"}), "o/r", "x") == "n"
    )
    assert _contract_text(GH({}), "o/r", "main") is None
