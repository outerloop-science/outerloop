"""The author syscall protocol: request parsing, budgets, wake rendering,
and the launch-job artifact copy-out."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from outerloop.compute import parse_elapsed
from outerloop.container import ApptainerRuntime
from outerloop.dispatch import write_eval_job
from outerloop.syscall import (
    MAX_EVAL_MINUTES,
    MAX_LAUNCH_ARRAY,
    MAX_OUTPUT_CHARS,
    MAX_REPORT_CHARS,
    MAX_REQUEST_BYTES,
    MAX_VERDICT_BYTES,
    MAX_WHY_CHARS,
    SYSCALL_DIR,
    SYSCALL_FILE,
    Launch,
    LaunchResult,
    SyscallError,
    SyscallRequest,
    VerdictError,
    annotate_launch_states,
    array_spec,
    budget_error,
    clamp_concurrency,
    ensure_excluded,
    evals_gpu_hours,
    gather_results,
    gpu_hours_cost,
    install_tool,
    launch_hours_refund,
    launch_jobs,
    launch_task_ids,
    launches_gpu_hours,
    mark_done,
    mark_synced,
    marker_requested,
    read_request,
    read_results,
    read_verdict,
    refresh_tool,
    shipped_channel,
    sync_requested,
    tool_update_note,
    write_budget,
    write_channel_json,
    write_siblings,
)
from outerloop.syscall_cli import cmd_siblings, cmd_sync
from outerloop.syscall_cli import main as cli_main


def write_req(tmp_path: Path, payload, *, typed: bool = True) -> Path:
    """Write a syscall ABI. `typed` (default) prepends `type: "sleep"` so
    callers write only the launch payload; pass typed=False to write the
    payload as-is (another syscall type, or a raw file)."""
    d = tmp_path / SYSCALL_DIR
    d.mkdir(exist_ok=True)
    f = d / SYSCALL_FILE
    if isinstance(payload, dict) and typed:
        payload = {"type": "sleep", **payload}
    f.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return f


def write_verdict(tmp_path: Path, payload: dict) -> Path:
    """Write a verdict ABI (the judge's committed `conclude` syscall)."""
    d = tmp_path / SYSCALL_DIR
    d.mkdir(exist_ok=True)
    f = d / SYSCALL_FILE
    f.write_text(json.dumps({"type": "verdict", **payload}))
    return f


def _job(
    run_dir: Path,
    name: str,
    *,
    code: str | None = "0",
    stdout: str | None = None,
    stderr: str | None = None,
    log: str = "",
    artifacts: bool = True,
) -> Path:
    """One job's dir under `run_dir`, as the job wrapper leaves it: exit-code,
    output tails and the copy-out log. `code=None` is a job that died before
    the wrapper ran — no exit-code file, no artifacts dir."""
    ev = run_dir / f"eval-launch-{name}"
    ev.mkdir(parents=True, exist_ok=True)
    if code is not None:
        (ev / "exit-code").write_text(code)
    if code is not None and artifacts:
        (ev / "artifacts").mkdir()
    if stdout is not None:
        (ev / "stdout").write_text(stdout)
    if stderr is not None:
        (ev / "stderr").write_text(stderr)
    if log:
        (ev / "artifacts.log").write_text(log)
    return ev


def test_read_request_success_paths(tmp_path: Path) -> None:
    """No file is no syscall; an empty launch list is a checkpoint sleep; a
    well-formed request is parsed and consumed exactly once."""
    assert read_request(tmp_path) is None
    write_req(tmp_path, {"launches": []})
    req = read_request(tmp_path)
    assert req is not None and req.launches == ()
    f = write_req(
        tmp_path,
        {
            "launches": [
                {
                    "name": "train-lr3",
                    "command": "uv run python train.py --lr 3e-4",
                    "minutes": 90,
                    "artifacts": ["results/curve.json"],
                }
            ],
            "messages": [
                {
                    "to": "self",
                    "text": "if the curve flattens, try the schedule next",
                    "reply_to": None,
                }
            ],
        },
    )
    req = read_request(tmp_path)
    assert req is not None
    assert req.launches[0].name == "train-lr3"
    assert req.launches[0].minutes == 90
    assert req.launches[0].artifacts == ("results/curve.json",)
    assert "schedule" in req.messages[0]["text"]
    assert not f.exists()  # consumed: honored (or refused) exactly once


def test_a_refused_request_is_still_consumed(tmp_path: Path) -> None:
    # A bad request can never re-park a later run. Malformed JSON and an
    # agent-controlled giant file are both refused and consumed.
    f = write_req(tmp_path, "{not json")
    with pytest.raises(SyscallError, match="not valid JSON"):
        read_request(tmp_path)
    assert not f.exists()
    # A giant request is refused by SIZE before any parse work.
    f = write_req(tmp_path, "x" * (MAX_REQUEST_BYTES + 1))
    with pytest.raises(SyscallError, match="exceeds"):
        read_request(tmp_path)
    assert not f.exists()


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"launches": [{"name": "UPPER", "command": "x"}]}, "name"),
        ({"launches": [{"name": "a", "command": ""}]}, "non-empty"),
        ({"launches": [{"name": "a", "command": "x", "minutes": 0}]}, "positive"),
        ({"launches": [{"name": "a", "command": "x", "minutes": True}]}, "positive"),
        ({"launches": [{"name": "a", "command": "x"}, {"name": "a", "command": "y"}]}, "duplicate"),
        (
            {"launches": [{"name": "a", "command": "x", "artifacts": ["../etc/pw"]}]},
            "repo-relative",
        ),
        ({"launches": [{"name": "a", "command": "x", "artifacts": ["/abs"]}]}, "repo-relative"),
        ({"launches": [{"name": "a", "command": "x", "extra": 1}]}, "unknown keys"),
        ({"surprise": 1}, "unknown syscall keys"),
    ],
)
def test_invalid_requests_are_refused_loudly(tmp_path: Path, payload, match) -> None:
    write_req(tmp_path, payload)
    with pytest.raises(SyscallError, match=match):
        read_request(tmp_path)


def test_read_request_submit_and_report_fields(tmp_path: Path) -> None:
    write_req(tmp_path, {"launches": [], "submit": True, "report": "H: the change helps"})
    req = read_request(tmp_path)
    assert req is not None and req.submit and req.report == "H: the change helps"
    write_req(tmp_path, {"launches": [], "submit": "yes"})
    with pytest.raises(SyscallError, match="submit must be a boolean"):
        read_request(tmp_path)
    # A submit without a report is accepted; whitespace trims to empty.
    write_req(tmp_path, {"launches": [], "submit": True})
    req = read_request(tmp_path)
    assert req is not None and req.submit and req.report == ""
    write_req(tmp_path, {"launches": [], "submit": True, "report": "   "})
    req = read_request(tmp_path)
    assert req is not None and req.report == ""
    write_req(tmp_path, {"launches": [], "submit": True, "report": "x" * (MAX_REPORT_CHARS + 1)})
    with pytest.raises(SyscallError, match="report must be"):
        read_request(tmp_path)
    write_req(tmp_path, {"launches": [], "submit": True, "report": "  H: it helps  "})
    req = read_request(tmp_path)
    assert req is not None and req.report == "H: it helps"
    # a sleep without a submit needs none
    write_req(tmp_path, {"launches": [{"name": "a", "command": "x"}]})
    req = read_request(tmp_path)
    assert req is not None and req.report == ""


def test_read_request_rejects_a_wrong_or_missing_type(tmp_path: Path) -> None:
    # a sleep is one syscall TYPE; a file with no type (a target-committed
    # booby-trap) or another type (a verdict) is not a sleep and is refused.
    write_req(tmp_path, {"launches": []}, typed=False)
    with pytest.raises(SyscallError, match="expected a sleep, message or stop syscall"):
        read_request(tmp_path)
    write_req(tmp_path, {"type": "verdict", "findings": []}, typed=False)
    with pytest.raises(SyscallError, match="expected a sleep, message or stop syscall"):
        read_request(tmp_path)


def test_minutes_are_clamped_to_the_ceiling(tmp_path: Path) -> None:
    write_req(tmp_path, {"launches": [{"name": "big", "command": "x", "minutes": 100000}]})
    req = read_request(tmp_path)
    assert req is not None and req.launches[0].minutes == 240


def test_siblings_command_reads_the_fleet_snapshot(tmp_path: Path) -> None:
    """`siblings` is author-PULLED: the kernel writes the snapshot at session
    start and the command renders it; absent or malformed means none known."""
    assert cli_main(["siblings"], root=tmp_path) == 0  # missing file: no crash
    write_siblings(
        tmp_path,
        [
            {
                "agent": "agent-02",
                "state": "parked",
                "phase": "candidate",
                "direction": "compose embedding decay with earlier warmdown",
            },
            {"agent": "agent-03", "state": "running", "phase": "", "direction": ""},
        ],
    )
    out = cmd_siblings(tmp_path, None)
    assert "agent-02" in out and "parked/candidate" in out and "compose embedding decay" in out
    assert "agent-03" in out and "running" in out
    assert "prefer a direction" not in out  # direction advice lives in the guidance pack
    (tmp_path / ".outerloop" / "siblings.json").write_text("not json")
    assert cmd_siblings(tmp_path, None) == "no sibling activity known."


def test_budget_error_rules() -> None:
    """Launch and sleep counts bound a request; a submit needs no prior
    launch, and the GPU rule applies only where the brief discloses it."""
    req = SyscallRequest(launches=(_launch("a"), _launch("b")))
    ok = budget_error(req, launches_used=0, launch_budget=4, sleeps_used=0, sleep_budget=2)
    assert ok == ""
    over = budget_error(req, launches_used=3, launch_budget=4, sleeps_used=0, sleep_budget=2)
    assert "launch budget" in over
    spent = budget_error(req, launches_used=0, launch_budget=4, sleeps_used=2, sleep_budget=2)
    assert "sleep budget exhausted" in spent

    def check(request, launches_used: int, launch_budget: int = 4) -> str:
        return budget_error(
            request,
            launches_used=launches_used,
            launch_budget=launch_budget,
            sleeps_used=0,
            sleep_budget=8,
            gpus=8,
            gpu_hour_budget=400.0,
        )

    bare = SyscallRequest(launches=(), submit=True)
    refused = check(bare, launches_used=0)
    assert refused == ""
    # Sibling launches share the same budgets.
    with_launch = SyscallRequest(launches=(_launch("a"),), submit=True)
    assert check(with_launch, launches_used=0) == ""
    # one completed launch from a prior park: submit freely, as often as
    # sleeps allow (revise-and-resubmit is unaffected)
    assert check(bare, launches_used=1) == ""
    # launches disabled (depth_k 0): the rule cannot apply
    assert check(bare, launches_used=0, launch_budget=0) == ""
    # GPU benchmark with a ZERO gpu-hour budget is not metered: the brief's
    # metered paragraph (which discloses the rule) is absent there, so the
    # rule must not silently apply
    assert (
        budget_error(
            bare,
            launches_used=0,
            launch_budget=4,
            sleeps_used=0,
            sleep_budget=8,
            gpus=8,
            gpu_hour_budget=0.0,
        )
        == ""
    )
    # CPU benchmark (in-job gate costs seconds): submit-to-measure stays legal
    assert budget_error(bare, launches_used=0, launch_budget=4, sleeps_used=0, sleep_budget=8) == ""


def _launch(name: str):
    return Launch(name=name, command="run", minutes=5)


def _lr(name: str, exit_code, state: str = "") -> LaunchResult:
    return LaunchResult(
        name=name,
        exit_code=exit_code,
        stdout_tail="",
        stderr_tail="",
        delivered=(),
        skipped=(),
        slurm_state=state,
    )


def test_annotate_launch_states_fills_only_exitless_jobs() -> None:
    results = (_lr("a", 0), _lr("b", None), _lr("c", 137))
    states = {"j-a": "COMPLETED", "j-b": "OUT_OF_MEMORY", "j-c": "FAILED"}
    out = annotate_launch_states(results, ["j-a", "j-b", "j-c"], lambda j: states[j])
    assert out[0].slurm_state == ""  # had an exit code: untouched, unqueried
    assert out[1].slurm_state == "OUT_OF_MEMORY"  # the exitless one is annotated
    assert out[2].slurm_state == ""  # had an exit code


def test_annotate_launch_states_survives_a_failing_query() -> None:
    def boom(_job_id: str) -> str:
        raise RuntimeError("sacct down")

    out = annotate_launch_states((_lr("a", None),), ["j-a"], boom)
    assert out[0].slurm_state == ""  # best-effort: blank, not a crash


def test_annotate_launch_states_bails_on_a_length_mismatch() -> None:
    results = (_lr("a", None), _lr("b", None))
    out = annotate_launch_states(results, ["only-one-id"], lambda j: "OUT_OF_MEMORY")
    assert out == results  # no positional mapping -> nothing guessed


def test_annotate_launch_states_skips_a_gone_record() -> None:
    # GONE = the scheduler forgot the job; it is not a failure state to show
    out = annotate_launch_states((_lr("a", None),), ["j-a"], lambda j: "GONE")
    assert out[0].slurm_state == ""


def test_annotate_launch_states_stops_at_the_time_budget() -> None:
    # a stalled sacct must not burn the wake: once the budget is spent, the
    # remaining exit-less jobs keep the blank fallback and are never queried.
    queried: list[str] = []

    def status_of(job_id: str) -> str:
        queried.append(job_id)
        return "OUT_OF_MEMORY"

    # clock: start=0, then every check is already past the 30s budget
    ticks = iter([0.0, 100.0, 100.0, 100.0])
    out = annotate_launch_states(
        (_lr("a", None), _lr("b", None)),
        ["j-a", "j-b"],
        status_of,
        time_budget_s=30.0,
        clock=lambda: next(ticks),
    )
    assert queried == []  # budget already spent at the first check -> no queries
    assert all(r.slurm_state == "" for r in out)


def test_ensure_excluded_is_idempotent_and_hides_the_dir_from_git(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / SYSCALL_DIR).mkdir()
    (tmp_path / SYSCALL_DIR / "results.txt").write_text("data")
    ensure_excluded(tmp_path)
    ensure_excluded(tmp_path)  # idempotent
    exclude = (tmp_path / ".git" / "info" / "exclude").read_text()
    assert exclude.count(f"/{SYSCALL_DIR}/") == 1
    # `git add -A` must NOT stage the syscall dir (drift/scope protection)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    staged = subprocess.run(
        ["git", "-C", str(tmp_path), "diff", "--cached", "--name-only"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert SYSCALL_DIR not in staged


# --- the judge's verdict syscall (read_verdict), the kernel-side reader -------


def _finding(**over) -> dict:
    f = {
        "file": "x",
        "line": 1,
        "confidence": "high",
        "summary": "s",
        "detail": "d",
        "blocking": True,
        "kind": "note",
    }
    f.update(over)
    return f


def test_read_verdict_success_paths(tmp_path: Path) -> None:
    assert read_verdict(tmp_path) is None  # judge never concluded
    write_verdict(tmp_path, {"findings": [], "notes": "materially sound"})
    assert read_verdict(tmp_path) == {"findings": [], "notes": "materially sound"}
    write_verdict(
        tmp_path,
        {
            "findings": [_finding(file="src/solver.py", line=42, summary="off-by-one")],
            "notes": "one real defect",
        },
    )
    verdict = read_verdict(tmp_path)
    assert verdict is not None
    assert verdict["notes"] == "one real defect"
    assert verdict["findings"][0]["file"] == "src/solver.py"
    assert verdict["findings"][0]["blocking"] is True


def test_read_verdict_rejects_a_wrong_or_missing_type(tmp_path: Path) -> None:
    write_req(tmp_path, {"findings": [], "notes": ""}, typed=False)  # no type
    with pytest.raises(VerdictError, match="expected a verdict syscall"):
        read_verdict(tmp_path)


def _one_finding(tmp_path: Path) -> dict:
    v = read_verdict(tmp_path)
    assert v is not None
    return v["findings"][0]


def test_verifier_category_is_carried_and_typos_clamp(tmp_path: Path) -> None:
    write_verdict(tmp_path, {"findings": [_finding(category="ruler-fishing")], "notes": ""})
    assert _one_finding(tmp_path)["category"] == "ruler-fishing"
    write_verdict(tmp_path, {"findings": [_finding(category="made-up-thing")], "notes": ""})
    assert _one_finding(tmp_path)["category"] == "other"


def test_read_verdict_rejects_a_falsy_non_string_category(tmp_path: Path) -> None:
    # a falsy non-string category (0, []) must raise, not be silently dropped
    # by a truthiness guard.
    bad_values: list[object] = [0, []]
    for bad in bad_values:
        write_verdict(tmp_path, {"findings": [_finding(category=bad)], "notes": ""})
        with pytest.raises(VerdictError, match="category must be a string"):
            read_verdict(tmp_path)


def test_read_verdict_rejects_non_positive_line(tmp_path: Path) -> None:
    write_verdict(tmp_path, {"findings": [_finding(line=-3)], "notes": ""})
    with pytest.raises(VerdictError, match="positive"):
        read_verdict(tmp_path)


def test_read_verdict_rejects_malformed_enums_loudly(tmp_path: Path) -> None:
    write_verdict(tmp_path, {"findings": [_finding(confidence="SURE")], "notes": ""})
    with pytest.raises(VerdictError, match="confidence"):
        read_verdict(tmp_path)


def test_read_verdict_rejects_a_finding_missing_blocking(tmp_path: Path) -> None:
    # fail-open guard: a finding that omits blocking must be REJECTED, never
    # defaulted to non-gating ("silence is never endorsement").
    bad = _finding()
    del bad["blocking"]
    write_verdict(tmp_path, {"findings": [bad], "notes": ""})
    with pytest.raises(VerdictError, match="missing required keys"):
        read_verdict(tmp_path)


def test_read_verdict_handles_unhashable_enum_values(tmp_path: Path) -> None:
    # a list where a string enum belongs must raise VerdictError, not crash the
    # reader with TypeError from `in frozenset`.
    for key in ("confidence", "kind"):
        write_verdict(tmp_path, {"findings": [_finding(**{key: []})], "notes": ""})
        with pytest.raises(VerdictError, match=key):
            read_verdict(tmp_path)


def test_read_verdict_size_caps_a_giant_verdict(tmp_path: Path) -> None:
    write_req(tmp_path, "x" * (MAX_VERDICT_BYTES + 1), typed=False)
    with pytest.raises(VerdictError, match="exceeds"):
        read_verdict(tmp_path)


def test_install_tool_owns_the_channel(tmp_path: Path) -> None:
    # A judge's checkout is author-authored: a .outerloop symlink to a host dir
    # must not let install write through it, and a pre-planted syscall.json
    # must not survive to be read as a forged verdict.
    escape = tmp_path / "ESCAPE"
    escape.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / SYSCALL_DIR).symlink_to(escape, target_is_directory=True)
    install_tool(ws)
    assert not (ws / SYSCALL_DIR).is_symlink()
    assert (ws / SYSCALL_DIR / "syscall").exists()
    assert list(escape.iterdir()) == []  # nothing written through to the target

    # a real channel dir is removed and recreated, dropping a planted ABI
    planted = tmp_path / "planted"
    (planted / SYSCALL_DIR).mkdir(parents=True)
    (planted / SYSCALL_DIR / SYSCALL_FILE).write_text(
        json.dumps({"type": "verdict", "findings": [_finding()], "notes": "forged"})
    )
    install_tool(planted)
    assert read_verdict(planted) is None  # the planted ABI is gone


def test_gather_results_reads_output_and_delivers_artifacts(tmp_path) -> None:
    # the wake side: read each launch's exit/stdout/stderr + skips, and copy
    # delivered artifacts into the excluded channel dir the author reads.
    run_dir = tmp_path / "run"
    ws = tmp_path / "ws"
    ws.mkdir()
    # a normal launch with a delivered artifact + a skip log
    ev = _job(
        run_dir,
        "train",
        code="0\n",
        stdout="loss: 0.4\n",
        stderr="",
        log="skipped (over 5000000 bytes): big.ckpt\n",
    )
    (ev / "artifacts" / "sub").mkdir()
    (ev / "artifacts" / "curve.json").write_text("{}")
    (ev / "artifacts" / "sub" / "extra.txt").write_text("x")
    # a launch whose job died before the wrapper ran -> no exit-code file
    _job(run_dir, "crashed", code=None, stderr="OOM\n")

    results = gather_results(
        run_dir,
        ws,
        (Launch("train", "c", 30, ("curve.json",)), Launch("crashed", "c", 30)),
    )
    assert [r.name for r in results] == ["train", "crashed"]  # request order
    train, crashed = results
    assert train.exit_code == 0 and "loss: 0.4" in train.stdout_tail
    assert set(train.delivered) == {
        ".outerloop/results/train/curve.json",
        ".outerloop/results/train/sub/extra.txt",
    }
    assert train.skipped == ("skipped (over 5000000 bytes): big.ckpt",)
    # the files really landed in the excluded channel
    assert (ws / ".outerloop" / "results" / "train" / "curve.json").read_text() == "{}"
    # a job with no exit-code file surfaces as None (infra failure), not a skip
    assert crashed.exit_code is None and "OOM" in crashed.stderr_tail


def test_gather_results_refuses_symlinked_destination_channel(tmp_path) -> None:
    # the author controls .outerloop in its sandbox: a symlinked results dir
    # (or a planted output symlink) must not make delivery write through it to a
    # host path (terra #135 r2).
    run_dir = tmp_path / "run"
    ws = tmp_path / "ws"
    ev = _job(run_dir, "probe")
    (ev / "artifacts" / "out.json").write_text("safe")
    # attacker target OUTSIDE the workspace
    escape = tmp_path / "ESCAPE"
    escape.mkdir()
    (escape / "out.json").write_text("ORIGINAL")
    # .outerloop/results -> the escape dir
    (ws / ".outerloop").mkdir(parents=True)
    (ws / ".outerloop" / "results").symlink_to(escape, target_is_directory=True)

    (r,) = gather_results(run_dir, ws, (Launch("probe", "c", 30, ("out.json",)),))
    assert r.delivered == ()  # nothing delivered through the symlink
    assert any("symlink" in s for s in r.skipped)
    assert (escape / "out.json").read_text() == "ORIGINAL"  # host file untouched


def test_gather_results_reads_only_the_tail_of_huge_output(tmp_path) -> None:
    # launch output is agent-controlled and can be arbitrarily large: the wake
    # must read only the trailing window, never load the whole file
    # (terra #135 r1).
    run_dir = tmp_path / "run"
    ws = tmp_path / "ws"
    ws.mkdir()
    _job(
        run_dir,
        "big",
        artifacts=False,
        stdout="x" * (MAX_OUTPUT_CHARS * 50) + "\nFINAL: 0.42\n",  # far past the window
    )
    (results,) = gather_results(run_dir, ws, (Launch("big", "c", 30),))
    assert len(results.stdout_tail) <= MAX_OUTPUT_CHARS
    assert "FINAL: 0.42" in results.stdout_tail  # the tail, not the head


def test_launch_job_script_copies_declared_artifacts(tmp_path: Path) -> None:
    """The job writer's copy-out, EXECUTED: declared file delivered; oversized
    and missing ones recorded in artifacts.log; a shell-metacharacter name is
    INERT (never runs as code — terra #132 r1); a symlink pointing outside the
    tree is refused, not dereferenced (terra #132 r1)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "small.txt").write_text("ok")
    (repo / "big.bin").write_bytes(b"x" * 200)
    # a symlink escaping the tree: committed like any file, reproduced by the
    # job's checkout — the copy-out must refuse to dereference it
    (tmp_path / "host-secret.txt").write_text("HOST-ONLY")
    (repo / "leak.txt").symlink_to(tmp_path / "host-secret.txt")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c"],
        check=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    run_dir = tmp_path / "run"
    script = write_eval_job(
        run_dir,
        "launch-t",
        repo_root=repo,
        snapshot_sha=sha,
        command="true",
        image="img.sif",
        container_runtime=ApptainerRuntime("apptainer"),
        artifacts=(
            "small.txt",
            "big.bin",
            "missing.txt",
            "leak.txt",
            "$(touch injected-marker).txt",
        ),
        artifact_max_bytes=100,
    )
    text = script.read_text()
    # run it with the apptainer line stubbed out (no apptainer on CI): the
    # jail line writes stdout; we replace it with a no-op that keeps files.
    doctored = text.replace(
        text[text.index("apptainer exec") : text.index('> "$EV/stdout"')],
        "true ",
    )
    script.write_text(doctored)
    subprocess.run(["sh", str(script)], check=True, cwd=repo)
    ev = run_dir / "eval-launch-t"
    assert (ev / "artifacts" / "small.txt").read_text() == "ok"
    log = (ev / "artifacts.log").read_text()
    assert "big.bin" in log and "missing.txt" in log
    assert not (ev / "artifacts" / "big.bin").exists()
    # INJECTION inert: the metacharacter name never executed, anywhere
    assert "injected-marker" in log
    for root in (repo, run_dir, tmp_path):
        assert not (root / "injected-marker").exists()
    # SYMLINK refused: the out-of-tree target was never delivered
    assert "leak.txt" in log
    assert not (ev / "artifacts" / "leak.txt").exists()


def test_submit_carries_a_declared_eval_walltime(tmp_path: Path) -> None:
    """`eval_minutes` is the author's walltime for the submit's paired gate
    evals: positive, clamped to the backstop, and meaningless without a
    submit (refused loudly rather than silently ignored)."""
    write_req(
        tmp_path,
        {"launches": [], "submit": True, "report": "H: the change helps", "eval_minutes": 420},
    )
    req = read_request(tmp_path)
    assert req is not None and req.submit and req.eval_minutes == 420
    write_req(
        tmp_path,
        {"launches": [], "submit": True, "report": "H: the change helps", "eval_minutes": 10**6},
    )
    req = read_request(tmp_path)
    assert req is not None and req.eval_minutes == MAX_EVAL_MINUTES
    write_req(tmp_path, {"launches": [], "submit": True, "report": "H: the change helps"})
    req = read_request(tmp_path)
    assert req is not None and req.eval_minutes is None
    for bad in (
        {"launches": [], "eval_minutes": 60},
        {"launches": [], "submit": True, "report": "H: the change helps", "eval_minutes": 0},
    ):
        write_req(tmp_path, bad)
        with pytest.raises(SyscallError):
            read_request(tmp_path)


def test_gpu_hours_are_metered_against_the_run_budget() -> None:
    """Compute is priced, steps are not: launches (minutes x GPUs) and a
    submit's two gate evals (at the declared, else default, walltime) draw on
    gpu_hours_per_run; an over-budget request is refused with the numbers.
    CPU benchmarks meter nothing."""
    launches = (_launch("a"), _launch("b"))  # 5 min each
    plain = SyscallRequest(launches=launches)
    assert gpu_hours_cost(plain, gpus=1, eval_minutes_default=240) == 10 / 60
    assert gpu_hours_cost(plain, gpus=0, eval_minutes_default=240) == 0.0
    assert evals_gpu_hours(plain, gpus=1, eval_minutes_default=240) == 0.0  # not a submit
    sub_default = SyscallRequest(launches=(), submit=True)
    assert gpu_hours_cost(sub_default, gpus=1, eval_minutes_default=240) == 8.0
    sub_declared = SyscallRequest(launches=(), submit=True, eval_minutes=420)
    assert gpu_hours_cost(sub_declared, gpus=2, eval_minutes_default=240) == 28.0
    # the two parts are charged where they happen: a submit's evals at
    # acceptance, its sibling launches only when the gate parks (terra #177)
    sub_with = SyscallRequest(launches=launches, submit=True)
    assert launches_gpu_hours(sub_with, gpus=1) == 10 / 60
    assert evals_gpu_hours(sub_with, gpus=1, eval_minutes_default=240) == 8.0
    # suite siblings' paired evals are charged as if measured, each at its
    # own GPU count (terra #177): 2 x 240 min x (1 + 1 + 0) GPUs
    assert evals_gpu_hours(sub_default, gpus=1, eval_minutes_default=240, suite_gpus=(1, 0)) == 16.0
    # fits: 8 GPU-hours of evals into a 60-hour budget with 50 used
    ok = budget_error(
        sub_default,
        launches_used=1,
        launch_budget=4,
        sleeps_used=0,
        sleep_budget=4,
        gpu_hours_used=50.0,
        gpu_hour_budget=60.0,
        gpus=1,
        eval_minutes_default=240,
    )
    assert ok == ""
    # does not fit: a 14-hour eval pair on top of 50 used
    over = budget_error(
        sub_declared,
        launches_used=1,
        launch_budget=4,
        sleeps_used=0,
        sleep_budget=4,
        gpu_hours_used=50.0,
        gpu_hour_budget=60.0,
        gpus=1,
        eval_minutes_default=240,
    )
    assert "GPU-hour budget would be exceeded" in over and "60" in over
    # a CPU benchmark never meters, whatever the numbers say
    assert (
        budget_error(
            sub_declared,
            launches_used=0,
            launch_budget=4,
            sleeps_used=0,
            sleep_budget=4,
            gpu_hours_used=999.0,
            gpu_hour_budget=1.0,
            gpus=0,
        )
        == ""
    )


def test_cached_gate_is_charged_one_main_eval() -> None:
    """A `baseline: cached` submit with a warm cache runs only the candidate:
    the gate charges one main eval, not two (terra #178); siblings stay paired."""
    sub = SyscallRequest(launches=(), submit=True, eval_minutes=240)
    assert evals_gpu_hours(sub, gpus=1, eval_minutes_default=0, main_evals=2) == 8.0
    assert evals_gpu_hours(sub, gpus=1, eval_minutes_default=0, main_evals=1) == 4.0
    assert (
        evals_gpu_hours(sub, gpus=1, eval_minutes_default=0, main_evals=1, suite_gpus=(1,)) == 12.0
    )


def test_array_launch_fans_out_and_gathers_per_index(tmp_path: Path) -> None:
    """`array: K` is one launch that becomes K jobs `<name>.<i>` with
    SWEEP_INDEX, gathered as K results with artifacts under
    results/<name>/<i>/; it costs K times the GPU-hours but one launch. The
    width is clamped; zero is refused. The dot is outside the name alphabet,
    so a plain launch can never share a job name with an array member."""
    write_req(
        tmp_path, {"launches": [{"name": "sweep", "command": "x", "minutes": 10, "array": 3}]}
    )
    req = read_request(tmp_path)
    assert req is not None and req.launches[0].array == 3
    assert launch_jobs(req.launches[0]) == (
        ("sweep.0", {"SWEEP_INDEX": "0"}),
        ("sweep.1", {"SWEEP_INDEX": "1"}),
        ("sweep.2", {"SWEEP_INDEX": "2"}),
    )
    write_req(tmp_path, {"launches": [{"name": "sweep.0", "command": "x"}]})
    with pytest.raises(SyscallError):
        read_request(tmp_path)
    assert launch_jobs(Launch(name="one", command="x", minutes=5)) == (("one", {}),)
    assert launches_gpu_hours(req, gpus=1) == 30 / 60
    write_req(tmp_path, {"launches": [{"name": "s", "command": "x", "array": 10**6}]})
    big = read_request(tmp_path)
    assert big is not None and big.launches[0].array == MAX_LAUNCH_ARRAY
    write_req(tmp_path, {"launches": [{"name": "s", "command": "x", "array": 0}]})
    with pytest.raises(SyscallError):
        read_request(tmp_path)

    run_dir = tmp_path / "run"
    ws = tmp_path / "ws"
    ws.mkdir()
    for i, code in enumerate(("0", "1")):
        ev = _job(run_dir, f"sweep.{i}", code=code, stdout=f"result {i}\n")
        (ev / "artifacts" / "out").mkdir()
        (ev / "artifacts" / "out" / "curve.json").write_text(f"[{i}]")
    results = gather_results(run_dir, ws, (Launch(name="sweep", command="x", minutes=10, array=2),))
    assert [r.name for r in results] == ["sweep.0", "sweep.1"]
    assert [r.exit_code for r in results] == [0, 1]
    assert results[1].stdout_tail.strip() == "result 1"
    assert results[1].delivered == (".outerloop/results/sweep/1/out/curve.json",)
    first = ws / ".outerloop" / "results" / "sweep" / "0" / "out" / "curve.json"
    assert first.read_text() == "[0]"
    # whatever the author left at the group path — a plain file here — is
    # replaced, not written through or tripped over (terra #181 round 2)
    shutil.rmtree(ws / ".outerloop" / "results" / "sweep")
    (ws / ".outerloop" / "results" / "sweep").write_text("not a dir")
    again = gather_results(run_dir, ws, (Launch(name="sweep", command="x", minutes=10, array=2),))
    assert again[0].delivered == (".outerloop/results/sweep/0/out/curve.json",)
    assert first.read_text() == "[0]"


def test_launch_hours_refund_hands_back_unused_declared_walltime() -> None:
    """A sweep charged 8 x 240 min at dispatch that died after 5 minutes per
    job is refunded almost all of it; a job that ran its full walltime is
    refunded nothing; an unknown elapsed time refunds nothing at all."""
    sweep = (Launch(name="sweep", command="x", minutes=240, array=8),)
    assert launch_hours_refund(sweep, [300] * 8, gpus=1) == 32.0 - 8 * 300 / 3600
    assert launch_hours_refund(sweep, [240 * 60] * 8, gpus=1) == 0.0
    assert launch_hours_refund(sweep, [250 * 60] * 8, gpus=1) == 0.0  # overran: never negative
    assert launch_hours_refund(sweep, [300] * 7 + [None], gpus=1) == 0.0
    assert launch_hours_refund(sweep, [], gpus=1) == 0.0
    assert launch_hours_refund(sweep, [300] * 8, gpus=0) == 0.0
    two = (Launch(name="a", command="x", minutes=60), Launch(name="b", command="x", minutes=30))
    assert launch_hours_refund(two, [1800, 1800], gpus=2) == (90 * 2 / 60) - (3600 * 2 / 3600)


def test_parse_elapsed_reads_sacct_fields() -> None:
    assert parse_elapsed("00:05:00") == 300
    assert parse_elapsed("04:10:05") == 4 * 3600 + 605
    assert parse_elapsed("1-02:00:00") == 86400 + 7200
    assert parse_elapsed("05:00") == 300
    assert parse_elapsed("") is None and parse_elapsed("INVALID") is None
    assert parse_elapsed("x-01:00:00") is None


def test_sync_cli_waits_then_times_out(tmp_path, monkeypatch) -> None:
    """The verb touches the request, then blocks until the kernel stamps
    done — the wait is the session's own time, never a new leg. An unstamped
    request returns at its deadline instead of blocking forever."""
    (tmp_path / SYSCALL_DIR).mkdir()
    monkeypatch.setattr("outerloop.syscall_cli.SYNC_POLL_S", 0.01)

    class A:
        minutes = 1

    def stamp_soon():
        time.sleep(0.3)
        mark_synced(tmp_path, time.time())

    threading.Thread(target=stamp_soon, daemon=True).start()
    started = time.monotonic()
    out = cmd_sync(tmp_path, A())
    elapsed = time.monotonic() - started
    assert "refreshed" in out
    # the poll interval was honored: the default 15 s would block far longer
    # than the marker's 0.3 s arrival, so this bounds it well under one interval
    assert elapsed < 5, elapsed

    class Probe:
        minutes = 0  # a pure probe: the deadline has already passed

    out = cmd_sync(tmp_path, Probe())
    assert "timed out" in out and "next wake" in out


def test_sync_request_tracking(tmp_path) -> None:
    ws = tmp_path
    (ws / SYSCALL_DIR).mkdir()
    assert sync_requested(ws) is None
    (ws / SYSCALL_DIR / "sync-request").touch()
    at = sync_requested(ws)
    assert at is not None
    mark_synced(ws, at)
    assert sync_requested(ws) is None
    # a request arriving MID-FETCH stays newer: the done stamp acknowledges
    # only the request it serviced
    later = at + 5
    os.utime(ws / SYSCALL_DIR / "sync-request", (later, later))
    assert sync_requested(ws) == later
    # a planted symlink at the done marker is replaced, never written
    # through: the victim is untouched
    done = ws / SYSCALL_DIR / "sync-done"
    victim = ws / "victim"
    done.unlink()
    done.symlink_to(victim)
    mark_synced(ws, later)
    assert not done.is_symlink() and not victim.exists()
    assert sync_requested(ws) is None


def test_mark_synced_never_touches_a_hardlinked_marker(tmp_path) -> None:
    ws = tmp_path
    (ws / SYSCALL_DIR).mkdir()
    (ws / SYSCALL_DIR / "sync-request").touch()
    victim = ws / "victim"
    victim.write_text("precious")
    victim_mtime = victim.stat().st_mtime
    # a session hard-links the marker name to the victim inode
    os.link(victim, ws / SYSCALL_DIR / "sync-done")
    mark_synced(ws, 12345.0)
    # the victim's content and mtime are unchanged: no utime, fresh inode
    assert victim.read_text() == "precious"
    assert victim.stat().st_mtime == victim_mtime


def test_mark_synced_refuses_a_hardlinked_temp(tmp_path, monkeypatch) -> None:
    """The temp inode is O_EXCL: a hard link planted at the temp name is not
    truncated (open fails); the victim survives."""
    ws = tmp_path
    (ws / SYSCALL_DIR).mkdir()
    (ws / SYSCALL_DIR / "sync-request").touch()
    victim = ws / "victim"
    victim.write_text("precious")
    # force a predictable temp name and pre-plant a hard link to the victim
    monkeypatch.setattr("os.urandom", lambda n: b"\x00" * n)
    os.link(victim, ws / SYSCALL_DIR / f".sync-done.{(b'\x00' * 8).hex()}")
    with contextlib.suppress(FileExistsError):
        mark_synced(ws, 1.0)  # O_EXCL refuses the planted link
    assert victim.read_text() == "precious"  # never truncated


def test_sync_refuses_a_symlinked_channel(tmp_path) -> None:
    ws = tmp_path
    outside = ws / "outside"
    outside.mkdir()
    (ws / ".outerloop").symlink_to(outside)  # channel is a symlink
    assert sync_requested(ws) is None
    with contextlib.suppress(OSError):
        mark_synced(ws, 1.0)  # refused
    assert not (outside / "sync-done").exists()  # nothing written outside


def test_read_request_carries_a_one_line_why(tmp_path: Path) -> None:
    def why(value) -> None:
        write_req(
            tmp_path,
            {"type": "sleep", "launches": [{"name": "a", "command": "x", "why": value}]},
            typed=False,
        )

    why("  two  lines\nhere  ")
    req = read_request(tmp_path)
    assert req is not None and req.launches[0].why == "two lines here"
    why("w" * (MAX_WHY_CHARS + 1))
    with pytest.raises(SyscallError, match="why"):
        read_request(tmp_path)
    why(3)
    with pytest.raises(SyscallError, match="why"):
        read_request(tmp_path)


def test_channel_markers_generalize_to_any_verb(tmp_path: Path) -> None:
    """The watcher's verbs ride the sync marker protocol: a request newer than
    the done marker is pending; the answer is written with a marker's care."""
    channel = tmp_path / ".outerloop"
    channel.mkdir()
    assert marker_requested(tmp_path, "queue-request", "queue-done") is None
    (channel / "queue-request").touch()
    at = marker_requested(tmp_path, "queue-request", "queue-done")
    assert at is not None
    write_channel_json(tmp_path, "queue.json", {"jobs": [1]})
    mark_done(tmp_path, "queue-done", at)
    assert json.loads((channel / "queue.json").read_text()) == {"jobs": [1]}
    assert marker_requested(tmp_path, "queue-request", "queue-done") is None  # acknowledged
    # a planted symlink in the answer's place is never written through
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (channel / "queue.json").unlink()
    (channel / "queue.json").symlink_to(victim)
    write_channel_json(tmp_path, "queue.json", {"jobs": []})
    assert victim.read_text() == "keep"
    assert json.loads((channel / "queue.json").read_text()) == {"jobs": []}


def test_a_fifo_in_the_done_markers_place_never_blocks_the_kernel(tmp_path: Path) -> None:
    """A session can replace `<verb>-done` with a FIFO; a blocking open would
    hang the watcher thread (and the tick's sync service) for good."""
    channel = tmp_path / ".outerloop"
    channel.mkdir()
    os.mkfifo(channel / "queue-done")
    (channel / "queue-request").touch()
    seen: list[float | None] = []
    t = threading.Thread(
        target=lambda: seen.append(marker_requested(tmp_path, "queue-request", "queue-done")),
        daemon=True,
    )
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "marker_requested blocked on the FIFO"
    assert seen and seen[0] is not None  # the FIFO counts as no acknowledgement at all


def test_sweep_pace_is_validated_clamped_and_expanded(tmp_path: Path) -> None:
    write_req(
        tmp_path,
        {
            "launches": [
                {"name": "s", "command": "x", "array": 4, "concurrency": 9},
                {"name": "p", "command": "y", "concurrency": 3},
            ]
        },
    )
    req = read_request(tmp_path)
    assert req is not None
    # clamped to the array; meaningless for a plain launch
    assert [la.concurrency for la in req.launches] == [4, 0]
    write_req(
        tmp_path, {"launches": [{"name": "s", "command": "x", "array": 4, "concurrency": -1}]}
    )
    with pytest.raises(SyscallError, match="concurrency"):
        read_request(tmp_path)
    s8 = Launch(name="s", command="x", minutes=5, array=8)
    assert array_spec(s8) == "0-7%8" and array_spec(Launch(name="p", command="y", minutes=5)) == ""
    assert array_spec(Launch(name="s", command="x", minutes=5, array=8, concurrency=3)) == "0-7%3"
    req = SyscallRequest(
        launches=(
            s8,
            Launch(name="s2", command="x", minutes=5, array=8, concurrency=2),
            Launch(name="p", command="y", minutes=5),
        ),
        submit=False,
    )
    clamped = clamp_concurrency(req, gpus=2, max_concurrent_gpus=12)  # 6 tasks of 2 GPUs
    assert [la.concurrency for la in clamped.launches] == [6, 2, 0]
    assert clamp_concurrency(req, gpus=2, max_concurrent_gpus=None) is req
    assert clamp_concurrency(req, gpus=0, max_concurrent_gpus=1).launches[0].concurrency == 1
    # one id per launch expands to the array's task ids; a legacy per-task list passes through
    ids = launch_task_ids(req.launches, ["10", "11", "12"])
    assert ids == [f"10_{k}" for k in range(8)] + [f"11_{k}" for k in range(8)] + ["12"]
    legacy = [str(i) for i in range(1, 9)]
    assert launch_task_ids((s8,), legacy) == legacy
    assert launch_task_ids((s8,), ["1", "2"]) == []  # an unknown mapping is never guessed


def test_read_results_reads_without_delivering(tmp_path: Path) -> None:
    ev = _job(tmp_path, "a", code="0\n", stdout="hello\nresult 1\n")
    (ev / "artifacts" / "out.txt").write_text("x")
    (result,) = read_results(tmp_path, (Launch(name="a", command="x", minutes=5, why="w"),))
    assert (result.exit_code, result.stdout_tail, result.delivered, result.why) == (
        0,
        "hello\nresult 1\n",
        (),
        "w",
    )
    assert not (tmp_path / ".outerloop").exists()  # nothing was delivered anywhere


def test_refresh_tool_rewrites_only_the_tool_and_never_through_a_symlink(tmp_path: Path) -> None:
    install_tool(tmp_path)
    channel = tmp_path / ".outerloop"
    (channel / "budget.json").write_text("{}")
    tool = channel / "syscall"
    assert refresh_tool(tmp_path) is False  # already this kernel's tool: nothing to tell
    tool.write_text("# stale tool\n")
    assert refresh_tool(tmp_path) is True  # an older tool was replaced: the wake says what is new
    tool.write_text("# stale tool\n")
    before = os.umask(0o177)  # an execute-masking umask must not strip the tool's x bits
    try:
        refresh_tool(tmp_path)
    finally:
        os.umask(before)
    assert "def main(" in tool.read_text() and (tool.stat().st_mode & 0o111) == 0o111
    assert (channel / "budget.json").exists()  # the channel is untouched
    victim = tmp_path / "victim"
    victim.write_text("keep")
    tool.unlink()
    tool.symlink_to(victim)
    refresh_tool(tmp_path)
    assert victim.read_text() == "keep" and "def main(" in tool.read_text()
    # a directory planted in the tool's place is replaced by the tool, removed
    # relative to the channel fd (no path re-resolution a swapped symlink
    # could redirect)
    assert shutil.rmtree.avoids_symlink_attacks
    tool.unlink()
    tool.mkdir()
    (tool / "junk").write_text("x")
    assert refresh_tool(tmp_path) is True
    assert tool.is_file() and "def main(" in tool.read_text()
    # the same bytes made unreadable (mode 000) are replaced, not left as a
    # tool the session cannot run
    tool.chmod(0)
    assert refresh_tool(tmp_path) is True
    assert (tool.stat().st_mode & 0o777) == 0o755 and "def main(" in tool.read_text()
    # a FIFO in the tool's place never blocks the wake: the comparison opens
    # non-blocking and checks what it opened, then the tool is rewritten
    tool.unlink()
    os.mkfifo(tool)
    seen: list[bool] = []
    t = threading.Thread(target=lambda: seen.append(refresh_tool(tmp_path)), daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "refresh_tool blocked on a FIFO"
    assert seen == [True] and tool.is_file() and "def main(" in tool.read_text()
    # The notice names this workspace's channel and the current verbs.
    note = tool_update_note(".outerloop")
    assert ".outerloop/syscall" in note and "--report" in note
    assert "`end` verb is now `stop`" in note
    self_report = tool_update_note(".outerloop", verification="self_report")
    assert "Self-reported submissions require" in self_report
    assert "`end` verb is now `stop`" in self_report


@pytest.mark.parametrize(
    "messages",
    [
        "text",
        [1],
        [None],
        [{"to": "thread", "text": "", "reply_to": None}],
        [{"to": "thread", "text": "x" * 100_001, "reply_to": None}],
    ],
)
def test_kernel_rejects_forged_messages(tmp_path, messages) -> None:
    write_req(tmp_path, {"type": "message", "messages": messages}, typed=False)
    with pytest.raises(SyscallError):
        read_request(tmp_path)
    assert read_request(tmp_path) is None


def test_budget_is_never_written_through_a_symlinked_budget_file(tmp_path):
    """A `budget.json` that is a symlink to a file outside the tree is never
    followed: the write replaces the link under a directory handle."""
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched")
    ws = tmp_path / "ws"
    (ws / ".outerloop").mkdir(parents=True)
    (ws / ".outerloop" / "budget.json").symlink_to(outside)
    write_budget(ws, launches_remaining=1, sleeps_remaining=1)
    # the link is replaced under a directory handle, never followed
    assert outside.read_text() == "untouched"
    target = ws / ".outerloop" / "budget.json"
    assert not target.is_symlink() and '"launches_remaining": 1' in target.read_text()


def test_budget_is_never_written_into_a_channel_the_target_tracks(tmp_path, caplog):
    """A regular `.outerloop/` directory the target committed is the target's,
    not the kernel's: nothing is written into it."""
    ws = tmp_path / "ws"
    (ws / ".outerloop").mkdir(parents=True)
    (ws / ".outerloop" / "budget.json").write_text("theirs")
    subprocess.run(["git", "-C", str(ws), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(ws), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(ws),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-qm",
            "ship",
        ],
        check=True,
    )
    assert shipped_channel(ws) == ".outerloop"
    write_budget(ws, launches_remaining=1, sleeps_remaining=1)
    assert (ws / ".outerloop" / "budget.json").read_text() == "theirs"
    assert "budget not written" in caplog.text
    # the kernel's own, untracked channel is not shipped
    (ws / ".outerloop" / "budget.json").unlink()
    subprocess.run(["git", "-C", str(ws), "rm", "-rq", "--cached", ".outerloop"], check=True)
    subprocess.run(
        ["git", "-C", str(ws), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "own"],
        check=True,
    )
    assert shipped_channel(ws) == ""
    write_budget(ws, launches_remaining=2, sleeps_remaining=3)
    assert '"launches_remaining": 2' in (ws / ".outerloop" / "budget.json").read_text()


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "sleep", "withdraw": "reason"},
        {"type": "stop", "withdraw": ""},
        {"type": "stop", "withdraw": " "},
        {"type": "end", "withdraw": ""},  # the legacy spelling is checked too
        {"type": "stop", "withdraw": 1},
        {"type": "stop", "withdraw": "x" * 20001},
    ],
)
def test_invalid_withdraw_request(tmp_path, payload):
    write_req(tmp_path, payload, typed=False)
    with pytest.raises(SyscallError):
        read_request(tmp_path)


def test_legacy_end_request_remains_valid(tmp_path):
    # A staged request an older kernel's CLI wrote before the verb was renamed
    # `stop`; the kernel honours the legacy `"end"` spelling of the type.
    for _ in range(2):
        write_req(tmp_path, {"type": "end", "report": "done", "messages": []}, typed=False)
        request = read_request(tmp_path)
        assert request and request.end and request.report == "done" and request.withdraw == ""
        assert read_request(tmp_path) is None
