"""The one launch command: `outerloop start`.

With `sbatch` on PATH it submits the resident tick
(docs/design/resident-tick.md) and returns; without it, or with
OUTERLOOP_COMPUTE=local, it runs the local loop in the foreground.
OUTERLOOP_TICK_HOST=login runs the foreground loop against Slurm.
Settings come from flags, then the process environment, then
~/.config/outerloop/.env, read once here at launch. The running chain
never takes identity or placement from that file (tick_deploy.sh reads an
allowlist of author knobs per tick), so editing it later cannot move a chain.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import stat
import subprocess
import sys
import webbrowser
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from outerloop import paths
from outerloop.endpoints import author_model_setting, endpoint_config_key
from outerloop.harness import HARNESS_INSTALL, default_binary
from outerloop.instance import job_name

if TYPE_CHECKING:
    from outerloop.init import AppPermissionGaps

RESIDENT_JOB_NAME = "outerloop-resident"
DEFAULT_RESIDENT_MINUTES = 360  # cpu_short's ceiling on Torch; the loop hands over to itself
DEFAULT_LOCAL_ROOT = Path.home() / ".outerloop"
ENV_FILE = paths.ENV_FILE

# What start itself decides from: mode, placement, root, cadence, walltime.
START_KEYS = (
    "OUTERLOOP_COMPUTE",
    "OUTERLOOP_TICK_HOST",
    "OUTERLOOP_ROOT",
    "OUTERLOOP_ACCOUNT",
    "OUTERLOOP_PARTITION",
    "OUTERLOOP_CADENCE_MIN",
    "OUTERLOOP_RESIDENT_MINUTES",
    "OUTERLOOP_PAT_FILE",
)
# The author knobs the chain's deploy step exports from .env every tick. The
# local loop has no deploy step, so start exports them once at launch; a test
# keeps this list identical to tick_deploy.sh's.
TICK_ENV_KEYS = (
    "OUTERLOOP_CLAUDE_VERSION",
    "OUTERLOOP_CODEX_VERSION",
    "OUTERLOOP_CLAUDE_SHA256",
    "OUTERLOOP_CODEX_SHA256",
    "OUTERLOOP_HERMES_REF",
    "OUTERLOOP_HERMES_SHA",
    "OUTERLOOP_CACHE_ROOT",
    "REVIEW_BACKEND",
    "OUTERLOOP_BRIDGE_RUNTIME",
    "OUTERLOOP_AUTHOR_ENDPOINT",
    "REVIEW_ENDPOINT",
    "REVIEW_MODEL",
    "OUTERLOOP_AUTHOR_BACKEND",
    "OUTERLOOP_AUTHOR_MODEL",
    "OUTERLOOP_AUTHOR_OVERRIDES",
    "OUTERLOOP_CLAUDE_MODEL",
    "OUTERLOOP_CLAUDE_BIN",
    "OUTERLOOP_CODEX_BIN",
    "OUTERLOOP_CODEX_KEY_FILE",
    "OUTERLOOP_HERMES_KEY_FILE",
    "OUTERLOOP_HERMES_RESUME_MAX_CHARS",
    "OUTERLOOP_CLAUDE_KEY_FILE",
    "OUTERLOOP_STEWARD_KEY_FILE",
    "OUTERLOOP_VERTEX_PROJECT",
    "OUTERLOOP_VERTEX_REGION",
    "OUTERLOOP_VERTEX_ADC",
    "OUTERLOOP_VERTEX_SMALL_MODEL",
    "OUTERLOOP_TARGET",
    "OUTERLOOP_GITHUB_APP_FILE",
    "OUTERLOOP_BOT_LOGIN",
    "OUTERLOOP_BOT_ALIASES",
    "OUTERLOOP_GPU_PARTITION",
    "OUTERLOOP_GPU_ACCOUNT",
    "OUTERLOOP_GPU_LANES",
    "OUTERLOOP_QOS",
    "OUTERLOOP_APPTAINER_BIN",
    "OUTERLOOP_IMAGE",
    "OUTERLOOP_PANEL",
    "OUTERLOOP_PANEL_KEY_FILE",
    "OUTERLOOP_PANEL_CODEX_KEY_FILE",
    "OUTERLOOP_PANEL_HERMES_KEY_FILE",
    "REVIEW_HERMES_REPO",
    "REVIEW_HERMES_PROVIDER",
)


class StartError(paths.ConfigError):
    """A start that cannot proceed; the message is the whole diagnosis."""


def operator_env_file(default: Path | None = None) -> Path:
    try:
        return paths.env_file(ENV_FILE if default is None else default)
    except paths.ConfigError as exc:
        raise StartError(str(exc)) from exc


def env_file_values(
    path: Path | None = None, keys: tuple[str, ...] | None = START_KEYS
) -> dict[str, str]:
    """`keys` from the operator's .env under the deploy step's trust rule: the
    file must be ours and not group/world-writable, or it is refused. Last
    assignment wins; surrounding quotes and a CR are stripped; a key set to
    an empty value is present (an off-switch), an absent key is absent.
    `keys=None` reads all assignments. No file: nothing."""
    path = operator_env_file() if path is None else path
    try:
        st = path.stat()
    except OSError:
        return {}
    if st.st_uid != os.getuid() or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise StartError(
            f"refusing to read {path}: it must be owned by you and not group/world-writable"
        )
    try:
        text = path.read_text()
    except OSError as e:
        raise StartError(f"cannot read {path}: {e}") from None
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.rstrip("\r").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key == "OUTERLOOP_ENV_FILE":
            continue
        if (
            keys is not None
            and key not in keys
            and not ("OUTERLOOP_AUTHOR_ENDPOINT" in keys and endpoint_config_key(key))
        ):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


@dataclass(frozen=True)
class StartPlan:
    mode: str  # "slurm" (resident) | "local" | "login"
    root: Path
    home: Path = Path(".")
    account: str = ""
    partition: str = ""
    cadence_min: str = ""
    resident_minutes: int = DEFAULT_RESIDENT_MINUTES
    pat_file: str = ""
    qos: str = ""

    def export_env(self) -> dict[str, str]:
        """The knobs the resident job needs. They ride the inherited environment
        (sbatch `--export=ALL`), NOT a comma-joined `--export=K=V,K=V` list — so a
        value may itself contain a comma (e.g. a multi-partition `a,b`, which Slurm
        reads as "whichever frees up first") without corrupting the export
        delimiter. start() merges these into the environment it hands sbatch."""
        env = {
            "OUTERLOOP_RESIDENT": "1",
            "OUTERLOOP_HOME": str(self.home),
            "OUTERLOOP_ROOT": str(self.root),
            "OUTERLOOP_RESIDENT_MINUTES": str(self.resident_minutes),  # successors reuse it
        }
        if self.qos:
            env["OUTERLOOP_QOS"] = self.qos
        if self.account:
            env["OUTERLOOP_ACCOUNT"] = self.account
        if self.partition:
            env["OUTERLOOP_PARTITION"] = self.partition
        if self.cadence_min:
            env["OUTERLOOP_CADENCE_MIN"] = self.cadence_min
        if self.pat_file:
            env["OUTERLOOP_PAT_FILE"] = self.pat_file
        return env

    def command(self) -> list[str]:
        if self.mode in ("local", "login"):
            return [sys.executable, "-m", "outerloop.tick", "--root", str(self.root), "--loop"]
        argv = [
            "sbatch",
            "--parsable",
            "--dependency=singleton",  # two starts can both submit; only one ever runs
            f"--time={self.resident_minutes}",
            f"--job-name={job_name()}",
        ]
        if self.account:  # unset bills the caller's default Slurm association
            argv.append(f"--account={self.account}")
        if self.partition:  # unset lets Slurm choose its default partition
            argv.append(f"--partition={self.partition}")
        if self.qos:
            argv.append(f"--qos={self.qos}")
        # --export=ALL carries export_env() from the inherited environment; a
        # comma-joined K=V list here would break on any value containing a comma.
        argv += ["--export=ALL", str(self.home / "scripts" / "tick_chain.sbatch")]
        return argv


def _setting(key: str, flag: str, environ: dict[str, str], from_file: dict[str, str]) -> str:
    """Flag, then the process environment, then .env."""
    if flag:
        return flag
    if key in environ:
        return environ[key]
    return from_file.get(key, "")


def _home(environ: dict[str, str], cwd: Path, *, local: bool, root: Path) -> Path:
    """The directory the loop runs from. A resident needs a source
    checkout (OUTERLOOP_HOME, else the current directory): the chain
    deploys from it and every job runs from a flight snapshot of its HEAD.
    A foreground loop has no deploy step and can run the installed package, so it
    uses a checkout when one is at hand and otherwise a `home` directory
    under the state root, where flights and logs land."""
    named = environ.get("OUTERLOOP_HOME")
    home = Path(named).expanduser() if named else cwd
    if (home / "scripts" / "tick_chain.sbatch").is_file():
        return home
    if local and not named:
        return root / "home"
    raise StartError(
        f"{home} is not a source checkout (no scripts/tick_chain.sbatch); "
        "run start from a clone of the kernel, or set OUTERLOOP_HOME to one"
    )


def plan_start(
    *,
    root: str,
    account: str,
    partition: str,
    local: bool,
    environ: dict[str, str],
    from_file: dict[str, str],
    sbatch_on_path: bool,
    cwd: Path,
    tick_host: str = "",
) -> StartPlan:
    compute = _setting("OUTERLOOP_COMPUTE", "local" if local else "", environ, from_file)
    mode = "local" if compute.strip().lower() == "local" or not sbatch_on_path else "slurm"
    host = _setting("OUTERLOOP_TICK_HOST", tick_host, environ, from_file).strip().lower()
    if host not in ("", "resident", "login"):
        raise StartError("OUTERLOOP_TICK_HOST must be resident or login")
    if host == "login" and compute.strip().lower() != "local":
        if not sbatch_on_path:
            raise StartError("login tick host needs sbatch on PATH")
        mode = "login"
    root_s = _setting("OUTERLOOP_ROOT", root, environ, from_file)
    cadence = _setting("OUTERLOOP_CADENCE_MIN", "", environ, from_file)
    if cadence:
        # the chain divides by it and the loop sleeps on it: a bad value would
        # only surface after the job started
        try:
            cadence_ok = float(cadence) > 0
        except ValueError:
            cadence_ok = False
        if not cadence_ok:
            raise StartError(
                f"OUTERLOOP_CADENCE_MIN must be a positive number of minutes, got {cadence!r}"
            )
    pat = _setting("OUTERLOOP_PAT_FILE", "", environ, from_file)
    # Foreground loops can run the installed package and need only a
    # directory (the launch lanes and GitHub servicing
    # switch off without OUTERLOOP_HOME, so one is always set)
    local_root = Path(root_s).expanduser() if root_s else default_local_root(environ)
    home = _home(environ, cwd, local=(mode in ("local", "login")), root=local_root)
    if mode == "local":
        return StartPlan(
            mode="local",
            root=local_root,
            home=home,
            cadence_min=cadence,
            pat_file=pat,
        )
    if mode == "login":
        root_s = str(local_root)
    if not root_s:
        raise StartError(
            "Slurm mode needs the state root on the shared filesystem: "
            "--root, OUTERLOOP_ROOT in the environment, or OUTERLOOP_ROOT= in "
            f"{operator_env_file()}"
        )
    acc = _setting("OUTERLOOP_ACCOUNT", account, environ, from_file)
    part = _setting("OUTERLOOP_PARTITION", partition, environ, from_file)
    qos = _setting("OUTERLOOP_QOS", "", environ, from_file)
    # Account and partition are both optional: left unset, Slurm bills the
    # caller's default association and places the job on its default partition.
    minutes_s = (
        _setting("OUTERLOOP_RESIDENT_MINUTES", "", environ, from_file) if mode == "slurm" else ""
    )
    try:
        minutes = int(minutes_s) if minutes_s else DEFAULT_RESIDENT_MINUTES
    except ValueError:
        raise StartError(
            f"OUTERLOOP_RESIDENT_MINUTES must be a whole number of minutes, got {minutes_s!r}"
        ) from None
    if minutes <= 0:
        raise StartError("OUTERLOOP_RESIDENT_MINUTES must be positive")
    # These ride the inherited environment (sbatch --export=ALL), so a comma is
    # safe now (a multi-partition `a,b` is valid) — only a newline would corrupt
    # the environment or the sbatch argv.
    for name, value in (
        ("root", root_s),
        ("account", acc),
        ("partition", part),
        ("QOS", qos),
        ("cadence", cadence),
        ("PAT file", pat),
        ("checkout path", str(home)),
    ):
        if "\n" in value or "\r" in value:
            raise StartError(f"{name} {value!r} cannot contain a newline")
    return StartPlan(
        mode=mode,
        qos=qos,
        root=Path(root_s).expanduser().resolve(),
        home=home,
        account=acc,
        partition=part,
        cadence_min=cadence,
        resident_minutes=minutes,
        pat_file=pat,
    )


def default_local_root(environ: Mapping[str, str]) -> Path:
    """The local-mode state root defaults to ~/.outerloop."""
    home = environ.get("HOME", "")
    return Path(home) / DEFAULT_LOCAL_ROOT.name if home else DEFAULT_LOCAL_ROOT


def _resident_jobs() -> list[str] | None:
    """Ids of queued or running resident ticks by name, lowest first;
    None when the scheduler could not be asked (a failed lookup must never
    read as 'none')."""
    try:
        proc = subprocess.run(
            [
                "squeue",
                "-u",
                os.environ.get("USER", ""),
                f"--name={job_name()}",
                "-h",
                "-o",
                "%i",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    ids = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return sorted(ids, key=lambda s: (len(s), s))


def _cancel(job: str) -> bool:
    try:
        proc = subprocess.run(
            ["scancel", job], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _exec(cmd: list[str], env: dict[str, str]) -> int:
    os.execvpe(cmd[0], cmd, env)
    return 1  # unreachable; keeps the signature honest for tests that stub this


# where the uv installer puts the binary before the shell's PATH knows it
UV_FALLBACK_DIRS = (".local/bin", ".cargo/bin")


def find_uv() -> tuple[str, str]:
    """(uv's path, the directory to prepend to PATH): the directory is "" when
    uv is already on PATH, both are "" when it is nowhere. Every evaluation and
    launch runs through `uv run` with the PATH start hands over, so a missing
    uv is caught here, not in a run that ends unmeasured."""
    found = shutil.which("uv")
    if found:
        return found, ""
    for rel in UV_FALLBACK_DIRS:
        candidate = Path.home() / rel / "uv"
        # a regular executable file, as `which` would accept: a directory of
        # that name is searchable, not runnable
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate), str(candidate.parent)
    return "", ""


HARNESS_BIN_KEYS = ("OUTERLOOP_CLAUDE_BIN", "OUTERLOOP_CODEX_BIN")


def _setting_of(key: str, values: Mapping[str, str], environ: Mapping[str, str]) -> str:
    """The process environment wins over .env, including an explicit empty value
    (the way to clear a recorded path from the shell)."""
    return (environ[key] if key in environ else values.get(key, "")).strip()


def missing_harness_binary(values: Mapping[str, str], environ: Mapping[str, str]) -> str:
    """Check all configured authors' host CLIs using the harness's lookup."""
    from outerloop.author_overrides import override_entries

    env = {**values, **environ}
    backend = _setting_of("OUTERLOOP_AUTHOR_BACKEND", values, environ).lower() or "claude"
    backends = dict.fromkeys([backend, *(value.backend for _, value in override_entries(env))])
    for selected in backends:
        problem = _missing_author_binary(selected, env)
        if problem:
            return problem
    return ""


def _missing_author_binary(backend: str, env: Mapping[str, str]) -> str:
    key = f"OUTERLOOP_{backend.upper()}_BIN"
    if backend == "hermes":
        from outerloop.hermes_install import hermes_ready

        repo = env.get("REVIEW_HERMES_REPO", "")
        return (
            ""
            if repo and hermes_ready(Path(repo).expanduser())
            else "hermes author needs REVIEW_HERMES_REPO with pinned source and runtime"
        )
    if key not in HARNESS_BIN_KEYS:
        return f"unsupported author backend {backend!r}; choose claude, codex or hermes."
    recorded = env.get(key, "")
    binary = default_binary(backend, env)
    if (not recorded and not os.path.isabs(binary)) or not (
        Path(binary).is_file() and os.access(binary, os.X_OK)
    ):
        looked_for = (
            f"{key}={recorded}" if recorded else f"`{backend}` on PATH or ~/.local/bin/{backend}"
        )
        return (
            f"{backend} author CLI: {looked_for} is not an executable file; "
            f"install it with `{HARNESS_INSTALL[backend]}`, then run "
            "`outerloop init --force` to re-record its path"
        )
    return ""


DEFAULT_PANEL = "verify,review"  # the tick's default when OUTERLOOP_PANEL is absent


def _configured(key: str, values: Mapping[str, str], environ: Mapping[str, str]) -> str | None:
    """`key`'s setting with presence kept: None when neither the process
    environment nor .env has it (a present empty value is an off-switch)."""
    if key in environ:
        return environ[key]
    return values.get(key)


def missing_panel_model(values: dict[str, str], environ: Mapping[str, str]) -> str:
    """Refuse a panel lens that names no model and does not run on the author's
    backend: it would run on whatever that backend's CLI defaults to, a model
    nobody chose. Empty when every lens has a model or inherits the author's."""
    backend = _setting_of("OUTERLOOP_AUTHOR_BACKEND", values, environ).lower() or "claude"
    panel = _configured("OUTERLOOP_PANEL", values, environ)
    panel = DEFAULT_PANEL if panel is None else panel.strip()
    if not panel:
        return ""
    from outerloop.panel import resolve_lenses

    try:
        env = {**values, **environ}
        model = author_model_setting(
            backend, _setting_of("OUTERLOOP_AUTHOR_MODEL", values, environ), env
        )
        resolve_lenses(panel, backend, model, environ=env)
    except ValueError as exc:
        return str(exc)
    return ""


def missing_claude_model(values: Mapping[str, str], environ: Mapping[str, str]) -> str:
    """Why this start would run a Claude role with no model ("" when it won't).
    OUTERLOOP_CLAUDE_MODEL is a required deployment setting, never a code
    default: it is needed by a claude author without its own OUTERLOOP_AUTHOR_MODEL,
    by a panel lens on the claude backend without an explicit model, and by the
    steward lane (always claude) once its key is provisioned. Reads the settings
    the way the tick will: the process environment wins over .env."""
    if _setting_of("OUTERLOOP_CLAUDE_MODEL", values, environ):
        return ""
    roles: list[str] = []
    backend = _setting_of("OUTERLOOP_AUTHOR_BACKEND", values, environ).lower() or "claude"
    if (
        backend == "claude"
        and not _setting_of("OUTERLOOP_AUTHOR_MODEL", values, environ)
        and not _setting_of("OUTERLOOP_AUTHOR_ENDPOINT", values, environ)
    ):
        roles.append("the claude author (no OUTERLOOP_AUTHOR_MODEL)")
    panel = _configured("OUTERLOOP_PANEL", values, environ)
    panel = DEFAULT_PANEL if panel is None else panel.strip()
    if panel:
        from outerloop.panel import resolve_lenses

        try:
            lenses = resolve_lenses(
                panel,
                backend,
                _setting_of("OUTERLOOP_AUTHOR_MODEL", values, environ),
                environ={**values, **environ},
            )
        except ValueError:
            lenses = ()  # missing_panel_model reports invalid panel configuration
        unnamed = [kind for kind, on, model in lenses if on == "claude" and not model]
        if unnamed:
            roles.append(
                f"the claude panel judge(s) {', '.join(unnamed)} (no model in OUTERLOOP_PANEL)"
            )
    if _setting_of("OUTERLOOP_STEWARD_KEY_FILE", values, environ):
        roles.append("the steward")
    if not roles:
        return ""
    return (
        "OUTERLOOP_CLAUDE_MODEL is not set, but this deployment runs Claude roles: "
        f"{'; '.join(roles)}. Add the line OUTERLOOP_CLAUDE_MODEL=<model> to {operator_env_file()} "
        "(or export it in the shell) and start again"
    )


APP_PERMISSION_KEYS = ("OUTERLOOP_GITHUB_APP_FILE", "OUTERLOOP_TARGET", "OUTERLOOP_PAT_FILE")


def _app_gaps_from_env(values: Mapping[str, str]) -> AppPermissionGaps | None:
    from outerloop.appauth import app_provider_from_file
    from outerloop.init import AppPermissionGaps, app_permission_gaps

    app_file = values.get("OUTERLOOP_GITHUB_APP_FILE", "").strip()
    if not app_file:
        return None
    try:
        return app_permission_gaps(
            app_provider_from_file(Path(app_file).expanduser()),
            values.get("OUTERLOOP_TARGET", "").strip(),
        )
    except Exception:
        return AppPermissionGaps((), "", "", "could not check the App permissions on GitHub.")


def permissions(args: argparse.Namespace) -> int:
    from outerloop.appmanifest import DEFAULT_PERMISSIONS

    try:
        values = {**env_file_values(operator_env_file(ENV_FILE), APP_PERMISSION_KEYS), **os.environ}
        gaps = _app_gaps_from_env(values)
        if gaps is None:
            if values.get("OUTERLOOP_PAT_FILE", "").strip():
                print("A PAT needs no App permissions.")
                return 0
            print(
                "App permissions need OUTERLOOP_GITHUB_APP_FILE; run outerloop init --github-app."
            )
            return 1
        labels = {name: f"{name}: {level}" for name, level in DEFAULT_PERMISSIONS.items()}
        width = max(map(len, labels.values()))
        if gaps.known:
            for name, label in labels.items():
                print(f"{label:<{width}}  {'missing' if name in gaps.missing else 'ok'}")
        if gaps.problem and not gaps.edit_url:
            print(gaps.problem)
            return 1
        if not gaps.problem:
            print(
                "All required App permissions are granted; restart the loop with outerloop start."
            )
            return 0
        if args.open:
            url = gaps.edit_url if gaps.configured_missing else gaps.accept_url
            if gaps.configured_missing:
                print(f"Edit the App permissions: {url}")
            else:
                print(f"Accept the installation permissions: {url}")
            print("After saving, run outerloop permissions --open again to check the next step.")
            if not webbrowser.open(url):
                print("Could not open a browser; follow the URL above.")
        else:
            print(gaps.problem)
            print("Next: outerloop permissions --open")
        return 1
    except Exception:
        print("could not check or open the App permissions; retry outerloop permissions.")
        return 1


def start(args: argparse.Namespace) -> int:
    try:
        values = env_file_values(
            operator_env_file(ENV_FILE), START_KEYS + TICK_ENV_KEYS
        )  # one read for everything
        from outerloop.author_overrides import validate_overrides

        try:
            validate_overrides(
                {**values, **os.environ}, _setting_of("OUTERLOOP_IMAGE", values, os.environ)
            )
        except ValueError as exc:
            raise StartError(str(exc)) from exc
        problem = "" if args.dry_run else missing_harness_binary(values, os.environ)
        try:
            author_model_setting(
                _setting_of("OUTERLOOP_AUTHOR_BACKEND", values, os.environ) or "claude",
                _setting_of("OUTERLOOP_AUTHOR_MODEL", values, os.environ),
                {**values, **os.environ},
            )
        except ValueError as exc:
            raise StartError(str(exc)) from exc
        # the model check holds for --dry-run too: it is configuration, not a host lookup
        problem = problem or missing_claude_model(values, os.environ)
        problem = problem or missing_panel_model(values, os.environ)
        if problem:
            raise StartError(problem)
        from_file = values
        plan = plan_start(
            root=args.root or "",
            account=args.account or "",
            partition=args.partition or "",
            local=args.local,
            tick_host=getattr(args, "tick_host", ""),
            environ=dict(os.environ),
            from_file=from_file,
            sbatch_on_path=shutil.which("sbatch") is not None,
            cwd=Path.cwd(),
        )
    except (StartError, ValueError, OSError) as e:
        print(f"outerloop start: {e}", file=sys.stderr)
        return 2
    try:
        cmd = plan.command()
    except (ValueError, OSError) as exc:
        print(f"outerloop start: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        print(shlex.join(cmd))
        return 0
    uv, uv_dir = find_uv()
    if not uv:
        print(
            "outerloop start: uv is not on PATH. Evaluations and launches run through "
            "`uv run`; install it (https://docs.astral.sh/uv/) or add its directory to "
            "PATH, then start again.",
            file=sys.stderr,
        )
        return 2
    path_env = {"PATH": uv_dir + os.pathsep + os.environ.get("PATH", "")} if uv_dir else {}
    if uv_dir:
        print(f"uv found at {uv}; {uv_dir} is added to the loop's PATH", file=sys.stderr)
    gaps = _app_gaps_from_env({**values, **os.environ})
    if gaps is not None and gaps.problem:
        print(gaps.problem, file=sys.stderr)
    # one loop per root: a foreground loop (local or login) holds the root's
    # tick lease, a resident chain holds the scheduler's singleton; every
    # start refuses over a held lease, and the two Slurm modes refuse over
    # each other (the resident submission re-checks the lease below)
    import socket
    import time

    from outerloop.runstate import tick_lease_holder
    from outerloop.tick import _loop_cadence_s

    ttl = 3 * _loop_cadence_s(float(plan.cadence_min or 0))
    held_by = tick_lease_holder(plan.root, time.time(), ttl, socket.gethostname())
    if held_by:
        print(
            f"outerloop start: a loop already holds this root's tick lease ({held_by}); "
            "stop it first",
            file=sys.stderr,
        )
        return 2
    if plan.mode == "login":
        existing = _resident_jobs()
        if existing is None or existing:
            reason = (
                f"resident job {existing[0]} is queued or running"
                if existing
                else "could not ask the scheduler (squeue failed)"
            )
            print(f"outerloop start: {reason}", file=sys.stderr)
            return 2
    if plan.mode in ("local", "login"):
        # the loop has no deploy step, so the author knobs the chain would
        # export from .env each tick are exported here once; the shell wins
        env = {**os.environ, **path_env}
        for key, value in values.items():
            if key in TICK_ENV_KEYS or endpoint_config_key(key):
                env.setdefault(key, value)
        env["OUTERLOOP_COMPUTE"] = "local" if plan.mode == "local" else "slurm"
        env.pop("OUTERLOOP_TICK_HOST", None)  # the plan decided; nothing inherited
        if plan.mode == "login":
            env.update(plan.export_env())
            env.pop("OUTERLOOP_RESIDENT", None)
            env["OUTERLOOP_TICK_HOST"] = "login"
            os.nice(10)  # Yield CPU to other users of the login node.
        env["OUTERLOOP_ROOT"] = str(plan.root)
        env["OUTERLOOP_HOME"] = str(plan.home)
        plan.home.mkdir(parents=True, exist_ok=True)  # <root>/home when there is no checkout
        if plan.cadence_min:
            env["OUTERLOOP_CADENCE_MIN"] = plan.cadence_min
        if plan.pat_file:
            env["OUTERLOOP_PAT_FILE"] = plan.pat_file
        print(
            f"{plan.mode} loop: state in {plan.root}; Ctrl-C stops it, the records resume it",
            file=sys.stderr,
        )
        return _exec(cmd, env)
    existing = _resident_jobs()
    if existing is None:
        print(
            "outerloop start: could not ask the scheduler whether a resident tick "
            "exists (squeue failed); nothing submitted. Retry, or check "
            f"`squeue --name {job_name()}`.",
            file=sys.stderr,
        )
        return 1
    if existing:
        print(
            f"a resident tick is already queued or running (job {existing[0]}); nothing "
            f"submitted. Stop it with `scancel {existing[0]}`, or pause it "
            f"with `touch {plan.root}/PAUSE`.",
            file=sys.stderr,
        )
        return 0
    # sbatch --export=ALL carries these to the resident job from the environment
    # we hand it here (so a comma in a value never breaks a --export delimiter).
    submit_env = {**os.environ, **path_env, **plan.export_env()}
    proc = subprocess.run(cmd, capture_output=True, text=True, env=submit_env, check=False)
    if proc.returncode != 0:
        print(
            f"outerloop start: sbatch failed: {(proc.stderr or proc.stdout).strip()}",
            file=sys.stderr,
        )
        return 1
    job = proc.stdout.strip().split(";")[0]
    # two starts can pass the check above together; singleton keeps them from
    # running at once, and the later submission withdraws so one chain remains
    after = _resident_jobs()
    if after and after[0] != job and job in after:
        if _cancel(job):
            print(
                f"another resident tick (job {after[0]}) was submitted at the same time; "
                f"withdrew this one (job {job}).",
                file=sys.stderr,
            )
            return 0
        # a queued loser would run after the winner and start a second chain
        print(
            f"another resident tick (job {after[0]}) was submitted at the same time and "
            f"this one (job {job}) could not be cancelled; cancel it by hand: scancel {job}",
            file=sys.stderr,
        )
        return 1
    # a foreground loop can take the root's lease between the check above and
    # the submission; the later party yields, here by withdrawing the job
    held_by = tick_lease_holder(plan.root, time.time(), ttl, socket.gethostname())
    if held_by:
        cancelled = _cancel(job)
        print(
            f"a loop took this root's tick lease ({held_by}) while the resident tick was "
            "submitted; "
            + (f"withdrew job {job}." if cancelled else f"cancel it by hand: scancel {job}"),
            file=sys.stderr,
        )
        return 0 if cancelled else 1
    print(
        f"resident tick submitted: job {job} on {plan.partition}, "
        f"{plan.resident_minutes} min walltime, hands over to itself. "
        f"Logs: {plan.root}/logs. Pause: touch {plan.root}/PAUSE. "
        f"Stop: scancel --name {job_name()}."
    )
    return 0


DIST = "outerloop-science"  # the PyPI distribution; imported as `outerloop`


def _installed_version(python: str) -> str:
    """The installed version of the distribution, read from a fresh interpreter
    so it reflects what pip just wrote rather than this process's imported copy."""
    proc = subprocess.run(
        [python, "-c", f"import importlib.metadata as m; print(m.version({DIST!r}))"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() or "unknown"


def upgrade(args: argparse.Namespace) -> int:
    """Upgrade the installed package in place, the local adopter's one verb.

    On Slurm the resident tick self-updates through OUTERLOOP_AUTO_UPDATE; a local
    install has no such loop, so this is the equivalent: `pip install --upgrade`,
    then you restart the loop to pick the new code up. Pip already picks the newest
    release and only falls back to a pre-release when that is all that is published;
    --pre forces pre-releases even once a stable exists."""
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", DIST]
    if args.pre:
        cmd.append("--pre")
    if args.dry_run:
        print(shlex.join(cmd))
        return 0
    before = _installed_version(sys.executable)
    proc = subprocess.run(cmd, check=False)
    if proc.returncode != 0:
        print(
            f"upgrade failed (pip exited {proc.returncode}). If this environment has no "
            f"pip, upgrade through its installer instead, e.g. `uv pip install --upgrade {DIST}`.",
            file=sys.stderr,
        )
        return proc.returncode
    # the permission check runs in a fresh process so it is the NEW code's
    # (this process is still the pre-upgrade one)
    check = subprocess.run([sys.executable, "-m", "outerloop", "permissions"], check=False)
    after = _installed_version(sys.executable)
    if before == after:
        print(f"already up to date: outerloop {after}.")
    else:
        print(
            f"upgraded outerloop {before} -> {after}. Restart the loop to pick it up: "
            f"stop the running tick, then `outerloop start`."
        )
    if check.returncode != 0:
        print("outerloop permissions --open")
    return 0


def main(argv: list[str] | None = None) -> int:
    from outerloop import __version__

    parser = argparse.ArgumentParser(
        prog="outerloop",
        description="autonomous research agents in an outer loop",
        epilog="Run `outerloop init` once, then `outerloop start`. Settings live in "
        "~/.config/outerloop/.env; the install guide is docs/install.md.",
    )
    parser.add_argument("--version", action="version", version=f"outerloop {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser(
        "start",
        help="start the loop: the resident tick on Slurm, the local loop elsewhere",
        description="Submit the resident tick to Slurm, or run the local loop in the "
        "foreground where there is no sbatch. Every flag defaults from "
        "~/.config/outerloop/.env, written by `outerloop init`.",
    )
    p.add_argument(
        "--root", help="state root (shared filesystem on Slurm; default ~/.outerloop locally)"
    )
    p.add_argument(
        "--account", help="Slurm account (optional; unset bills your default association)"
    )
    p.add_argument(
        "--partition",
        help="Slurm partition for the tick (optional; unset lets Slurm choose; a,b = list)",
    )
    p.add_argument(
        "--local", action="store_true", help="run the local loop even where sbatch exists"
    )
    p.add_argument(
        "--tick-host",
        choices=("resident", "login"),
        default="",
        help="run the Slurm tick in a resident job (default) or on this login host",
    )
    p.add_argument("--dry-run", action="store_true", help="print the command and exit")
    sub.add_parser(
        "tick", help="run one tick now; --loop keeps ticking (the local loop)", add_help=False
    )
    sub.add_parser(
        "init",
        help="guided setup: write ~/.config/outerloop/.env and the PAT file",
        add_help=False,
    )
    up = sub.add_parser(
        "upgrade",
        help="upgrade the installed package, then restart the loop to pick it up",
        description="Upgrade outerloop-science in this environment with pip. On Slurm the "
        "resident tick self-updates through OUTERLOOP_AUTO_UPDATE; this is the equivalent "
        "for a local install. Restart the loop afterwards to run the new code.",
    )
    up.add_argument(
        "--pre", action="store_true", help="include pre-releases even once a stable exists"
    )
    up.add_argument("--dry-run", action="store_true", help="print the command and exit")
    sub.add_parser("harness", help="inspect or upgrade harness installations", add_help=False)
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["harness"]:
        from outerloop.harness_cli import main as harness_main

        return harness_main(argv[1:])
    if argv[:1] == ["tick"]:
        # the tick entry owns its own parser; hand it the rest untouched
        from outerloop import tick

        sys.argv = ["outerloop tick", *argv[1:]]
        return tick.main()
    if argv[:1] == ["init"]:
        # init owns its own parser too; hand it the args after "init"
        from outerloop import init

        return init.main(argv[1:])
    p = sub.add_parser("limits", help="show live operator ceilings and fleet GPU usage")
    p.add_argument("--root", help="state root (defaults to OUTERLOOP_ROOT or ~/.outerloop)")
    p = sub.add_parser("permissions", help="check and update the App's required permissions")
    p.add_argument("--open", action="store_true", help="open the next permission settings page")
    p = sub.add_parser("migrate-ledger", help="seed research-log from a pinned main ledger")
    p.add_argument("--target", required=True, help="owner/repo")
    p.add_argument("--main-sha", required=True, help="full current default-branch commit SHA")
    p.add_argument("--force", action="store_true", help="replace an existing branch ledger")
    p.add_argument("--dry-run", action="store_true", help="print the table without writing")
    args = parser.parse_args(argv)
    if args.command == "migrate-ledger":
        from outerloop.ledger_migrate import migrate

        return migrate(args)
    if args.command == "limits":
        from outerloop.compute import compute_from_env
        from outerloop.operator_limits import CapacityError, report

        values = env_file_values(keys=None)
        root = Path(
            args.root
            or os.environ.get("OUTERLOOP_ROOT")
            or values.get("OUTERLOOP_ROOT")
            or DEFAULT_LOCAL_ROOT
        ).expanduser()
        try:
            print(report(root, compute_from_env()))
        except CapacityError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    if args.command == "permissions":
        return permissions(args)
    if args.command == "upgrade":
        return upgrade(args)
    return start(args)


if __name__ == "__main__":
    sys.exit(main())
