"""Inspect and transactionally upgrade deployment harnesses."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

from outerloop import paths
from outerloop.cli import StartError, env_file_values
from outerloop.harness import default_binary
from outerloop.harness_pins import NAMES, OVERRIDE_KEYS, effective, pins


def path_key(name: str) -> str:
    return "REVIEW_HERMES_REPO" if name == "hermes" else f"OUTERLOOP_{name.upper()}_BIN"


def installed_path(name: str, env: Mapping[str, str]) -> Path:
    value = (
        env.get("REVIEW_HERMES_REPO") or str(Path.home() / "hermes-agent")
        if name == "hermes"
        else default_binary(name, env)
    )
    return Path(value).expanduser()


def probe(name: str, path: Path) -> tuple[str, bool]:
    """Installed identity and completeness; never provision during inspection."""
    sha = ""
    try:
        argv = (
            ["git", "-C", str(path), "rev-parse", "HEAD"]
            if name == "hermes"
            else [str(path), "--version"]
        )
        result = subprocess.run(argv, capture_output=True, text=True, timeout=15, check=True)
        if name != "hermes":
            match = re.search(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", result.stdout)
            return (match[0], True) if match else ("unknown", False)
        sha = result.stdout.strip()
        runtime = Path(f"{path.resolve()}.runtime") / sha
        complete = (
            (runtime / ".complete").read_text().strip() == sha
            and os.access(runtime / "venv/bin/python", os.X_OK)
            and (path / "run_agent.py").is_file()
        )
        dirty = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout.strip()
        return sha, complete and not dirty
    except TimeoutError:
        raise
    except (OSError, subprocess.SubprocessError):
        # Preserve the source identity when only its runtime is missing.
        if sha:
            return sha, False
        return "missing", False


def identity(name: str, desired: Mapping[str, str]) -> str:
    return desired["sha" if name == "hermes" else "version"]


CONFIG_KEYS = (
    *OVERRIDE_KEYS,
    "OUTERLOOP_CLAUDE_BIN",
    "OUTERLOOP_CODEX_BIN",
    "REVIEW_HERMES_REPO",
    "OUTERLOOP_AUTHOR_BACKEND",
    "OUTERLOOP_AUTHOR_OVERRIDES",
    "OUTERLOOP_STEWARD_KEY_FILE",
    "REVIEW_BACKEND",
    "OUTERLOOP_PANEL",
    "OUTERLOOP_CACHE_ROOT",
    "OUTERLOOP_ROOT",
)


def verified(name: str, path: Path, desired: Mapping[str, str]) -> bool:
    if name == "hermes":
        path = Path(f"{path.resolve()}.runtime") / desired["sha"] / "venv/bin/python"
    try:
        with path.open("rb") as file:
            actual = hashlib.file_digest(file, "sha256").hexdigest()
        if name == "hermes":
            return Path(f"{path}.verified-sha256").read_text().strip() == actual
        if name == "claude":
            return actual in [v for k, v in desired.items() if k != "version"]
        return (
            Path(f"{path}.verified-sha256").read_text().strip() == f"{desired['sha256']} {actual}"
        )
    except TimeoutError:
        raise
    except (OSError, UnicodeError):
        return False


def installer_environment(env: Mapping[str, str], root: Path) -> dict[str, str]:
    allowed = (*OVERRIDE_KEYS, "HOME", "PATH", "TMPDIR", "OUTERLOOP_CACHE_ROOT", "npm_config_cache")
    result = {k: env[k] for k in allowed if k in env}
    result["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', os.defpath)}"
    result.setdefault("OUTERLOOP_CACHE_ROOT", str(root / "cache"))
    result.setdefault("npm_config_cache", str(Path(result["OUTERLOOP_CACHE_ROOT"]) / "npm"))
    return result


def run_installer(script: Path, target: Path, env: dict[str, str]) -> None:
    with subprocess.Popen(
        ["bash", str(script), str(target)], env=env, start_new_session=True
    ) as process:
        try:
            code = process.wait()
            if code:
                raise subprocess.CalledProcessError(code, process.args)
        finally:
            # Also reap descendants when the shell exits before its children.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)


def status(env: Mapping[str, str]) -> int:
    failed = False
    for name in NAMES:
        pinned = pins(name)
        try:
            desired = effective(name, env)
        except ValueError as exc:
            print(f"{name}: REFUSED: {exc}")
            failed = True
            continue
        path = installed_path(name, env)
        have, ready = probe(name, path)
        override = desired != pinned
        drift = have != identity(name, desired) or not ready or not verified(name, path, desired)
        label = f"{pinned['ref']} ({pinned['sha']})" if name == "hermes" else pinned["version"]
        runtime = (
            f" runtime .complete={'valid' if ready else 'missing/invalid'}"
            if name == "hermes"
            else ""
        )
        trial = f" override={identity(name, desired)}" if override else ""
        print(
            f"{name}: pinned={label} installed={have} path={path}{runtime}{trial}"
            f" {'DRIFT' if drift else 'OK'}"
        )
    return int(failed)


def used_harnesses(env: Mapping[str, str]) -> list[str]:
    author = env.get("OUTERLOOP_AUTHOR_BACKEND") or "claude"
    from outerloop.author_overrides import overrides

    used = {author, *(selected.backend for selected in overrides(env).values())}
    if env.get("OUTERLOOP_STEWARD_KEY_FILE"):
        used.add("claude")
    if env.get("REVIEW_BACKEND"):
        used.add(env["REVIEW_BACKEND"])
    panel = env.get("OUTERLOOP_PANEL", "verify,review").strip()
    if panel:
        from outerloop.panel import parse_lenses

        used.update(backend for _, backend, _ in parse_lenses(panel, author))
    return [name for name in NAMES if name in used]


def record_path(env_file: Path, key: str, target: Path) -> None:
    """Keep legacy assignments and comments; atomic replacement is the commit."""
    env_file_values(env_file, keys=None)
    value = str(target)
    if any(c in value for c in '\n\r"'):
        raise ValueError("harness path cannot contain quotes or newlines")
    old = env_file.read_text() if env_file.exists() else ""
    lines = [line for line in old.splitlines() if line.partition("=")[0].strip() != key]
    lines.append(f'{key}="{value}"')
    write_atomic(env_file, "\n".join(lines) + "\n")


def write_atomic(path: Path, content: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def upgrade_one(name: str, env: dict[str, str], env_file: Path, root: Path) -> None:
    desired = effective(name, env)
    timeout = float(env.get("OUTERLOOP_HARNESS_TIMEOUT_SECONDS", "300"))
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("OUTERLOOP_HARNESS_TIMEOUT_SECONDS must be positive and finite")
    key = hashlib.sha256(json.dumps(desired, sort_keys=True).encode()).hexdigest()
    state = (
        Path(env.get("OUTERLOOP_CACHE_ROOT", str(root / "cache")))
        / "harness-failures"
        / f"{name}-{key}.json"
    )
    attempts = 0
    try:
        saved = json.loads(state.read_text())
        if (
            not isinstance(saved, dict)
            or type(saved.get("attempts")) is not int
            or saved["attempts"] < 0
            or type(saved.get("retry_after")) not in (int, float)
            or not math.isfinite(saved["retry_after"])
        ):
            raise ValueError("invalid retry state")
    except FileNotFoundError:
        saved = None
    except (OSError, ValueError, UnicodeError, OverflowError) as exc:
        print(f"{name}: ignoring unreadable/invalid retry state {state}: {exc}", file=sys.stderr)
        saved = None
    if saved is not None:
        attempts = saved["attempts"]
        if time.time() < saved["retry_after"]:
            raise ValueError(f"retry backoff active until {saved['retry_after']}")
    candidate = None

    def expired(signum: int, frame: object) -> None:
        raise TimeoutError(f"upgrade timed out after {timeout:g} seconds")

    handler = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        previous = installed_path(name, env)
        have, ready = probe(name, previous)
        if ready and have == identity(name, desired) and verified(name, previous, desired):
            print(f"{name}: unchanged {have} at {previous}")
            return
        parent = root / "harnesses" / name / identity(name, desired)
        parent.mkdir(parents=True, exist_ok=True)
        candidate = Path(tempfile.mkdtemp(prefix="install-", dir=parent))
        target = candidate / name
        script = Path(__file__).resolve().parents[2] / "scripts" / f"install_{name}.sh"
        if not script.is_file():
            script = Path(__file__).with_name("_installers") / script.name
        run_installer(script, target, installer_environment(env, root))
        got, complete = probe(name, target)
        if not complete or got != identity(name, desired) or not verified(name, target, desired):
            raise ValueError(
                f"verification failed: installed {got}, wanted {identity(name, desired)}"
                " with verified hash"
            )
        # Verification finishes within the deadline; publishing the path is atomic.
        signal.setitimer(signal.ITIMER_REAL, 0)
        state.unlink(missing_ok=True)
        record_path(env_file, path_key(name), target)
        candidate = None
        env[path_key(name)] = str(target)
        print(f"{name}: {have} at {previous} -> {got} at {target}")
    except (OSError, ValueError, subprocess.SubprocessError):
        signal.setitimer(signal.ITIMER_REAL, 0)
        state.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(
            state,
            json.dumps(
                {
                    "attempts": attempts + 1,
                    "retry_after": time.time() + min(300 * 2 ** min(attempts, 9), 86400),
                }
            ),
        )
        raise
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, handler)
        if candidate is not None:
            shutil.rmtree(candidate)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="outerloop harness")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="report pins, overrides, installed paths and drift")
    up = sub.add_parser("upgrade", help="verify new installations before switching paths")
    up.add_argument("names", nargs="*", metavar="NAME")
    up.add_argument("--root", help="state root (flag, environment, then .env)")
    up.add_argument("--used", action="store_true", help="only configured deployment backends")
    args = parser.parse_args(argv)
    if args.command == "upgrade" and any(name not in NAMES for name in args.names):
        parser.error("harness names must be claude, codex or hermes")
    try:
        env_file = paths.env_file(paths.ENV_FILE)
        env = {**env_file_values(env_file, keys=CONFIG_KEYS), **os.environ}
        if args.command == "status":
            return status(env)
        env_file.parent.mkdir(parents=True, exist_ok=True)
        with (env_file.parent / ".harness-upgrade.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            env = {**env_file_values(env_file, keys=CONFIG_KEYS), **os.environ}
            names = args.names or (used_harnesses(env) if args.used else list(NAMES))
            root = (
                Path(args.root or env.get("OUTERLOOP_ROOT") or Path.home() / ".outerloop")
                .expanduser()
                .resolve()
            )
            failed = False
            for name in dict.fromkeys(names):
                try:
                    upgrade_one(name, env, env_file, root)
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    print(
                        f"{name}: upgrade failed; previous installation retained: {exc}",
                        file=sys.stderr,
                    )
                    failed = True
            return int(failed)
    except (OSError, ValueError, StartError) as exc:
        print(f"harness: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
