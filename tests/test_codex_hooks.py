"""Hook policy regressions; real 0.160.0 probes opt in with OUTERLOOP_TEST_CODEX.

No credentials or paid model calls: the real CLI talks only to a loopback fixture.
Set OUTERLOOP_TEST_CODEX_IMAGE too to exercise the Apptainer requirements bind
(on a Linux host with Apptainer and the Linux CLI). Default CI tests the policy,
launch wiring, and removal of persisted trust without needing either executable.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import tomllib
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import pytest

from outerloop import harness as harness_mod
from outerloop.endpoints import EndpointProfile
from outerloop.harness import CodexHarness, _seed_codex_config


def test_seed_config_removes_trust_without_touching_link_targets(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text('[hooks.state.planted]\ntrusted_hash="old"\n')
    config.symlink_to(outside)
    assert _seed_codex_config(home, "")
    assert config.read_text() == ""
    assert "trusted_hash" in outside.read_text()
    config.unlink()
    os.link(outside, config)
    assert _seed_codex_config(home, "")
    assert config.read_text() == ""
    assert "trusted_hash" in outside.read_text()


@pytest.mark.parametrize("component", ["home", ".codex"])
def test_seed_config_refuses_symlinked_parents(tmp_path: Path, component: str) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    outside.mkdir()
    if component == "home":
        home.symlink_to(outside)
    else:
        home.mkdir()
        (home / ".codex").symlink_to(outside)
    assert not _seed_codex_config(home, "")
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("contained", [False, True])
@pytest.mark.parametrize("resume", [None, "prior-thread"])
@pytest.mark.parametrize("endpoint", [False, True])
def test_every_launch_has_hook_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contained: bool,
    resume: str | None,
    endpoint: bool,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    config = tmp_path / "ws-home" / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[hooks.state.planted]\ntrusted_hash="old"\n')
    seen: dict[str, Any] = {}

    class Process:
        returncode = 0

        def __init__(self, command: list[str], **kwargs: Any) -> None:
            seen.update(command=command, **kwargs)
            assert "trusted_hash" not in config.read_text()

        def communicate(self, **_: Any) -> tuple[str, str]:
            return '{"type":"turn.completed"}', ""

    monkeypatch.setattr(harness_mod.subprocess, "Popen", Process)
    monkeypatch.setattr(CodexHarness, "_login", lambda *_: None)
    profile = EndpointProfile(
        "fixture", "http://127.0.0.1:1/v1", tmp_path / "key", "m", ("responses",)
    )
    harness = CodexHarness(
        "unused",
        binary="/opt/codex",
        container_image="/image.sif" if contained else "",
        endpoint=profile if endpoint else None,
        extra_args=("-c", "features.hooks=true"),
    )
    harness.run("fixture", ws, resume)
    command = seen["command"]
    assert "--dangerously-bypass-hook-trust" not in command
    assert seen["env"]["CODEX_HOME"] == str(config.parent)
    if contained:
        source = Path(harness_mod.__file__).with_name("codex_requirements.toml").resolve()
        assert f"{source}:/etc/codex/requirements.toml:ro" in command
        assert tomllib.loads(source.read_text()) == {"allow_managed_hooks_only": True}
        assert seen["env"]["APPTAINERENV_CODEX_HOME"] == str(config.parent)
    else:
        assert command[-4:] == ["-c", "features.plugins=false", "-c", "features.hooks=false"]


def test_hook_trust_bypass_rejected(tmp_path: Path) -> None:
    result = CodexHarness("unused", extra_args=("--dangerously-bypass-hook-trust",)).run(
        "fixture", tmp_path
    )
    assert result.is_error and "hook trust bypass" in result.error_detail


@pytest.fixture
def responses_server() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            pass

        def do_POST(self) -> None:
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            events = [
                {
                    "type": "response.created",
                    "response": {"id": "r1", "status": "in_progress", "output": []},
                },
                {
                    "type": "response.completed",
                    "response": {
                        "id": "r1",
                        "status": "completed",
                        "output": [],
                        "usage": {"input_tokens": 1, "output_tokens": 0, "total_tokens": 1},
                    },
                },
            ]
            body = "".join(
                f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("contained", [False, True])
def test_real_project_hook_mutation_and_config_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    responses_server: tuple[str, list[dict[str, Any]]],
    contained: bool,
) -> None:
    binary = os.environ.get("OUTERLOOP_TEST_CODEX")
    image = os.environ.get("OUTERLOOP_TEST_CODEX_IMAGE", "") if contained else ""
    if not binary or (contained and not image):
        pytest.skip(
            "opt in with OUTERLOOP_TEST_CODEX (and OUTERLOOP_TEST_CODEX_IMAGE for containment)"
        )
    version = subprocess.run([binary, "--version"], capture_output=True, text=True, check=True)
    assert version.stdout.strip() == "codex-cli 0.160.0"
    ws = (tmp_path / "ws").resolve()
    (ws / ".codex").mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", str(ws)], check=True)
    marker = ws / "hook-ran"
    command = f"touch {shlex.quote(str(marker))}"
    hook = {"type": "command", "command": command, "timeout": 5, "async": False}
    hook_path = ws / ".codex" / "hooks.json"
    hook_path.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [hook]}]}}))
    # 0.160.0 hashes canonical JSON of normalized TOML (absent Option fields omitted).
    identity = {"event_name": "session_start", "hooks": [hook]}
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    hook_key = json.dumps(str(hook_path) + ":session_start:0:0")
    trust = (
        f'\n[projects.{json.dumps(str(ws))}]\ntrust_level="trusted"\n'
        f'[hooks.state.{hook_key}]\ntrusted_hash="sha256:{digest}"\n'
    )
    url, requests = responses_server
    (ws / ".codex" / "config.toml").write_text(
        'model="project-model"\nmodel_provider="project_provider"\nsandbox_mode="danger-full-access"\n'
        '[features]\nhooks=true\n[model_providers.project_provider]\nname="Project fixture"\n'
        'base_url="http://127.0.0.1:1/v1"\nwire_api="responses"\nrequires_openai_auth=false\n'
    )
    profile = EndpointProfile("fixture", url, tmp_path / "key", "fixture", ("responses",))
    harness = CodexHarness(
        "unused",
        binary=binary,
        model="",
        endpoint=profile,
        container_image=image,
        timeout_s=30,
    )
    # The embedded app-server auto-trusts the writable cwd and reloads project
    # config. Its model applies; provider settings are stripped (the request
    # reaches our kernel endpoint, not the project's unreachable address).
    first = harness.run("fixture", ws)
    assert first.session_id
    assert requests and requests[-1]["model"] == "project-model"
    assert not marker.exists()

    # Inject persisted trust AFTER the production scrub to isolate the launch
    # guard from the independent config-reset defense. Both sides get identical
    # trust; only the hook guard is removed for the positive mutation control.
    def seed_trusted(home: Path, config: str) -> bool:
        return _seed_codex_config(home, config + trust)

    monkeypatch.setattr(harness_mod, "_seed_codex_config", seed_trusted)
    guarded = harness.run("fixture", ws)
    assert guarded.session_id and requests[-1]["model"] == "project-model"
    assert '"type":"turn.completed"' in Path(guarded.transcript_path).read_text()
    assert not marker.exists()
    resumed = harness.run("fixture", ws, guarded.session_id)
    assert resumed.session_id == guarded.session_id
    assert '"type":"turn.completed"' in Path(resumed.transcript_path).read_text()
    assert not marker.exists()
    if contained:
        original_wrap = CodexHarness._apptainer_argv

        def without_policy(self: CodexHarness, *args: Any) -> list[str]:
            argv = original_wrap(self, *args)
            index = next(
                i for i, arg in enumerate(argv) if arg.endswith(":/etc/codex/requirements.toml:ro")
            )
            del argv[index - 1 : index + 1]
            return argv

        monkeypatch.setattr(CodexHarness, "_apptainer_argv", without_policy)
    else:
        original_command = harness_mod._codex_command

        def without_guard(*args: Any) -> list[str]:
            argv = original_command(*args)
            assert argv[-2:] == ["-c", "features.hooks=false"]
            return argv[:-2]

        monkeypatch.setattr(harness_mod, "_codex_command", without_guard)
    unguarded = harness.run("fixture", ws)
    assert unguarded.session_id
    assert '"type":"turn.completed"' in Path(unguarded.transcript_path).read_text()
    assert marker.exists(), "mutation must execute the planted hook; absence alone proves nothing"
