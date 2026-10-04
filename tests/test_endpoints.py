"""Endpoint wiring is checked at the process boundary without model calls."""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
import yaml

from outerloop import harness as harness_mod
from outerloop.attempt import codex_author_config_error, fleet_author_model, resume_author
from outerloop.cli import TICK_ENV_KEYS, env_file_values, missing_claude_model, missing_panel_model
from outerloop.endpoints import endpoint_profile, model_key, resolve_endpoint, split_endpoint
from outerloop.harness import CodexHarness, _parse_hermes_result
from outerloop.panel import parse_lenses, resolve_lenses
from outerloop.review_agent_cli import resolve_reviewer_harness
from outerloop.role_runner import build_harness
from outerloop.roles import author_spec, reviewer_spec

pytestmark = pytest.mark.usefixtures("codex_host")


@pytest.fixture
def profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    connection = Mock()
    connection.getresponse.return_value.status = 200
    monkeypatch.setattr("outerloop.endpoints.HTTPConnection", Mock(return_value=connection))
    monkeypatch.setattr("outerloop.endpoints.HTTPSConnection", Mock(return_value=connection))
    key = tmp_path / "endpoint-key"
    key.write_text("endpoint-secret")
    key.chmod(0o600)
    env = {
        "OUTERLOOP_ENDPOINT_LOCAL_URL": "https://llm.example.internal/v1",
        "OUTERLOOP_ENDPOINT_LOCAL_KEY_FILE": str(key),
        "OUTERLOOP_ENDPOINT_LOCAL_MODEL": "open-model",
        "OUTERLOOP_ENDPOINT_LOCAL_API": "anthropic,responses,chat",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return env


@pytest.mark.parametrize("backend", ["claude", "codex", "hermes"])
def test_profile_for_every_backend(profile: dict[str, str], backend: str) -> None:
    served, endpoint = resolve_endpoint("open-model[endpoint=LOCAL]", backend)
    assert served == "open-model" and endpoint is not None
    assert endpoint.name == "local"
    assert endpoint.key() == "endpoint-secret"
    assert resolve_endpoint("[endpoint=local]", backend)[0] == "open-model"
    assert "endpoint-secret" not in repr(endpoint)
    with pytest.raises(ValueError, match="does not match"):
        resolve_endpoint("wrong-model[endpoint=local]", backend)


@pytest.mark.parametrize(
    ("suffix", "value", "error"),
    [
        ("URL", "", "missing"),
        ("MODEL", "", "missing"),
        ("KEY_FILE", "/does/not/exist", "credential file"),
        ("KEY_FILE", "relative-key", "absolute"),
        ("URL", "https://user:password@llm.example.internal/v1", "without credentials"),
        ("URL", "https://llm.example.internal/v1?key=secret", "without credentials"),
        ("URL", "file:///tmp/server", "HTTP"),
    ],
)
def test_invalid_profile(profile: dict[str, str], suffix: str, value: str, error: str) -> None:
    profile[f"OUTERLOOP_ENDPOINT_LOCAL_{suffix}"] = value
    with pytest.raises(ValueError, match=error):
        endpoint_profile("local", "hermes", environ=profile)


def test_bad_names_keys_and_backend(profile: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="unknown endpoint"):
        endpoint_profile("absent", "claude", environ=profile)
    with pytest.raises(ValueError, match="unsupported backend"):
        endpoint_profile("local", "other", environ=profile)
    for model in (
        "open-model[endpoint=]",
        "open-model[endpoint=bad-name]",
        "x[endpoint=x][endpoint=local]",
    ):
        with pytest.raises(ValueError, match="selector"):
            split_endpoint(model)
    key = Path(profile["OUTERLOOP_ENDPOINT_LOCAL_KEY_FILE"])
    key.chmod(0o644)
    with pytest.raises(ValueError, match="chmod 600"):
        endpoint_profile("local", "claude", environ=profile)
    key.chmod(0o600)
    key.write_text("")
    with pytest.raises(ValueError, match="empty"):
        endpoint_profile("local", "claude", environ=profile)


def test_lens_endpoint_grammar(profile: dict[str, str]) -> None:
    spec = "verify:hermes:open-model[endpoint=local],review:codex:[endpoint=local]"
    assert parse_lenses(spec)[0] == ("verify", "hermes", "open-model[endpoint=local]")
    assert resolve_lenses(spec, "claude", "claude-model")[1] == (
        "review",
        "codex",
        "open-model[endpoint=local]",
    )
    # Judges must select their own endpoint/credential, never inherit the author's.
    with pytest.raises(ValueError, match="judge endpoint"):
        resolve_lenses("review", "codex", "open-model[endpoint=local]")
    with pytest.raises(ValueError, match="unknown endpoint"):
        resolve_lenses("verify:hermes:[endpoint=missing]", "codex", "")


def test_author_and_start_preflight(
    profile: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OUTERLOOP_AUTHOR_ENDPOINT", "local")
    monkeypatch.delenv("OUTERLOOP_AUTHOR_MODEL", raising=False)
    assert fleet_author_model("claude") == "open-model[endpoint=local]"
    assert codex_author_config_error("claude", "open-model[endpoint=local]", "") == ""
    assert "requires --image" in codex_author_config_error(
        "codex", "open-model[endpoint=local]", ""
    )
    assert "unknown endpoint" in codex_author_config_error("codex", "[endpoint=missing]", "image")
    env = {
        **profile,
        "OUTERLOOP_AUTHOR_ENDPOINT": "local",
        "OUTERLOOP_PANEL": "verify:claude:[endpoint=local]",
    }
    assert missing_claude_model(env, {}) == ""
    assert missing_panel_model(env, {}) == ""
    env["OUTERLOOP_PANEL"] = "verify:hermes:[endpoint=missing]"
    assert "unknown endpoint" in missing_panel_model(env, {})


@pytest.mark.parametrize("backend", ["claude", "codex", "hermes"])
@pytest.mark.parametrize("contained", [False, True])
@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("url_file", [False, True])
def test_exact_client_configuration_at_process_boundary(
    profile: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    contained: bool,
    failed: bool,
    url_file: bool,
) -> None:
    seen: dict[str, Any] = {}
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(harness_mod, "hermes_ready", lambda _: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-secret")
    monkeypatch.setenv("OUTERLOOP_VERTEX_PROJECT", "ambient-project")
    monkeypatch.setenv("APPTAINERENV_ANTHROPIC_API_KEY", "ambient-secret")

    class Process:
        returncode = 1 if failed else 0

        def __init__(self, command: list[str], **kwargs: Any):
            seen.update(argv=command, env=kwargs["env"])
            home = Path(kwargs["env"]["HOME"])
            for name in (".codex/config.toml", ".hermes/config.yaml"):
                path = home / name
                if path.is_file():
                    seen["config"] = path.read_text()
            (home / "sample_test.json").write_text(
                json.dumps({"conversations": [{"from": "gpt", "value": "done"}]})
            )

        def communicate(self, **kwargs: Any) -> tuple[str, str]:
            if failed:
                return "", "HTTP 503 endpoint unavailable"
            return '{"type":"result","subtype":"success","result":"done"}', ""

    monkeypatch.setattr(harness_mod.subprocess, "Popen", Process)
    monkeypatch.setattr(
        CodexHarness, "_login", lambda *_: pytest.fail("custom provider must not log in to OpenAI")
    )
    address = tmp_path / "server-address"
    if url_file:
        monkeypatch.delenv("OUTERLOOP_ENDPOINT_LOCAL_URL")
        monkeypatch.setenv("OUTERLOOP_ENDPOINT_LOCAL_URL_FILE", str(address))
        address.write_text(profile["OUTERLOOP_ENDPOINT_LOCAL_URL"])
    harness = build_harness(
        "ignored-key",
        author_spec(),
        backend=backend,
        endpoint="local",
        binary="/opt/agent-cli",
        hermes_repo=tmp_path / "hermes",
        container_image="/opt/image.sif" if contained else "",
    )
    result = harness.run("brief", workspace)
    if url_file and not failed:
        profile["OUTERLOOP_ENDPOINT_LOCAL_URL"] = "https://llm.example.internal:8443/v1"
        address.write_text(json.dumps({"url": profile["OUTERLOOP_ENDPOINT_LOCAL_URL"]}))
        result = harness.run("wake", workspace, resume_session_id=result.session_id or "existing")
    if failed:
        assert result.is_error
    assert "endpoint-secret" not in repr(seen["argv"])
    assert "ambient-secret" not in repr(seen)
    assert "ignored-key" not in repr(seen)
    env = seen["env"]
    key_env = "ANTHROPIC_AUTH_TOKEN" if backend == "claude" else "OUTERLOOP_SESSION_KEY"
    assert env[key_env] == "endpoint-secret"
    assert (
        not {
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENROUTER_API_KEY",
            "ANTHROPIC_API_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
        }
        & env.keys()
    )
    if contained:
        assert env[f"APPTAINERENV_{key_env}"] == "endpoint-secret"
    if backend == "claude":
        # Claude Code appends /v1/messages itself
        assert env["ANTHROPIC_BASE_URL"] == profile["OUTERLOOP_ENDPOINT_LOCAL_URL"].removesuffix(
            "/v1"
        )
        assert "ANTHROPIC_API_KEY" not in env
        assert "GOOGLE_APPLICATION_CREDENTIALS" not in env
        assert env["CLAUDE_CODE_USE_VERTEX"] == "0"
        assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
        for size in ("OPUS", "SONNET", "HAIKU"):
            assert env[f"ANTHROPIC_DEFAULT_{size}_MODEL"] == "open-model"
        assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "open-model"
    elif backend == "codex":
        config = tomllib.loads(seen["config"])
        provider = config["model_providers"][config["model_provider"]]
        assert provider["base_url"] == profile["OUTERLOOP_ENDPOINT_LOCAL_URL"]
        assert provider["env_key"] == key_env
        assert provider["wire_api"] == "responses"
        assert provider["requires_openai_auth"] is False
    else:
        config = yaml.safe_load(seen["config"])
        assert config["model"]["reasoning_echo"] is True
        provider = config["custom_providers"][0]
        assert provider["name"] == config["model"]["provider"]
        assert provider["api_mode"] == "chat_completions"
        assert len(config["custom_providers"]) == 1
        assert provider["key_env"] == key_env
        assert provider["base_url"] == profile["OUTERLOOP_ENDPOINT_LOCAL_URL"]
        assert not any(arg.startswith("--base_url") for arg in seen["argv"])
        assert "--enabled_toolsets=file,terminal,web,search" in seen["argv"]
        assert env["TERMINAL_CWD"] == str(workspace.resolve())
    assert "endpoint-secret" not in seen.get("config", "")


@pytest.mark.parametrize("backend", ["claude", "codex", "hermes"])
def test_reviewer_endpoint(
    profile: dict[str, str], monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    monkeypatch.setenv("REVIEW_BACKEND", backend)
    monkeypatch.setenv("REVIEW_ENDPOINT", "local")
    monkeypatch.setenv("REVIEW_HERMES_REPO", "/opt/hermes")
    monkeypatch.delenv("REVIEW_MODEL", raising=False)
    harness, error, label = resolve_reviewer_harness(reviewer_spec())
    assert not error and label == backend
    assert getattr(harness, "model", "") == "open-model"
    monkeypatch.setenv("REVIEW_ENDPOINT", "missing")
    assert "unknown endpoint" in resolve_reviewer_harness(reviewer_spec())[1]


def test_dynamic_deploy_allowlists(profile: dict[str, str], tmp_path: Path) -> None:
    path = tmp_path / ".env"
    settings = {
        **profile,
        "REVIEW_ENDPOINT": "local",
        "OUTERLOOP_AUTHOR_ENDPOINT": "local",
        "OUTERLOOP_ENDPOINT_LOCAL_URL_FILE": "/tmp/server-address",
        "OUTERLOOP_AUTHOR_OVERRIDES": '{"owner/repo":{"backend":"claude","model":"served-model"}}',
    }
    path.write_text(
        "\n".join(f"{k}={v}" for k, v in settings.items())
        + "\nOUTERLOOP_ENDPOINT_LOCAL_KEY=must-not-forward\n"
    )
    assert env_file_values(path, TICK_ENV_KEYS) == settings
    # Run the actual deployment config block without its unrelated update/install actions.
    script = Path("scripts/tick_deploy.sh").read_text()
    helpers = script[script.index("env_line() {") : script.index("# --- 2. deploy")]
    start = script.index('if [ -n "$ENV_TRUSTED" ]; then', script.index("# --- config knobs"))
    block = script[start : script.index("# Host-side caches", start)]
    result = subprocess.run(
        ["bash", "-c", helpers + block + "\n/usr/bin/env"],
        env={"PATH": os.environ["PATH"], "ENV_TRUSTED": "1", "ENV_FILE": str(path)},
        capture_output=True,
        text=True,
        check=True,
    )
    forwarded = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    for key, value in settings.items():
        assert forwarded[key] == value
    assert "OUTERLOOP_ENDPOINT_LOCAL_KEY" not in forwarded


def test_saved_author_route_survives_fleet_change(
    profile: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OUTERLOOP_AUTHOR_ENDPOINT", "new_fleet")
    record = SimpleNamespace(
        author_backend="codex",
        author_model="open-model[endpoint=local]",
        author_key_file="/old/key",
    )
    for _ in range(3):  # first read, repeat, and retry after an interrupted session
        backend, model, key_file = resume_author(record, "different-model", "claude")
        assert model == "open-model[endpoint=local]" and backend == "codex"
        assert model_key(key_file, backend, model) == "endpoint-secret"
    legacy = json.loads(Path("tests/fixtures/author_route_legacy.json").read_text())
    for _ in range(3):
        assert resume_author(SimpleNamespace(**legacy), "different-model", "claude") == (
            "codex",
            "gpt-5.6-terra",
            "/keys/author",
        )


def test_pinned_hermes_sample() -> None:
    sample = json.loads(Path("tests/fixtures/hermes_sample_20260924.json").read_text())
    result = _parse_hermes_result("noisy stdout", sample, 0)
    assert not result.is_error
    assert result.num_turns == 2
    assert result.final_text.endswith("Checked the workspace.")


def test_record_compatibility_roundtrip(tmp_path, profile):
    from dataclasses import replace

    from outerloop.runstate import RECORD_NAME, load_record, run_dir, save_record

    fixture = Path("tests/fixtures/author_route_legacy.json").read_text()
    directory = run_dir(tmp_path, "legacy-author")
    directory.mkdir(parents=True)
    (directory / RECORD_NAME).write_text(fixture)
    legacy = load_record(tmp_path, "legacy-author")
    assert resume_author(legacy, "open-model[endpoint=local]", "hermes")[:2] == (
        "codex",
        "gpt-5.6-terra",
    )
    assert load_record(tmp_path, "legacy-author") == legacy
    save_record(tmp_path, legacy, now=legacy.updated)
    assert load_record(tmp_path, "legacy-author") == legacy
    # An interrupted writer's temporary file does not replace the committed record.
    (directory / (RECORD_NAME + ".interrupted")).write_text('{"author_model":')
    assert load_record(tmp_path, "legacy-author") == legacy
    endpoint = replace(legacy, author_model="open-model[endpoint=local]")
    save_record(tmp_path, endpoint, now=legacy.updated)
    assert load_record(tmp_path, "legacy-author").author_model == "open-model[endpoint=local]"
    assert (
        model_key(endpoint.author_key_file, endpoint.author_backend, endpoint.author_model)
        == "endpoint-secret"
    )


def test_tick_endpoint_preflight(profile, tmp_path, monkeypatch):
    from outerloop.tick import ServiceSpec, _author_config_error, _panel_preflight_error

    image = tmp_path / "image.sif"
    image.touch()
    author_key = tmp_path / "author-key"
    author_key.write_text("separate-author-secret")
    author_key.chmod(0o600)
    monkeypatch.setenv("OUTERLOOP_AUTHOR_BACKEND", "codex")
    monkeypatch.setenv("OUTERLOOP_AUTHOR_MODEL", "gpt-5.6-terra")
    monkeypatch.setenv("OUTERLOOP_CODEX_KEY_FILE", str(author_key))
    spec = ServiceSpec(
        account="",
        partition="",
        run_root=tmp_path,
        home=tmp_path,
        panel="verify:codex:[endpoint=local]",
        image=str(image),
        panel_key_file="",
    )
    assert _panel_preflight_error(spec) == ""
    author_key.write_text("endpoint-secret")
    assert "role separation" in _panel_preflight_error(spec)
    monkeypatch.setenv("OUTERLOOP_AUTHOR_ENDPOINT", "missing")
    assert "unknown endpoint" in _author_config_error(spec)
    monkeypatch.setenv("OUTERLOOP_AUTHOR_ENDPOINT", "local")
    monkeypatch.setenv("OUTERLOOP_AUTHOR_MODEL", "")
    monkeypatch.setenv("OUTERLOOP_AUTHOR_BACKEND", "hermes")
    monkeypatch.setenv("REVIEW_HERMES_REPO", "/opt/hermes-agent")
    monkeypatch.setattr("outerloop.hermes_install.hermes_ready", lambda _: True)
    assert _author_config_error(spec) == ""


def test_panel_endpoint_builds_on_own_key(profile, tmp_path):
    from outerloop.attempt import _panel_lenses_from_args

    key = tmp_path / "author-key"
    key.write_text("own-author-secret")
    key.chmod(0o600)
    args = SimpleNamespace(
        key_file=str(key),
        panel="verify:codex:[endpoint=local],review:claude:open-model[endpoint=local]",
        author_backend="codex",
        model="gpt-5.6-terra",
        panel_key_file="/missing/native-key",
        image="/opt/image.sif",
        claude_bin="/opt/claude",
        codex_bin="/opt/codex",
    )
    lenses, secrets = _panel_lenses_from_args(args)
    assert len(lenses) == 2 and secrets == ("endpoint-secret",)
    assert all(getattr(lens.harness, "model", "") == "open-model" for lens in lenses)
    args.model = "open-model[endpoint=local]"
    with pytest.raises(ValueError, match="role separation"):
        _panel_lenses_from_args(args)


@pytest.mark.parametrize("model", ["claude-sonnet-4@20250514", "org/model:free", "x@y"])
def test_native_model_ids_are_unchanged(model):
    assert split_endpoint(model) == (model, "")
    assert parse_lenses(f"review:claude:{model}")[0][2] == model


@pytest.mark.parametrize(
    "backend,api", [("claude", "anthropic"), ("codex", "responses"), ("hermes", "chat")]
)
def test_api_compatibility(profile, backend, api):
    profile["OUTERLOOP_ENDPOINT_LOCAL_API"] = api
    assert endpoint_profile("local", backend, environ=profile).apis == (api,)
    profile["OUTERLOOP_ENDPOINT_LOCAL_API"] = (
        "anthropic" if backend == "codex" else ("chat" if api != "chat" else "responses")
    )
    with pytest.raises(ValueError, match="requires API"):
        endpoint_profile("local", backend, environ=profile)
    del profile["OUTERLOOP_ENDPOINT_LOCAL_API"]
    with pytest.raises(ValueError, match=r"missing.*API"):
        endpoint_profile("local", backend, environ=profile)


@pytest.mark.parametrize("same_as", ["author", "panel"])
@pytest.mark.parametrize("alias", ["direct", "symlink", "hardlink"])
def test_endpoint_key_file_separation(profile, tmp_path, monkeypatch, same_as, alias):
    from outerloop.attempt import _panel_lenses_from_args
    from outerloop.tick import ServiceSpec, _panel_preflight_error

    endpoint_key = Path(profile["OUTERLOOP_ENDPOINT_LOCAL_KEY_FILE"])
    shared = endpoint_key
    if alias != "direct":
        shared = tmp_path / "alias"
        if alias == "symlink":
            shared.symlink_to(endpoint_key)
        else:
            shared.hardlink_to(endpoint_key)
    own = tmp_path / "own"
    own.write_text("separate-secret")
    own.chmod(0o600)
    author = shared if same_as == "author" else own
    panel = shared if same_as == "panel" else own
    image = tmp_path / "image.sif"
    image.touch()
    monkeypatch.setenv("OUTERLOOP_AUTHOR_BACKEND", "codex")
    monkeypatch.setenv("OUTERLOOP_AUTHOR_MODEL", "gpt-native")
    monkeypatch.setenv("OUTERLOOP_CODEX_KEY_FILE", str(author))
    args = SimpleNamespace(
        panel="review:codex:[endpoint=local]",
        author_backend="codex",
        model="gpt-native",
        key_file=str(author),
        panel_key_file=str(panel),
        image=str(image),
    )
    with pytest.raises(
        ValueError, match=f"{same_as if same_as == 'author' else 'claude panel'} key file"
    ):
        _panel_lenses_from_args(args)
    spec = ServiceSpec(
        account="",
        partition="",
        run_root=tmp_path,
        home=tmp_path,
        panel=args.panel,
        image=str(image),
        panel_key_file=str(panel),
    )
    assert "key file (role separation)" in _panel_preflight_error(spec)


@pytest.mark.parametrize(
    "fixture", ["author_route_missing_model.json", "author_route_missing_route.json"]
)
def test_missing_route_never_inherits_endpoint(fixture, tmp_path, monkeypatch):
    from outerloop.runstate import RECORD_NAME, load_record, run_dir, save_record

    monkeypatch.setenv("OUTERLOOP_CLAUDE_MODEL", "claude-native")
    directory = run_dir(tmp_path, "legacy-author")
    directory.mkdir(parents=True)
    (directory / RECORD_NAME).write_text(Path("tests/fixtures", fixture).read_text())
    for _ in range(2):
        record = load_record(tmp_path, "legacy-author")
        backend, model, _ = resume_author(record, "open-model[endpoint=missing]", "claude")
        assert not split_endpoint(model)[1]
        assert model == ("claude-native" if backend == "claude" else "")
        save_record(tmp_path, record, now=record.updated)


def test_new_kernel_wakes_missing_route_record(tmp_path, monkeypatch):
    from outerloop import attempt
    from outerloop.runstate import RECORD_NAME, run_dir

    directory = run_dir(tmp_path, "legacy-author")
    directory.mkdir(parents=True)
    data = json.loads(Path("tests/fixtures/author_route_missing_route.json").read_text())
    data["stage"] = {"phase": "author-sleep"}
    (directory / RECORD_NAME).write_text(json.dumps(data))
    monkeypatch.setenv("OUTERLOOP_AUTHOR_ENDPOINT", "absent_fleet_profile")
    monkeypatch.setenv("OUTERLOOP_AUTHOR_BACKEND", "claude")
    monkeypatch.delenv("OUTERLOOP_AUTHOR_MODEL", raising=False)
    monkeypatch.setenv("OUTERLOOP_CLAUDE_MODEL", "claude-native")
    image = tmp_path / "image.sif"
    image.touch()
    monkeypatch.setattr(
        "sys.argv",
        [
            "climb",
            "--resume",
            "legacy-author",
            "--run-root",
            str(tmp_path),
            "--image",
            str(image),
            "--panel-skip",
            "test",
        ],
    )
    monkeypatch.setattr(
        attempt, "resolve_bot_auth", lambda *args: SimpleNamespace(token=lambda: "bot-secret")
    )
    monkeypatch.setattr(attempt, "_lease_held_by_another_job", lambda *args: "")
    monkeypatch.setattr("outerloop.tick.dispatch_wake_armed", lambda *args: True)
    monkeypatch.setattr(attempt, "model_key", lambda *args: "native-key")
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        attempt, "build_harness", lambda *args, **kwargs: seen.update(kwargs) or object()
    )

    class Resumed(Exception):
        pass

    def resume(*args, **kwargs):
        assert kwargs["harness"] is not None
        raise Resumed

    monkeypatch.setattr(attempt, "resume_run", resume)
    monkeypatch.setattr(attempt, "_release_own_lease", lambda *args: None)
    with pytest.raises(Resumed):
        attempt.main()
    assert seen["backend"] == "claude" and seen["model"] == "claude-native"


@pytest.mark.parametrize("backend", ["claude", "codex", "hermes"])
@pytest.mark.parametrize("same_path", [False, True])
def test_endpoint_author_native_panel_separation(
    profile, tmp_path, monkeypatch, backend, same_path
):
    from outerloop.attempt import _panel_lenses_from_args
    from outerloop.tick import ServiceSpec, _panel_preflight_error

    image = tmp_path / "image.sif"
    image.touch()
    monkeypatch.setenv("OUTERLOOP_AUTHOR_BACKEND", "codex")
    monkeypatch.setenv("OUTERLOOP_AUTHOR_MODEL", "open-model[endpoint=local]")
    judge = Path(profile["OUTERLOOP_ENDPOINT_LOCAL_KEY_FILE"]) if same_path else tmp_path / "judge"
    judge.write_text("endpoint-secret")
    judge.chmod(0o600)
    monkeypatch.setenv(f"OUTERLOOP_PANEL_{backend.upper()}_KEY_FILE", str(judge))
    panel = f"verify:{backend}:judge-model"
    spec = ServiceSpec(
        account="",
        partition="",
        run_root=tmp_path,
        home=tmp_path,
        panel=panel,
        image=str(image),
        panel_key_file=str(judge) if backend == "claude" else "",
    )
    assert "role separation" in _panel_preflight_error(spec)
    args = SimpleNamespace(
        panel=panel,
        author_backend="codex",
        model="open-model[endpoint=local]",
        image=str(image),
        panel_key_file=spec.panel_key_file,
    )
    with pytest.raises(ValueError, match="role separation"):
        _panel_lenses_from_args(args)


@pytest.mark.parametrize("json_value", [False, True])
def test_url_file_reloaded_for_sessions(profile, tmp_path, json_value):
    path = tmp_path / "address"
    profile.pop("OUTERLOOP_ENDPOINT_LOCAL_URL")
    profile["OUTERLOOP_ENDPOINT_LOCAL_URL_FILE"] = str(path)
    endpoint = endpoint_profile("local", "claude", environ=profile)
    for port in (8000, 8001):
        url = f"http://localhost:{port}/v1"
        path.write_text(json.dumps({"url": url}) if json_value else url + "\n")
        # Same harness-held profile, another session/wake.
        assert endpoint.session_url() == url


@pytest.mark.parametrize(
    "value",
    [
        '{"url": 3}',
        "{}",
        "{",
        "file:///tmp/server",
        "http://user:pass@localhost",
        "http://localhost?key=secret",
        "http://localhost:not-a-port",
        "http://localhost:99999",
    ],
)
def test_url_file_validates_values(profile, tmp_path, value):
    path = tmp_path / "address"
    path.write_text(value)
    profile.pop("OUTERLOOP_ENDPOINT_LOCAL_URL")
    profile["OUTERLOOP_ENDPOINT_LOCAL_URL_FILE"] = str(path)
    with pytest.raises(ValueError, match="endpoint"):
        _ = endpoint_profile("local", "codex", environ=profile).url


def test_url_file_exclusive_absolute_and_missing(profile, tmp_path, monkeypatch, caplog):
    from outerloop.endpoints import EndpointUnavailable, endpoint_config_key

    assert endpoint_config_key("OUTERLOOP_ENDPOINT_LOCAL_URL_FILE")
    profile["OUTERLOOP_ENDPOINT_LOCAL_URL_FILE"] = "relative"
    with pytest.raises(ValueError, match="exactly one"):
        endpoint_profile("local", "claude", environ=profile)
    profile.pop("OUTERLOOP_ENDPOINT_LOCAL_URL")
    with pytest.raises(ValueError, match="absolute"):
        endpoint_profile("local", "claude", environ=profile)
    path = tmp_path / "address"
    profile["OUTERLOOP_ENDPOINT_LOCAL_URL_FILE"] = str(path)
    endpoint = endpoint_profile("local", "claude", environ=profile)
    with pytest.raises(EndpointUnavailable, match="waiting for server"):
        _ = endpoint.url
    with pytest.raises(EndpointUnavailable):
        endpoint.session_url()


def test_url_file_wake_keeps_park_and_refunds_retry(profile, tmp_path, monkeypatch):
    from outerloop.attempt import defer_endpoint_wake
    from outerloop.runstate import RunRecord, load_record, save_record

    monkeypatch.delenv("OUTERLOOP_ENDPOINT_LOCAL_URL")
    path = tmp_path / "address"
    monkeypatch.setenv("OUTERLOOP_ENDPOINT_LOCAL_URL_FILE", str(path))
    record = RunRecord(
        run_id="waiting",
        target="owner/repo",
        task_title="trial",
        state="parked",
        author_backend="codex",
        author_model="open-model[endpoint=local]",
        wake_attempts=1,
        resume_session_id="existing",
        stage={"phase": "author-sleep", "candidate_ref": "keep"},
    )
    save_record(tmp_path, record, 1)
    assert defer_endpoint_wake(tmp_path, record)
    waiting = load_record(tmp_path, record.run_id)
    assert waiting.state == "parked" and waiting.wake_attempts == 0
    assert waiting.resume_session_id == "existing"
    wait = waiting.stage["endpoint_wait"]
    assert isinstance(wait, dict) and wait["endpoint"] == "local"
    assert isinstance(wait["since"], float)
    assert {k: v for k, v in waiting.stage.items() if k != "endpoint_wait"} == record.stage
    path.write_text("http://localhost:8000/v1")
    assert not defer_endpoint_wake(tmp_path, waiting)


def test_unreadable_url_file_is_retryable(profile, tmp_path, monkeypatch):
    from outerloop.endpoints import EndpointUnavailable

    path = tmp_path / "address"
    profile.pop("OUTERLOOP_ENDPOINT_LOCAL_URL")
    profile["OUTERLOOP_ENDPOINT_LOCAL_URL_FILE"] = str(path)
    endpoint = endpoint_profile("local", "claude", environ=profile)
    original = Path.read_text

    def unreadable(self, *args, **kwargs):
        if self == path:
            raise PermissionError("unreadable")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(EndpointUnavailable, match=r"URL_FILE.*unavailable"):
        _ = endpoint.url


@pytest.mark.parametrize("backend", ["claude", "codex", "hermes"])
@pytest.mark.parametrize("failure", ["missing", "dead", "http", "term", "interrupt"])
def test_session_probe_propagates_without_starting_model(
    profile, tmp_path, monkeypatch, backend, failure
):
    from outerloop.attempt import Terminated
    from outerloop.endpoints import EndpointUnavailable

    address = tmp_path / "address"
    monkeypatch.delenv("OUTERLOOP_ENDPOINT_LOCAL_URL")
    monkeypatch.setenv("OUTERLOOP_ENDPOINT_LOCAL_URL_FILE", str(address))
    if failure != "missing":
        address.write_text("http://localhost:8000/v1")
    connection = Mock()
    connection.getresponse.return_value.status = 503 if failure == "http" else 200
    errors = {
        "dead": ConnectionRefusedError(),
        "term": Terminated(),
        "interrupt": KeyboardInterrupt(),
    }
    if failure in errors:
        connection.request.side_effect = errors[failure]
    factory = Mock(return_value=connection)
    monkeypatch.setattr("outerloop.endpoints.HTTPConnection", factory)
    monkeypatch.setattr(harness_mod, "hermes_ready", lambda _: True)
    monkeypatch.setattr(harness_mod.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    harness = build_harness(
        "ignored",
        author_spec(),
        backend=backend,
        endpoint="local",
        binary="/opt/cli",
        hermes_repo=tmp_path / "hermes",
        container_image="/opt/image.sif",
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(EndpointUnavailable):
        harness.run("brief", workspace)
    if failure == "missing":
        factory.assert_not_called()
    else:
        factory.assert_called_once_with("localhost", 8000, timeout=3)
        connection.request.assert_called_once_with(
            "GET", "/v1/models", headers={"Authorization": "Bearer endpoint-secret"}
        )
        connection.close.assert_called_once()


@pytest.mark.parametrize("runtime_state", ["missing", "stale", "ready"])
@pytest.mark.parametrize(
    "backend,apis,endpoint",
    [
        ("codex", "chat", True),
        ("codex", "responses", True),
        ("codex", "responses,chat", True),
        ("codex", "chat", False),
        ("claude", "anthropic,chat", True),
        ("hermes", "chat", True),
    ],
)
def test_role_preflights_bridge_readiness(
    profile, tmp_path, monkeypatch, runtime_state, backend, apis, endpoint
):
    import hashlib

    from outerloop import bridge_install
    from outerloop.attempt import _panel_lenses_from_args, author_config_error
    from outerloop.harness_pins import pins
    from outerloop.tick import ServiceSpec, _author_config_error, _panel_preflight_error

    runtime = tmp_path / "bridge"
    monkeypatch.setenv("OUTERLOOP_BRIDGE_RUNTIME", str(runtime))
    if runtime_state != "missing":
        python = runtime / "venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text("interpreter")
        python.chmod(0o755)
        (runtime / "python.sha256").write_text(hashlib.sha256(python.read_bytes()).hexdigest())
        (runtime / ".complete").write_text(
            f"{pins('bridge')['version']} {bridge_install.lock_digest()}"
            if runtime_state == "ready"
            else "outdated runtime"
        )
    monkeypatch.setenv("OUTERLOOP_ENDPOINT_LOCAL_API", apis)
    monkeypatch.setenv("REVIEW_HERMES_REPO", str(tmp_path / "hermes"))
    monkeypatch.setattr("outerloop.hermes_install.hermes_ready", lambda _: True)
    model = "open-model[endpoint=local]" if endpoint else "gpt-native"
    image = tmp_path / "image.sif"
    image.touch()
    author_key = tmp_path / "author-key"
    author_key.write_text("separate-author-secret")
    author_key.chmod(0o600)
    monkeypatch.setenv("OUTERLOOP_CODEX_KEY_FILE", str(author_key))
    monkeypatch.setenv(
        "OUTERLOOP_PANEL_CODEX_KEY_FILE", profile["OUTERLOOP_ENDPOINT_LOCAL_KEY_FILE"]
    )
    monkeypatch.delenv("OUTERLOOP_AUTHOR_ENDPOINT", raising=False)
    monkeypatch.setenv("OUTERLOOP_AUTHOR_BACKEND", backend)
    monkeypatch.setenv("OUTERLOOP_AUTHOR_MODEL", model)
    spec = ServiceSpec(
        account="",
        partition="",
        run_root=tmp_path,
        home=tmp_path,
        panel=f"verify:{backend}:{model},review:{backend}:{model}",
        image=str(image),
        panel_key_file=str(tmp_path / "unused-panel-key"),
    )
    blocked = backend == "codex" and endpoint and apis == "chat" and runtime_state != "ready"
    message = (
        "codex chat-only endpoint bridge runtime is not ready; run outerloop harness upgrade --used"
    )
    expected = message if blocked else ""
    assert author_config_error(backend, model, str(image), environ=dict(os.environ)) == expected
    assert _author_config_error(spec) == expected

    # Judges use separate credentials from a native author, before any author work.
    monkeypatch.setenv("OUTERLOOP_AUTHOR_BACKEND", "codex")
    monkeypatch.setenv("OUTERLOOP_AUTHOR_MODEL", "gpt-native")
    panel_error = _panel_preflight_error(spec)
    if blocked:
        assert message in panel_error
    else:
        assert panel_error == ""
    args = SimpleNamespace(
        panel=spec.panel,
        author_backend="codex",
        model="gpt-native",
        key_file=str(author_key),
        panel_key_file=spec.panel_key_file,
        image=str(image),
        claude_bin="/opt/claude",
        codex_bin="/opt/codex",
    )
    if blocked:
        with pytest.raises(ValueError, match=message):
            _panel_lenses_from_args(args)
    else:
        lenses, _ = _panel_lenses_from_args(args)
        assert len(lenses) == 2

    monkeypatch.setenv("REVIEW_BACKEND", backend)
    monkeypatch.setenv("REVIEW_MODEL", model)
    monkeypatch.delenv("REVIEW_ENDPOINT", raising=False)
    monkeypatch.setenv("OPENAI_REVIEWER_KEY", "reviewer-secret")
    harness, error, _ = resolve_reviewer_harness(reviewer_spec())
    assert error == expected
    assert (harness is None) == blocked
