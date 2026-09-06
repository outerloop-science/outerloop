"""`outerloop init` — the guided setup writes a minimal, correct .env + PAT."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from outerloop import init
from outerloop.init import (
    InitAnswers,
    author_key_env,
    render_env,
    validate_pat,
    write_author_key,
    write_config,
)


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
    assert "OUTERLOOP_COMPUTE=slurm" in env
    assert "OUTERLOOP_ROOT=/scratch/r" in env
    assert "OUTERLOOP_ACCOUNT=acct" in env
    assert "OUTERLOOP_PARTITION=cpu,gpu" in env  # comma-list preserved verbatim
    assert "OUTERLOOP_TARGET=o/r" in env
    assert "OUTERLOOP_PAT_FILE=/home/me/.config/autoresearch/bot_pat" in env
    assert "OUTERLOOP_AUTHOR_BACKEND=claude" in env


def test_render_env_omits_the_optional_keys() -> None:
    env = render_env(InitAnswers(compute="slurm", target="o/r", root="/r", account="a"), "")
    assert "OUTERLOOP_PARTITION" not in env  # optional -> Slurm default
    assert "OUTERLOOP_PAT_FILE" not in env
    assert "OUTERLOOP_AUTHOR_BACKEND" not in env


def test_render_env_local_has_no_slurm_placement() -> None:
    env = render_env(InitAnswers(compute="local", target="o/r"), "")
    assert "OUTERLOOP_COMPUTE=local" in env
    assert "OUTERLOOP_ROOT" not in env and "OUTERLOOP_ACCOUNT" not in env


def test_write_config_writes_env_and_pat_0600(tmp_path: Path) -> None:
    a = InitAnswers(compute="slurm", target="o/r", root="/r", account="a")
    env_path, pat_path = write_config(a, "ghp_secrettoken", "", config_dir=tmp_path)
    assert env_path == tmp_path / ".env"
    assert pat_path == tmp_path / "bot_pat"
    assert pat_path.read_text() == "ghp_secrettoken"  # no trailing newline
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(pat_path.stat().st_mode) == 0o600
    assert f"OUTERLOOP_PAT_FILE={pat_path}" in env_path.read_text()


def test_write_config_no_token_keeps_the_given_pat_file(tmp_path: Path) -> None:
    env_path, pat_path = write_config(
        InitAnswers(compute="local", target="o/r"), "", "/existing/pat", config_dir=tmp_path
    )
    assert pat_path is None
    assert "OUTERLOOP_PAT_FILE=/existing/pat" in env_path.read_text()


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
    assert "OUTERLOOP_TARGET=o/r" in env
    assert "OUTERLOOP_PARTITION=cpu,gpu" in env  # comma-list survives the wizard
    assert "OUTERLOOP_PAT_FILE=/some/pat" in env


def test_main_yes_requires_target(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(init, "CONFIG_DIR", tmp_path)
    assert init.main(["--yes", "--compute", "local"]) == 2
    assert "target repo is required" in capsys.readouterr().err


def test_main_yes_slurm_requires_root_and_account(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(init, "CONFIG_DIR", tmp_path)
    assert init.main(["--yes", "--compute", "slurm", "--target", "o/r"]) == 2
    assert "Slurm needs --root and --account" in capsys.readouterr().err


def test_github_app_run_asks_only_for_the_organization(tmp_path: Path, monkeypatch) -> None:
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
    # the organization question is the only prompt; nothing about the author
    assert asked == [init.ORG_PROMPT]


def test_main_yes_rejects_an_unknown_author_backend(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(init, "CONFIG_DIR", tmp_path)
    rc = init.main(["--yes", "--compute", "local", "--target", "o/r", "--author-backend", "hermes"])
    assert rc == 2
    assert "author backend must be one of claude, codex" in capsys.readouterr().err


def test_render_env_app_file_wins_over_pat() -> None:
    env = render_env(
        InitAnswers(compute="local", target="o/r"), "some-pat", app_file="/c/github_app.x.json"
    )
    assert "OUTERLOOP_GITHUB_APP_FILE=/c/github_app.x.json" in env
    assert "OUTERLOOP_PAT_FILE" not in env


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
    assert f"OUTERLOOP_GITHUB_APP_FILE={app_json}" in env
    assert "OUTERLOOP_PAT_FILE" not in env


KEEP = "OUTERLOOP_TARGET=keep/me\n"


def test_init_refuses_to_overwrite_an_existing_env_non_interactively(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(init, "CONFIG_DIR", tmp_path)
    (tmp_path / ".env").write_text(KEEP)
    rc = init.main(["--yes", "--compute", "local", "--target", "o/r", "--pat-file", "/p"])
    assert rc == 1
    assert "exists; pass --force" in capsys.readouterr().err
    assert (tmp_path / ".env").read_text() == KEEP  # untouched


def test_init_force_overwrites_an_existing_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(init, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(init, "validate_pat", lambda pf, t: "")
    (tmp_path / ".env").write_text(KEEP)
    rc = init.main(
        ["--yes", "--force", "--compute", "local", "--target", "o/r", "--pat-file", "/p"]
    )
    assert rc == 0
    assert "OUTERLOOP_TARGET=o/r" in (tmp_path / ".env").read_text()


def test_init_interactive_declined_overwrite_keeps_the_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(init, "CONFIG_DIR", tmp_path)
    (tmp_path / ".env").write_text(KEEP)
    # every other answer comes from flags, so the overwrite question is the one prompt
    monkeypatch.setattr(init, "_ask", lambda *a, **k: "n")
    rc = init.main(
        [
            "--compute",
            "local",
            "--target",
            "o/r",
            "--pat-file",
            "/p",
            "--author-backend",
            "claude",
            "--author-model",
            "m",
        ]
    )
    assert rc == 1
    assert (tmp_path / ".env").read_text() == KEEP


def test_render_env_names_the_key_file_per_backend() -> None:
    """One rule for every backend: OUTERLOOP_<BACKEND>_KEY_FILE; blank backend is claude."""
    claude = InitAnswers(compute="local", target="o/r", author_key_file="/k/claude_key")
    assert "OUTERLOOP_CLAUDE_KEY_FILE=/k/claude_key" in render_env(claude, "")
    codex = InitAnswers(
        compute="local", target="o/r", author_backend="codex", author_key_file="/k/codex_key"
    )
    assert "OUTERLOOP_CODEX_KEY_FILE=/k/codex_key" in render_env(codex, "")
    assert "KEY_FILE" not in render_env(InitAnswers(compute="local", target="o/r"), "")
    assert author_key_env("") == "OUTERLOOP_CLAUDE_KEY_FILE"


def test_write_author_key_is_owner_only_and_per_backend(tmp_path: Path) -> None:
    path = write_author_key("codex", "sk-secret", config_dir=tmp_path / "cfg")
    assert path == tmp_path / "cfg" / "codex_key"
    assert path.read_text() == "sk-secret"  # no trailing newline
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert write_author_key("", "k", config_dir=tmp_path).name == "claude_key"


def test_main_yes_author_key_file_flag_lands_in_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(init, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(init, "validate_pat", lambda pf, t: "")
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys" / "codex").write_text("sk")
    rc = init.main(
        [
            "--yes",
            "--compute",
            "local",
            "--target",
            "o/r",
            "--pat-file",
            "/p",
            "--author-backend",
            "codex",
            "--author-key-file",
            str(tmp_path / "keys" / "codex"),
        ]
    )
    assert rc == 0
    env = (tmp_path / ".env").read_text()
    assert f"OUTERLOOP_CODEX_KEY_FILE={tmp_path / 'keys' / 'codex'}" in env


def test_interactive_pasted_key_is_written_and_recorded(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The full interactive run asks for the key once (hidden), writes
    <config>/<backend>_key 0600, and points .env at it."""
    monkeypatch.setattr(init, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(init, "validate_pat", lambda pf, t: "")
    prompts: list[str] = []

    def fake_getpass(prompt: str) -> str:
        prompts.append(prompt)
        return "sk-ant-pasted\n"

    monkeypatch.setattr(init.getpass, "getpass", fake_getpass)
    rc = init.main(
        [
            "--compute",
            "local",
            "--target",
            "o/r",
            "--pat-file",
            "/p",
            "--author-backend",
            "claude",
            "--author-model",
            "claude-opus-5",
        ]
    )
    assert rc == 0
    assert len(prompts) == 1 and "claude author" in prompts[0]
    key = tmp_path / "claude_key"
    assert key.read_text() == "sk-ant-pasted"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert f"OUTERLOOP_CLAUDE_KEY_FILE={key}" in (tmp_path / ".env").read_text()
    assert "no author key set" not in capsys.readouterr().out


def test_interactive_blank_key_leaves_a_hint(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(init, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(init, "validate_pat", lambda pf, t: "")
    monkeypatch.setattr(init.getpass, "getpass", lambda prompt: "")
    rc = init.main(
        [
            "--compute",
            "local",
            "--target",
            "o/r",
            "--pat-file",
            "/p",
            "--author-backend",
            "claude",
            "--author-model",
            "claude-opus-5",
        ]
    )
    assert rc == 0
    assert not (tmp_path / "claude_key").exists()
    assert "KEY_FILE" not in (tmp_path / ".env").read_text()
    assert f"no author key set — put it in {tmp_path / 'claude_key'}" in capsys.readouterr().out


def test_github_app_run_never_asks_for_the_key(tmp_path: Path, monkeypatch) -> None:
    from outerloop import appmanifest

    monkeypatch.setattr(init, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(appmanifest, "request_manifest_code", lambda *a, **k: "c")
    monkeypatch.setattr(
        appmanifest, "convert_manifest", lambda code, **k: {"id": 1, "slug": "s", "pem": "p"}
    )
    monkeypatch.setattr(appmanifest, "capture_installation_id", lambda *a, **k: 0)
    monkeypatch.setattr("builtins.input", lambda *a: "")

    def boom(prompt: str) -> str:
        raise AssertionError("the focused --github-app run must not ask for a key")

    monkeypatch.setattr(init.getpass, "getpass", boom)
    assert init.main(["--github-app", "--compute", "local", "--target", "o/r"]) == 0


def test_write_private_never_widens(tmp_path: Path, monkeypatch) -> None:
    """A secret file is 0600 from creation even under a permissive umask, and an
    existing wider file is tightened before the write."""
    import os

    from outerloop.paths import write_private

    old = os.umask(0o000)
    try:
        fresh = tmp_path / "fresh"
        write_private(fresh, "s")
        assert stat.S_IMODE(fresh.stat().st_mode) == 0o600
        wide = tmp_path / "wide"
        wide.write_text("old")
        wide.chmod(0o644)
        write_private(wide, "new")
        assert wide.read_text() == "new"
        assert stat.S_IMODE(wide.stat().st_mode) == 0o600
    finally:
        os.umask(old)


def test_author_key_file_flag_must_be_readable_and_is_stored_absolute(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(init, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(init, "validate_pat", lambda pf, t: "")
    base = ["--yes", "--compute", "local", "--target", "o/r", "--pat-file", "/p"]
    assert init.main([*base, "--author-key-file", str(tmp_path / "missing")]) == 2
    assert "not a readable file" in capsys.readouterr().err
    assert not (tmp_path / ".env").exists()  # nothing written on a bad flag
    key = tmp_path / "k" / "codex_key"
    key.parent.mkdir()
    key.write_text("sk")
    monkeypatch.chdir(tmp_path)
    rc = init.main([*base, "--author-backend", "codex", "--author-key-file", "k/codex_key"])
    assert rc == 0
    assert f"OUTERLOOP_CODEX_KEY_FILE={key}" in (tmp_path / ".env").read_text()  # absolute
