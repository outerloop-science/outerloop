"""The config dir: `~/.config/outerloop`."""

from __future__ import annotations

from pathlib import Path

from outerloop.paths import CONFIG_DIR_NAME, config_dir


def test_fresh_machine_gets_the_new_dir(tmp_path: Path) -> None:
    assert config_dir(tmp_path) == tmp_path / ".config" / "outerloop"
    assert CONFIG_DIR_NAME == "outerloop"


def test_existing_config_dir(tmp_path: Path) -> None:
    (tmp_path / ".config" / "outerloop").mkdir(parents=True)
    assert config_dir(tmp_path) == tmp_path / ".config" / "outerloop"


def test_deploy_script_config_dir() -> None:
    sh = (Path(__file__).resolve().parents[1] / "scripts" / "tick_deploy.sh").read_text()
    assert 'ENV_FILE="$HOME/.config/outerloop/.env"' in sh
