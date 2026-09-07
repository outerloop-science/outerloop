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
import re
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
    # a positive nice LOWERS priority (Slurm, like Unix): experiments yield to
    # the kernel's own evals and re-measures when a slot frees
    nice: int = 0
    output: str = "/dev/null"
    # Slurm scheduling controls
    dependency: str = ""  # e.g. "afterany:12345" or "singleton"
    begin: str = ""  # e.g. "now+30" or an absolute "YYYY-MM-DDTHH:MM:SS"
    # a job array: "0-15%4" runs tasks 0..15, at most 4 at a time; the queue
    # holds one entry and squeue names its tasks `<id>_<k>`
    array: str = ""
    extra: tuple[str, ...] = ()

    def to_argv(self) -> list[str]:
        if bool(self.command) == bool(self.script):
            raise ValueError("exactly one of command/script must be set")
        argv = [
            "sbatch",
            "--parsable",
            f"--job-name={self.job_name}",
            f"--time={self.time_minutes}",
            f"--cpus-per-task={self.cpus}",
            f"--mem={self.mem}",
            f"--output={self.output}",
        ]
        if self.account:  # unset bills the caller's default Slurm association
            argv.append(f"--account={self.account}")
        if self.partition:  # unset lets Slurm pick its default partition
            argv.append(f"--partition={self.partition}")
        if self.gpus:
            # per-NODE, not per-job (--gpus): every job here is single-node,
            # and Slurm submit plugins commonly classify a job by its
            # per-node GRES — the per-job form has been rejected on a GPU
            # partition as "CPU job setup is not valid"
            argv.append(f"--gpus-per-node={self.gpus}")
        if self.qos:
            argv.append(f"--qos={self.qos}")
        if self.nice:
            argv.append(f"--nice={self.nice}")
        if self.dependency:
            argv.append(f"--dependency={self.dependency}")
        if self.begin:
            argv.append(f"--begin={self.begin}")
        if self.array:
            argv.append(f"--array={self.array}")
        argv.extend(self.extra)
        if self.command:
            argv.append(f"--wrap={self.command}")
        else:
            argv.append(self.script)
            argv.extend(self.script_args)
        return argv


# `reason`, `gres` and `limit` feed the queue view: why a job waits, what it holds
# waits, whether it holds GPUs); the board reads the first six by key
QUEUE_FIELDS = (
    "id",
    "name",
    "state",
    "elapsed",
    "partition",
    "submitted",
    "reason",
    "gres",
    "limit",
)


class Compute(Protocol):
    """The verbs every compute backend implements. Callers (the measurer, the
    launcher, the wake dispatcher) depend on this, never on a backend."""

    def submit(self, spec: JobSpec) -> str: ...
    def status(self, job_id: str) -> str: ...
    def pending_reason(self, job_id: str) -> str: ...
    def job_partition(self, job_id: str) -> str: ...
    def active_job_names(self) -> list[str]: ...
    def queue_snapshot(self) -> list[dict[str, str]]: ...
    def lane_load(self, partition: str) -> dict[str, int]: ...
    def job_id_for_name(self, name: str) -> str: ...
    def cancel(self, job_id: str) -> bool: ...


def local_mode() -> bool:
    """OUTERLOOP_COMPUTE=local selects the monolith: every job a
    synchronous subprocess of the caller (docs/design/onboarding.md) — the
    zero-cluster on-ramp and the paper's serialized-baseline ablation. Any
    other value (or none) is Slurm. This helper is the only reader of the
    env var, so mode checks cannot drift."""
    return os.environ.get("OUTERLOOP_COMPUTE", "").strip().lower() == "local"


def compute_from_env() -> SlurmCompute | LocalCompute:
    """The deployment's compute backend, per `local_mode`."""
    return LocalCompute() if local_mode() else SlurmCompute()


_JOB_ID = re.compile(r"^\d+(_\d+)?$")  # a job, or one task of a job array (`<id>_<k>`)


def _check_job_id(job_id: str) -> None:
    if not _JOB_ID.match(job_id):
        raise ValueError(f"not a job id: {job_id!r}")


def array_indices(spec: str) -> list[int]:
    """The task indices of an array spec: "0-15%4" -> 0..15 (the %K throttle
    is the scheduler's concern); "" -> none."""
    body = spec.split("%", 1)[0].strip()
    if not body:
        return []
    lo, sep, hi = body.partition("-")
    if not sep:
        return [int(lo)] if lo.isdigit() else []
    if not (lo.isdigit() and hi.isdigit()):
        return []
    return list(range(int(lo), int(hi) + 1))


def combine_states(states: Sequence[str]) -> str:
    """One state for a job array from its tasks' states: running while any
    task runs, pending while any task waits, terminal only when every task
    is — COMPLETED if all are, else the first other terminal state (FAILED,
    TIMEOUT, CANCELLED...), so a sweep with one dead task reads as failed."""
    if len(states) == 1:
        return states[0]
    for want in ("RUNNING", "COMPLETING"):
        if any(s.startswith(want) for s in states):
            return want
    if any(is_pending(s) for s in states):
        return "PENDING"
    live = [s for s in states if not is_terminal(s)]
    if live:
        return live[0]
    bad = [s for s in states if not s.startswith("COMPLETED")]
    return bad[0] if bad else "COMPLETED"


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
        _check_job_id(job_id)
        try:
            result = self.runner(
                ["sacct", "-j", job_id, "--parsable2", "--noheader", "-X", "-o", "State"],
                self.command_timeout_s,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SlurmQueryError(f"sacct did not run: {exc}") from exc
        if result.returncode != 0:
            raise SlurmQueryError(f"sacct failed ({result.returncode}): {result.stderr.strip()}")
        # a job array answers one line per task; the array's state is theirs combined
        states = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
        return combine_states(states) if states else GONE

    def elapsed_seconds(self, job_id: str) -> int | None:
        """How long the job actually ran (sacct Elapsed), or None when sacct
        has no record. Raises SlurmQueryError when the query itself fails."""
        _check_job_id(job_id)
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
        _check_job_id(job_id)
        try:
            result = self.runner(["squeue", "-j", job_id, "-h", "-o", "%r"], self.command_timeout_s)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SlurmQueryError(f"squeue did not run: {exc}") from exc
        if result.returncode != 0:
            raise SlurmQueryError(f"squeue failed ({result.returncode}): {result.stderr.strip()}")
        return result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""

    def job_partition(self, job_id: str) -> str:
        """The partition(s) a queued job currently sits in, as squeue prints
        them, or "" when squeue no longer lists it. A site can MOVE a pending
        job off the partition it was submitted to (Torch does, under
        congestion); callers compare this with what they asked for."""
        _check_job_id(job_id)
        try:
            result = self.runner(["squeue", "-j", job_id, "-h", "-o", "%P"], self.command_timeout_s)
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

    def queue_snapshot(self) -> list[dict[str, str]]:
        """This user's PENDING and RUNNING jobs as rows of QUEUE_FIELDS — what
        a queue view needs, nothing a caller acts on. Raises SlurmQueryError
        on failure, like active_job_names."""
        try:
            result = self.runner(
                ["squeue", "--me", "--noheader", "-o", "%i|%j|%T|%M|%P|%V|%r|%b|%l"],
                self.command_timeout_s,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SlurmQueryError(f"squeue did not run: {exc}") from exc
        if result.returncode != 0:
            raise SlurmQueryError(f"squeue failed ({result.returncode}): {result.stderr.strip()}")
        rows: list[dict[str, str]] = []
        for line in result.stdout.splitlines():
            parts = line.strip().split("|")
            if len(parts) == len(QUEUE_FIELDS) and parts[0]:
                rows.append(dict(zip(QUEUE_FIELDS, parts, strict=True)))
        return rows

    def lane_load(self, partition: str) -> dict[str, int]:
        """Node counts by state on a lane (a partition or a comma-separated
        list), from sinfo: {"idle": 3, "mixed": 20, "allocated": 11}. Context
        for the queue view, nothing a caller acts on. Empty when no lane is
        named; raises SlurmQueryError on a failed query."""
        if not partition:
            return {}
        try:
            result = self.runner(
                ["sinfo", "--noheader", "-p", partition, "-o", "%T %D"], self.command_timeout_s
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SlurmQueryError(f"sinfo did not run: {exc}") from exc
        if result.returncode != 0:
            raise SlurmQueryError(f"sinfo failed ({result.returncode}): {result.stderr.strip()}")
        load: dict[str, int] = {}
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) != 2 or not parts[1].isdigit():
                continue
            state = parts[0].rstrip("*~#!%$@^-")  # sinfo's state flags (draining, no-respond...)
            load[state] = load.get(state, 0) + int(parts[1])
        return load

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

    def cancel(self, job_id: str) -> bool:
        """Cancel; idempotent (cancelling a finished job is not an error).
        False when scancel itself failed, so a caller that must know (the
        sweep's cancel-on-end) can try again; most callers are best-effort."""
        _check_job_id(job_id)
        result = self.runner(["scancel", job_id], self.command_timeout_s)
        if result.returncode != 0:
            log.warning("scancel %s: %s", job_id, result.stderr.strip())
            return False
        return True


# Local job ids start far above any real Slurm id so the two can never be
# confused in a record; they stay numeric because callers validate isdigit.
_LOCAL_JOB_BASE = 9_000_000_000


def _local_state_dir() -> Path | None:
    """Where local job states persist across processes (the tick and the
    attempts it spawns each hold their own LocalCompute): under the state
    root when the deployment names one, else nowhere (memory-only — tests).
    Local jobs are synchronous, so only TERMINAL states ever need sharing."""
    root = os.environ.get("OUTERLOOP_ROOT", "").strip()
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
        # unique across processes: the tick and its attempts each count from 1.
        # A million-wide slot per (pid mod 10k); exhausting it fails LOUD —
        # a silent wraparound would let one process read another's terminal
        # state under a reused id.
        if self._seq >= 1_000_000:
            raise SlurmError("local job id space exhausted for this process")
        job_id = str(_LOCAL_JOB_BASE + (os.getpid() % 10_000) * 1_000_000 + self._seq)

        # An explicit env allowlist:
        # the submitting process holds live keys (and any inherited
        # APPTAINERENV_* would cross --cleanenv into the container), so the
        # job script starts from a minimal environment and sets its own.
        # OUTERLOOP_* / REVIEW_HERMES_* pass through as a PREFIX rule:
        # Slurm jobs inherit the tick's whole environment, and local jobs
        # need the same config surface (compute mode, author backend, panel,
        # key-file PATHS). Enumerating allowed names is how a mode flag dies
        # silently (terra #222/#223) — but VALUE-bearing secret names under
        # the prefix (a *_PAT / *_TOKEN / *_KEY, as opposed to a *_KEY_FILE
        # path) must never reach a job that runs untrusted evaluation code.
        def _secret_name(name: str) -> bool:
            return name.endswith(("_PAT", "_TOKEN", "_SECRET", "_PASSWORD", "_KEY"))

        job_env = {
            k: v
            for k, v in os.environ.items()
            if k in ("PATH", "HOME", "LANG", "TMPDIR", "SLURM_TMPDIR", "USER", "LOGNAME")
            or (k.startswith(("OUTERLOOP_", "REVIEW_HERMES_")) and not _secret_name(k))
        }
        indices = array_indices(spec.array)
        if indices:
            # a job array runs its tasks in turn — there is no queue here to
            # throttle; each task keeps its own state and output under
            # `<id>_<k>`, and the array's own state is theirs combined
            states = [
                self._run_and_record(
                    spec, argv, {**job_env, "SLURM_ARRAY_TASK_ID": str(i)}, f"{job_id}_{i}"
                )
                for i in indices
            ]
            state = combine_states(states)
            self._record(spec, job_id, state, "")
        else:
            state = self._run_and_record(spec, argv, job_env, job_id)
        state_dir = _local_state_dir()
        where = (
            f"; output in {state_dir / (job_id + '.out')}"
            if state_dir and state != "COMPLETED"
            else ""
        )
        log.info("ran %s locally as job %s: %s%s", spec.job_name, job_id, state, where)
        return job_id

    def _run_and_record(
        self, spec: JobSpec, argv: list[str], job_env: dict[str, str], job_id: str
    ) -> str:
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
        self._record(spec, job_id, state, output)
        return state

    def _record(self, spec: JobSpec, job_id: str, state: str, output: str) -> None:
        state_dir = _local_state_dir()
        if state_dir is not None:
            try:
                state_dir.mkdir(parents=True, exist_ok=True)
                tmp = state_dir / f".{job_id}.{os.getpid()}.tmp"
                tmp.write_text(state)
                os.replace(tmp, state_dir / job_id)
                # the job's combined stdout/stderr beside its state: a failed local
                # job otherwise leaves nothing to read (#295)
                out_fd = os.open(
                    state_dir / f"{job_id}.out", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
                )
                with os.fdopen(out_fd, "w") as fh:
                    fh.write(output)
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
                with open(spec.output, "a" if "_" in job_id else "w") as fh:
                    fh.write(output)
            except OSError as exc:
                log.warning("local job %s: output write failed: %s", spec.job_name, exc)
        self._states[job_id] = state

    def status(self, job_id: str) -> str:
        _check_job_id(job_id)
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

    def job_partition(self, job_id: str) -> str:
        return ""  # no scheduler, no partitions

    def active_job_names(self) -> list[str]:
        return []  # synchronous: nothing is ever pending or running

    def queue_snapshot(self) -> list[dict[str, str]]:
        return []

    def lane_load(self, partition: str) -> dict[str, int]:
        return {}  # no lanes in the monolith

    def job_id_for_name(self, name: str) -> str:
        return ""

    def cancel(self, job_id: str) -> bool:
        _check_job_id(job_id)
        return True  # already terminal; cancelling a finished job is not an error


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


def gpus_in_gres(gres: str) -> int:
    """GPUs a queue row asks for, from squeue's %b (`gres/gpu:8`,
    `gres/gpu:h200:2`, `gres:gpu:4`; `N/A` or anything else = 0)."""
    total = 0
    for part in gres.replace(";", ",").split(","):
        fields = part.strip().split(":")
        if (len(fields) >= 2 and fields[0] in ("gres/gpu", "gpu")) or (
            len(fields) >= 3 and fields[0] == "gres" and fields[1] == "gpu"
        ):
            try:
                total += int(fields[-1])
            except ValueError:
                continue
    return total


def quote_command(parts: Sequence[str]) -> str:
    """Shell-quote a command for JobSpec.command (--wrap takes a string)."""
    return " ".join(shlex.quote(p) for p in parts)
