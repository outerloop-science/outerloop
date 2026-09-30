"""The config dir: `~/.config/outerloop`."""

from __future__ import annotations

from pathlib import Path

import pytest

from outerloop.paths import CONFIG_DIR_NAME, config_dir


def test_fresh_machine_gets_the_new_dir(tmp_path: Path) -> None:
    (tmp_path / ".config" / "autoresearch").mkdir(parents=True)  # not looked for
    assert config_dir(tmp_path) == tmp_path / ".config" / "outerloop"
    assert CONFIG_DIR_NAME == "outerloop"


def test_existing_config_dir(tmp_path: Path) -> None:
    (tmp_path / ".config" / "outerloop").mkdir(parents=True)
    assert config_dir(tmp_path) == tmp_path / ".config" / "outerloop"


def test_deploy_script_config_dir() -> None:
    sh = (Path(__file__).resolve().parents[1] / "scripts" / "tick_deploy.sh").read_text()
    assert 'ENV_FILE="${_sel:-$HOME/.config/outerloop/.env}"' in sh


def test_env_file_default_and_process_override(monkeypatch, tmp_path):
    from outerloop import paths

    monkeypatch.delenv("OUTERLOOP_ENV_FILE", raising=False)
    assert paths.env_file() == paths.CONFIG_DIR / ".env"
    selected = tmp_path / "settings.env"
    monkeypatch.setenv("OUTERLOOP_ENV_FILE", str(selected))
    assert paths.env_file() == selected


def test_harness_status_reads_selected_settings(monkeypatch, tmp_path):
    from outerloop import harness_cli

    selected = tmp_path / "settings.env"
    selected.write_text("OUTERLOOP_CLAUDE_BIN=/sandbox/claude\n")
    selected.chmod(0o600)
    monkeypatch.setenv("OUTERLOOP_ENV_FILE", str(selected))
    monkeypatch.delenv("OUTERLOOP_CLAUDE_BIN", raising=False)
    seen = {}

    def status(env):
        seen.update(env)
        return 0

    monkeypatch.setattr(harness_cli, "status", status)
    assert harness_cli.main(["status"]) == 0
    assert seen["OUTERLOOP_CLAUDE_BIN"] == "/sandbox/claude"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("", "DEFAULT"), ("   ", "DEFAULT"), ("/x/y.env", "/x/y.env"), ("  /x/y.env  ", "/x/y.env")],
)
def test_deploy_selector_is_trimmed_like_python(value, expected):
    # Run the deploy script's own selector lines: a blank selector must mean the default
    # file (as in Python), never a failed deploy.
    import subprocess

    sh = (Path(__file__).parents[1] / "scripts" / "tick_deploy.sh").read_text().splitlines()
    start = next(i for i, line in enumerate(sh) if line.startswith("_sel="))
    snippet = "\n".join(sh[start : start + 2]) + '\necho "${_sel:-DEFAULT}"\n'
    out = subprocess.run(
        ["bash", "-c", snippet],
        env={"OUTERLOOP_ENV_FILE": value, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == expected
