"""Single-source pins and transactional upgrades, including legacy .env paths."""

import ast
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from outerloop import harness_cli, paths
from outerloop.cli import env_file_values
from outerloop.compute import cache_environment
from outerloop.harness_pins import NAMES, effective, pins

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", NAMES)
def test_loader_and_checkout_entry(name):
    field = "ref" if name == "hermes" else "version"
    env = {k: v for k, v in os.environ.items() if not k.startswith("OUTERLOOP_")}
    for argv in (
        [sys.executable, "-m", "outerloop.harness_pins"],
        [sys.executable, str(ROOT / "src/outerloop/harness_pins.py")],
    ):
        assert (
            subprocess.check_output([*argv, name, field], env=env, text=True).strip()
            == pins(name)[field]
        )
    assert effective(name, {}) == pins(name)


def test_invalid_override():
    with pytest.raises(ValueError, match="requires OUTERLOOP_HERMES_SHA"):
        effective("hermes", {"OUTERLOOP_HERMES_REF": "v2099.1.1"})
    with pytest.raises(ValueError, match="invalid codex version"):
        effective("codex", {"OUTERLOOP_CODEX_VERSION": "../../elsewhere"})


def test_no_duplicate_pins():
    forbidden = [value for name in NAMES for value in pins(name).values()]
    for directory in ("scripts", ".github/workflows", "src/outerloop"):
        for path in (ROOT / directory).rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".sh", ".sbatch", ".yml"}:
                continue
            text = path.read_text()
            if path.suffix == ".py":
                tree = ast.parse(text)
                docstrings = {
                    id(node.value)
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                }
                literals = [
                    node.value
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstrings
                ]
                assert not any(value in literals for value in forbidden), path
            else:
                assert not any(value in text for value in forbidden), path
            assert not re.search(r'WANT(?:_SHA)?=["\'](?:v?\d|[a-f0-9]{40})', text), path
            assert not re.search(
                r"@openai/codex@\d|bash -s -- \d|HERMES_(?:REF|SHA):\s*[a-fv0-9]", text
            ), path
    for name in NAMES:
        assert f"pin {name} " in (ROOT / f"scripts/install_{name}.sh").read_text()
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        workflow = yaml.safe_load(path.read_text())
        for job in workflow.get("jobs", {}).values():
            steps = job.get("steps", [])
            for step in steps:
                command = step.get("run", "")
                if "@openai/codex@" not in command and "claude.ai/install.sh" not in command:
                    continue
                resolver = next(
                    s for s in steps[: steps.index(step)] if s.get("name") == "Resolve harness pins"
                )
                assert "set -euo pipefail" in resolver["run"]
                assert "::error::" in resolver["run"]
                assert "$(python3" not in command


def binary(path, version):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho '{version}'\n")
    path.chmod(0o755)


@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize("drift", [False, True])
def test_status_read_only(tmp_path, capsys, override, drift):
    version = "99.1.2" if override else pins("claude")["version"]
    target = tmp_path / "claude"
    binary(target, "0.0.1" if drift else version)
    env = {"OUTERLOOP_CLAUDE_BIN": str(target), "PATH": "", "HOME": str(tmp_path)}
    if override:
        env["OUTERLOOP_CLAUDE_VERSION"] = version
        env["OUTERLOOP_CLAUDE_SHA256"] = hashlib.sha256(
            f"#!/bin/sh\necho '{version}'\n".encode()
        ).hexdigest()
    before = set(tmp_path.rglob("*"))
    assert harness_cli.status(env) == 0
    line = capsys.readouterr().out.splitlines()[0]
    assert ("DRIFT" in line) == (drift or not override)
    assert ("override=" in line) == override
    assert f"path={target}" in line
    assert set(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("failure", ["install", "verify", "switch"])
def test_upgrade_legacy_failure_retry_and_idempotence(tmp_path, monkeypatch, failure):
    # Legacy init writes a direct binary path and unrelated operator settings.
    old = tmp_path / "legacy" / "claude"
    binary(old, "0.0.1")
    env_file = tmp_path / ".env"
    legacy = f'# operator settings\nOUTERLOOP_CLAUDE_BIN="{old}"\nOUTERLOOP_PANEL=\n'
    env_file.write_text(legacy)
    env = {**os.environ, **env_file_values(env_file, keys=None)}
    calls = []
    broken = True

    def run(script, target, install_env):
        argv = ["bash", str(script), str(target)]
        calls.append(argv)
        if broken and failure == "install":
            raise subprocess.CalledProcessError(17, argv)
        binary(
            Path(argv[-1]), "0.0.2" if broken and failure == "verify" else pins("claude")["version"]
        )
        return subprocess.CompletedProcess(argv, 0)

    replace = os.replace

    def switch(source, target):
        if broken and failure == "switch":
            raise OSError("interrupted before switch")
        replace(source, target)

    monkeypatch.setattr(harness_cli, "run_installer", run)
    monkeypatch.setattr(harness_cli, "verified", lambda *a: True)
    monkeypatch.setattr(harness_cli.os, "replace", switch)
    with pytest.raises((OSError, ValueError, subprocess.CalledProcessError)):
        harness_cli.upgrade_one("claude", env, env_file, tmp_path)
    assert env_file.read_text() == legacy
    assert harness_cli.probe("claude", old) == ("0.0.1", True)
    broken = False
    for state in (tmp_path / "cache/harness-failures").glob("*.json"):
        state.unlink()
    harness_cli.upgrade_one("claude", env, env_file, tmp_path)
    recorded = env_file_values(env_file, keys=None)
    assert recorded["OUTERLOOP_CLAUDE_BIN"] != str(old)
    assert recorded["OUTERLOOP_PANEL"] == ""
    assert old.exists()
    assert "/harnesses/claude/" in recorded["OUTERLOOP_CLAUDE_BIN"]
    before = len(calls)
    harness_cli.upgrade_one("claude", env, env_file, tmp_path)
    assert len(calls) == before


def test_upgrade_returns_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(paths, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(
        harness_cli, "upgrade_one", lambda *a: (_ for _ in ()).throw(ValueError("broken"))
    )
    assert harness_cli.main(["upgrade", "claude"]) == 1
    assert "previous installation retained: broken" in capsys.readouterr().err


def test_hermes_legacy_runtime_follows_installed_sha(tmp_path, monkeypatch, capsys):
    from outerloop.hermes_install import hermes_ready, hermes_runtime

    repo = tmp_path / "hermes"
    repo.mkdir()
    (repo / "run_agent.py").write_text("pass\n")
    for args in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "legacy"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    env = {
        "REVIEW_HERMES_REPO": str(repo),
        "OUTERLOOP_HERMES_REF": "v2099.1.1",
        "OUTERLOOP_HERMES_SHA": sha,
        "PATH": "",
    }
    assert harness_cli.probe("hermes", repo) == (sha, False)
    runtime = hermes_runtime(repo)
    binary(runtime / "venv/bin/python", "3.12")
    (runtime / ".complete").write_text(sha)
    # Legacy runtime markers are still launchable, but upgrades require a digest.
    assert not harness_cli.verified("hermes", repo, effective("hermes", env))
    python = runtime / "venv/bin/python"
    Path(f"{python}.verified-sha256").write_text(hashlib.sha256(python.read_bytes()).hexdigest())
    for _ in range(2):
        assert hermes_ready(repo)
        assert harness_cli.probe("hermes", repo) == (sha, True)
    harness_cli.status(env)
    line = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("hermes:"))
    assert "override=" in line and "DRIFT" not in line and ".complete=valid" in line
    (runtime / ".complete").write_text("interrupted")
    assert not hermes_ready(repo)
    assert harness_cli.probe("hermes", repo) == (sha, False)


def test_used_and_cache_environment(tmp_path):
    env = {
        "OUTERLOOP_AUTHOR_BACKEND": "codex",
        "OUTERLOOP_PANEL": "review:hermes:model",
        "OUTERLOOP_CACHE_ROOT": str(tmp_path / "cache"),
    }
    assert harness_cli.used_harnesses(env) == ["codex", "hermes"]
    env["OUTERLOOP_STEWARD_KEY_FILE"] = "/keys/steward"
    assert harness_cli.used_harnesses(env) == ["claude", "codex", "hermes"]
    cache = cache_environment(env)
    assert {"UV_CACHE_DIR", "XDG_CACHE_HOME", "WANDB_DIR"} <= cache.keys()
    assert all(
        Path(value).is_relative_to(tmp_path) and Path(value).is_dir() for value in cache.values()
    )
    assert cache_environment({}) == {}


def test_tick_caches_exported_before_uv():
    for name in ("tick_resident.sh", "tick_chain.sbatch"):
        text = (ROOT / "scripts" / name).read_text()
        export = 'export UV_CACHE_DIR="${UV_CACHE_DIR:-$OUTERLOOP_ROOT/cache/uv}"'
        assert text.index(export) < text.index("uv run --no-sync")


@pytest.mark.parametrize("name", ["claude", "codex"])
@pytest.mark.parametrize("valid", [False, True])
def test_override_installer_verifies_before_switch(tmp_path, name, valid):
    import hashlib
    import json

    version = "99.1.2-beta.1"
    payload = f"#!/bin/sh\necho '{version}'\n"
    target = tmp_path / name
    binary(target, "0.0.1")
    asset = tmp_path / "asset"
    asset.write_text(payload)
    asset.chmod(0o755)
    manifest = tmp_path / "manifest.json"
    checksum = hashlib.sha256(payload.encode()).hexdigest() if valid else "0" * 64
    manifest.write_text(json.dumps({"platforms": {"linux-x64": {"checksum": checksum}}}))
    shim = tmp_path / "bin"
    shim.mkdir()
    scripts = {
        "uname": '#!/bin/sh\ncase "$1" in -s) echo Linux;; -m) echo x86_64;; esac\n',
        "curl": '#!/bin/sh\ncase "$*" in *manifest.json*) cat "$MANIFEST";; *) '
        'while [ "$1" != -o ]; do shift; done; cp "$ASSET" "$2";; esac\n',
        "npm": '#!/bin/sh\n[ "$VALID" = True ] || exit 12\n'
        'dest="$3/node_modules/@openai/codex-linux-x64/vendor/target/codex"\n'
        'mkdir -p "$dest"\ncp "$ASSET" "$dest/codex"\n',
    }
    for command, content in scripts.items():
        path = shim / command
        path.write_text(content)
        path.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{shim}:{os.environ['PATH']}",
        f"OUTERLOOP_{name.upper()}_VERSION": version,
        f"OUTERLOOP_{name.upper()}_SHA256": checksum,
        "MANIFEST": str(manifest),
        "ASSET": str(asset),
        "VALID": str(valid),
    }
    argv = ["bash", str(ROOT / f"scripts/install_{name}.sh"), str(target)]
    result = subprocess.run(argv, env=env, capture_output=True, text=True)
    assert (result.returncode == 0) == valid, result.stderr
    assert harness_cli.probe(name, target) == (version if valid else "0.0.1", True)
    if valid:
        (shim / "curl").write_text("#!/bin/sh\nexit 99\n")
        (shim / "npm").write_text("#!/bin/sh\nexit 99\n")
        assert subprocess.run(argv, env=env).returncode == 0


def test_local_job_cache_allowlist(tmp_path, monkeypatch):
    import json
    import shlex

    from outerloop.compute import JobSpec, LocalCompute

    monkeypatch.setenv("OUTERLOOP_CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("WANDB_DIR", raising=False)
    output = tmp_path / "env.json"
    code = f"import json, os; open({str(output)!r}, 'w').write(json.dumps(dict(os.environ)))"
    job = LocalCompute().submit(
        JobSpec(
            job_name="cache-test",
            account="",
            partition="",
            time_minutes=1,
            command=shlex.join([sys.executable, "-c", code]),
        )
    )
    assert job
    values = json.loads(output.read_text())
    assert values["XDG_CACHE_HOME"] == str(tmp_path / "cache/xdg")
    assert values["WANDB_DIR"] == str(tmp_path / "cache/wandb")


def test_slurm_cache_environment(tmp_path, monkeypatch):
    from outerloop.compute import _subprocess_runner

    monkeypatch.setenv("OUTERLOOP_CACHE_ROOT", str(tmp_path))
    monkeypatch.delenv("WANDB_DIR", raising=False)
    captured = {}

    def run(argv, **kwargs):
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "123", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert _subprocess_runner(["sbatch", "job.sh"], 10).returncode == 0
    assert captured["WANDB_DIR"] == str(tmp_path / "wandb")


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_override_requires_hash_status_and_upgrade(tmp_path, capsys, name):
    env = {f"OUTERLOOP_{name.upper()}_VERSION": "99.1.2"}
    assert harness_cli.status(env) == 1
    assert f"OUTERLOOP_{name.upper()}_SHA256" in capsys.readouterr().out
    with pytest.raises(ValueError, match="requires"):
        harness_cli.upgrade_one(name, env, tmp_path / ".env", tmp_path)


def test_installer_environment_allowlist(tmp_path):
    env = {
        "BASH_ENV": "/evil",
        "PYTHONPATH": "/evil",
        "SECRET": "secret",
        "HOME": str(tmp_path),
        "OUTERLOOP_CLAUDE_VERSION": "99.1.2",
        "OUTERLOOP_CACHE_ROOT": str(tmp_path / "cache"),
    }
    actual = harness_cli.installer_environment(env, tmp_path)
    assert set(actual) == {
        "HOME",
        "PATH",
        "OUTERLOOP_CLAUDE_VERSION",
        "OUTERLOOP_CACHE_ROOT",
        "npm_config_cache",
    }
    assert actual["npm_config_cache"] == str(tmp_path / "cache/npm")
    env["npm_config_cache"] = str(tmp_path / "custom")
    assert (
        harness_cli.installer_environment(env, tmp_path)["npm_config_cache"]
        == env["npm_config_cache"]
    )
    assert harness_cli.used_harnesses({"OUTERLOOP_PANEL": ""}) == ["claude"]


def test_timeout_cleanup_and_backoff(tmp_path, monkeypatch):
    old = tmp_path / "old"
    binary(old, "0.0.1")
    env_file = tmp_path / ".env"
    original = f"OUTERLOOP_CLAUDE_BIN={old}\n"
    env_file.write_text(original)
    env = {"OUTERLOOP_CLAUDE_BIN": str(old), "OUTERLOOP_HARNESS_TIMEOUT_SECONDS": "0.2"}
    script = tmp_path / "hung.sh"
    child = tmp_path / "child"
    script.write_text(f"(sleep 1; touch '{child}') &\nwait\n")
    run = harness_cli.run_installer
    calls = []

    def hung(unused, target, install_env):
        calls.append(target)
        run(script, target, install_env)

    monkeypatch.setattr(harness_cli, "probe", lambda *a: ("0.0.1", True))
    monkeypatch.setattr(harness_cli, "run_installer", hung)
    with pytest.raises(TimeoutError, match="timed out"):
        harness_cli.upgrade_one("claude", env, env_file, tmp_path)
    assert env_file.read_text() == original
    assert not list((tmp_path / "harnesses").rglob("install-*"))
    with pytest.raises(ValueError, match="backoff"):
        harness_cli.upgrade_one("claude", env, env_file, tmp_path)
    assert len(calls) == 1
    import time

    time.sleep(1)
    assert not child.exists()


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_hash_mismatch_fast_path(tmp_path, name):
    target = tmp_path / name
    binary(target, "99.1.2")
    env = {
        **os.environ,
        f"OUTERLOOP_{name.upper()}_VERSION": "99.1.2",
        f"OUTERLOOP_{name.upper()}_SHA256": "0" * 64,
    }
    assert not harness_cli.verified(name, target, effective(name, env))
    # Force supported Claude platform for the standalone installer.
    shim = tmp_path / "bin"
    shim.mkdir()
    uname = shim / "uname"
    uname.write_text('#!/bin/sh\ncase "$1" in -s) echo Linux;; -m) echo x86_64;; esac\n')
    uname.chmod(0o755)
    for command in ("curl", "npm"):
        executable = shim / command
        executable.write_text("#!/bin/sh\nexit 19\n")
        executable.chmod(0o755)
    env["PATH"] = f"{shim}:{env['PATH']}"
    result = subprocess.run(
        ["bash", str(ROOT / f"scripts/install_{name}.sh"), str(target)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "sha256" in result.stderr
    assert not Path(f"{target}.verified-sha256").exists()


def test_workflow_missing_pin_fails_loudly(tmp_path):
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        for job in yaml.safe_load(path.read_text()).get("jobs", {}).values():
            for step in job.get("steps", []):
                if step.get("name") != "Resolve harness pins":
                    continue
                result = subprocess.run(
                    ["bash", "-c", step["run"]], cwd=tmp_path, capture_output=True, text=True
                )
                assert result.returncode != 0
                assert "::error::Harness pins reader missing" in result.stdout


def test_env_file_does_not_supply_installer_environment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BASH_ENV=/unexpected\nPYTHONPATH=/unexpected\nHOME=/unexpected\n"
        "PATH=/unexpected\nUNRELATED_SECRET=hidden\nOUTERLOOP_CLAUDE_VERSION=99.1.2\n"
        f"OUTERLOOP_CLAUDE_SHA256={'0' * 64}\n"
    )
    monkeypatch.setattr(paths, "ENV_FILE", env_file)
    monkeypatch.setattr(os, "environ", {})
    captured = {}

    def upgrade(name, env, env_file, root):
        captured.update(harness_cli.installer_environment(env, root))

    monkeypatch.setattr(harness_cli, "upgrade_one", upgrade)
    assert harness_cli.main(["upgrade", "claude"]) == 0
    assert set(captured) == {
        "PATH",
        "OUTERLOOP_CLAUDE_VERSION",
        "OUTERLOOP_CLAUDE_SHA256",
        "OUTERLOOP_CACHE_ROOT",
        "npm_config_cache",
    }
    assert "/unexpected" not in captured.values()


@pytest.mark.parametrize("output", ["", "99.1.2"])
def test_workflow_missing_field_fails_loudly(tmp_path, output):
    workflow = yaml.safe_load((ROOT / ".github/workflows/advisory-review-agent.yml").read_text())
    steps = next(job["steps"] for job in workflow["jobs"].values() if "steps" in job)
    resolver = next(step["run"] for step in steps if step.get("name") == "Resolve harness pins")
    reader = tmp_path / ".autoresearch/src/outerloop/harness_pins.py"
    reader.parent.mkdir(parents=True)
    reader.write_text(
        "import sys\n"
        f"if sys.argv[1] == 'claude': print({output!r})\n"
        "else: sys.exit('missing pin')\n"
    )
    result = subprocess.run(
        ["bash", "-c", resolver],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "GITHUB_ENV": str(tmp_path / "env")},
    )
    assert result.returncode != 0
    assert "::error::Cannot read Codex pin" in result.stdout
    assert not (tmp_path / "env").exists()


@pytest.mark.parametrize("source", ["file", "env", "flag"])
@pytest.mark.parametrize("failure", [False, True])
def test_manual_upgrade_root_precedence(tmp_path, monkeypatch, source, failure):
    env_file = tmp_path / ".env"
    env_file.write_text(f"OUTERLOOP_ROOT={tmp_path / 'file'}\n")
    monkeypatch.setattr(paths, "ENV_FILE", env_file)
    monkeypatch.setattr(os, "environ", {})
    if source in {"env", "flag"}:
        monkeypatch.setenv("OUTERLOOP_ROOT", str(tmp_path / "env"))
    monkeypatch.setattr(harness_cli, "verified", lambda *a: True)

    def fail(script, target, env):
        assert target.is_relative_to(tmp_path / source / "harnesses")
        if failure:
            raise OSError("test failure")
        binary(target, pins("claude")["version"])

    monkeypatch.setattr(harness_cli, "run_installer", fail)
    argv = ["upgrade", "claude"]
    if source == "flag":
        argv += ["--root", str(tmp_path / "flag")]
    assert harness_cli.main(argv) == int(failure)
    assert len(list((tmp_path / source / "cache/harness-failures").glob("*.json"))) == int(failure)
    if not failure:
        recorded = env_file_values(env_file, keys=None)
        assert Path(recorded["OUTERLOOP_CLAUDE_BIN"]).is_relative_to(
            tmp_path / source / "harnesses"
        )


@pytest.mark.parametrize("content", ['{"attempts":', "{}", "[]", '{"attempts": -1}', "\xff"])
def test_invalid_retry_state_does_not_block_upgrade(tmp_path, monkeypatch, capsys, content):
    import json

    desired = effective("claude", {})
    key = hashlib.sha256(json.dumps(desired, sort_keys=True).encode()).hexdigest()
    state = tmp_path / "cache/harness-failures" / f"claude-{key}.json"
    state.parent.mkdir(parents=True)
    state.write_bytes(content.encode("latin1"))
    env: dict[str, str] = {}
    monkeypatch.setattr(harness_cli, "verified", lambda *a: True)
    monkeypatch.setattr(
        harness_cli, "run_installer", lambda script, target, env: binary(target, desired["version"])
    )
    harness_cli.upgrade_one("claude", env, tmp_path / ".env", tmp_path)
    assert capsys.readouterr().err.count("ignoring unreadable/invalid retry state") == 1
    assert not state.exists()
    assert Path(env["OUTERLOOP_CLAUDE_BIN"]).is_relative_to(tmp_path / "harnesses")


def test_retry_state_atomic_replace(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(harness_cli, "probe", lambda *a: ("missing", False))
    monkeypatch.setattr(
        harness_cli, "run_installer", lambda *a: (_ for _ in ()).throw(OSError("install failed"))
    )
    replace = os.replace
    published = []

    def inspect(source, target):
        assert source != target
        assert Path(source).parent == target.parent
        assert json.loads(Path(source).read_text())["attempts"] == 1
        published.append(target)
        replace(source, target)

    monkeypatch.setattr(harness_cli.os, "replace", inspect)
    with pytest.raises(OSError, match="install failed"):
        harness_cli.upgrade_one("claude", {}, tmp_path / ".env", tmp_path)
    assert len(published) == 1
    assert list(published[0].parent.iterdir()) == published


@pytest.mark.parametrize("role", ["author", "override", "reviewer", "panel", "lens_model"])
def test_upgrade_used_continues_after_role_resolution_error(tmp_path, monkeypatch, capsys, role):
    (tmp_path / "key").write_text("test-key")
    (tmp_path / "key").chmod(0o600)
    env = {
        "OUTERLOOP_AUTHOR_BACKEND": "claude",
        "OUTERLOOP_AUTHOR_MODEL": "model",
        "OUTERLOOP_PANEL": "review:codex:model[endpoint=good]",
        "OUTERLOOP_ENDPOINT_GOOD_URL": "https://example.com/v1",
        "OUTERLOOP_ENDPOINT_GOOD_KEY_FILE": str(tmp_path / "key"),
        "OUTERLOOP_ENDPOINT_GOOD_API": "chat",
        "OUTERLOOP_ENDPOINT_GOOD_MODEL": "model",
    }
    if role == "author":
        env.update(OUTERLOOP_AUTHOR_BACKEND="codex", OUTERLOOP_AUTHOR_ENDPOINT="missing")
    elif role == "override":
        env["OUTERLOOP_AUTHOR_OVERRIDES"] = (
            '{"owner/repo":{"backend":"codex","model":"model[endpoint=missing]"}}'
        )
    elif role == "reviewer":
        env.update(REVIEW_BACKEND="codex", REVIEW_ENDPOINT="missing")
    elif role == "panel":
        env["OUTERLOOP_PANEL"] = "verify:codex:model[endpoint=missing]," + env["OUTERLOOP_PANEL"]
    else:
        env["OUTERLOOP_PANEL"] = "verify:hermes," + env["OUTERLOOP_PANEL"]
    monkeypatch.setattr(os, "environ", env)
    monkeypatch.setattr(paths, "ENV_FILE", tmp_path / ".env")
    upgraded = []
    monkeypatch.setattr(harness_cli, "upgrade_one", lambda name, *args: upgraded.append(name))
    assert harness_cli.main(["upgrade", "--used", "--root", str(tmp_path)]) == 0
    assert "codex" in upgraded and "bridge" in upgraded
    assert ("hermes" in upgraded) == (role == "lens_model")
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    assert "skipping bridge detection" in lines[0]
    assert ("names no model" if role == "lens_model" else "unknown endpoint profile") in lines[0]
