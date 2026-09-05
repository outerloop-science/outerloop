"""The one launch command: `outerloop start`.

With `sbatch` on PATH it submits the resident tick
(docs/design/resident-tick.md) and returns; without it, or with
AUTORESEARCH_COMPUTE=local, it runs the local loop in the foreground.
Settings come from flags, then the process environment, then
~/.config/autoresearch/.env, read once here at launch. The running chain
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
from dataclasses import dataclass
from pathlib import Path

RESIDENT_JOB_NAME = "autoresearch-resident"
DEFAULT_RESIDENT_MINUTES = 360  # cpu_short's ceiling on Torch; the loop hands over to itself
DEFAULT_LOCAL_ROOT = Path.home() / ".autoresearch"
ENV_FILE = Path.home() / ".config" / "autoresearch" / ".env"

# What start itself decides from: mode, placement, root, cadence, walltime.
START_KEYS = (
    "AUTORESEARCH_COMPUTE",
    "AUTORESEARCH_ROOT",
    "AUTORESEARCH_ACCOUNT",
    "AUTORESEARCH_PARTITION",
    "AUTORESEARCH_CADENCE_MIN",
    "AUTORESEARCH_RESIDENT_MINUTES",
    "AUTORESEARCH_PAT_FILE",
)
# The author knobs the chain's deploy step exports from .env every tick. The
# local loop has no deploy step, so start exports them once at launch; a test
# keeps this list identical to tick_deploy.sh's.
TICK_ENV_KEYS = (
    "AUTORESEARCH_AUTHOR_BACKEND",
    "AUTORESEARCH_AUTHOR_MODEL",
    "AUTORESEARCH_CODEX_BIN",
    "AUTORESEARCH_CODEX_KEY_FILE",
    "AUTORESEARCH_HARNESS_KEY_FILE",
    "AUTORESEARCH_VERTEX_PROJECT",
    "AUTORESEARCH_VERTEX_REGION",
    "AUTORESEARCH_VERTEX_ADC",
    "AUTORESEARCH_TARGET",
    "AUTORESEARCH_GITHUB_APP_FILE",
    "AUTORESEARCH_BOT_LOGIN",
    "AUTORESEARCH_BOT_ALIASES",
    "AUTORESEARCH_GPU_PARTITION",
    "AUTORESEARCH_GPU_ACCOUNT",
    "AUTORESEARCH_PANEL",
    "AUTORESEARCH_PANEL_KEY_FILE",
    "AUTORESEARCH_PANEL_CODEX_KEY_FILE",
    "AUTORESEARCH_PANEL_HERMES_KEY_FILE",
    "REVIEW_HERMES_REPO",
    "REVIEW_HERMES_PROVIDER",
)


class StartError(Exception):
    """A start that cannot proceed; the message is the whole diagnosis."""


def env_file_values(path: Path = ENV_FILE, keys: tuple[str, ...] = START_KEYS) -> dict[str, str]:
    """`keys` from the operator's .env under the deploy step's trust rule: the
    file must be ours and not group/world-writable, or it is refused. Last
    assignment wins; surrounding quotes and a CR are stripped; a key set to
    an empty value is present (an off-switch), an absent key is absent.
    No file: nothing."""
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
        if key not in keys:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


@dataclass(frozen=True)
class StartPlan:
    mode: str  # "slurm" | "local"
    root: Path
    home: Path = Path(".")
    account: str = ""
    partition: str = ""
    cadence_min: str = ""
    resident_minutes: int = DEFAULT_RESIDENT_MINUTES
    pat_file: str = ""

    def export_env(self) -> dict[str, str]:
        """The knobs the resident job needs. They ride the inherited environment
        (sbatch `--export=ALL`), NOT a comma-joined `--export=K=V,K=V` list — so a
        value may itself contain a comma (e.g. a multi-partition `a,b`, which Slurm
        reads as "whichever frees up first") without corrupting the export
        delimiter. start() merges these into the environment it hands sbatch."""
        env = {
            "AUTORESEARCH_RESIDENT": "1",
            "AUTORESEARCH_HOME": str(self.home),
            "AUTORESEARCH_ROOT": str(self.root),
            "AUTORESEARCH_ACCOUNT": self.account,
            "AUTORESEARCH_RESIDENT_MINUTES": str(self.resident_minutes),  # successors reuse it
        }
        if self.partition:
            env["AUTORESEARCH_PARTITION"] = self.partition
        if self.cadence_min:
            env["AUTORESEARCH_CADENCE_MIN"] = self.cadence_min
        if self.pat_file:
            env["AUTORESEARCH_PAT_FILE"] = self.pat_file
        return env

    def command(self) -> list[str]:
        if self.mode == "local":
            return [sys.executable, "-m", "outerloop.tick", "--root", str(self.root), "--loop"]
        argv = [
            "sbatch",
            "--parsable",
            "--dependency=singleton",  # two starts can both submit; only one ever runs
            f"--time={self.resident_minutes}",
            f"--job-name={RESIDENT_JOB_NAME}",
            f"--account={self.account}",
        ]
        if self.partition:  # unset lets Slurm choose its default partition
            argv.append(f"--partition={self.partition}")
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


def _home(environ: dict[str, str], cwd: Path) -> Path:
    """The checkout the loop runs from: AUTORESEARCH_HOME, else the current
    directory when it is one. Both modes need it; the tick's launch lanes
    and GitHub servicing switch off without it."""
    home = (
        Path(environ["AUTORESEARCH_HOME"]).expanduser() if environ.get("AUTORESEARCH_HOME") else cwd
    )
    if not (home / "scripts" / "tick_chain.sbatch").is_file():
        raise StartError(
            f"{home} is not an autoresearch checkout (no scripts/tick_chain.sbatch); "
            "run start from the checkout the chain should deploy from, or set AUTORESEARCH_HOME"
        )
    return home


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
) -> StartPlan:
    compute = _setting("AUTORESEARCH_COMPUTE", "local" if local else "", environ, from_file)
    mode = "local" if compute.strip().lower() == "local" or not sbatch_on_path else "slurm"
    root_s = _setting("AUTORESEARCH_ROOT", root, environ, from_file)
    cadence = _setting("AUTORESEARCH_CADENCE_MIN", "", environ, from_file)
    if cadence:
        # the chain divides by it and the loop sleeps on it: a bad value would
        # only surface after the job started
        try:
            cadence_ok = float(cadence) > 0
        except ValueError:
            cadence_ok = False
        if not cadence_ok:
            raise StartError(
                f"AUTORESEARCH_CADENCE_MIN must be a positive number of minutes, got {cadence!r}"
            )
    pat = _setting("AUTORESEARCH_PAT_FILE", "", environ, from_file)
    # both modes run from a checkout: the tick's launch lanes and GitHub
    # servicing switch off without AUTORESEARCH_HOME
    home = _home(environ, cwd)
    if mode == "local":
        return StartPlan(
            mode="local",
            root=Path(root_s).expanduser() if root_s else DEFAULT_LOCAL_ROOT,
            home=home,
            cadence_min=cadence,
            pat_file=pat,
        )
    if not root_s:
        raise StartError(
            "Slurm mode needs the state root on the shared filesystem: "
            "--root, AUTORESEARCH_ROOT, or AUTORESEARCH_ROOT= in ~/.config/autoresearch/.env"
        )
    acc = _setting("AUTORESEARCH_ACCOUNT", account, environ, from_file)
    part = _setting("AUTORESEARCH_PARTITION", partition, environ, from_file)
    # Partition is optional: left unset, Slurm places the job on its default
    # partition. Account stays required (clusters bill by it).
    if not acc:
        raise StartError("Slurm mode needs --account / AUTORESEARCH_ACCOUNT")
    minutes_s = _setting("AUTORESEARCH_RESIDENT_MINUTES", "", environ, from_file)
    try:
        minutes = int(minutes_s) if minutes_s else DEFAULT_RESIDENT_MINUTES
    except ValueError:
        raise StartError(
            f"AUTORESEARCH_RESIDENT_MINUTES must be a whole number of minutes, got {minutes_s!r}"
        ) from None
    if minutes <= 0:
        raise StartError("AUTORESEARCH_RESIDENT_MINUTES must be positive")
    # These ride the inherited environment (sbatch --export=ALL), so a comma is
    # safe now (a multi-partition `a,b` is valid) — only a newline would corrupt
    # the environment or the sbatch argv.
    for name, value in (
        ("root", root_s),
        ("account", acc),
        ("partition", part),
        ("cadence", cadence),
        ("PAT file", pat),
        ("checkout path", str(home)),
    ):
        if "\n" in value or "\r" in value:
            raise StartError(f"{name} {value!r} cannot contain a newline")
    return StartPlan(
        mode="slurm",
        root=Path(root_s).expanduser(),
        home=home,
        account=acc,
        partition=part,
        cadence_min=cadence,
        resident_minutes=minutes,
        pat_file=pat,
    )


def _resident_jobs() -> list[str] | None:
    """Ids of queued or running resident ticks, lowest first; None when the
    scheduler could not be asked (a failed lookup must never read as 'none')."""
    try:
        proc = subprocess.run(
            [
                "squeue",
                "-u",
                os.environ.get("USER", ""),
                f"--name={RESIDENT_JOB_NAME}",
                "-h",
                "-o",
                "%i",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    ids = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return sorted(ids, key=lambda s: (len(s), s))


def _cancel(job: str) -> bool:
    try:
        proc = subprocess.run(["scancel", job], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _exec(cmd: list[str], env: dict[str, str]) -> int:
    os.execvpe(cmd[0], cmd, env)
    return 1  # unreachable; keeps the signature honest for tests that stub this


def start(args: argparse.Namespace) -> int:
    try:
        values = env_file_values(ENV_FILE, START_KEYS + TICK_ENV_KEYS)  # one read for everything
        from_file = {k: v for k, v in values.items() if k in START_KEYS}
        plan = plan_start(
            root=args.root or "",
            account=args.account or "",
            partition=args.partition or "",
            local=args.local,
            environ=dict(os.environ),
            from_file=from_file,
            sbatch_on_path=shutil.which("sbatch") is not None,
            cwd=Path.cwd(),
        )
    except StartError as e:
        print(f"outerloop start: {e}", file=sys.stderr)
        return 2
    cmd = plan.command()
    if args.dry_run:
        print(shlex.join(cmd))
        return 0
    if plan.mode == "local":
        # the loop has no deploy step, so the author knobs the chain would
        # export from .env each tick are exported here once; the shell wins
        env = dict(os.environ)
        for key, value in values.items():
            if key in TICK_ENV_KEYS:
                env.setdefault(key, value)
        env["AUTORESEARCH_COMPUTE"] = "local"
        env["AUTORESEARCH_ROOT"] = str(plan.root)
        env["AUTORESEARCH_HOME"] = str(plan.home)
        if plan.cadence_min:
            env["AUTORESEARCH_CADENCE_MIN"] = plan.cadence_min
        if plan.pat_file:
            env["AUTORESEARCH_PAT_FILE"] = plan.pat_file
        print(
            f"local loop: state in {plan.root}; Ctrl-C stops it, the records resume it",
            file=sys.stderr,
        )
        return _exec(cmd, env)
    existing = _resident_jobs()
    if existing is None:
        print(
            "outerloop start: could not ask the scheduler whether a resident tick "
            "exists (squeue failed); nothing submitted. Retry, or check "
            f"`squeue --name {RESIDENT_JOB_NAME}`.",
            file=sys.stderr,
        )
        return 1
    if existing:
        print(
            f"a resident tick is already queued or running (job {existing[0]}); nothing "
            f"submitted. Stop it with `scancel --name {RESIDENT_JOB_NAME}`, or pause it "
            f"with `touch {plan.root}/PAUSE`.",
            file=sys.stderr,
        )
        return 0
    # sbatch --export=ALL carries these to the resident job from the environment
    # we hand it here (so a comma in a value never breaks a --export delimiter).
    submit_env = {**os.environ, **plan.export_env()}
    proc = subprocess.run(cmd, capture_output=True, text=True, env=submit_env)
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
    print(
        f"resident tick submitted: job {job} on {plan.partition}, "
        f"{plan.resident_minutes} min walltime, hands over to itself. "
        f"Logs: {plan.root}/logs. Pause: touch {plan.root}/PAUSE. "
        f"Stop: scancel --name {RESIDENT_JOB_NAME}."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="outerloop", description="autonomous research agents in an outer loop"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser(
        "start", help="start the loop: the resident tick on Slurm, the local loop elsewhere"
    )
    p.add_argument(
        "--root", help="state root (shared filesystem on Slurm; default ~/.autoresearch locally)"
    )
    p.add_argument("--account", help="Slurm account")
    p.add_argument("--partition", help="Slurm partition for the tick")
    p.add_argument(
        "--local", action="store_true", help="run the local loop even where sbatch exists"
    )
    p.add_argument("--dry-run", action="store_true", help="print the command and exit")
    sub.add_parser("tick", help="one tick, or --loop; the chain's own entry", add_help=False)
    sub.add_parser(
        "init",
        help="guided setup: write ~/.config/autoresearch/.env and the PAT file",
        add_help=False,
    )
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["tick"]:
        # the tick entry owns its own parser; hand it the rest untouched
        from outerloop import tick

        sys.argv = ["outerloop tick", *argv[1:]]
        return tick.main()
    if argv[:1] == ["init"]:
        # init owns its own parser too; hand it the args after "init"
        from outerloop import init

        return init.main(argv[1:])
    return start(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
