"""The config dir: `~/.config/outerloop`."""

from __future__ import annotations

from pathlib import Path

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
    assert 'ENV_FILE="${OUTERLOOP_ENV_FILE-$HOME/.config/outerloop/.env}"' in sh


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
