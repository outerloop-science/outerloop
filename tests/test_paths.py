"""The config dir: `~/.config/outerloop`, with the pre-rename dir still honored."""

from __future__ import annotations

from pathlib import Path

from outerloop.paths import CONFIG_DIR_NAMES, config_dir


def test_fresh_machine_gets_the_new_dir(tmp_path: Path) -> None:
    assert config_dir(tmp_path) == tmp_path / ".config" / "outerloop"
    assert CONFIG_DIR_NAMES[0] == "outerloop"


def test_legacy_dir_is_honored_when_it_is_the_only_one(tmp_path: Path) -> None:
    (tmp_path / ".config" / "autoresearch").mkdir(parents=True)
    assert config_dir(tmp_path) == tmp_path / ".config" / "autoresearch"


def test_new_dir_wins_when_both_exist(tmp_path: Path) -> None:
    (tmp_path / ".config" / "autoresearch").mkdir(parents=True)
    (tmp_path / ".config" / "outerloop").mkdir(parents=True)
    assert config_dir(tmp_path) == tmp_path / ".config" / "outerloop"


def test_deploy_script_checks_both_dirs() -> None:
    sh = (Path(__file__).resolve().parents[1] / "scripts" / "tick_deploy.sh").read_text()
    assert 'ENV_FILE="$HOME/.config/outerloop/.env"' in sh
    assert '[ -r "$ENV_FILE" ] || ENV_FILE="$HOME/.config/autoresearch/.env"' in sh
