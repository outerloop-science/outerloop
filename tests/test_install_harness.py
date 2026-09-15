"""Pinned installation and init's missing-only provisioning."""

import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest

from outerloop import init
from outerloop.harness import HARNESS_INSTALL

ROOT = Path(__file__).resolve().parents[1]


def test_installer_mapping():
    assert set(init.AUTHOR_BACKENDS) <= HARNESS_INSTALL.keys()
    assert "hermes" in HARNESS_INSTALL
    for command in HARNESS_INSTALL.values():
        assert (ROOT / command.split()[1]).is_file()


@pytest.mark.parametrize("backend", init.AUTHOR_BACKENDS)
@pytest.mark.parametrize(
    "mode", ["missing", "present", "skip", "failure", "no_output", "permission"]
)
def test_init_install(tmp_path, monkeypatch, capsys, backend, mode):
    monkeypatch.setattr(init, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(init, "ensure_image", lambda **kw: "")
    target = tmp_path / "bin" / backend
    monkeypatch.setenv(init.author_bin_env(backend), str(target))
    monkeypatch.setattr(init, "locate_harness", lambda _: str(target) if mode == "present" else "")
    calls = []

    def run(argv, *, check):
        calls.append(argv)
        assert check
        assert argv == ["bash", str(ROOT / f"scripts/install_{backend}.sh"), str(target)]
        if mode == "permission":
            raise PermissionError("read-only target")
        if mode == "failure":
            raise subprocess.CalledProcessError(1, argv)
        if mode != "no_output":
            target.parent.mkdir()
            target.write_text("#!/bin/sh\n")
            target.chmod(0o755)

    monkeypatch.setattr(init.subprocess, "run", run)
    args = ["--yes", "--compute", "local", "--target", "o/r", "--author-backend", backend]
    if mode == "skip":
        args.append("--no-install-harness")
    rc = init.main(args)
    if mode in ("failure", "no_output", "permission"):
        assert rc == 1
        assert "Run manually: bash " in capsys.readouterr().err
        assert not (tmp_path / "config" / ".env").exists()
    else:
        assert rc == 0
        env = (tmp_path / "config" / ".env").read_text()
        assert (f"{init.author_bin_env(backend)}={target}" in env) == (mode != "skip")
    assert len(calls) == (mode not in ("present", "skip"))
    if mode == "missing":
        monkeypatch.setattr(init, "locate_harness", lambda _: str(target))
        assert init.main([*args, "--force"]) == 0
        assert len(calls) == 1


@pytest.mark.parametrize("valid", [True, False])
def test_claude_checksum_gate(tmp_path, valid):
    # Substitute only the test copy's pins, never the production verification
    # or hash tool: a small executable stands in for the 227 MB release asset.
    payload = b"#!/bin/sh\necho '2.1.272 (Claude Code)'\n"
    asset = tmp_path / "asset"
    asset.write_bytes(payload if valid else b"untrusted")
    script = tmp_path / "installer.sh"
    source = (ROOT / "scripts/install_claude.sh").read_text()
    script.write_text(
        re.sub(
            r'WANT_SHA256="[0-9a-f]{64}"',
            f'WANT_SHA256="{hashlib.sha256(payload).hexdigest()}"',
            source,
        )
    )
    shim = tmp_path / "shim"
    shim.mkdir()
    for name, body in {
        "curl": '#!/bin/bash\nwhile [ "$1" != "-o" ]; do shift; done\ncp "$ASSET" "$2"\n',
        "uname": '#!/bin/sh\ncase "$1" in -s) echo Linux;; -m) echo x86_64;; esac\n',
    }.items():
        path = shim / name
        path.write_text(body)
        path.chmod(0o755)
    target = tmp_path / "installed" / "claude"
    env = {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}", "ASSET": str(asset)}
    result = subprocess.run(
        ["bash", str(script), str(target)], env=env, capture_output=True, text=True
    )
    assert (result.returncode == 0) == valid, result.stderr
    assert target.exists() == valid
    assert not list(tmp_path.rglob("*.tmp.*"))
    if valid:
        assert target.read_bytes() == payload
        assert os.access(target, os.X_OK)
        (shim / "curl").write_text("#!/bin/sh\nexit 99\n")
        assert subprocess.run(["bash", str(script), str(target)], env=env).returncode == 0
    else:
        assert "sha256 mismatch" in result.stderr


def _claude_provisioning_block(deploy: str) -> str:
    start = deploy.index("# The configured author's CLI is a host prerequisite")
    end = deploy.index("esac\n", start) + len("esac\n")
    return deploy[start:end]


@pytest.mark.parametrize(
    ("author", "present", "on_path", "expected"),
    [
        ("claude", False, False, True),  # missing everywhere: install to ~/.local/bin
        ("claude", True, False, False),  # already under ~/.local/bin
        ("claude", False, True, False),  # already on PATH
        ("codex", False, False, False),  # not the configured author
    ],
)
def test_deploy_provisions_the_configured_author_cli(tmp_path, author, present, on_path, expected):
    """The chain's deploy step runs the pinned claude installer only when the
    configured author is claude and no CLI is found; codex keeps its own case."""
    home = tmp_path / "checkout"
    (home / "scripts").mkdir(parents=True)
    log = tmp_path / "calls"
    (home / "scripts" / "install_claude.sh").write_text(f'#!/bin/bash\necho "claude $1" >> {log}\n')
    userhome = tmp_path / "userhome"
    (userhome / ".local" / "bin").mkdir(parents=True)
    path_dir = tmp_path / "path"
    path_dir.mkdir()
    if present:
        binary = userhome / ".local" / "bin" / "claude"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
    if on_path:
        binary = path_dir / "claude"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
    env = {
        "HOME": str(userhome),
        "OUTERLOOP_HOME": str(home),
        "OUTERLOOP_AUTHOR_BACKEND": author,
        "PATH": f"{path_dir}:/usr/bin:/bin",
    }
    block = _claude_provisioning_block((ROOT / "scripts/tick_deploy.sh").read_text())
    subprocess.run(["/bin/bash", "-c", block], env=env, check=True, capture_output=True, text=True)
    calls = log.read_text().splitlines() if log.exists() else []
    assert calls == ([f"claude {userhome / '.local' / 'bin' / 'claude'}"] if expected else [])
