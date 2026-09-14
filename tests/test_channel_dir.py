"""The syscall channel is `.outerloop/`."""

from __future__ import annotations

from pathlib import Path

from outerloop import brief, syscall
from outerloop.syscall import SYSCALL_DIR, channel_dir, install_tool, tool_command


def test_brief_names_the_channel() -> None:
    assert SYSCALL_DIR == ".outerloop" == brief._CHANNEL


def test_resolver(tmp_path: Path) -> None:
    (tmp_path / ".autoresearch").mkdir()  # an old channel dir is not looked for
    assert channel_dir(tmp_path) == ".outerloop"
    (tmp_path / ".outerloop").mkdir()
    assert channel_dir(tmp_path) == ".outerloop"


def test_install_and_tool_command_follow_the_resolved_dir(tmp_path: Path) -> None:
    install_tool(tmp_path)  # fresh -> installs the new default
    assert (tmp_path / ".outerloop" / "syscall").exists()
    assert tool_command(tmp_path).endswith("/.outerloop/syscall")


def test_budget_uses_channel(tmp_path: Path) -> None:
    (tmp_path / ".outerloop").mkdir()
    syscall.write_budget(tmp_path, launches_remaining=1, sleeps_remaining=1)
    assert (tmp_path / ".outerloop" / "budget.json").exists()
    assert tool_command(tmp_path).endswith("/.outerloop/syscall")
