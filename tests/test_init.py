"""`outerloop init` — the guided setup writes a minimal, correct .env + PAT."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from outerloop import init
from outerloop.init import InitAnswers, render_env, validate_pat, write_config


def test_render_env_slurm_full() -> None:
    a = InitAnswers(
        compute="slurm",
        target="o/r",
        root="/scratch/r",
        account="acct",
        partition="cpu,gpu",
        author_backend="claude",
        author_model="opus",
    )
    env = render_env(a, "/home/me/.config/autoresearch/bot_pat")
    assert "AUTORESEARCH_COMPUTE=slurm" in env
    assert "AUTORESEARCH_ROOT=/scratch/r" in env
    assert "AUTORESEARCH_ACCOUNT=acct" in env
    assert "AUTORESEARCH_PARTITION=cpu,gpu" in env  # comma-list preserved verbatim
    assert "AUTORESEARCH_TARGET=o/r" in env
    assert "AUTORESEARCH_PAT_FILE=/home/me/.config/autoresearch/bot_pat" in env
    assert "AUTORESEARCH_AUTHOR_BACKEND=claude" in env


def test_render_env_omits_the_optional_keys() -> None:
    env = render_env(InitAnswers(compute="slurm", target="o/r", root="/r", account="a"), "")
    assert "AUTORESEARCH_PARTITION" not in env  # optional -> Slurm default
    assert "AUTORESEARCH_PAT_FILE" not in env
    assert "AUTORESEARCH_AUTHOR_BACKEND" not in env


def test_render_env_local_has_no_slurm_placement() -> None:
    env = render_env(InitAnswers(compute="local", target="o/r"), "")
    assert "AUTORESEARCH_COMPUTE=local" in env
    assert "AUTORESEARCH_ROOT" not in env and "AUTORESEARCH_ACCOUNT" not in env


def test_write_config_writes_env_and_pat_0600(tmp_path: Path) -> None:
    a = InitAnswers(compute="slurm", target="o/r", root="/r", account="a")
    env_path, pat_path = write_config(a, "ghp_secrettoken", "", config_dir=tmp_path)
    assert env_path == tmp_path / ".env"
    assert pat_path == tmp_path / "bot_pat"
    assert pat_path.read_text() == "ghp_secrettoken"  # no trailing newline
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(pat_path.stat().st_mode) == 0o600
    assert f"AUTORESEARCH_PAT_FILE={pat_path}" in env_path.read_text()


def test_write_config_no_token_keeps_the_given_pat_file(tmp_path: Path) -> None:
    env_path, pat_path = write_config(
        InitAnswers(compute="local", target="o/r"), "", "/existing/pat", config_dir=tmp_path
    )
    assert pat_path is None
    assert "AUTORESEARCH_PAT_FILE=/existing/pat" in env_path.read_text()


def test_validate_pat_read_errors(tmp_path: Path) -> None:
    assert "could not read" in validate_pat(str(tmp_path / "nope"), "o/r")
    empty = tmp_path / "empty"
    empty.write_text("")
    assert "empty" in validate_pat(str(empty), "o/r")


class _Resp:
    def __init__(self, body: dict) -> None:
        self._b = body

    def read(self) -> bytes:
        return json.dumps(self._b).encode()

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: object) -> None:
        return None


def test_validate_pat_ok_and_no_push(tmp_path: Path, monkeypatch) -> None:
    pat = tmp_path / "pat"
    pat.write_text("ghp_x")
    monkeypatch.setattr(
        init.urllib.request,
        "urlopen",
        lambda req, timeout=15: _Resp({"permissions": {"push": True}}),
    )
    assert validate_pat(str(pat), "o/r") == ""
    monkeypatch.setattr(
        init.urllib.request,
        "urlopen",
        lambda req, timeout=15: _Resp({"permissions": {"push": False}}),
    )
    assert "lacks write" in validate_pat(str(pat), "o/r")


def test_main_yes_writes_config(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(init, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(init, "validate_pat", lambda pf, t: "")
    rc = init.main(
        [
            "--yes",
            "--compute",
            "slurm",
            "--target",
            "o/r",
            "--root",
            "/r",
            "--account",
            "a",
            "--partition",
            "cpu,gpu",
            "--pat-file",
            "/some/pat",
        ]
    )
    assert rc == 0
    env = (tmp_path / ".env").read_text()
    assert "AUTORESEARCH_TARGET=o/r" in env
    assert "AUTORESEARCH_PARTITION=cpu,gpu" in env  # comma-list survives the wizard
    assert "AUTORESEARCH_PAT_FILE=/some/pat" in env


def test_main_yes_requires_target(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(init, "CONFIG_DIR", tmp_path)
    assert init.main(["--yes", "--compute", "local"]) == 2
    assert "target repo is required" in capsys.readouterr().err


def test_main_yes_slurm_requires_root_and_account(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(init, "CONFIG_DIR", tmp_path)
    assert init.main(["--yes", "--compute", "slurm", "--target", "o/r"]) == 2
    assert "Slurm needs --root and --account" in capsys.readouterr().err


def test_github_app_run_asks_nothing_about_the_author(tmp_path: Path, monkeypatch) -> None:
    """A focused --github-app run is about auth; it must not prompt for the author."""
    from outerloop import appmanifest

    asked: list[str] = []

    def fake_ask(prompt: str, *a: object, **k: object) -> str:
        asked.append(prompt)
        return ""

    monkeypatch.setattr(init, "_ask", fake_ask)
    monkeypatch.setattr(init, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(appmanifest, "request_manifest_code", lambda *a, **k: "c")
    monkeypatch.setattr(
        appmanifest, "convert_manifest", lambda code, **k: {"id": 1, "slug": "s", "pem": "p"}
    )
    monkeypatch.setattr(appmanifest, "capture_installation_id", lambda *a, **k: 0)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    assert init.main(["--github-app", "--compute", "local", "--target", "o/r"]) == 0
    assert asked == []  # no author (or any other) prompt on the way


def test_main_yes_rejects_an_unknown_author_backend(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(init, "CONFIG_DIR", tmp_path)
    rc = init.main(["--yes", "--compute", "local", "--target", "o/r", "--author-backend", "hermes"])
    assert rc == 2
    assert "author backend must be one of claude, codex" in capsys.readouterr().err


def test_render_env_app_file_wins_over_pat() -> None:
    env = render_env(
        InitAnswers(compute="local", target="o/r"), "some-pat", app_file="/c/github_app.x.json"
    )
    assert "AUTORESEARCH_GITHUB_APP_FILE=/c/github_app.x.json" in env
    assert "AUTORESEARCH_PAT_FILE" not in env


def test_main_github_app_writes_app_env(tmp_path: Path, monkeypatch, capsys) -> None:
    from outerloop import appmanifest

    monkeypatch.setattr(init, "CONFIG_DIR", tmp_path)
    conv = {"id": 42, "slug": "sl", "pem": "PEMDATA"}
    monkeypatch.setattr(appmanifest, "request_manifest_code", lambda *a, **k: "code123")
    monkeypatch.setattr(appmanifest, "convert_manifest", lambda code, **k: conv)
    monkeypatch.setattr(appmanifest, "capture_installation_id", lambda *a, **k: 999)
    monkeypatch.setattr("builtins.input", lambda *a: "")  # author prompts + the install-Enter
    rc = init.main(["--github-app", "--compute", "local", "--target", "o/r"])
    assert rc == 0
    app_json = tmp_path / "github_app.sl.json"
    assert json.loads(app_json.read_text())["installation_id"] == 999
    env = (tmp_path / ".env").read_text()
    assert f"AUTORESEARCH_GITHUB_APP_FILE={app_json}" in env
    assert "AUTORESEARCH_PAT_FILE" not in env
