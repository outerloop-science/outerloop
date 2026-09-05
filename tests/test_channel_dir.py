"""The syscall channel is `.outerloop/`; a workspace parked at `.autoresearch/` keeps it."""

from __future__ import annotations

from pathlib import Path

from outerloop import brief, syscall
from outerloop.syscall import CHANNEL_DIR_NAMES, channel_dir, install_tool, tool_command


def test_names_new_first_and_brief_matches() -> None:
    assert CHANNEL_DIR_NAMES == (".outerloop", ".autoresearch")
    assert CHANNEL_DIR_NAMES[0] == brief._CHANNEL  # the author brief names the new default


def test_resolver(tmp_path: Path) -> None:
    assert channel_dir(tmp_path) == ".outerloop"  # fresh clone -> new default
    (tmp_path / ".autoresearch").mkdir()
    assert channel_dir(tmp_path) == ".autoresearch"  # a run parked before the rename
    (tmp_path / ".outerloop").mkdir()
    assert channel_dir(tmp_path) == ".outerloop"  # both present -> new wins


def test_install_and_tool_command_follow_the_resolved_dir(tmp_path: Path) -> None:
    install_tool(tmp_path)  # fresh -> installs the new default
    assert (tmp_path / ".outerloop" / "syscall").exists()
    assert tool_command(tmp_path).endswith("/.outerloop/syscall")


def test_reads_follow_a_legacy_workspace(tmp_path: Path) -> None:
    # a persisted pre-rename workspace: the kernel still finds its channel
    (tmp_path / ".autoresearch").mkdir()
    syscall.write_budget(tmp_path, launches_remaining=1, sleeps_remaining=1)
    assert (tmp_path / ".autoresearch" / "budget.json").exists()
    assert tool_command(tmp_path).endswith("/.autoresearch/syscall")
