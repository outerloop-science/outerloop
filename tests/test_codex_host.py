"""Codex companion installation, drift repair, and launch refusal."""

import hashlib
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from outerloop import harness_cli
from outerloop.harness import CodexHarness
from outerloop.harness_pins import effective, pins

ROOT = Path(__file__).resolve().parents[1]
HOST = "codex-code-mode-host"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("failure", ["", "host-sha", "host-fetch", "codex-sha"])
@pytest.mark.parametrize("legacy", [False, True])
def test_install_pair(tmp_path, failure, legacy):
    version = pins("codex")["version"]
    assets = tmp_path / "assets"
    assets.mkdir()
    checksums = {}
    for name in ("codex", HOST):
        payload = assets / name
        payload.write_text(f"#!/bin/sh\necho '{version}'\n# {name}\n")
        archive = assets / f"{name}-x86_64-unknown-linux-musl.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(payload, arcname=f"{name}-x86_64-unknown-linux-musl")
        checksums[name] = digest(archive)
    target = tmp_path / "installed/codex"
    target.parent.mkdir()
    if legacy:
        target.write_bytes((assets / "codex").read_bytes())
        target.chmod(0o755)
        Path(f"{target}.verified-sha256").write_text(f"{checksums['codex']} {digest(target)}")
    before = target.read_bytes() if legacy else None
    script = tmp_path / "installer.sh"
    source = (ROOT / "scripts/install_codex.sh").read_text()
    for variable, field, name in (
        ("WANT_SHA256", "sha256", "codex"),
        ("HOST_SHA256", "code_mode_host_sha256", HOST),
    ):
        bad = failure == ("host-sha" if name == HOST else "codex-sha")
        checksum = "0" * 64 if bad else checksums[name]
        source = source.replace(f'{variable}="$(pin codex {field})"', f'{variable}="{checksum}"')
    script.write_text(source)
    shim = tmp_path / "shim"
    shim.mkdir()
    for name, content in {
        "uname": "#!/bin/sh\necho x86_64\n",
        "curl": """#!/bin/bash
set -eu
while [[ "$1" != https:* ]]; do shift; done
url="$1"
echo "$url" >> "$FETCHES"
if [[ "$url" == *codex-code-mode-host* && "$FAILURE" == host-fetch ]]; then exit 22; fi
while [ "$1" != -o ]; do shift; done
cp "$ASSETS/${url##*/}" "$2"
""",
        "tar": '#!/bin/sh\necho extracted >> "$EXTRACTS"\nexec /usr/bin/tar "$@"\n',
    }.items():
        path = shim / name
        path.write_text(content)
        path.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("OUTERLOOP_CODEX_")}
    env.update(
        PATH=f"{shim}:{env['PATH']}",
        ASSETS=str(assets),
        FAILURE=failure,
        FETCHES=str(tmp_path / "fetches"),
        EXTRACTS=str(tmp_path / "extracts"),
    )
    argv = ["bash", str(script), str(target)]
    result = subprocess.run(argv, env=env, capture_output=True, text=True)
    assert (result.returncode == 0) == (not failure), result.stderr
    host = target.with_name(HOST)
    if failure:
        assert (target.read_bytes() if target.exists() else None) == before
        assert not host.exists()
        if failure in ("host-sha", "host-fetch"):
            assert not (tmp_path / "extracts").exists()
    else:
        assert host.read_bytes() == (assets / HOST).read_bytes()
        assert host.stat().st_mode & 0o777 == 0o755
        desired = {"sha256": checksums["codex"], "code_mode_host_sha256": checksums[HOST]}
        assert harness_cli.verified("codex", target, desired)
        fetches = (tmp_path / "fetches").read_text()
        assert fetches.count(f"rust-v{version}/") == 2
        assert subprocess.run(argv, env=env).returncode == 0
        assert (tmp_path / "fetches").read_text() == fetches
        host.unlink()
        assert subprocess.run(argv, env=env).returncode == 0
        assert host.exists()
    assert not list(target.parent.glob("*.tmp.*"))


def test_host_pin_override():
    assert len(pins("codex")["code_mode_host_sha256"]) == 64
    env = {"OUTERLOOP_CODEX_VERSION": "99.0.0", "OUTERLOOP_CODEX_SHA256": "a" * 64}
    key = "OUTERLOOP_CODEX_CODE_MODE_HOST_SHA256"
    with pytest.raises(ValueError, match=f"requires {key}"):
        effective("codex", env)
    env[key] = "invalid"
    with pytest.raises(ValueError, match=f"invalid {key}"):
        effective("codex", env)
    env[key] = "b" * 64
    assert effective("codex", env)["code_mode_host_sha256"] == "b" * 64
    assert harness_cli.installer_environment(env, Path("/tmp"))[key] == env[key]
    from outerloop.cli import TICK_ENV_KEYS

    assert key in TICK_ENV_KEYS
    assert key in (ROOT / "scripts/tick_deploy.sh").read_text()


@pytest.mark.parametrize("host_state", ["missing", "not-executable"])
def test_preflight_missing_host(tmp_path, monkeypatch, host_state):
    binary = tmp_path / "codex"
    binary.touch()
    if host_state == "not-executable":
        binary.with_name(HOST).touch()
    monkeypatch.setattr(CodexHarness, "_login", lambda *a: pytest.fail("login started"))
    result = CodexHarness("key", binary=str(binary)).run("task", tmp_path / "ws")
    assert result.is_error
    assert result.stop_reason == "codex-code-mode-host-missing"
    assert "run outerloop harness upgrade codex" in result.error_detail


def test_container_host_bind_and_path_lookup(tmp_path, monkeypatch):
    binary = tmp_path / "codex"
    binary.touch()
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    harness = CodexHarness("key", binary="codex", container_image="image.sif")
    host = tmp_path / HOST
    assert harness._code_mode_host() == host
    argv = harness._apptainer_argv(["/opt/agent/codex"], tmp_path / "home", tmp_path)
    index = argv.index(f"{host}:/opt/agent/{HOST}:ro")
    assert argv[index - 1] == "--bind"


@pytest.mark.parametrize("drift", ["missing", "tampered", "marker", "mode"])
def test_status_and_upgrade_repair_legacy(tmp_path, monkeypatch, capsys, drift):
    desired = pins("codex")
    binary = tmp_path / "legacy/codex"
    binary.parent.mkdir()
    binary.write_text(f"#!/bin/sh\necho '{desired['version']}'\n")
    binary.chmod(0o755)
    Path(f"{binary}.verified-sha256").write_text(f"{desired['sha256']} {digest(binary)}")
    host = binary.with_name(HOST)
    if drift != "missing":
        host.write_text("#!/bin/sh\nexit 0\n")
        host.chmod(0o755)
        marker = Path(f"{host}.verified-sha256")
        marker.write_text(f"{desired['code_mode_host_sha256']} {digest(host)}")
        if drift == "tampered":
            host.write_text("changed")
        elif drift == "marker":
            marker.write_text(f"{'0' * 64} {digest(host)}")
        else:
            host.chmod(0o644)
    env = {"OUTERLOOP_CODEX_BIN": str(binary), "PATH": "", "HOME": str(tmp_path)}
    harness_cli.status(env)
    assert "DRIFT" in next(
        line for line in capsys.readouterr().out.splitlines() if line.startswith("codex:")
    )
    calls = []

    def install(script, target, install_env):
        calls.append(target)
        target.write_bytes(binary.read_bytes())
        target.chmod(0o755)
        Path(f"{target}.verified-sha256").write_text(f"{desired['sha256']} {digest(target)}")
        host = target.with_name(HOST)
        host.write_text("#!/bin/sh\nexit 0\n")
        host.chmod(0o755)
        Path(f"{host}.verified-sha256").write_text(
            f"{desired['code_mode_host_sha256']} {digest(host)}"
        )

    monkeypatch.setattr(harness_cli, "run_installer", install)
    env_file = tmp_path / ".env"
    harness_cli.upgrade_one("codex", env, env_file, tmp_path)
    assert len(calls) == 1
    assert env["OUTERLOOP_CODEX_BIN"] != str(binary)
    assert harness_cli.verified("codex", Path(env["OUTERLOOP_CODEX_BIN"]), desired)
    harness_cli.upgrade_one("codex", env, env_file, tmp_path)
    assert len(calls) == 1
