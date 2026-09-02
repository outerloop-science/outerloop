"""Compute behind one small interface: submit, status, cancel.

Everything the loop knows about compute goes through these verbs, so a
backend is one implementation: `SlurmCompute` submits real cluster jobs;
`LocalCompute` runs the same job specs as subprocesses in the current
allocation. A CI runner or a cloud/GPU-rental backend would be another
implementation of the same verbs — the callers never change.

The status query preserves a distinction the fail-safe design depends on
(docs/design/architecture.md, "Wake delivery and fail-safety"): a FAILED
query ("Slurm unknown") is not the same as a successful query that finds
nothing ("job gone") — misreading an outage as a vanished job would
terminate healthy runs.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shlex
import signal
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

# Terminal Slurm states (prefix-matched: sacct reports e.g. "CANCELLED by 123").
TERMINAL_STATES = (
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "TIMEOUT",
    "OUT_OF_MEMORY",
    "NODE_FAIL",
    "PREEMPTED",
    "BOOT_FAIL",
    "DEADLINE",
)
# A successful query that returns no record: the job left Slurm's memory.
GONE = "GONE"


class SlurmError(RuntimeError):
    """A Slurm command failed (submit/cancel), with its stderr."""


class SlurmQueryError(RuntimeError):
    """A status query failed — the answer is UNKNOWN, not 'job gone'.

    Callers must treat this as "defer and retry", never as a terminal state.
    """


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], int], CommandResult]


def _subprocess_runner(argv: Sequence[str], timeout_s: int) -> CommandResult:
    completed = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout_s)
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True)
class JobSpec:
    """One sbatch submission. `command` is run via --wrap; a script path can
    be passed as `script` instead (mutually exclusive).

    --wrap executes under a shell on the compute node: `command` must be
    built from trusted parts, with anything variable passed through
    `quote_command`. Never interpolate agent- or contract-supplied text."""

    job_name: str
    account: str
    partition: str
    time_minutes: int
    command: str = ""
    script: str = ""
    script_args: tuple[str, ...] = ()
    cpus: int = 1
    mem: str = "2G"
    gpus: int = 0
    qos: str = ""
    output: str = "/dev/null"
    # Slurm scheduling controls
    dependency: str = ""  # e.g. "afterany:12345" or "singleton"
    begin: str = ""  # e.g. "now+30" or an absolute "YYYY-MM-DDTHH:MM:SS"
    extra: tuple[str, ...] = ()

    def to_argv(self) -> list[str]:
        if bool(self.command) == bool(self.script):
            raise ValueError("exactly one of command/script must be set")
        argv = [
            "sbatch",
            "--parsable",
            f"--job-name={self.job_name}",
            f"--account={self.account}",
            f"--partition={self.partition}",
            f"--time={self.time_minutes}",
            f"--cpus-per-task={self.cpus}",
            f"--mem={self.mem}",
            f"--output={self.output}",
        ]
        if self.gpus:
            # per-NODE, not per-job (--gpus): every job here is single-node,
            # and Slurm submit plugins commonly classify a job by its
            # per-node GRES — the per-job form has been rejected on a GPU
            # partition as "CPU job setup is not valid"
            argv.append(f"--gpus-per-node={self.gpus}")
        if self.qos:
            argv.append(f"--qos={self.qos}")
        if self.dependency:
            argv.append(f"--dependency={self.dependency}")
        if self.begin:
            argv.append(f"--begin={self.begin}")
        argv.extend(self.extra)
        if self.command:
            argv.append(f"--wrap={self.command}")
        else:
            argv.append(self.script)
            argv.extend(self.script_args)
        return argv


class Compute(Protocol):
    """The verbs every compute backend implements. Callers (the measurer, the
    launcher, the wake dispatcher) depend on this, never on a backend."""

    def submit(self, spec: JobSpec) -> str: ...
    def status(self, job_id: str) -> str: ...
    def pending_reason(self, job_id: str) -> str: ...
    def active_job_names(self) -> list[str]: ...
    def job_id_for_name(self, name: str) -> str: ...
    def cancel(self, job_id: str) -> None: ...


def local_mode() -> bool:
    """AUTORESEARCH_COMPUTE=local selects the monolith: every job a
    synchronous subprocess of the caller (docs/design/onboarding.md) — the
    zero-cluster on-ramp and the paper's serialized-baseline ablation. Any
    other value (or none) is Slurm. This helper is the only reader of the
    env var, so mode checks cannot drift."""
    return os.environ.get("AUTORESEARCH_COMPUTE", "").strip().lower() == "local"


def compute_from_env() -> SlurmCompute | LocalCompute:
    """The deployment's compute backend, per `local_mode`."""
    return LocalCompute() if local_mode() else SlurmCompute()


@dataclass
class SlurmCompute:
    """The three verbs, plus afterany for wake jobs."""

    runner: Runner = field(default=_subprocess_runner)
    command_timeout_s: int = 60

    def submit(self, spec: JobSpec) -> str:
        """Submit; returns the job id. Raises SlurmError on failure."""
        result = self.runner(spec.to_argv(), self.command_timeout_s)
        if result.returncode != 0:
            raise SlurmError(f"sbatch failed ({result.returncode}): {result.stderr.strip()}")
        job_id = result.stdout.strip().split(";")[0]
        if not job_id.isdigit():
            raise SlurmError(f"sbatch returned no job id: {result.stdout.strip()!r}")
        log.info("submitted %s as job %s", spec.job_name, job_id)
        return job_id

    def status(self, job_id: str) -> str:
        """The job's Slurm state, or GONE when a *successful* query finds no
        record. Raises SlurmQueryError when the query itself fails."""
        if not job_id.isdigit():
            raise ValueError(f"not a job id: {job_id!r}")
        try:
            result = self.runner(
                ["sacct", "-j", job_id, "--parsable2", "--noheader", "-X", "-o", "State"],
                self.command_timeout_s,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SlurmQueryError(f"sacct did not run: {exc}") from exc
        if result.returncode != 0:
            raise SlurmQueryError(f"sacct failed ({result.returncode}): {result.stderr.strip()}")
        state = result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""
        return state if state else GONE

    def elapsed_seconds(self, job_id: str) -> int | None:
        """How long the job actually ran (sacct Elapsed), or None when sacct
        has no record. Raises SlurmQueryError when the query itself fails."""
        if not job_id.isdigit():
            raise ValueError(f"not a job id: {job_id!r}")
        try:
            result = self.runner(
                ["sacct", "-j", job_id, "--parsable2", "--noheader", "-X", "-o", "Elapsed"],
                self.command_timeout_s,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SlurmQueryError(f"sacct did not run: {exc}") from exc
        if result.returncode != 0:
            raise SlurmQueryError(f"sacct failed ({result.returncode}): {result.stderr.strip()}")
        text = result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""
        return parse_elapsed(text) if text else None

    def pending_reason(self, job_id: str) -> str:
        """Why a PENDING job is pending — Slurm's reason (`Dependency`,
        `DependencyNeverSatisfied`, `Priority`, ...), or "" when squeue no
        longer lists it. Raises SlurmQueryError when the query itself fails."""
        if not job_id.isdigit():
            raise ValueError(f"not a job id: {job_id!r}")
        try:
            result = self.runner(["squeue", "-j", job_id, "-h", "-o", "%r"], self.command_timeout_s)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SlurmQueryError(f"squeue did not run: {exc}") from exc
        if result.returncode != 0:
            raise SlurmQueryError(f"squeue failed ({result.returncode}): {result.stderr.strip()}")
        return result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""

    def active_job_names(self) -> list[str]:
        """The names of this user's PENDING and RUNNING jobs. Names, not
        commands: squeue's Command field is not guaranteed to carry --wrap
        strings, while %j is always the submitted name. Raises
        SlurmQueryError on failure — callers that delete things keyed on
        this must treat blindness as "delete nothing"."""
        try:
            result = self.runner(
                ["squeue", "--me", "--noheader", "-o", "%j"], self.command_timeout_s
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SlurmQueryError(f"squeue did not run: {exc}") from exc
        if result.returncode != 0:
            raise SlurmQueryError(f"squeue failed ({result.returncode}): {result.stderr.strip()}")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def job_id_for_name(self, name: str) -> str:
        """The id of this user's PENDING/RUNNING job with exactly `name`, or
        "" if none. Authoritative for "is this still live" independent of any
        local bookkeeping — a dispatched job is visible here even if the
        submitter died before recording its id. Raises SlurmQueryError on a
        failed query (the caller must not treat blindness as 'not running')."""
        try:
            result = self.runner(
                ["squeue", "--me", "--name", name, "--noheader", "-o", "%i"],
                self.command_timeout_s,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SlurmQueryError(f"squeue did not run: {exc}") from exc
        if result.returncode != 0:
            raise SlurmQueryError(f"squeue failed ({result.returncode}): {result.stderr.strip()}")
        ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return ids[0] if ids else ""

    def cancel(self, job_id: str) -> None:
        """Cancel; idempotent (cancelling a finished job is not an error)."""
        if not job_id.isdigit():
            raise ValueError(f"not a job id: {job_id!r}")
        result = self.runner(["scancel", job_id], self.command_timeout_s)
        if result.returncode != 0:
            log.warning("scancel %s: %s", job_id, result.stderr.strip())


# Local job ids start far above any real Slurm id so the two can never be
# confused in a record; they stay numeric because callers validate isdigit.
_LOCAL_JOB_BASE = 9_000_000_000


def _local_state_dir() -> Path | None:
    """Where local job states persist across processes (the tick and the
    attempts it spawns each hold their own LocalCompute): under the state
    root when the deployment names one, else nowhere (memory-only — tests).
    Local jobs are synchronous, so only TERMINAL states ever need sharing."""
    root = os.environ.get("AUTORESEARCH_ROOT", "").strip()
    return Path(root) / "local_jobs" if root else None


@dataclass
class LocalCompute:
    """The same verbs, run as subprocesses in THIS allocation — synchronously:
    `submit` returns with the job already terminal, so a caller that checks
    for the result after submitting finds it on disk and nothing ever parks.
    This is the degenerate backend for evals cheap enough to ride the current
    allocation, for deployments with no cluster at all, and for tests. It runs
    the identical job scripts the cluster runs (fresh checkout of the sealed
    sha, results to the job dir); only WHERE they run differs."""

    _states: dict[str, str] = field(default_factory=dict)
    _seq: int = 0
    minute_s: int = 60  # a walltime minute; tests shrink it to exercise the kill

    def submit(self, spec: JobSpec) -> str:
        if bool(spec.command) == bool(spec.script):
            # same contract SlurmCompute enforces via to_argv
            raise ValueError("exactly one of command/script must be set")
        argv = ["sh", spec.script, *spec.script_args] if spec.script else ["sh", "-c", spec.command]
        self._seq += 1
        # unique across processes: the tick and its attempts each count from 1
        job_id = str(_LOCAL_JOB_BASE + (os.getpid() % 100_000) * 10_000 + self._seq)
        # An explicit env allowlist:
        # the submitting process holds live keys (and any inherited
        # APPTAINERENV_* would cross --cleanenv into the container), so the
        # job script starts from a minimal environment and sets its own.
        # AUTORESEARCH_* / REVIEW_HERMES_* pass through as a PREFIX rule:
        # Slurm jobs inherit the tick's whole environment, and local jobs
        # need the same config surface (compute mode, author backend, panel,
        # key-file PATHS — never raw secrets, which are not env-borne under
        # this prefix). Enumerating names here is how a mode flag dies
        # silently (terra #222 and #223 both found exactly that).
        job_env = {
            k: v
            for k, v in os.environ.items()
            if k in ("PATH", "HOME", "LANG", "TMPDIR", "SLURM_TMPDIR", "USER", "LOGNAME")
            or k.startswith(("AUTORESEARCH_", "REVIEW_HERMES_"))
        }
        try:
            # the job runs in its OWN session (= process group), so the
            # walltime kill takes the whole tree — a job script waiting on
            # children must not leave them running past the walltime, exactly
            # as Slurm kills the job's group
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
                env=job_env,
            )
        except OSError as exc:
            raise SlurmError(f"local job {spec.job_name} failed to start: {exc}") from exc
        try:
            output, _ = proc.communicate(timeout=spec.time_minutes * self.minute_s)
            state = "COMPLETED" if proc.returncode == 0 else "FAILED"
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)  # pgid == pid (new session)
            try:
                # bounded drain: a child that re-setsid'd ESCAPED the group
                # kill and still holds the pipe — it must not hang the
                # submitter past the walltime. (A cgroup-less backend cannot
                # reach a double-setsid escapee; Slurm's cgroup containment
                # is the real jail — accepted local residual, logged.)
                output, _ = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                if proc.stdout is not None:
                    proc.stdout.close()
                output = ""
                log.warning(
                    "local job %s: an escaped child survived the walltime kill", spec.job_name
                )
            state = "TIMEOUT"
        state_dir = _local_state_dir()
        if state_dir is not None:
            try:
                state_dir.mkdir(parents=True, exist_ok=True)
                tmp = state_dir / f".{job_id}.{os.getpid()}.tmp"
                tmp.write_text(state)
                os.replace(tmp, state_dir / job_id)
                # opportunistic prune: one entry per job would leak forever
                # on a long-running loop; anything the sweep could still want
                # is far younger than a day
                cutoff = time.time() - 24 * 3600
                for old in state_dir.iterdir():
                    try:
                        if old.stat().st_mtime < cutoff:
                            old.unlink()
                    except OSError:
                        pass
            except OSError as exc:
                log.warning("local job %s: state persist failed: %s", spec.job_name, exc)
        if spec.output and spec.output != "/dev/null":
            try:
                with open(spec.output, "w") as fh:
                    fh.write(output)
            except OSError as exc:
                log.warning("local job %s: output write failed: %s", spec.job_name, exc)
        self._states[job_id] = state
        log.info("ran %s locally as job %s: %s", spec.job_name, job_id, state)
        return job_id

    def status(self, job_id: str) -> str:
        if not job_id.isdigit():
            raise ValueError(f"not a job id: {job_id!r}")
        state = self._states.get(job_id, "")
        if state:
            return state
        # another process's job (an attempt's launch, polled by the tick):
        # synchronous jobs are terminal, so the persisted state is the truth
        state_dir = _local_state_dir()
        if state_dir is not None:
            try:
                return (state_dir / job_id).read_text().strip() or GONE
            except OSError:
                pass
        return GONE

    def pending_reason(self, job_id: str) -> str:
        return ""  # synchronous jobs are terminal at submit — never pending

    def active_job_names(self) -> list[str]:
        return []  # synchronous: nothing is ever pending or running

    def job_id_for_name(self, name: str) -> str:
        return ""

    def cancel(self, job_id: str) -> None:
        if not job_id.isdigit():
            raise ValueError(f"not a job id: {job_id!r}")
        # already terminal; cancelling a finished job is not an error


def parse_elapsed(text: str) -> int | None:
    """Seconds in a sacct Elapsed field: `MM:SS`, `HH:MM:SS` or `D-HH:MM:SS`.
    None for anything else (an unknown field never becomes a refund)."""
    days = 0
    if "-" in text:
        day_part, _, text = text.partition("-")
        if not day_part.isdigit():
            return None
        days = int(day_part)
    parts = text.split(":")
    if not parts or not all(p.isdigit() for p in parts) or len(parts) > 3:
        return None
    nums = [int(p) for p in parts]
    while len(nums) < 3:
        nums.insert(0, 0)
    hours, minutes, seconds = nums
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def is_terminal(state: str) -> bool:
    """Whether a state string from `status` means the job is over.

    GONE is deliberately NOT terminal here: it means "no record", and the
    deadline-floor logic decides what that implies — not this predicate.
    """
    return any(state.startswith(t) for t in TERMINAL_STATES)


def is_pending(state: str) -> bool:
    return state.startswith("PENDING")


def quote_command(parts: Sequence[str]) -> str:
    """Shell-quote a command for JobSpec.command (--wrap takes a string)."""
    return " ".join(shlex.quote(p) for p in parts)
