"""Slurm seam tests against a fake runner — no cluster involved."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from autoresearch.compute import (
    GONE,
    CommandResult,
    JobSpec,
    SlurmCompute,
    SlurmError,
    SlurmQueryError,
    is_pending,
    is_terminal,
    quote_command,
)

SPEC = JobSpec(
    job_name="test-job",
    account="acct",
    partition="cpu_short",
    time_minutes=10,
    command="echo hi",
)


@dataclass
class FakeRunner:
    results: list[CommandResult]
    seen: list[list[str]] = field(default_factory=list)

    def __call__(self, argv, timeout_s):
        self.seen.append(list(argv))
        return self.results.pop(0)


def test_submit_parses_job_id() -> None:
    runner = FakeRunner([CommandResult(0, "12345\n", "")])
    assert SlurmCompute(runner=runner).submit(SPEC) == "12345"
    argv = runner.seen[0]
    assert argv[0] == "sbatch"
    assert "--parsable" in argv
    assert "--wrap=echo hi" in argv


def test_submit_parses_cluster_suffixed_id() -> None:
    runner = FakeRunner([CommandResult(0, "12345;torch\n", "")])
    assert SlurmCompute(runner=runner).submit(SPEC) == "12345"


def test_submit_failure_raises_with_stderr() -> None:
    runner = FakeRunner([CommandResult(1, "", "Invalid qos specification")])
    with pytest.raises(SlurmError, match="Invalid qos"):
        SlurmCompute(runner=runner).submit(SPEC)


def test_submit_garbage_output_raises() -> None:
    runner = FakeRunner([CommandResult(0, "not-a-job-id", "")])
    with pytest.raises(SlurmError, match="no job id"):
        SlurmCompute(runner=runner).submit(SPEC)


def test_submit_after_sets_afterany() -> None:
    runner = FakeRunner([CommandResult(0, "777\n", "")])
    SlurmCompute(runner=runner).submit_after(SPEC, "12345")
    assert "--dependency=afterany:12345" in runner.seen[0]


def test_submit_after_rejects_non_numeric_id() -> None:
    with pytest.raises(ValueError):
        SlurmCompute(runner=FakeRunner([])).submit_after(SPEC, "$(rm -rf /)")


def test_status_distinguishes_gone_from_query_failure() -> None:
    """The fail-safe design's core distinction: empty-on-success vs error."""
    gone = SlurmCompute(runner=FakeRunner([CommandResult(0, "", "")]))
    assert gone.status("1") == GONE

    outage = SlurmCompute(runner=FakeRunner([CommandResult(1, "", "slurmdbd down")]))
    with pytest.raises(SlurmQueryError, match="slurmdbd down"):
        outage.status("1")


def test_status_returns_state_string() -> None:
    runner = FakeRunner([CommandResult(0, "RUNNING\n", "")])
    assert SlurmCompute(runner=runner).status("1") == "RUNNING"


def test_status_rejects_injection_shaped_ids() -> None:
    with pytest.raises(ValueError):
        SlurmCompute(runner=FakeRunner([])).status("1; rm -rf /")


def test_terminal_and_pending_predicates() -> None:
    assert is_terminal("COMPLETED")
    assert is_terminal("CANCELLED by 501")
    assert is_terminal("FAILED")
    assert is_terminal("TIMEOUT")
    assert not is_terminal("RUNNING")
    assert not is_terminal("PENDING")
    assert not is_terminal(GONE)  # the deadline floor decides, not this
    assert is_pending("PENDING")


def test_jobspec_requires_exactly_one_payload() -> None:
    with pytest.raises(ValueError):
        JobSpec(job_name="x", account="a", partition="p", time_minutes=1).to_argv()
    with pytest.raises(ValueError):
        JobSpec(
            job_name="x",
            account="a",
            partition="p",
            time_minutes=1,
            command="c",
            script="s.sh",
        ).to_argv()


def test_jobspec_script_form_appends_args() -> None:
    argv = JobSpec(
        job_name="x",
        account="a",
        partition="p",
        time_minutes=1,
        script="run.sh",
        script_args=("1", "2"),
    ).to_argv()
    assert argv[-3:] == ["run.sh", "1", "2"]


def test_quote_command_shell_safety() -> None:
    quoted = quote_command(["python", "-c", "print('hi; rm -rf /')"])
    assert "'" in quoted
    assert quoted.startswith("python -c ")
