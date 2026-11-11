"""`outerloop init` — the guided setup writes a minimal, correct .env + PAT."""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from outerloop import init
from outerloop.appmanifest import DEFAULT_PERMISSIONS
from outerloop.init import (
    InitAnswers,
    author_key_env,
    render_env,
    validate_pat,
    write_author_key,
    write_config,
)


@pytest.fixture
def configured(tmp_path: Path, monkeypatch) -> Path:
    """A temp config dir and a PAT check that passes."""
    monkeypatch.setattr(init, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(init, "validate_pat", lambda pf, t: "")
    return tmp_path


def test_render_env_writes_the_claude_model() -> None:
    a = InitAnswers(compute="local", target="o/r", claude_model="claude-x", author_backend="codex")
    env = render_env(a, "")
    assert "\nOUTERLOOP_CLAUDE_MODEL=claude-x\n" in env
    assert "CLAUDE_MODEL" not in render_env(InitAnswers(compute="local", target="o/r"), "")


def test_claude_model_is_required_flag_then_shell_then_prompt(
    configured: Path, monkeypatch, capsys
) -> None:
    """Every Claude role reads OUTERLOOP_CLAUDE_MODEL and there is no code
    default, so the full setup insists on it: --claude-model, else the shell's
    value, else a required prompt; --yes without either refuses."""
    monkeypatch.delenv("OUTERLOOP_CLAUDE_MODEL", raising=False)
    base = ["--yes", "--compute", "local", "--target", "o/r", "--pat-file", "/p", "--force"]
    assert init.main(base) == 2
    err = capsys.readouterr().err
    assert "--claude-model is required" in err and "OUTERLOOP_CLAUDE_MODEL" in err
    assert not (configured / ".env").exists()
    # a codex author still keeps the default (claude) panel: required all the same
    assert init.main([*base, "--author-backend", "codex", "--author-model", "gpt-x"]) == 2
    assert init.main([*base, "--claude-model", "claude-flag"]) == 0
    assert "OUTERLOOP_CLAUDE_MODEL=claude-flag\n" in (configured / ".env").read_text()
    monkeypatch.setenv("OUTERLOOP_CLAUDE_MODEL", " claude-shell ")
    assert init.main(base) == 0
    assert "OUTERLOOP_CLAUDE_MODEL=claude-shell\n" in (configured / ".env").read_text()
    assert init.main([*base, "--claude-model", "claude-flag"]) == 0  # the flag wins
    assert "OUTERLOOP_CLAUDE_MODEL=claude-flag\n" in (configured / ".env").read_text()
    # interactive: asked (required) when neither flag nor shell has it
    monkeypatch.delenv("OUTERLOOP_CLAUDE_MODEL", raising=False)
    asked: list[tuple[str, dict[str, Any]]] = []

    def fake_ask(prompt: str, default: str = "", **kw: Any) -> str:
        asked.append((prompt, kw))
        return "claude-typed" if prompt.startswith("Claude model") else default

    monkeypatch.setattr(init, "_ask", fake_ask)
    monkeypatch.setattr(init.getpass, "getpass", lambda prompt: "")
    argv = ["--compute", "local", "--target", "o/r", "--pat-file", "/p", "--force"]
    assert init.main([*argv, "--author-backend", "claude"]) == 0
    assert [kw for prompt, kw in asked if prompt.startswith("Claude model")] == [{"required": True}]
    assert "OUTERLOOP_CLAUDE_MODEL=claude-typed\n" in (configured / ".env").read_text()


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
    slurm = render_env(InitAnswers(compute="slurm", target="o/r", root="/r", account="a"), "")
    assert "OUTERLOOP_PARTITION" not in slurm  # optional -> Slurm default
    assert "OUTERLOOP_PAT_FILE" not in slurm
    assert "OUTERLOOP_AUTHOR_BACKEND" not in slurm
    local = render_env(InitAnswers(compute="local", target="o/r"), "")
    assert "OUTERLOOP_COMPUTE=local" in local
    assert "OUTERLOOP_ROOT" not in local and "OUTERLOOP_ACCOUNT" not in local


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


@pytest.fixture(autouse=True)
def _no_image_download(monkeypatch, request):
    """init never reaches the network in tests: no image is found or fetched, the
    token's login is not looked up, and no harness binary is found on this machine."""
    monkeypatch.setattr(init, "install_harness", lambda backend: "")
    monkeypatch.setattr(init, "ensure_image", lambda **kw: "")
    monkeypatch.setattr(init, "_token_login", lambda token: "")
    # the runtime question comes only where enroot is installed and Apptainer
    # is not, so it depends on the machine; answered here up front
    monkeypatch.setenv("OUTERLOOP_CONTAINER_RUNTIME", "apptainer")
    if not request.node.get_closest_marker("real_locate_harness"):
        monkeypatch.setattr(init, "locate_harness", lambda backend: "")


def test_the_image_reaches_the_tick() -> None:
    """start exports OUTERLOOP_IMAGE from .env to the loop, and the chain's deploy
    allowlist carries it every tick (a parity test keeps the two lists equal)."""
    from outerloop.cli import TICK_ENV_KEYS

    assert "OUTERLOOP_IMAGE" in TICK_ENV_KEYS


def test_render_env_records_the_image_when_set() -> None:
    a = InitAnswers(compute="local", target="o/r", image="/img/agent-py312.sif")
    assert "OUTERLOOP_IMAGE=/img/agent-py312.sif" in render_env(a, "/p")
    assert "OUTERLOOP_IMAGE" not in render_env(InitAnswers(compute="local", target="o/r"), "/p")


def test_a_slurm_env_carries_the_default_update_policy() -> None:
    """The checkout-based deployment gets `off` written out so the knob is
    visible where it is set; the local loop runs the installed package."""
    slurm = render_env(InitAnswers(compute="slurm", target="o/r", root="/r"), "")
    assert "\nOUTERLOOP_AUTO_UPDATE=off\n" in slurm
    assert "AUTO_UPDATE" not in render_env(InitAnswers(compute="local", target="o/r"), "")
    # --no-image writes the off-switch, so an image already on disk is not picked up
    off = InitAnswers(compute="local", target="o/r", uncontained=True)
    assert "\nOUTERLOOP_IMAGE=\n" in render_env(off, "/p")


def test_main_yes_records_a_given_image_and_no_image_skips(configured: Path, monkeypatch) -> None:
    calls: list[dict] = []

    def record(**kw: Any) -> str:
        calls.append(kw)
        return ""

    monkeypatch.setattr(init, "ensure_image", record)
    base = ["--yes", "--compute", "local", "--target", "o/r", "--pat-file", "/some/pat"]
    sif = configured / "x.sif"
    sif.write_text("")
    assert init.main([*base, "--image", str(sif)]) == 0
    assert f"OUTERLOOP_IMAGE={sif}" in (configured / ".env").read_text()
    assert calls == []  # an explicit image is never re-fetched
    assert init.main([*base, "--no-image", "--force"]) == 0
    assert "\nOUTERLOOP_IMAGE=\n" in (configured / ".env").read_text()  # the off-switch
    assert calls == []
    assert init.main([*base, "--force"]) == 0
    assert calls and calls[0]["interactive"] is False  # the default asks the image module


def test_main_checks_the_image_path_and_downloads_only_when_setup_proceeds(
    configured: Path, monkeypatch, capsys
) -> None:
    """A mistyped --image fails init instead of surfacing as an uncontained loop
    later; the published image is fetched only after every check passed and
    the existing-config question is settled (terra, #305)."""
    calls: list[dict] = []

    def record(**kw: Any) -> str:
        calls.append(kw)
        return ""

    monkeypatch.setattr(init, "ensure_image", record)
    base = ["--yes", "--compute", "local", "--pat-file", "/some/pat"]
    missing = configured / "missing.sif"
    assert init.main([*base, "--target", "o/r", "--image", str(missing)]) == 2
    assert "is not a file" in capsys.readouterr().err
    assert calls == []
    assert init.main(base) == 2  # no target: rejected before any download
    assert calls == []
    (configured / ".env").write_text("OUTERLOOP_COMPUTE=local\n")
    assert init.main([*base, "--target", "o/r"]) == 1  # existing config, no --force
    assert calls == []
    assert init.main([*base, "--target", "o/r", "--force"]) == 0
    assert len(calls) == 1
    # the two flags contradict each other: argparse refuses the pair
    with pytest.raises(SystemExit):
        init.main([*base, "--target", "o/r", "--image", "/x.sif", "--no-image"])
    assert "not allowed with" in capsys.readouterr().err


def test_render_env_writes_a_named_runtime_and_leaves_an_unnamed_one_out() -> None:
    for name in ("enroot", "apptainer"):
        named = InitAnswers(compute="local", target="o/r", container_runtime=name)
        assert f"\nOUTERLOOP_CONTAINER_RUNTIME={name}\n" in render_env(named, "/p")
    assert "CONTAINER_RUNTIME" not in render_env(InitAnswers(compute="local", target="o/r"), "/p")


def test_main_records_the_runtime_and_probes_and_fetches_with_it(
    configured: Path, monkeypatch
) -> None:
    from outerloop.container import ApptainerRuntime, EnrootRuntime

    calls: list[dict] = []

    def record(**kw: Any) -> str:
        calls.append(kw)
        return ""

    monkeypatch.setattr(init, "ensure_image", record)
    for key in ("OUTERLOOP_CONTAINER_RUNTIME", "OUTERLOOP_ENROOT_BIN", "OUTERLOOP_APPTAINER_BIN"):
        monkeypatch.delenv(key, raising=False)
    base = ["--yes", "--compute", "local", "--target", "o/r", "--pat-file", "/p", "--force"]
    assert init.main([*base, "--container-runtime", "enroot"]) == 0
    assert "\nOUTERLOOP_CONTAINER_RUNTIME=enroot\n" in (configured / ".env").read_text()
    assert calls[-1]["runtime"] == EnrootRuntime("enroot")
    # a later run keeps the recorded choice without the flag
    assert init.main(base) == 0
    assert "\nOUTERLOOP_CONTAINER_RUNTIME=enroot\n" in (configured / ".env").read_text()
    assert calls[-1]["runtime"] == EnrootRuntime("enroot")
    # the flag is written even for the default, so an exported value cannot win
    monkeypatch.setenv("OUTERLOOP_CONTAINER_RUNTIME", "enroot")
    assert init.main([*base, "--container-runtime", "apptainer"]) == 0
    assert "\nOUTERLOOP_CONTAINER_RUNTIME=apptainer\n" in (configured / ".env").read_text()
    assert calls[-1]["runtime"] == ApptainerRuntime("apptainer")


def test_main_keeps_the_container_settings_it_probes_with(configured: Path, monkeypatch) -> None:
    """A full re-run rewrites .env; the runtime's settings survive it, and the
    probe already reads them from the file."""
    from outerloop.container import EnrootRuntime

    calls: list[dict] = []

    def record(**kw: Any) -> str:
        calls.append(kw)
        return ""

    monkeypatch.setattr(init, "ensure_image", record)
    kept = {
        "OUTERLOOP_CONTAINER_RUNTIME": "enroot",
        "OUTERLOOP_ENROOT_BIN": "/apps/enroot",
        "OUTERLOOP_ENROOT_SHARED_IPC": "1",
        "OUTERLOOP_APPTAINER_BIN": "/apps/apptainer",
    }
    for key in (*kept, "OUTERLOOP_ENROOT_SHARED_PROCESSES"):
        monkeypatch.delenv(key, raising=False)
    (configured / ".env").write_text("".join(f"{k}={v}\n" for k, v in kept.items()))
    base = ["--yes", "--compute", "local", "--target", "o/r", "--pat-file", "/p", "--force"]
    assert init.main(base) == 0
    env = (configured / ".env").read_text()
    assert all(f"\n{k}={v}\n" in f"\n{env}" for k, v in kept.items())
    assert calls[-1]["runtime"] == EnrootRuntime("/apps/enroot", shared_ipc=True)


def test_main_refuses_an_unknown_runtime(configured: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("OUTERLOOP_CONTAINER_RUNTIME", "docker")
    base = ["--yes", "--compute", "local", "--target", "o/r", "--pat-file", "/p", "--force"]
    assert init.main(base) == 2
    assert "choose apptainer or enroot" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        init.main([*base, "--container-runtime", "podman"])


def test_enroot_is_proposed_only_when_it_is_installed_and_apptainer_is_not(monkeypatch) -> None:
    """init never switches runtimes on its own: it asks, and only where
    Apptainer is missing and enroot is on PATH."""
    from argparse import Namespace

    installed: set[str] = set()
    monkeypatch.setattr(init.shutil, "which", lambda name: name if name in installed else None)
    asked: list[str] = []
    answer = ["y"]

    def ask(prompt: str, default: str = "", **kw: Any) -> str:
        asked.append(prompt)
        return answer[0]

    monkeypatch.setattr(init, "_ask", ask)
    none = Namespace(container_runtime=None)
    assert init._container_runtime(none, {}, True) == "" and asked == []
    installed.add("enroot")
    assert init._container_runtime(none, {}, True) == "enroot" and len(asked) == 1
    answer[0] = "n"
    assert init._container_runtime(none, {}, True) == ""
    assert init._container_runtime(none, {}, False) == "" and len(asked) == 2
    installed.add("apptainer")
    assert init._container_runtime(none, {}, True) == "" and len(asked) == 2
    # a flag or a recorded choice is never second-guessed
    assert init._container_runtime(Namespace(container_runtime="enroot"), {}, True) == "enroot"
    recorded = {"OUTERLOOP_CONTAINER_RUNTIME": "apptainer"}
    assert init._container_runtime(none, recorded, True) == "apptainer"
    assert len(asked) == 2


def test_main_yes_requires_target(configured: Path, capsys) -> None:
    assert init.main(["--yes", "--compute", "local"]) == 2
    assert "target repo is required" in capsys.readouterr().err


def test_main_yes_slurm_requires_root_only(configured: Path, capsys) -> None:
    """The state root is the one Slurm setting init insists on; the account is
    optional (#300), so an accountless config omits the line and Slurm's
    default applies."""
    assert init.main(["--yes", "--compute", "slurm", "--target", "o/r"]) == 2
    assert "Slurm needs --root" in capsys.readouterr().err
    argv = ["--yes", "--compute", "slurm", "--target", "o/r", "--root", "/r"]
    assert init.main([*argv, "--partition", "cpu,gpu", "--pat-file", "/some/pat"]) == 0
    env = (configured / ".env").read_text()
    assert "OUTERLOOP_ROOT=/r" in env
    assert "OUTERLOOP_PARTITION=cpu,gpu" in env  # comma-list survives the wizard
    assert "OUTERLOOP_PAT_FILE=/some/pat" in env
    assert "OUTERLOOP_ACCOUNT" not in env


def test_github_app_run_asks_only_for_the_organization(configured: Path, monkeypatch) -> None:
    """A focused --github-app run is about auth; it must not prompt for the
    author or the author key."""
    from outerloop import appmanifest

    asked: list[str] = []

    def fake_ask(prompt: str, *a: object, **k: object) -> str:
        asked.append(prompt)
        return ""

    def boom(prompt: str) -> str:
        raise AssertionError("the focused --github-app run must not ask for a key")

    monkeypatch.setattr(init, "_ask", fake_ask)
    monkeypatch.setattr(init.getpass, "getpass", boom)
    monkeypatch.delenv("OUTERLOOP_CLAUDE_MODEL", raising=False)  # nor for the Claude model
    monkeypatch.setattr(appmanifest, "request_manifest_code", lambda *a, **k: "c")
    monkeypatch.setattr(
        appmanifest, "convert_manifest", lambda code, **k: {"id": 1, "slug": "s", "pem": "p"}
    )
    monkeypatch.setattr(appmanifest, "capture_installation_id", lambda *a, **k: 0)
    monkeypatch.setattr(init, "_owner_type", lambda owner: "Organization")
    monkeypatch.setattr("builtins.input", lambda *a: "")
    # capture_installation_id returned 0: the App was never installed, so init keeps
    # the credentials but exits 1 and never says start (terra, #309)
    assert init.main(["--github-app", "--compute", "local", "--target", "o/r"]) == 1
    written = (configured / ".env").read_text()
    assert "OUTERLOOP_BOT_LOGIN=s[bot]" in written  # the login it recognizes itself by
    # the organization question is the only prompt; nothing about the author
    assert asked == [init.ORG_PROMPT]


def test_app_owner_mismatch_note_fires_only_on_a_fallback() -> None:
    org, target = "agentic-learning-ai-lab", "agentic-learning-ai-lab/autoresearch-forecast"
    # non-owner fallback: App landed under a personal account, not the org
    note = init._app_owner_mismatch_note({"owner": {"login": "amelia"}}, org, target)
    assert "'amelia'" in note and f"'{org}'" in note
    assert "Make public" in note or "make the App public" in note
    assert "approve" in note and "--force" in note
    # landed where intended (org owner, or a personal target): no note
    assert init._app_owner_mismatch_note({"owner": {"login": org}}, org, target) == ""
    assert init._app_owner_mismatch_note({"owner": {"login": "Amelia"}}, "amelia", "amelia/r") == ""
    assert init._app_owner_mismatch_note({}, org, target) == ""


def test_app_failure_names_the_make_public_and_owner_approval_path(capsys) -> None:
    rc = init._app_failure(InitAnswers(compute="local", target="the-lab/forecast"), "s", "no write")
    assert rc == 1  # the check failed, so init keeps the creds and never says start
    err = capsys.readouterr().err
    assert "cannot write the-lab/forecast" in err
    assert "make the App public" in err and "org owner approve" in err


def test_github_app_warns_when_it_lands_under_a_different_account(
    configured: Path, monkeypatch, capsys
) -> None:
    """A non-owner org member gets a personal App; init must say so and name the
    make-public + owner-approval path rather than silently dead-ending (#349)."""
    from outerloop import appmanifest

    monkeypatch.setattr(init, "_ask", lambda *a, **k: "")
    monkeypatch.setattr(init, "_owner_type", lambda owner: "Organization")
    monkeypatch.setattr(appmanifest, "request_manifest_code", lambda *a, **k: "c")
    monkeypatch.setattr(
        appmanifest,
        "convert_manifest",
        lambda code, **k: {"id": 1, "slug": "s", "pem": "p", "owner": {"login": "amelia"}},
    )
    monkeypatch.setattr(appmanifest, "capture_installation_id", lambda *a, **k: 0)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    rc = init.main(["--github-app", "--compute", "local", "--target", "the-lab/forecast"])
    assert rc == 1  # not installed, so it still exits 1 and keeps the creds
    err = capsys.readouterr().err
    assert "was created under 'amelia', not 'the-lab'" in err
    assert "make the App public" in err and "approve" in err


def test_main_yes_rejects_an_unknown_author_backend(configured: Path, capsys) -> None:
    rc = init.main(
        ["--yes", "--compute", "local", "--target", "o/r", "--author-backend", "unknown"]
    )
    assert rc == 2
    assert "author backend must be one of claude, codex" in capsys.readouterr().err


def test_render_env_app_file_wins_over_pat() -> None:
    env = render_env(
        InitAnswers(compute="local", target="o/r"), "some-pat", app_file="/c/github_app.x.json"
    )
    assert "OUTERLOOP_GITHUB_APP_FILE=/c/github_app.x.json" in env
    assert "OUTERLOOP_PAT_FILE" not in env


def test_main_github_app_writes_app_env(configured: Path, monkeypatch, capsys) -> None:
    from outerloop import appmanifest

    conv = {"id": 42, "slug": "sl", "pem": "PEMDATA"}
    monkeypatch.setattr(appmanifest, "request_manifest_code", lambda *a, **k: "code123")
    monkeypatch.setattr(appmanifest, "convert_manifest", lambda code, **k: conv)
    monkeypatch.setattr(appmanifest, "capture_installation_id", lambda *a, **k: 999)
    monkeypatch.setattr("builtins.input", lambda *a: "")  # author prompts + the install-Enter
    (configured / ".env").write_text("OUTERLOOP_IDENTITY=personal\n")  # switching to the App
    rc = init.main(["--github-app", "--force", "--compute", "local", "--target", "o/r"])
    assert rc == 0
    app_json = configured / "github_app.sl.json"
    assert json.loads(app_json.read_text())["installation_id"] == 999
    env = (configured / ".env").read_text()
    assert f"OUTERLOOP_GITHUB_APP_FILE={app_json}" in env
    assert "OUTERLOOP_PAT_FILE" not in env
    assert "OUTERLOOP_IDENTITY" not in env  # a dedicated identity, not personal mode


KEEP = "OUTERLOOP_TARGET=keep/me\n"


def test_init_refuses_to_overwrite_an_existing_env_non_interactively(
    configured: Path, capsys
) -> None:
    (configured / ".env").write_text(KEEP)
    rc = init.main(["--yes", "--compute", "local", "--target", "o/r", "--pat-file", "/p"])
    assert rc == 1
    assert "exists; pass --force" in capsys.readouterr().err
    assert (configured / ".env").read_text() == KEEP  # untouched


def test_init_force_overwrites_an_existing_env(configured: Path) -> None:
    (configured / ".env").write_text(KEEP)
    rc = init.main(
        ["--yes", "--force", "--compute", "local", "--target", "o/r", "--pat-file", "/p"]
    )
    assert rc == 0
    assert "OUTERLOOP_TARGET=o/r" in (configured / ".env").read_text()


def test_init_local_forge_bootstraps_without_github_credentials(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """`--forge local` seeds a bare repository under the state root and writes
    no GitHub credential. Re-running init never replaces an existing forge."""
    monkeypatch.setattr(init, "CONFIG_DIR", tmp_path / "config")
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "README.md").write_text("seed\n")
    git = ["git", "-C", str(seed)]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run(
        [*git, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed"], check=True
    )
    state = tmp_path / "state"
    argv = [
        "--yes",
        "--compute",
        "local",
        "--target",
        "o/r",
        "--root",
        str(state),
        "--forge",
        "local",
        "--forge-source",
        str(seed),
        "--claude-model",
        "claude-x",
    ]
    assert init.main(argv) == 0
    env = (tmp_path / "config" / ".env").read_text()
    assert "OUTERLOOP_FORGE=local" in env
    assert f"OUTERLOOP_ROOT={state}" in env
    assert "OUTERLOOP_PAT_FILE" not in env and "OUTERLOOP_GITHUB_APP_FILE" not in env
    repository = state / "forge" / "o" / "r.git"
    assert repository.is_dir()
    head = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    # a re-run refreshes the settings and leaves the forge repository alone
    assert init.main([*argv, "--force"]) == 0
    assert "left unchanged" in capsys.readouterr().out
    assert (
        subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "main"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == head
    )


def test_init_local_forge_writes_the_state_root_it_bootstrapped_under(
    tmp_path: Path, monkeypatch
) -> None:
    """`OUTERLOOP_ROOT` from the environment may be the only root the bootstrap
    sees; the config must record it, or a later start without the variable
    opens a different root than the forge repository lives under."""
    monkeypatch.setattr(init, "CONFIG_DIR", tmp_path / "config")
    state = tmp_path / "state"
    monkeypatch.setenv("OUTERLOOP_ROOT", str(state))
    rc = init.main(
        [
            "--yes",
            "--compute",
            "local",
            "--target",
            "o/r",
            "--forge",
            "local",
            "--claude-model",
            "claude-x",
        ]
    )
    assert rc == 0
    env = (tmp_path / "config" / ".env").read_text()
    assert f"OUTERLOOP_ROOT={state.resolve()}" in env
    assert (state / "forge" / "o" / "r.git").is_dir()
    # the variable was only the bootstrap's source; the file now carries it
    monkeypatch.delenv("OUTERLOOP_ROOT")
    assert f"OUTERLOOP_ROOT={state.resolve()}" in (tmp_path / "config" / ".env").read_text()


def test_init_interactive_declined_overwrite_keeps_the_config(
    configured: Path, monkeypatch
) -> None:
    (configured / ".env").write_text(KEEP)
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
    assert (configured / ".env").read_text() == KEEP


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


def test_main_yes_author_key_file_flag_lands_in_env(configured: Path) -> None:
    (configured / "keys").mkdir()
    (configured / "keys" / "codex").write_text("sk")
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
            str(configured / "keys" / "codex"),
        ]
    )
    assert rc == 0
    env = (configured / ".env").read_text()
    assert f"OUTERLOOP_CODEX_KEY_FILE={configured / 'keys' / 'codex'}" in env


def test_interactive_pasted_key_is_written_and_recorded(
    configured: Path, monkeypatch, capsys
) -> None:
    """The full interactive run asks for the key once (hidden), writes
    <config>/<backend>_key 0600, and points .env at it."""
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
    key = configured / "claude_key"
    assert key.read_text() == "sk-ant-pasted"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert f"OUTERLOOP_CLAUDE_KEY_FILE={key}" in (configured / ".env").read_text()
    assert "no author key set" not in capsys.readouterr().out


def test_interactive_blank_key_leaves_a_hint(configured: Path, monkeypatch, capsys) -> None:
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
    assert not (configured / "claude_key").exists()
    assert "KEY_FILE" not in (configured / ".env").read_text()
    assert f"no author key set — put it in {configured / 'claude_key'}" in capsys.readouterr().out


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
    configured: Path, monkeypatch, capsys
) -> None:
    base = ["--yes", "--compute", "local", "--target", "o/r", "--pat-file", "/p"]
    assert init.main([*base, "--author-key-file", str(configured / "missing")]) == 2
    assert "not a readable file" in capsys.readouterr().err
    assert not (configured / ".env").exists()  # nothing written on a bad flag
    key = configured / "k" / "codex_key"
    key.parent.mkdir()
    key.write_text("sk")
    monkeypatch.chdir(configured)
    rc = init.main([*base, "--author-backend", "codex", "--author-key-file", "k/codex_key"])
    assert rc == 0
    assert f"OUTERLOOP_CODEX_KEY_FILE={key}" in (configured / ".env").read_text()  # absolute


def test_app_check_asks_the_installation_not_the_repo_permissions(monkeypatch) -> None:
    """An installation token leaves a repository's `permissions` object all
    false even when the App can push, so the App check reads the installation's
    repository list and its granted permissions instead."""
    import io
    import json as _json
    import urllib.error

    class Provider:
        app_id = 1
        installation_id = 2

        def _sign(self, data: bytes) -> bytes:
            return b"sig"

        def _transport(self, req):
            with init.urllib.request.urlopen(req) as response:
                return json.loads(response.read())

        def token(self) -> str:
            return "tok"

    answers: dict[str, Any] = {
        "app": {"slug": "app", "owner": {"login": "o", "type": "User"}, "permissions": {}},
        "repos/o/r/installation": {
            "id": 2,
            "permissions": dict(DEFAULT_PERMISSIONS),
        },
    }

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        for suffix, body in answers.items():
            if req.full_url.endswith(suffix):
                if body is None:  # the installation does not cover it: GitHub 404s
                    raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)  # type: ignore[arg-type]
                return Resp(_json.dumps(body).encode())
        raise AssertionError(req.full_url)

    monkeypatch.setattr(init.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("outerloop.appauth.build_app_jwt", lambda *a, **k: "jwt")
    assert init.app_permission_gaps(Provider(), "o/r").problem == ""
    answers["repos/o/r/installation"] = None  # not installed there: GitHub 404s, public or not
    assert "not installed on o/r" in init.app_permission_gaps(Provider(), "o/r").problem
    answers["repos/o/r/installation"] = {
        "id": 2,
        "permissions": {"contents": "read", "issues": "write", "pull_requests": "write"},
        "account": {"login": "o", "type": "User"},
    }
    problem = init.app_permission_gaps(Provider(), "o/r").problem
    assert "contents: write" in problem
    assert "https://github.com/settings/apps/app/permissions" in problem
    answers["repos/o/r/installation"] = {"id": 9, "permissions": {"contents": "write"}}
    assert "installation 9" in init.app_permission_gaps(Provider(), "o/r").problem


def test_a_credential_that_cannot_open_prs_fails_init(
    configured: Path, monkeypatch, capsys
) -> None:
    """#285: a 401/403/404 or no write access exits 1 and never says
    `next: outerloop start`; a network failure stays a warning."""
    base = ["--yes", "--compute", "local", "--target", "o/r", "--pat-file", "/some/pat"]
    for problem in (
        "the token is invalid or expired (GitHub returned 401)",
        "o/r not found, or the token cannot see it",
        "reaches o/r but lacks write access (it opens PRs)",
    ):
        monkeypatch.setattr(init, "validate_pat", lambda pf, t, p=problem: p)
        assert init.main([*base, "--force"]) == 1, problem
        captured = capsys.readouterr()
        assert "FAILED" in captured.err and "init --force" in captured.err
        assert "next: outerloop start" not in captured.out
    monkeypatch.setattr(init, "validate_pat", lambda pf, t: "could not reach GitHub: timed out")
    assert init.main([*base, "--force"]) == 0
    captured = capsys.readouterr()
    assert "WARNING" in captured.out and "next: outerloop start" in captured.out
    # the login is recorded only after a passing check: say so, or the tick
    # silently skips the target for want of OUTERLOOP_BOT_LOGIN
    assert "OUTERLOOP_BOT_LOGIN not recorded" in captured.out


def test_pat_path_records_the_tokens_login_or_says_it_could_not(
    configured: Path, monkeypatch, capsys
) -> None:
    """#298: the kernel posts as this token; its login is recorded so every
    own-comment and own-PR filter keys on the adopter's identity, not the lab's.
    An unreadable login is left out of the file and reported."""
    pat = configured / "pat"
    pat.write_text("ghp_x")
    base = ["--yes", "--compute", "local", "--target", "o/r", "--pat-file", str(pat)]
    assert init.main(base) == 0
    out = capsys.readouterr().out
    assert "login could not be read" in out and "OUTERLOOP_BOT_LOGIN" in out
    assert "OUTERLOOP_BOT_LOGIN=" not in (configured / ".env").read_text()
    monkeypatch.setattr(init, "_token_login", lambda token: "someone" if token == "ghp_x" else "")
    assert init.main([*base, "--force"]) == 0
    assert "OUTERLOOP_BOT_LOGIN=someone" in (configured / ".env").read_text()


def test_personal_init_writes_the_gh_token_login_and_identity(
    configured: Path, monkeypatch, capsys
) -> None:
    """--personal takes `gh auth token` (captured, never echoed), writes it 0600
    to the default PAT file, records its login, and turns the mode on."""
    monkeypatch.setattr(init.shutil, "which", lambda name: "/usr/bin/gh")
    seen: list[list[str]] = []

    def fake_run(argv, **kw):
        seen.append(argv)
        return init.subprocess.CompletedProcess(argv, 0, "ghp_personal\n", "")

    monkeypatch.setattr(init.subprocess, "run", fake_run)
    monkeypatch.setattr(init, "_token_login", lambda token: "octocat")
    argv = [
        "--yes",
        "--personal",
        "--compute",
        "local",
        "--target",
        "o/r",
        "--claude-model",
        "m",
        "--no-image",
        "--no-install-harness",
    ]
    assert init.main(argv) == 0
    assert ["gh", "auth", "token"] in seen
    pat = configured / "bot_pat"
    assert pat.read_text() == "ghp_personal"
    assert stat.S_IMODE(pat.stat().st_mode) == 0o600
    env = (configured / ".env").read_text()
    assert "OUTERLOOP_BOT_LOGIN=octocat" in env
    assert "OUTERLOOP_IDENTITY=personal" in env
    captured = capsys.readouterr()
    assert "ghp_personal" not in captured.out + captured.err


def test_personal_without_gh_falls_back_to_a_pasted_token(
    configured: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(init.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        init, "_ask", lambda prompt, default="", **kw: default
    )  # author prompts take their defaults
    monkeypatch.setattr(
        init.getpass,
        "getpass",
        lambda prompt: "ghp_pasted" if "token" in prompt.lower() else "",
    )
    monkeypatch.setattr(init, "_token_login", lambda token: "octocat")
    argv = [
        "--personal",
        "--compute",
        "local",
        "--target",
        "o/r",
        "--claude-model",
        "m",
        "--no-image",
        "--no-install-harness",
    ]
    assert init.main(argv) == 0
    assert (configured / "bot_pat").read_text() == "ghp_pasted"
    assert "OUTERLOOP_IDENTITY=personal" in (configured / ".env").read_text()
    out = capsys.readouterr().out
    assert "gh auth token" in out and "fine-grained" in out


def test_personal_refuses_the_app_and_the_local_forge(configured: Path, capsys) -> None:
    base = [
        "--yes",
        "--personal",
        "--compute",
        "local",
        "--target",
        "o/r",
        "--claude-model",
        "m",
    ]
    assert init.main([*base, "--forge", "local"]) == 2
    assert "GitHub-only" in capsys.readouterr().err
    assert init.main([*base, "--github-app"]) == 2
    assert "different identities" in capsys.readouterr().err
    assert not (configured / ".env").exists()


def test_a_rerun_without_personal_keeps_the_mode(configured: Path) -> None:
    """The mode is deployment state, not a credential: updating a model without
    --personal must not silently turn the marker rule off."""
    (configured / ".env").write_text(
        "OUTERLOOP_COMPUTE=local\n"
        "OUTERLOOP_TARGET=o/r\n"
        "OUTERLOOP_PAT_FILE=/some/pat\n"
        "OUTERLOOP_IDENTITY=personal\n"
    )
    argv = [
        "--yes",
        "--force",
        "--compute",
        "local",
        "--target",
        "o/r",
        "--pat-file",
        "/some/pat",
        "--claude-model",
        "m",
        "--no-image",
        "--no-install-harness",
    ]
    assert init.main(argv) == 0
    assert "OUTERLOOP_IDENTITY=personal" in (configured / ".env").read_text()


def test_init_records_the_harness_binary_or_says_how_to_install_it(
    configured: Path, monkeypatch, capsys
) -> None:
    """#294: the author's CLI path is recorded (jobs have no login PATH); when it
    is absent, init says so and how to install it instead of a later spawn-error."""
    base = ["--yes", "--compute", "local", "--target", "o/r", "--pat-file", "/some/pat"]
    monkeypatch.setattr(init, "locate_harness", lambda backend: f"/opt/bin/{backend or 'claude'}")
    assert init.main([*base, "--author-backend", "codex"]) == 0
    assert "OUTERLOOP_CODEX_BIN=/opt/bin/codex" in (configured / ".env").read_text()
    monkeypatch.setattr(init, "locate_harness", lambda backend: "")
    assert init.main([*base, "--force", "--no-install-harness"]) == 0
    out = capsys.readouterr().out
    assert "no `claude` binary" in out and "bash scripts/install_claude.sh" in out
    assert "OUTERLOOP_CLAUDE_BIN" not in (configured / ".env").read_text()


@pytest.mark.real_locate_harness
def test_locate_harness_prefers_path_then_local_bin(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OUTERLOOP_CLAUDE_BIN", raising=False)
    assert init.locate_harness("claude") == ""
    local = tmp_path / ".local" / "bin" / "claude"
    local.parent.mkdir(parents=True)
    local.write_text("#!/bin/sh\n")
    local.chmod(0o755)
    assert init.locate_harness("claude") == str(local)
    onpath = tmp_path / "onpath"
    onpath.mkdir()
    (onpath / "claude").write_text("#!/bin/sh\n")
    (onpath / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", str(onpath))
    assert init.locate_harness("claude") == str((onpath / "claude").resolve())


@pytest.mark.real_locate_harness
def test_locate_harness_never_records_the_working_directory(tmp_path: Path, monkeypatch) -> None:
    """A relative or empty PATH entry must not turn a checkout's ./claude into
    the deployment's recorded CLI."""
    cwd = tmp_path / "checkout"
    cwd.mkdir()
    (cwd / "claude").write_text("#!/bin/sh\n")
    (cwd / "claude").chmod(0o755)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OUTERLOOP_CLAUDE_BIN", raising=False)
    for path in ("", ".", f":{tmp_path / 'nowhere'}", "./"):
        monkeypatch.setenv("PATH", path)
        assert init.locate_harness("claude") == ""
    monkeypatch.setenv("PATH", str(cwd))  # the same file through an absolute entry is fine
    assert init.locate_harness("claude") == str((cwd / "claude").resolve())


def test_repo_access_messages_name_the_cause(monkeypatch) -> None:
    import email.message
    import io
    import urllib.error

    def raising(code):
        def urlopen(req, timeout=15):
            raise urllib.error.HTTPError(
                req.full_url, code, "x", email.message.Message(), io.BytesIO(b"")
            )

        return urlopen

    monkeypatch.setattr(init.urllib.request, "urlopen", raising(401))
    assert "invalid or expired" in init._check_repo_access("t", "o/r")
    monkeypatch.setattr(init.urllib.request, "urlopen", raising(403))
    assert "not allowed" in init._check_repo_access("t", "o/r")
    assert init._auth_is_fatal("the token is invalid or expired (GitHub returned 401)")
    assert not init._auth_is_fatal("could not reach GitHub: timeout")
    assert not init._auth_is_fatal("could not read the App credentials: x")
    assert not init._auth_is_fatal("")


def test_transient_github_failures_are_not_dead_credentials(monkeypatch) -> None:
    """A 5xx or a rate limit is GitHub's problem, not the credential's: init warns
    and continues instead of failing (terra, #309)."""
    import email.message
    import io
    import urllib.error

    def raising(code):
        def urlopen(req, timeout=15):
            raise urllib.error.HTTPError(
                req.full_url, code, "x", email.message.Message(), io.BytesIO(b"")
            )

        return urlopen

    for code in (500, 502, 503, 429):
        monkeypatch.setattr(init.urllib.request, "urlopen", raising(code))
        problem = init._check_repo_access("t", "o/r")
        assert problem.startswith("could not reach GitHub") and not init._auth_is_fatal(problem), (
            code
        )

    class _Provider:
        app_id = 1
        installation_id = 7

        def _sign(self, data: bytes) -> bytes:
            return b"sig"

        def _transport(self, req):
            with init.urllib.request.urlopen(req) as response:
                return json.loads(response.read())

    monkeypatch.setattr("outerloop.appauth.build_app_jwt", lambda *a, **k: "jwt")
    monkeypatch.setattr(init.urllib.request, "urlopen", raising(502))
    problem = init.app_permission_gaps(_Provider(), "o/r").problem
    assert problem.startswith("could not reach GitHub") and not init._auth_is_fatal(problem)
    monkeypatch.setattr(init.urllib.request, "urlopen", raising(404))
    assert init._auth_is_fatal(init.app_permission_gaps(_Provider(), "o/r").problem)


def test_github_app_rerun_rechecks_the_existing_app_instead_of_creating_one(
    configured: Path, monkeypatch, capsys
) -> None:
    """After fixing the installation, `init --force --github-app` re-checks the App
    this machine already has; it never mints a second App (terra, #309)."""
    from outerloop import appmanifest

    app_json = configured / "github_app.myapp.json"
    app_json.write_text(json.dumps({"app_id": 1, "installation_id": 7, "private_key": "/k.pem"}))

    def never(*a, **k):
        raise AssertionError("a second App must not be created")

    monkeypatch.setattr(appmanifest, "request_manifest_code", never)
    monkeypatch.setattr("outerloop.appauth.app_provider_from_file", lambda path: object())
    monkeypatch.setattr(
        init,
        "app_permission_gaps",
        lambda provider, target: init.AppPermissionGaps((), "", "", "", known=True),
    )
    argv = ["--yes", "--force", "--github-app", "--compute", "local", "--target", "o/r"]
    assert init.main(argv) == 0
    env = (configured / ".env").read_text()
    assert f"OUTERLOOP_GITHUB_APP_FILE={app_json}" in env
    assert "OUTERLOOP_BOT_LOGIN=myapp[bot]" in env
    capsys.readouterr()  # drop the successful run's output
    # still failing: exit 1, credentials kept, the fix and the re-check named, never `start`
    monkeypatch.setattr(
        init,
        "app_permission_gaps",
        lambda provider, target: init.AppPermissionGaps(
            ("contents",), "", "", "the App lacks write on contents (x)", known=True
        ),
    )
    assert init.main(argv) == 1
    captured = capsys.readouterr()
    assert "FAILED" in captured.err and "init --force --github-app" in captured.err
    assert "apps/myapp/installations/new" in captured.err
    assert "next: outerloop start" not in captured.out and app_json.exists()


def test_github_app_rerun_captures_a_missing_installation_id(
    configured: Path, monkeypatch, capsys
) -> None:
    """A first run that ended before the App was installed left installation_id 0;
    the re-run looks the installation up, records it, then checks access."""
    from outerloop import appmanifest

    app_json = configured / "github_app.myapp.json"
    creds = {"app_id": 1, "installation_id": 0, "private_key": "/k.pem"}
    app_json.write_text(json.dumps(creds))
    monkeypatch.setattr(
        appmanifest, "request_manifest_code", lambda *a, **k: pytest.fail("no new App")
    )
    monkeypatch.setattr(appmanifest, "capture_installation_id", lambda app_id, pem, owner: 7)
    monkeypatch.setattr("outerloop.appauth.app_provider_from_file", lambda path: object())
    monkeypatch.setattr(
        init,
        "app_permission_gaps",
        lambda provider, target: init.AppPermissionGaps((), "", "", "", known=True),
    )
    argv = ["--yes", "--force", "--github-app", "--compute", "local", "--target", "o/r"]
    assert init.main(argv) == 0
    assert json.loads(app_json.read_text())["installation_id"] == 7
    assert "installation id 7 recorded" in capsys.readouterr().out
    # still not installed: exit 1 with the install page, credentials kept
    app_json.write_text(json.dumps(creds))
    monkeypatch.setattr(appmanifest, "capture_installation_id", lambda app_id, pem, owner: 0)
    monkeypatch.setattr(
        init,
        "app_permission_gaps",
        lambda provider, target: init.AppPermissionGaps(
            ("contents",), "", "", "the App is not installed on o/r"
        ),
    )
    assert init.main(argv) == 1
    err = capsys.readouterr().err
    assert "apps/myapp/installations/new" in err and "init --force --github-app" in err


@pytest.mark.real_locate_harness
def test_init_records_real_path_lookup(configured: Path, monkeypatch) -> None:
    for backend in ("claude", "codex"):
        cfg = configured / backend
        cfg.mkdir()
        monkeypatch.setattr(init, "CONFIG_DIR", cfg)
        bin_dir = configured / f"{backend}-bin"
        bin_dir.mkdir()
        binary = bin_dir / backend
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        monkeypatch.setenv("PATH", str(bin_dir))
        assert (
            init.main(
                [
                    "--yes",
                    "--compute",
                    "local",
                    "--target",
                    "o/r",
                    "--pat-file",
                    "/some/pat",
                    "--author-backend",
                    backend,
                ]
            )
            == 0
        )
        assert f"OUTERLOOP_{backend.upper()}_BIN={binary.resolve()}" in (cfg / ".env").read_text()


def test_whitespace_claude_model_counts_as_missing(configured: Path, monkeypatch) -> None:
    """A blank --claude-model is missing: the shell value fills it, otherwise
    --yes refuses and an interactive run asks for it as required."""
    monkeypatch.delenv("OUTERLOOP_CLAUDE_MODEL", raising=False)
    flag = ["--compute", "local", "--target", "o/r", "--pat-file", "/p", "--claude-model", "   "]
    assert init.main(["--yes", *flag]) == 2
    assert not (configured / ".env").exists()
    monkeypatch.setenv("OUTERLOOP_CLAUDE_MODEL", " claude-shell ")
    assert init.main(["--yes", *flag]) == 0
    assert "OUTERLOOP_CLAUDE_MODEL=claude-shell\n" in (configured / ".env").read_text()
    # interactive: the blank flag is prompted for, and still required
    monkeypatch.delenv("OUTERLOOP_CLAUDE_MODEL", raising=False)
    monkeypatch.setattr(init.getpass, "getpass", lambda *a: "")
    asked = []

    def ask(prompt, default="", **kw):
        if prompt.startswith("Claude model"):
            asked.append(kw)
            return "claude-prompt"
        return default

    monkeypatch.setattr(init, "_ask", ask)
    assert init.main([*flag, "--force"]) == 0
    assert asked == [{"required": True}]
    assert "OUTERLOOP_CLAUDE_MODEL=claude-prompt\n" in (configured / ".env").read_text()


@pytest.mark.parametrize("model_source", ["existing", "shell", "flag"])
def test_focused_github_app_preserves_deployment_env(configured: Path, monkeypatch, model_source):
    monkeypatch.delenv("OUTERLOOP_CLAUDE_MODEL", raising=False)
    monkeypatch.delenv("OUTERLOOP_AUTHOR_MODEL", raising=False)
    existing = {
        "OUTERLOOP_COMPUTE": "slurm",
        "OUTERLOOP_TARGET": "o/r",
        "OUTERLOOP_ROOT": "/shared/state",
        "OUTERLOOP_ACCOUNT": "acct",
        "OUTERLOOP_PARTITION": "cpu",
        "OUTERLOOP_CLAUDE_MODEL": "claude-x",
        "OUTERLOOP_AUTHOR_MODEL": "gpt-pinned",
        "OUTERLOOP_AUTHOR_BACKEND": "codex",
        "OUTERLOOP_CODEX_BIN": "/custom/codex",
        "OUTERLOOP_CODEX_KEY_FILE": "/custom/key",
        "OUTERLOOP_IMAGE": "",
        "OUTERLOOP_AUTO_UPDATE": "release",
        "OUTERLOOP_PANEL": "",
        "OUTERLOOP_STEWARD_KEY_FILE": "/custom/steward",
        "CUSTOM_SETTING": "keep me",
    }
    for key in existing:
        monkeypatch.delenv(key, raising=False)
    path = configured / ".env"
    path.write_text(
        "".join(f"{k}={v}\n" for k, v in existing.items()) + "OUTERLOOP_PAT_FILE=/old\n"
    )
    app_json = configured / "github_app.myapp.json"
    app_json.write_text(json.dumps({"app_id": 1, "installation_id": 7, "private_key": "/k.pem"}))
    monkeypatch.setattr("outerloop.appauth.app_provider_from_file", lambda path: object())
    monkeypatch.setattr(
        init, "app_permission_gaps", lambda *a: init.AppPermissionGaps((), "", "", "", known=True)
    )
    flags = []
    if model_source == "shell":
        monkeypatch.setenv("OUTERLOOP_CLAUDE_MODEL", "claude-shell")
        monkeypatch.setenv("OUTERLOOP_AUTHOR_MODEL", "gpt-shell")
        existing.update(OUTERLOOP_CLAUDE_MODEL="claude-shell", OUTERLOOP_AUTHOR_MODEL="gpt-shell")
    elif model_source == "flag":
        monkeypatch.setenv("OUTERLOOP_CLAUDE_MODEL", "claude-shell")
        flags = ["--claude-model", " claude-flag ", "--author-model", "gpt-flag"]
        existing.update(OUTERLOOP_CLAUDE_MODEL="claude-flag", OUTERLOOP_AUTHOR_MODEL="gpt-flag")
    assert init.main(["--yes", "--force", "--github-app", *flags]) == 0
    result = dict(line.split("=", 1) for line in path.read_text().splitlines())
    for key, value in existing.items():
        assert result.get(key) == value, key
    assert "OUTERLOOP_PAT_FILE" not in result
    assert result["OUTERLOOP_GITHUB_APP_FILE"] == str(app_json)


def test_focused_github_app_drops_a_preserved_local_forge(configured: Path, monkeypatch):
    """`--github-app` means GitHub: updating a local deployment must not keep
    OUTERLOOP_FORGE=local beside the GitHub credentials it writes."""
    path = configured / ".env"
    path.write_text(
        "OUTERLOOP_COMPUTE=local\n"
        "OUTERLOOP_TARGET=o/r\n"
        "OUTERLOOP_ROOT=/shared/state\n"
        "OUTERLOOP_FORGE=local\n"
        "OUTERLOOP_CLAUDE_MODEL=claude-x\n"
    )
    app_json = configured / "github_app.myapp.json"
    app_json.write_text(json.dumps({"app_id": 1, "installation_id": 7, "private_key": "/k.pem"}))
    monkeypatch.setattr("outerloop.appauth.app_provider_from_file", lambda path: object())
    monkeypatch.setattr(
        init, "app_permission_gaps", lambda *a: init.AppPermissionGaps((), "", "", "", known=True)
    )
    assert init.main(["--yes", "--force", "--github-app"]) == 0
    result = dict(line.split("=", 1) for line in path.read_text().splitlines())
    assert "OUTERLOOP_FORGE" not in result
    assert result["OUTERLOOP_GITHUB_APP_FILE"] == str(app_json)


@pytest.mark.parametrize(
    ("recorded", "flags", "written"),
    [
        ("enroot", ["--container-runtime", "apptainer", "--image", "agent.sif"], "apptainer"),
        ("apptainer", ["--container-runtime", "enroot", "--image", "agent.sqsh"], "enroot"),
        ("enroot", [], "enroot"),
    ],
)
def test_focused_github_app_writes_the_runtime_its_flag_names(
    configured: Path, monkeypatch, recorded, flags, written
) -> None:
    """--container-runtime wins over the deployment's recorded runtime in a
    focused --github-app run, so an image is never paired with the other
    runtime; without the flag the recorded one is kept."""
    monkeypatch.delenv("OUTERLOOP_CONTAINER_RUNTIME", raising=False)
    if flags:
        image = configured / flags[-1]
        image.write_text("")
        flags = [*flags[:-1], str(image)]
    old = configured / "agent.old"
    old.write_text("")
    path = configured / ".env"
    path.write_text(
        "OUTERLOOP_COMPUTE=local\nOUTERLOOP_TARGET=o/r\nOUTERLOOP_CLAUDE_MODEL=claude-x\n"
        f"OUTERLOOP_CONTAINER_RUNTIME={recorded}\nOUTERLOOP_IMAGE={old}\n"
    )
    app_json = configured / "github_app.myapp.json"
    app_json.write_text(json.dumps({"app_id": 1, "installation_id": 7, "private_key": "/k.pem"}))
    monkeypatch.setattr("outerloop.appauth.app_provider_from_file", lambda path: object())
    monkeypatch.setattr(
        init, "app_permission_gaps", lambda *a: init.AppPermissionGaps((), "", "", "", known=True)
    )
    assert init.main(["--yes", "--force", "--github-app", *flags]) == 0
    result = dict(line.split("=", 1) for line in path.read_text().splitlines())
    assert result["OUTERLOOP_CONTAINER_RUNTIME"] == written
    assert result["OUTERLOOP_IMAGE"] == (flags[-1] if flags else str(old))


@pytest.mark.parametrize("focused", [True, False])
def test_init_refuses_an_image_in_the_other_runtimes_format(
    configured: Path, monkeypatch, capsys, focused
) -> None:
    """A focused run that switches runtimes keeps the recorded image, and an
    explicit --image can name the wrong format: enroot cannot unpack a .sif
    (nor Apptainer run a .sqsh), so init refuses and leaves .env as it was."""
    monkeypatch.delenv("OUTERLOOP_CONTAINER_RUNTIME", raising=False)
    sif = configured / "agent.sif"
    sif.write_text("")
    path = configured / ".env"
    before = (
        "OUTERLOOP_COMPUTE=local\nOUTERLOOP_TARGET=o/r\nOUTERLOOP_CLAUDE_MODEL=claude-x\n"
        f"OUTERLOOP_IMAGE={sif}\n"
    )
    path.write_text(before)
    full = ["--compute", "local", "--target", "o/r", "--pat-file", "/p", "--image", str(sif)]
    flags = ["--github-app"] if focused else full
    assert init.main(["--yes", "--force", "--container-runtime", "enroot", *flags]) == 2
    err = capsys.readouterr().err
    assert f"{sif} is an apptainer image and the runtime is enroot" in err
    assert "pass --container-runtime apptainer, --image with a .sqsh image" in err
    assert path.read_text() == before


def test_init_local_forge_records_the_configured_kernel_login(tmp_path, monkeypatch):
    """The bootstrap reads OUTERLOOP_BOT_LOGIN; the .env must carry it too, or
    once the shell variable is gone the tick posts as the default `outerloop`."""
    monkeypatch.setattr(init, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setenv("OUTERLOOP_BOT_LOGIN", "lab-kernel")
    state = tmp_path / "state"
    rc = init.main(
        [
            "--yes",
            "--compute",
            "local",
            "--target",
            "o/r",
            "--root",
            str(state),
            "--forge",
            "local",
            "--claude-model",
            "claude-x",
        ]
    )
    assert rc == 0
    assert "OUTERLOOP_BOT_LOGIN=lab-kernel" in (tmp_path / "config" / ".env").read_text()
    monkeypatch.delenv("OUTERLOOP_BOT_LOGIN")
    assert "OUTERLOOP_BOT_LOGIN=lab-kernel" in (tmp_path / "config" / ".env").read_text()


def test_render_env_keeps_comments_and_order_of_a_hand_edited_file() -> None:

    existing = (
        "# my deployment\n"
        "OUTERLOOP_COMPUTE=slurm\n"
        "OUTERLOOP_ROOT=/old/root\n"
        "\n"
        "# keep this key even though init does not manage it\n"
        "OUTERLOOP_QOS=priority\n"
        "OUTERLOOP_PAT_FILE=/gone\n"
    )
    answers = InitAnswers(
        compute="slurm",
        root="/new/root",
        target="org/repo",
        author_backend="claude",
        author_model="m",
        claude_model="m",
        preserved_env={"OUTERLOOP_QOS": "priority"},
    )
    text = render_env(answers, app_file="/app.json", bot_login="x[bot]", existing_text=existing)
    lines = text.splitlines()
    assert lines[:3] == ["# my deployment", "OUTERLOOP_COMPUTE=slurm", "OUTERLOOP_ROOT=/new/root"]
    assert "" in lines and "# keep this key even though init does not manage it" in lines
    assert "OUTERLOOP_QOS=priority" in lines
    assert not any(
        line.startswith("OUTERLOOP_PAT_FILE=") for line in lines
    )  # App auth replaces the PAT
    assert "OUTERLOOP_GITHUB_APP_FILE=/app.json" in lines  # appended
    assert text.endswith("\n") and "\n\n\n" not in text


def test_init_hermes_author(configured: Path, monkeypatch):
    monkeypatch.setattr("outerloop.hermes_install.hermes_ready", lambda repo: True)
    monkeypatch.setenv("REVIEW_HERMES_PROVIDER", "openrouter")
    image = configured / "image.sif"
    image.touch()
    monkeypatch.setattr(init, "ensure_image", lambda **kw: str(image))
    monkeypatch.setattr(init, "hermes_ready", lambda repo: True)
    monkeypatch.setattr(init, "locate_harness", lambda backend: "/opt/hermes")
    assert (
        init.main(
            [
                "--yes",
                "--compute",
                "local",
                "--target",
                "o/r",
                "--author-backend",
                "hermes",
                "--author-model",
                "org/model",
                "--no-install-harness",
            ]
        )
        == 0
    )
    env = (configured / ".env").read_text()
    assert "OUTERLOOP_AUTHOR_BACKEND=hermes" in env
    assert "REVIEW_HERMES_REPO=/opt/hermes" in env
    assert "OUTERLOOP_HERMES_BIN" not in env


@pytest.mark.parametrize("missing", ["model", "provider", "image", "runtime", "budget"])
def test_init_refuses_incomplete_hermes(configured: Path, monkeypatch, capsys, missing):
    monkeypatch.setattr(init, "locate_harness", lambda backend: "/opt/hermes")
    monkeypatch.setattr("outerloop.hermes_install.hermes_ready", lambda repo: missing != "runtime")
    monkeypatch.setenv("REVIEW_HERMES_PROVIDER", "openai" if missing != "provider" else "")
    monkeypatch.setenv(
        "OUTERLOOP_HERMES_RESUME_MAX_CHARS", "bad" if missing == "budget" else "120000"
    )
    monkeypatch.delenv("OUTERLOOP_AUTHOR_ENDPOINT", raising=False)
    image = configured / "image.sif"
    image.touch()
    monkeypatch.setattr(init, "ensure_image", lambda **kw: "" if missing == "image" else str(image))
    args = [
        "--yes",
        "--compute",
        "local",
        "--target",
        "o/r",
        "--author-backend",
        "hermes",
        "--no-install-harness",
    ]
    if missing != "model":
        args += ["--author-model", "gpt-native"]
    assert init.main(args) == 2
    assert not (configured / ".env").exists()
    assert "outerloop init:" in capsys.readouterr().err


def test_init_ignores_hermes_budget_for_other_authors(configured: Path, monkeypatch):
    monkeypatch.setenv("OUTERLOOP_HERMES_RESUME_MAX_CHARS", "invalid")
    assert (
        init.main(
            [
                "--yes",
                "--compute",
                "local",
                "--target",
                "o/r",
                "--author-backend",
                "claude",
                "--author-model",
                "native-model",
                "--claude-model",
                "claude-model",
                "--no-install-harness",
            ]
        )
        == 0
    )


@pytest.mark.parametrize("backend", ["codex", "hermes"])
def test_fresh_init_provisions_override_before_validation(configured: Path, monkeypatch, backend):
    monkeypatch.setenv(
        "OUTERLOOP_AUTHOR_OVERRIDES",
        json.dumps({"owner/repo": {"backend": backend, "model": "org/model"}}),
    )
    monkeypatch.setenv("REVIEW_HERMES_PROVIDER", "openrouter")
    monkeypatch.setenv("REVIEW_HERMES_REPO", str(configured / "hermes"))
    installed = []

    def install(name, target=None):
        name = name or "claude"
        installed.append(name)
        return str(configured / name)

    def ready(repo):
        return "hermes" in installed

    monkeypatch.setattr(init, "install_harness", install)
    monkeypatch.setattr(init, "hermes_ready", ready)
    monkeypatch.setattr("outerloop.hermes_install.hermes_ready", ready)
    image = configured / "image.sif"

    def download(**kwargs):
        assert "claude" in installed and backend in installed
        image.touch()
        return str(image)

    monkeypatch.setattr(init, "ensure_image", download)
    assert init.main(["--yes", "--compute", "local", "--target", "owner/repo"]) == 0
    env = (configured / ".env").read_text()
    assert f"OUTERLOOP_IMAGE={image}" in env
    key = "REVIEW_HERMES_REPO" if backend == "hermes" else "OUTERLOOP_CODEX_BIN"
    assert f"{key}={configured / backend}" in env


def test_init_bad_override_json_fails_before_provisioning(configured: Path, monkeypatch, capsys):
    monkeypatch.setenv("OUTERLOOP_AUTHOR_OVERRIDES", "{")
    monkeypatch.setattr(init, "install_harness", lambda *a, **k: pytest.fail("installed"))
    monkeypatch.setattr(init, "ensure_image", lambda **k: pytest.fail("downloaded"))
    assert init.main(["--yes", "--compute", "local", "--target", "owner/repo"]) == 2
    assert capsys.readouterr().err.count("outerloop init:") == 1


def test_init_selects_separate_settings_and_credentials(tmp_path, monkeypatch):
    selected = tmp_path / "sandbox" / "operator.env"
    monkeypatch.setenv("OUTERLOOP_ENV_FILE", str(selected))
    monkeypatch.setattr(init, "CONFIG_DIR", tmp_path / "production")
    monkeypatch.setattr(init, "validate_pat", lambda pf, t: "")
    assert (
        init.main(
            [
                "--yes",
                "--compute",
                "local",
                "--target",
                "owner/sandbox",
                "--pat-file",
                "/token",
                "--claude-model",
                "claude-test",
            ]
        )
        == 0
    )
    assert "OUTERLOOP_TARGET=owner/sandbox" in selected.read_text()
    assert not (tmp_path / "production").exists()
    # Direct writes put pasted credentials beside the selected settings too.
    _, pat = write_config(InitAnswers(compute="local", target="owner/sandbox"), "fake", "")
    assert pat == selected.parent / "bot_pat"
    selected.chmod(0o622)
    assert init.main(["--yes", "--force"]) == 2


def test_init_warns_on_harness_older_than_pin(tmp_path, monkeypatch, capsys):
    from outerloop.harness_pins import effective

    pinned = effective("claude")["version"]
    reported = ["claude 0.0.1\n"]
    monkeypatch.setattr(
        init.subprocess,
        "run",
        lambda argv, **kw: init.subprocess.CompletedProcess(argv, 0, reported[0], ""),
    )
    init._check_harness_version("claude", str(tmp_path / "claude"))
    output = capsys.readouterr().out
    assert "0.0.1" in output and pinned in output
    assert "outerloop harness upgrade claude" in output
    reported[0] = f"{pinned} (Claude Code)\n"
    init._check_harness_version("claude", str(tmp_path / "claude"))
    assert capsys.readouterr().out == ""


def test_panel_hint_requires_no_image_and_nonempty_panel(capsys, monkeypatch):
    monkeypatch.setenv("OUTERLOOP_PANEL_UNCONTAINED", "1")  # shell-only: not written
    answers = InitAnswers(compute="local", target="o/r", uncontained=True)
    init._panel_hint(answers)
    output = capsys.readouterr().out
    assert "OUTERLOOP_PANEL_UNCONTAINED=1" in output
    assert "OUTERLOOP_PANEL_KEY_FILE" in output
    assert "verifier_key" in output
    answers.preserved_env["OUTERLOOP_PANEL"] = ""
    init._panel_hint(answers)
    answers.preserved_env["OUTERLOOP_PANEL"] = "verify,review"
    answers.preserved_env["OUTERLOOP_PANEL_UNCONTAINED"] = "1"
    init._panel_hint(answers)
    answers.image = "/path/to/agent.sif"
    init._panel_hint(answers)
    assert capsys.readouterr().out == ""


def test_init_keeps_panel_opt_in_settings(configured: Path, monkeypatch):
    monkeypatch.setattr(init, "locate_harness", lambda _: "")
    monkeypatch.setenv("OUTERLOOP_PANEL_UNCONTAINED", "1")
    monkeypatch.setenv("OUTERLOOP_PANEL_KEY_FILE", str(configured / "panel_key"))
    argv = ["--yes", "--compute", "local", "--target", "o/r", "--no-image"]
    assert init.main([*argv, "--no-install-harness"]) == 0
    written = (configured / ".env").read_text()
    assert "OUTERLOOP_PANEL_UNCONTAINED=1" in written
    assert f"OUTERLOOP_PANEL_KEY_FILE={configured / 'panel_key'}" in written
