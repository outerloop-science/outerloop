"""Slurm behind one small interface: submit, status, cancel.

Everything the loop knows about the cluster goes through here, so a CI
runner or cloud backend is a new implementation of the same three verbs.
Commands run through an injectable runner (tests use a fake; nothing here
requires a cluster).

The status query preserves a distinction the fail-safe design depends on
(docs/design/architecture.md, "Wake delivery and fail-safety"): a FAILED
query ("Slurm unknown") is not the same as a successful query that finds
nothing ("job gone") — misreading an outage as a vanished job would
terminate healthy runs.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

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
    be passed as `script` instead (mutually exclusive)."""

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
            argv.append(f"--gpus={self.gpus}")
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

    def submit_after(self, spec: JobSpec, after_job_id: str) -> str:
        """Submit `spec` to run when `after_job_id` terminates — however it
        terminates (afterany: the wake-on-failure semantics the fail-safe
        design requires; verified live on Torch 2026-08-06)."""
        if not after_job_id.isdigit():
            raise ValueError(f"not a job id: {after_job_id!r}")
        dependent = JobSpec(
            **{
                **{f: getattr(spec, f) for f in spec.__dataclass_fields__},
                "dependency": f"afterany:{after_job_id}",
            }
        )
        return self.submit(dependent)

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

    def cancel(self, job_id: str) -> None:
        """Cancel; idempotent (cancelling a finished job is not an error)."""
        if not job_id.isdigit():
            raise ValueError(f"not a job id: {job_id!r}")
        result = self.runner(["scancel", job_id], self.command_timeout_s)
        if result.returncode != 0:
            log.warning("scancel %s: %s", job_id, result.stderr.strip())


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
