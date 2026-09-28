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
            if backend == "hermes":
                from outerloop.hermes_install import HERMES_SHA, hermes_runtime

                target.mkdir(parents=True)
                (target / "run_agent.py").touch()
                runtime = hermes_runtime(target)
                (runtime / "venv/bin").mkdir(parents=True)
                python = runtime / "venv/bin/python"
                python.write_text("#!/bin/sh\n")
                python.chmod(0o755)
                (runtime / ".complete").write_text(HERMES_SHA)
            else:
                target.parent.mkdir()
                target.write_text("#!/bin/sh\n")
                target.chmod(0o755)

    monkeypatch.setattr(init.subprocess, "run", run)
    args = ["--yes", "--compute", "local", "--target", "o/r", "--author-backend", backend]
    if mode == "skip":
        args.append("--no-install-harness")
    if backend == "hermes":
        image = tmp_path / "image.sif"
        image.touch()
        monkeypatch.setattr(init, "ensure_image", lambda **kw: str(image))
        monkeypatch.setenv("REVIEW_HERMES_PROVIDER", "openai")
        args += ["--author-model", "gpt-native"]
        if mode == "present":
            # Provision the same pinned runtime an earlier installer left.
            run(["bash", str(ROOT / "scripts/install_hermes.sh"), str(target)], check=True)
            calls.clear()
    rc = init.main(args)
    if mode in ("failure", "no_output", "permission"):
        assert rc == 1
        assert "Run manually: bash " in capsys.readouterr().err
        assert not (tmp_path / "config" / ".env").exists()
    elif backend == "hermes" and mode == "skip":
        assert rc == 2  # skipping a missing runtime cannot report successful setup
        assert not (tmp_path / "config" / ".env").exists()
    else:
        assert rc == 0
        env = (tmp_path / "config" / ".env").read_text()
        assert (f"{init.author_bin_env(backend)}={target}" in env) == (
            mode != "skip" or backend == "hermes"
        )
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
            r'WANT_SHA256="\$\(pin claude [a-z0-9-]+\)"',
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


@pytest.mark.parametrize(
    ("author_bin", "github_app", "no_install", "wanted"),
    [
        ("", False, False, True),
        ("/usr/local/bin/claude", False, False, False),
        ("", True, False, False),  # the focused App run sets up the App only
        ("", False, True, False),
    ],
)
def test_cli_install_wanted(author_bin, github_app, no_install, wanted):
    import argparse

    args = argparse.Namespace(github_app=github_app, no_install_harness=no_install)
    assert init.cli_install_wanted(author_bin, args) is wanted


@pytest.mark.parametrize("setting", ["panel", "reviewer"])
@pytest.mark.parametrize("mode", ["missing", "present", "skip", "failure"])
def test_init_installs_hermes_judges(tmp_path, monkeypatch, setting, mode):
    from outerloop.hermes_install import HERMES_SHA, hermes_runtime

    config = tmp_path / "config"
    config.mkdir()
    repo = tmp_path / "hermes"
    monkeypatch.setattr(init, "CONFIG_DIR", config)
    monkeypatch.setattr(init, "ensure_image", lambda **kw: "")
    monkeypatch.setattr(init, "locate_harness", lambda _: "/bin/claude")
    monkeypatch.delenv("OUTERLOOP_PANEL", raising=False)
    monkeypatch.delenv("REVIEW_BACKEND", raising=False)
    monkeypatch.setenv("REVIEW_HERMES_REPO", str(repo))
    key, value = (
        ("OUTERLOOP_PANEL", "verify,review:hermes:judge-model")
        if setting == "panel"
        else ("REVIEW_BACKEND", "hermes")
    )
    # Existing deployment settings must survive the setup rewrite too.
    (config / ".env").write_text(f"{key}={value}\n")

    def provision():
        repo.mkdir(exist_ok=True)
        (repo / "run_agent.py").touch()
        runtime = hermes_runtime(repo)
        (runtime / "venv/bin").mkdir(parents=True, exist_ok=True)
        python = runtime / "venv/bin/python"
        python.write_text("#!/bin/sh\n")
        python.chmod(0o755)
        (runtime / ".complete").write_text(HERMES_SHA)

    if mode == "present":
        provision()
    calls = []

    def run(argv, *, check):
        calls.append(argv)
        assert argv == ["bash", str(ROOT / "scripts/install_hermes.sh"), str(repo)]
        if mode == "failure":
            raise subprocess.CalledProcessError(1, argv)
        provision()

    monkeypatch.setattr(init.subprocess, "run", run)
    args = ["--yes", "--force", "--compute", "local", "--target", "o/r"]
    if mode == "skip":
        args.append("--no-install-harness")
    assert init.main(args) == (1 if mode == "failure" else 0)
    assert len(calls) == (mode in ("missing", "failure"))
    if mode != "failure":
        written = (config / ".env").read_text()
        assert f"REVIEW_HERMES_REPO={repo}" in written
        assert f"{key}={value}" in written


def test_hermes_source_only_upgrade_retry_and_reuse(tmp_path):
    # Legacy installer output: a clean pinned checkout without any runtime.
    repo = tmp_path / "hermes"
    repo.mkdir()
    (repo / "run_agent.py").write_text("pass\n")
    for args in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    source = (ROOT / "scripts/install_hermes.sh").read_text()
    assert 'WANT_SHA="$(pin hermes sha)"' in source
    script = tmp_path / "installer.sh"
    script.write_text(source.replace('WANT_SHA="$(pin hermes sha)"', f'WANT_SHA="{sha}"'))
    shim = tmp_path / "bin"
    shim.mkdir()
    uv = shim / "uv"
    uv.write_text("""#!/bin/bash
set -eu
test -d "${REPO}.installing"
printf '%s\\n' "$*" >> "$CALLS"
case "$1 $2" in
    "python install")
        mkdir -p "$UV_PYTHON_INSTALL_DIR/bin"
        printf '#!/bin/sh\\nexit 0\\n' > "$UV_PYTHON_INSTALL_DIR/bin/python"
        chmod +x "$UV_PYTHON_INSTALL_DIR/bin/python"
        ;;
    "python find")
        [ "$*" = "python find --system 3.12" ]
        [ "$UV_PYTHON_PREFERENCE" = "only-managed" ]
        echo "$UV_PYTHON_INSTALL_DIR/bin/python"
        ;;
    "sync --project")
        expected="sync --project $REPO --frozen --no-install-project"
        [ "$*" = "$expected --python $UV_PYTHON_INSTALL_DIR/bin/python" ]
        [ ! -f "$FAIL" ] || exit 9
        mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"
        ln -sf "$UV_PYTHON_INSTALL_DIR/bin/python" "$UV_PROJECT_ENVIRONMENT/bin/python"
        ;;
    *) exit 8 ;;
esac
""")
    uv.chmod(0o755)
    calls = tmp_path / "calls"
    fail = tmp_path / "fail"
    env = {
        **os.environ,
        "PATH": f"{shim}:{os.environ['PATH']}",
        "CALLS": str(calls),
        "FAIL": str(fail),
        "REPO": str(repo),
    }
    runtime = Path(f"{repo}.runtime") / sha
    fail.touch()
    assert subprocess.run(["bash", str(script), str(repo)], env=env).returncode == 9
    assert not (runtime / ".complete").exists()
    fail.unlink()
    assert subprocess.run(["bash", str(script), str(repo)], env=env).returncode == 0
    assert (runtime / ".complete").read_text().strip() == sha
    assert (runtime / "venv/bin/python").resolve().is_relative_to(runtime)
    assert subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"]) == b""
    prior = calls.read_text()
    assert subprocess.run(["bash", str(script), str(repo)], env=env).returncode == 0
    assert calls.read_text() == prior
    # A matching completion SHA cannot bless a modified interpreter.
    python = runtime / "venv/bin/python"
    python.write_text("#!/bin/sh\nexit 19\n")
    assert subprocess.run(["bash", str(script), str(repo)], env=env).returncode == 0
    assert calls.read_text() != prior
    assert python.read_text() == "#!/bin/sh\nexit 0\n"


@pytest.mark.parametrize("source_exists", [False, True])
def test_hermes_lock_precedes_source_access(tmp_path, source_exists):
    repo = tmp_path / "hermes"
    if source_exists:
        repo.mkdir()
        (repo / "keep").write_text("untouched")
    lock = Path(f"{repo}.installing")
    lock.mkdir()
    shim = tmp_path / "bin"
    shim.mkdir()
    git = shim / "git"
    git.write_text(f'#!/bin/sh\ntouch "{tmp_path / "git-called"}"\nexit 99\n')
    git.chmod(0o755)
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/install_hermes.sh"), str(repo)],
        env={**os.environ, "PATH": f"{shim}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "installation in progress" in result.stderr
    assert not (tmp_path / "git-called").exists()
    assert repo.exists() is source_exists
    if source_exists:
        assert (repo / "keep").read_text() == "untouched"
    assert lock.is_dir()  # a contender must not release the owner's lock
    assert not Path(f"{repo}.runtime").exists()


@pytest.mark.parametrize("override", [False, True])
def test_full_init_preserves_review_settings(tmp_path, monkeypatch, override):
    monkeypatch.setattr(init, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(init, "locate_harness", lambda _: "/bin/claude")
    values = {
        "REVIEW_BACKEND": "hermes",
        "REVIEW_MODEL": "file-model",
        "REVIEW_HERMES_PROVIDER": "openai",
    }
    (tmp_path / ".env").write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    for key in values:
        monkeypatch.delenv(key, raising=False)
    if override:
        values.update(REVIEW_MODEL="env-model", REVIEW_HERMES_PROVIDER="openrouter")
        for key, value in values.items():
            monkeypatch.setenv(key, value)
    assert (
        init.main(
            [
                "--yes",
                "--force",
                "--compute",
                "local",
                "--target",
                "o/r",
                "--no-image",
                "--no-install-harness",
            ]
        )
        == 0
    )
    written = init.env_file_values(tmp_path / ".env", keys=None)
    for key, value in values.items():
        assert written[key] == value


def test_workflow_hermes_installers(tmp_path):
    import yaml

    count = 0
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        for job in workflow.get("jobs", {}).values():
            steps = job.get("steps", [])
            for step in steps:
                command = step.get("run", "")
                if "hermes)" not in command:
                    continue
                count += 1
                # Reusable jobs run in the caller's workspace: the installer
                # must come from the kernel checkout, not the caller's scripts.
                assert any(
                    s.get("uses", "").startswith("actions/checkout@")
                    and s.get("with", {}).get("path") == ".autoresearch"
                    for s in steps[: steps.index(step)]
                )
                installer = tmp_path / ".autoresearch/scripts/install_hermes.sh"
                installer.parent.mkdir(parents=True, exist_ok=True)
                installer.write_text(
                    'test "$1" = "$GITHUB_WORKSPACE/hermes-agent"\n'
                    'touch "$GITHUB_WORKSPACE/called"\n'
                )
                called = tmp_path / "called"
                called.unlink(missing_ok=True)
                env = {
                    **os.environ,
                    **job.get("env", {}),
                    **step.get("env", {}),
                    "BACKEND": "hermes",
                    "GITHUB_WORKSPACE": str(tmp_path),
                }
                result = subprocess.run(["bash", "-c", command], env=env)
                assert result.returncode == 0, path
                assert called.exists(), path
    assert count == 5


@pytest.mark.parametrize("moved_tag", [False, True])
def test_hermes_clone_retry_and_pin_verification(tmp_path, moved_tag):
    from outerloop.hermes_install import HERMES_SHA

    shim = tmp_path / "bin"
    shim.mkdir()
    scripts = {
        "git": """#!/bin/bash
set -eu
[ -d "${REPO}.installing" ]
printf '%s\\n' "$*" >> "$CALLS"
if [ "$1" = clone ]; then
    # A failed clone may leave a partial checkout; the retry must clear it.
    if [ ! -f "$ATTEMPT" ]; then
        touch "$ATTEMPT"
        mkdir -p "$REPO"
        touch "$REPO/partial"
        exit 1
    fi
    [ ! -e "$REPO/partial" ]
    expected="clone --depth 1 --branch v2026.9.24"
    [ "$*" = "$expected https://github.com/NousResearch/hermes-agent $REPO" ]
    mkdir -p "$REPO/.git"
elif [ "$3" = rev-parse ]; then
    echo "$RESOLVED_SHA"
fi
""",
        "sleep": "#!/bin/sh\nexit 0\n",
        "uv": '#!/bin/sh\ntouch "$UV_CALLED"\nexit 29\n',
    }
    for name, content in scripts.items():
        path = shim / name
        path.write_text(content)
        path.chmod(0o755)
    repo = tmp_path / "hermes"
    calls = tmp_path / "calls"
    uv_called = tmp_path / "uv-called"
    env = {
        **os.environ,
        "PATH": f"{shim}:{os.environ['PATH']}",
        "REPO": str(repo),
        "CALLS": str(calls),
        "ATTEMPT": str(tmp_path / "attempt"),
        "UV_CALLED": str(uv_called),
        "RESOLVED_SHA": "wrong-commit" if moved_tag else HERMES_SHA,
    }
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/install_hermes.sh"), str(repo)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == (1 if moved_tag else 29)
    assert calls.read_text().count("clone --depth") == 2
    assert uv_called.exists() is not moved_tag
    assert not Path(f"{repo}.installing").exists()
    if moved_tag:
        assert "expected" in result.stderr


def test_hermes_workflow_pin_drift_refused(tmp_path):
    repo = tmp_path / "hermes"
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/install_hermes.sh"), str(repo)],
        env={**os.environ, "HERMES_SHA": "wrong-pin"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "pins disagree" in result.stderr
    assert not repo.exists()


@pytest.mark.parametrize("name", ["claude", "codex"])
@pytest.mark.parametrize("marker", ["legacy", "stale"])
def test_tampered_binary_reinstalled(tmp_path, name, marker):
    import tarfile

    from outerloop import harness_cli
    from outerloop.harness_pins import pins

    version = pins(name)["version"]
    payload = f"#!/bin/sh\necho '{version}'\n"
    binary = tmp_path / name
    binary.write_text(payload)
    asset = tmp_path / "asset"
    if name == "codex":
        with tarfile.open(asset, "w:gz") as archive:
            archive.add(binary, arcname="codex")
    else:
        asset.write_text(payload)
    checksum = hashlib.sha256(asset.read_bytes()).hexdigest()
    digest = hashlib.sha256(payload.encode()).hexdigest()
    source = (ROOT / f"scripts/install_{name}.sh").read_text()
    source = re.sub(
        rf'WANT_SHA256="\$\(pin {name} [a-z0-9-]+\)"',
        f'WANT_SHA256="{checksum}"',
        source,
    )
    script = tmp_path / "installer.sh"
    script.write_text(source)
    binary.write_text(payload + "# tampered\n")
    binary.chmod(0o755)
    sidecar = Path(f"{binary}.verified-sha256")
    sidecar.write_text(checksum if marker == "legacy" else f"{checksum} {digest}")
    desired = {"version": version, "sha256": checksum}
    assert not harness_cli.verified(name, binary, desired)
    shim = tmp_path / "shim"
    shim.mkdir()
    for command, body in {
        "curl": '#!/bin/sh\nwhile [ "$1" != -o ]; do shift; done\ncp "$ASSET" "$2"\n',
        "uname": '#!/bin/sh\ncase "$1" in -s) echo Linux;; -m) echo x86_64;; esac\n',
    }.items():
        executable = shim / command
        executable.write_text(body)
        executable.chmod(0o755)
    env = {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}", "ASSET": str(asset)}
    argv = ["bash", str(script), str(binary)]
    result = subprocess.run(argv, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert binary.read_text() == payload
    assert harness_cli.verified(name, binary, desired)
    if name == "codex":
        assert sidecar.read_text().strip() == f"{checksum} {digest}"
        assert checksum != digest
    (shim / "curl").write_text("#!/bin/sh\nexit 99\n")
    assert subprocess.run(argv, env=env).returncode == 0
