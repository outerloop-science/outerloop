"""Operator rebinding preserves work while moving only the next author's leg."""

import json
import logging
import os
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from outerloop.cli import main
from outerloop.rebind import apply, request, requested
from outerloop.runstate import RunRecord, load_record, save_record


@pytest.fixture
def parked(tmp_path):
    record = RunRecord(
        "one",
        "owner/repo",
        "research",
        "parked",
        author_backend="claude",
        author_model="old-model[endpoint=retired]",
        author_key_file="old-key",
        resume_session_id="session",
        stage={
            "hermes_resume_required_chars": 100,
            "endpoint_wait": {"endpoint": "retired", "since": 10},
        },
    )
    save_record(tmp_path, record, 10)
    return load_record(tmp_path, "one")


@pytest.fixture
def selection(tmp_path, monkeypatch):
    key = tmp_path / "key"
    key.write_text("secret")
    key.chmod(0o600)
    monkeypatch.setenv("OUTERLOOP_ENDPOINT_ONPREM_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("OUTERLOOP_ENDPOINT_ONPREM_MODEL", "served-model")
    monkeypatch.setenv("OUTERLOOP_ENDPOINT_ONPREM_API", "anthropic,responses")
    monkeypatch.setenv("OUTERLOOP_ENDPOINT_ONPREM_KEY_FILE", str(key))
    monkeypatch.setenv("OUTERLOOP_AUTHOR_BACKEND", "claude")
    monkeypatch.setenv("OUTERLOOP_AUTHOR_MODEL", "claude-fleet")
    monkeypatch.setenv("OUTERLOOP_CLAUDE_KEY_FILE", str(key))

    def select(backend="claude"):
        monkeypatch.setenv(
            "OUTERLOOP_AUTHOR_OVERRIDES",
            json.dumps(
                {
                    "owner/repo": {
                        "backend": backend,
                        "model": "[endpoint=onprem]",
                        "slots": ["agent-01"],
                        "session_minutes": 180,
                        "session_max_turns": 250,
                    }
                }
            ),
        )

    select()
    return select


def test_command_atomic_idempotent(tmp_path, parked, monkeypatch):
    import outerloop.rebind as module

    original = module.os.replace
    published = []

    def atomic(source, destination):
        assert not destination.exists()
        published.append(json.loads(source.read_text()))
        original(source, destination)

    monkeypatch.setattr(module.os, "replace", atomic)
    args = ["rebind", "one", "--root", str(tmp_path), "--note", "server retired"]
    assert main(args) == main(args) == 0
    assert len(published) == 1
    assert published[0]["note"] == "server retired"
    assert published[0]["time"] > 0
    assert load_record(tmp_path, "one") == parked
    assert not list((tmp_path / "runs/one").glob("*.tmp"))


@pytest.mark.parametrize("run_id", ["unknown", "../one"])
def test_unknown(tmp_path, run_id):
    with pytest.raises(SystemExit) as exc:
        main(["rebind", run_id, "--root", str(tmp_path)])
    assert exc.value.code == 2
    assert not list(tmp_path.iterdir())


def test_ended(tmp_path, parked):
    save_record(tmp_path, replace(parked, state="ended", ending="aborted"), 20)
    with pytest.raises(ValueError, match="ended"):
        request(tmp_path, "one")
    assert not requested(tmp_path, "one")


@pytest.mark.parametrize("backend", ["claude", "codex"])
def test_rebind(tmp_path, parked, selection, backend, caplog):
    selection(backend)
    request(tmp_path, "one", "move")
    caplog.set_level(logging.INFO)
    rebound = apply(tmp_path, parked, "image.sif")
    assert rebound.author_backend == backend
    assert rebound.author_model == "served-model[endpoint=onprem]"
    assert rebound.author_key_file == str(tmp_path / "key")
    assert rebound.author_limits is not None
    assert rebound.author_limits["session_max_turns"] == 250
    assert rebound.author_limits["session_minutes"] == 180
    assert rebound.author_overridden
    assert rebound.resume_session_id == ("session" if backend == "claude" else "")
    assert ("hermes_resume_required_chars" in rebound.stage) == (backend == "claude")
    assert "endpoint_wait" not in rebound.stage
    assert [a["model"] for a in rebound.author_history] == [
        parked.author_model,
        rebound.author_model,
    ]
    assert rebound.author_history[0]["since"] == 10
    assert rebound.author_history[1]["note"] == "move"
    assert not requested(tmp_path, "one")
    assert apply(tmp_path, rebound, "image.sif") == rebound
    assert len([r for r in caplog.records if " -> " in r.message]) == 1


def test_sweep_wakes_retired_endpoint(tmp_path, parked, selection, monkeypatch):
    from outerloop.attempt import defer_endpoint_wake
    from outerloop.tick import _sweep_one

    monkeypatch.setenv("OUTERLOOP_ENDPOINT_RETIRED_URL_FILE", str(tmp_path / "missing"))
    monkeypatch.setenv("OUTERLOOP_ENDPOINT_RETIRED_API", "anthropic")
    monkeypatch.setenv("OUTERLOOP_ENDPOINT_RETIRED_MODEL", "old-model")
    monkeypatch.setenv("OUTERLOOP_ENDPOINT_RETIRED_KEY_FILE", str(tmp_path / "key"))
    assert defer_endpoint_wake(tmp_path, parked)
    request(tmp_path, "one")
    wake = Mock()
    _sweep_one(
        tmp_path,
        Mock(),
        Mock(),
        100,
        60,
        60,
        False,
        load_record(tmp_path, "one"),
        "holder",
        wake,
        [],
        [],
        [],
    )
    wake.assert_called_once()
    rebound = apply(tmp_path, parked, "")
    assert not defer_endpoint_wake(tmp_path, rebound)


@pytest.mark.parametrize("failure", ["endpoint", "key", "config", "override"])
def test_unusable_keeps_request(tmp_path, parked, selection, monkeypatch, failure, caplog):
    if failure == "endpoint":
        monkeypatch.delenv("OUTERLOOP_ENDPOINT_ONPREM_URL")
        monkeypatch.setenv("OUTERLOOP_ENDPOINT_ONPREM_URL_FILE", str(tmp_path / "missing"))
    elif failure == "key":
        (tmp_path / "key").unlink()
    elif failure == "config":
        selection("codex")  # requires an image
    else:
        monkeypatch.setenv("OUTERLOOP_AUTHOR_OVERRIDES", '{"owner/repo": {}}')
    request(tmp_path, "one")
    assert apply(tmp_path, parked, "") == parked
    assert load_record(tmp_path, "one") == parked
    assert requested(tmp_path, "one")
    assert "rebind pending" in caplog.text


def test_legacy_fleet_history_report(tmp_path, parked, selection, monkeypatch):
    from outerloop.orchestrator import AttemptResult, RunConfig

    path = tmp_path / "runs/one/state.json"
    raw = json.loads(path.read_text())
    raw.pop("author_history")
    path.write_text(json.dumps(raw))
    legacy = load_record(tmp_path, "one")
    assert not legacy.author_history
    monkeypatch.delenv("OUTERLOOP_AUTHOR_OVERRIDES")
    request(tmp_path, "one")
    rebound = apply(tmp_path, legacy, "")
    assert not rebound.author_overridden
    assert rebound.author_model == "claude-fleet"
    assert len(rebound.author_history) == 2
    report = AttemptResult(outcome="parked").report(
        RunConfig(
            "owner/repo",
            "bench",
            author_history=rebound.author_history,
        )
    )
    assert f"Authors: claude/{parked.author_model} → claude/claude-fleet" in report


def test_consumption_crash_recovery(tmp_path, parked, selection):
    request(tmp_path, "one")
    path = tmp_path / "runs/one/rebind.json"
    pending = path.read_text()
    rebound = apply(tmp_path, parked, "")
    path.write_text(pending)
    assert apply(tmp_path, rebound, "") == rebound
    assert not path.exists()


def test_measure_provenance_survives_rebind(tmp_path, parked, selection):
    from outerloop.dispatch import write_eval_job

    directory = tmp_path / "runs/one"

    def measure(name, sha):
        write_eval_job(
            directory, name, repo_root=directory / "ws", snapshot_sha=sha, command="true", image=""
        )
        return json.loads((directory / f"eval-{name}/provenance.json").read_text())

    before = measure("first", "old-sha")
    request(tmp_path, "one")
    rebound = apply(tmp_path, parked, "")
    assert measure("first", "old-sha") == before
    after = measure("second", "new-sha")
    assert before["author"]["model"] == parked.author_model
    assert after["author"]["model"] == rebound.author_model
    # A reused eval directory measuring a new commit records that commit's author.
    reused = measure("first", "new-sha")
    assert (reused["commit"], reused["author"]["model"]) == ("new-sha", rebound.author_model)


def test_launch_rows_credit_the_launched_commit(tmp_path, parked, selection):
    from outerloop.launchlog import append_submitted

    parked = replace(parked, stage={"candidate_sha": "old-sha"})
    save_record(tmp_path, parked, 11)
    request(tmp_path, "one")
    apply(tmp_path, parked, "")
    directory = tmp_path / "runs/one"
    launch = SimpleNamespace(name="probe", why="", minutes=10, array=1, concurrency=1, jobs=None)
    launches = cast(Any, (launch,))
    append_submitted(directory, sleep=1, launches=launches, job_ids=["5"], at=12, commit="old-sha")
    rows = [json.loads(line) for line in (directory / "launches.jsonl").read_text().splitlines()]
    assert rows[-1]["author"]["model"] == parked.author_model


def test_pending_candidate_credit(tmp_path, parked, selection):
    from outerloop.provenance import producing_author

    parked = replace(parked, stage={"candidate_sha": "old-sha"})
    save_record(tmp_path, parked, 11)
    request(tmp_path, "one")
    rebound = apply(tmp_path, parked, "")
    assert producing_author(tmp_path / "runs/one", "old-sha")["model"] == parked.author_model
    assert producing_author(tmp_path / "runs/one", "new-sha")["model"] == rebound.author_model


@pytest.mark.parametrize("endpoint", [False, True])
def test_wake_entry_consumes_before_endpoint_check(
    tmp_path, parked, selection, monkeypatch, endpoint
):
    from outerloop import attempt
    from outerloop.runstate import acquire_lease, read_lease

    if not endpoint:
        overrides = json.loads(os.environ["OUTERLOOP_AUTHOR_OVERRIDES"])
        overrides["owner/repo"]["model"] = "claude-new-model"
        monkeypatch.setenv("OUTERLOOP_AUTHOR_OVERRIDES", json.dumps(overrides))
    save_record(tmp_path, replace(parked, stage={"phase": "author-sleep"}), 11)
    request(tmp_path, "one")
    assert acquire_lease(tmp_path, "one", "wake:1", "1", 12)
    monkeypatch.setenv("SLURM_JOB_ID", "1")
    image = tmp_path / "image.sif"
    image.touch()
    seen: dict[str, Any] = {}

    def build(key, spec, **kwargs):
        seen.update(kwargs, key=key, spec=spec)
        return Mock()

    monkeypatch.setattr(attempt, "build_harness", build)
    monkeypatch.setattr(attempt, "arm_sigterm_containment", lambda: None)
    monkeypatch.setattr(
        attempt,
        "resume_run",
        lambda *a, **k: attempt.AttemptOutcome(
            run_id="one",
            outcome="parked",
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "attempt",
            "--resume",
            "one",
            "--run-root",
            str(tmp_path),
            "--image",
            str(image),
            "--key-file",
            "old-key",
            "--pat-file",
            str(tmp_path / "key"),
            "--panel",
            "",
        ],
    )
    assert attempt.main() == 0  # The old endpoint profile does not even exist.
    assert seen["model"] == ("served-model[endpoint=onprem]" if endpoint else "claude-new-model")
    assert seen["key"] == "secret"
    assert seen["spec"].budget.max_turns == 250
    assert seen["spec"].budget.walltime_s == 180 * 60
    assert read_lease(tmp_path, "one") is None
    assert not requested(tmp_path, "one")
    manual_key = tmp_path / "manual-key"
    manual_key.write_text("manual-secret")
    manual_key.chmod(0o600)
    import sys

    monkeypatch.setattr("sys.argv", [*sys.argv, "--key-file", str(manual_key)])
    assert attempt.main() == 0
    assert seen["key"] == ("secret" if endpoint else "manual-secret")


def test_inflight_lease_prevents_sweep_rebind(tmp_path, parked, selection):
    from outerloop.runstate import acquire_lease
    from outerloop.tick import _sweep_one

    request(tmp_path, "one")
    assert acquire_lease(tmp_path, "one", "wake:1", "1", 90)
    compute = Mock()
    compute.status.return_value = "RUNNING"
    wake = Mock()
    _sweep_one(tmp_path, compute, Mock(), 100, 60, 60, False, parked, "holder", wake, [], [], [])
    wake.assert_not_called()
    assert load_record(tmp_path, "one").author_model == parked.author_model
    assert requested(tmp_path, "one")


def test_rebound_judges_use_fleet(tmp_path, parked, selection, monkeypatch):
    from types import SimpleNamespace

    from outerloop.attempt import _panel_lenses_from_args

    selection("codex")
    request(tmp_path, "one")
    rebound = apply(tmp_path, parked, "image.sif")
    resolved = Mock(return_value=())
    monkeypatch.setattr("outerloop.panel.resolve_lenses", resolved)
    _panel_lenses_from_args(
        SimpleNamespace(
            panel="verify,review",
            author_backend=rebound.author_backend,
            model=rebound.author_model,
            author_overridden=rebound.author_overridden,
            key_file=rebound.author_key_file,
            panel_key_file="",
        )
    )
    resolved.assert_called_once_with("verify,review", "claude", "claude-fleet")


def test_board_exposes_ordered_authors(tmp_path, parked, selection):
    from outerloop.climbboard import collect_status

    request(tmp_path, "one")
    rebound = apply(tmp_path, parked, "")
    status = collect_status(tmp_path, "owner/repo", 100, records=[rebound])
    assert status["runs"][0]["author_history"] == rebound.author_history


@pytest.mark.parametrize("unexpected", [False, True])
def test_pending_rebind_wake_refunds_its_attempt(
    tmp_path, parked, selection, monkeypatch, unexpected, caplog
):
    from outerloop import attempt
    from outerloop.runstate import acquire_lease, read_lease

    save_record(tmp_path, replace(parked, wake_attempts=2), 11)
    if unexpected:
        monkeypatch.setattr("outerloop.rebind.apply", Mock(side_effect=RuntimeError("unexpected")))
    (tmp_path / "key").unlink()  # the new author cannot be used yet
    request(tmp_path, "one")
    assert acquire_lease(tmp_path, "one", "wake:1", "1", 12)
    monkeypatch.setenv("SLURM_JOB_ID", "1")  # this job holds the lease
    image = tmp_path / "image.sif"
    image.touch()
    monkeypatch.setattr(attempt, "resume_run", Mock(side_effect=AssertionError("leg started")))
    argv = ["attempt", "--resume", "one", "--run-root", str(tmp_path), "--image", str(image)]
    monkeypatch.setattr("sys.argv", argv)
    assert attempt.main() == 0
    assert load_record(tmp_path, "one").wake_attempts == 1  # the sweep's bump is refunded
    assert requested(tmp_path, "one")
    assert read_lease(tmp_path, "one") is None
    pending = json.loads((tmp_path / "runs/one/rebind.json").read_text())
    assert pending["failures"] == 1
    if unexpected:
        assert pending["last_error"] == "unexpected"
        assert "rebind application failed" in caplog.text


@pytest.mark.parametrize("normal", ["endpoint", "deadline", "stuck"])
def test_failed_rebind_returns_to_normal_sweeps(tmp_path, parked, selection, monkeypatch, normal):
    from outerloop import attempt
    from outerloop.rebind import request_status
    from outerloop.runstate import acquire_lease, read_lease
    from outerloop.tick import MAX_WAKE_ATTEMPTS, _sweep_one

    record = replace(
        parked,
        author_model="old-model" if normal != "endpoint" else parked.author_model,
        stage={},
        deadline=50,
        wake_attempts=MAX_WAKE_ATTEMPTS if normal == "stuck" else 0,
    )
    save_record(tmp_path, record, 11)
    monkeypatch.setenv("OUTERLOOP_ENDPOINT_RETIRED_URL_FILE", str(tmp_path / "missing"))
    monkeypatch.setenv("OUTERLOOP_ENDPOINT_RETIRED_API", "anthropic")
    monkeypatch.setenv("OUTERLOOP_ENDPOINT_RETIRED_MODEL", "old-model")
    retired_key = tmp_path / "retired-key"
    retired_key.write_text("secret")
    retired_key.chmod(0o600)
    monkeypatch.setenv("OUTERLOOP_ENDPOINT_RETIRED_KEY_FILE", str(retired_key))
    (tmp_path / "key").unlink()
    request(tmp_path, "one")
    monkeypatch.setenv("SLURM_JOB_ID", "1")
    image = tmp_path / "image.sif"
    image.touch()
    monkeypatch.setattr(
        "sys.argv",
        [
            "attempt",
            "--resume",
            "one",
            "--run-root",
            str(tmp_path),
            "--image",
            str(image),
        ],
    )
    finish = Mock()
    monkeypatch.setattr(attempt, "finish_run", finish)
    wake = Mock()
    deferred: list[str] = []
    stuck: list[str] = []

    def sweep():
        _sweep_one(
            tmp_path,
            Mock(),
            Mock(),
            100,
            60,
            60,
            False,
            load_record(tmp_path, "one"),
            "holder",
            wake,
            deferred,
            [],
            stuck,
        )

    for count in range(1, 4):
        sweep()
        assert wake.call_args.args[1] == "operator rebind"
        current = load_record(tmp_path, "one")
        save_record(tmp_path, replace(current, wake_attempts=current.wake_attempts + 1), 12)
        assert acquire_lease(tmp_path, "one", "wake:1", "1", 12)
        assert attempt.main() == 0
        assert read_lease(tmp_path, "one") is None
        assert load_record(tmp_path, "one").wake_attempts == record.wake_attempts
        pending = request_status(tmp_path, "one")
        assert pending is not None
        assert pending["failures"] == count
        assert pending["status"] == ("failed" if count == 3 else "pending")
        assert pending["last_error"]
        assert requested(tmp_path, "one") == (count < 3)
    assert wake.call_count == 3
    wake.reset_mock()
    for _ in range(2):
        sweep()
    if normal == "endpoint":
        assert deferred == ["one", "one"]
        wake.assert_not_called()
    elif normal == "stuck":
        assert stuck == ["one", "one"]
        assert finish.call_count == 2
        wake.assert_not_called()
    else:
        assert wake.call_count == 2
        assert all(call.args[1] != "operator rebind" for call in wake.call_args_list)
    assert apply(tmp_path, load_record(tmp_path, "one"), "") == load_record(tmp_path, "one")
    assert request_status(tmp_path, "one") == pending


def test_cancel_pending_only(tmp_path, parked, capsys):
    args = ["rebind", "one", "--root", str(tmp_path), "--cancel"]
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2
    request(tmp_path, "one")
    assert main(args) == 0
    assert "rebind cancelled" in capsys.readouterr().out
    assert not (tmp_path / "runs/one/rebind.json").exists()
    assert load_record(tmp_path, "one") == parked
    with pytest.raises(SystemExit):
        main(args)


def test_failed_request_status_and_replacement(tmp_path, parked, selection, capsys):
    from outerloop.rebind import request_status

    (tmp_path / "key").unlink()
    request(tmp_path, "one", "first")
    first = request_status(tmp_path, "one")
    for count in range(1, 4):
        apply(tmp_path, parked, "")
        pending = request_status(tmp_path, "one")
        assert pending is not None
        assert main(["status", "--root", str(tmp_path)]) == 0
        output = capsys.readouterr().out
        assert f"rebind={'failed' if count == 3 else 'pending'}" in output
        assert f"failures={count}" in output
        assert pending["last_error"] in output
        assert main(["status", "--root", str(tmp_path), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["runs"][0]["rebind"] == pending
    with pytest.raises(SystemExit):
        main(["rebind", "one", "--root", str(tmp_path), "--cancel"])
    assert main(["rebind", "one", "--root", str(tmp_path), "--note", "retry"]) == 0
    replacement = request_status(tmp_path, "one")
    assert replacement is not None and first is not None
    assert replacement["id"] != first["id"]
    assert replacement["note"] == "retry"
    assert replacement["failures"] == 0
    assert replacement["last_error"] == ""
    assert requested(tmp_path, "one")
    (tmp_path / "key").write_text("secret")
    (tmp_path / "key").chmod(0o600)
    assert apply(tmp_path, parked, "").author_model == "served-model[endpoint=onprem]"
    assert request_status(tmp_path, "one") is None


def test_older_candidate_credit_after_new_candidate(tmp_path, parked, selection):
    from outerloop.attempt import _clear_stage, _park_run
    from outerloop.dispatch import write_eval_job
    from outerloop.orchestrator import RunParked

    def seal(record, sha):
        _park_run(
            tmp_path,
            record,
            RunParked(
                phase="candidate",
                base_sha="",
                candidate_sha=sha,
                afterany="afterany:1",
                seed=1,
                suite_seed=2,
            ),
            "ref",
            None,
            100,
        )
        return load_record(tmp_path, "one")

    first = seal(parked, "old-sha")
    request(tmp_path, "one")
    rebound = apply(tmp_path, first, "")
    rebound = _clear_stage(rebound, tmp_path)
    save_record(tmp_path, rebound, 101)
    second = seal(rebound, "new-sha")
    directory = tmp_path / "runs/one"
    for sha, model in [("old-sha", parked.author_model), ("new-sha", rebound.author_model)]:
        write_eval_job(
            directory, sha, repo_root=directory / "ws", snapshot_sha=sha, command="true", image=""
        )
        data = json.loads((directory / f"eval-{sha}/provenance.json").read_text())
        assert data["author"]["model"] == model
    # Re-sealing an earlier candidate also preserves its original producer.
    third = seal(second, "old-sha")
    assert third.stage["candidate_author"]["model"] == parked.author_model


def test_board_author_chain_retains_override():
    import shutil
    import subprocess

    from outerloop.climbboard import render_html

    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to evaluate the board's JavaScript")
    html = render_html("owner/repo", {}, {})
    # Locate the author expression, rather than the preceding PR link label.
    expression = html[html.index("a.textContent = ((r.author_history") :].split(";", 1)[0]
    rows = [
        {
            "author_backend": "claude",
            "author_model": "new",
            "author_overridden": True,
            "author_history": [
                {"backend": "claude", "model": "old"},
                {"backend": "claude", "model": "new"},
            ],
        },
        {"author_backend": "claude", "author_model": "new", "author_overridden": True},
    ]
    script = (
        f"for (const r of {json.dumps(rows)}) {{ const a = {{}}; "
        f"{expression}; console.log(a.textContent); }}"
    )
    result = subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
    assert result.stdout.splitlines() == [
        "claude / old → claude / new (override)",
        "claude / new (override)",
    ]


@pytest.mark.parametrize("kind", ["open", "merged", "ended"])
@pytest.mark.parametrize("interrupt", ["none", "before-save", "after-save"])
def test_v021_rebind_retry(tmp_path, rc1_record, selection, monkeypatch, kind, interrupt):
    from pathlib import Path

    import outerloop.rebind as module
    from outerloop.provenance import producing_author

    directory, source = rc1_record(kind)
    raw = json.loads((directory / "state.json").read_text())
    assert not {"author_history", "author_rebind_id"} & raw.keys()
    assert not {"candidate_author", "candidate_authors"} & raw["stage"].keys()
    monkeypatch.setattr(
        "outerloop.github.contract_at",
        lambda *a: (
            """
benchmarks: [{name: tsp, command: echo, metric: score, direction: max}]
budgets: {gpu_hours_per_run: 1, runs_per_week: 5}
scope: {allowed: [src/]}
roadmap: README.md
"""
        ),
    )
    record = load_record(tmp_path, "one")
    before = (directory / "state.json").read_bytes()
    assert apply(tmp_path, record, "") == record  # absent request is a no-op
    (directory / "rebind.json").write_bytes((source / "rebind.json").read_bytes())
    if kind == "ended":
        for _ in range(2):
            assert apply(tmp_path, record, "") == record
            assert (directory / "state.json").read_bytes() == before
        return
    if interrupt != "none":
        original_save, original_unlink = module._save_record, Path.unlink

        def fail_save(*args):
            raise KeyboardInterrupt

        def fail_unlink(path, *args, **kwargs):
            if path.name == "rebind.json":
                raise KeyboardInterrupt
            return original_unlink(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(
                module, "_save_record", fail_save if interrupt == "before-save" else original_save
            )
            patch.setattr(Path, "unlink", fail_unlink)
            with pytest.raises(KeyboardInterrupt):
                apply(tmp_path, record, "")
        assert requested(tmp_path, "one")
        if interrupt == "before-save":
            assert (directory / "state.json").read_bytes() == before
    rebound = apply(tmp_path, load_record(tmp_path, "one"), "")
    assert len(rebound.author_history) == 2
    assert rebound.author_rebind_id == "upgrade-request"
    assert rebound.author_history[0]["model"] == "old-model"
    assert rebound.author_history[1]["model"] == "served-model[endpoint=onprem]"
    assert rebound.pr_url == record.pr_url
    assert rebound.resume_session_id == record.resume_session_id
    assert producing_author(directory, "a" * 40)["model"] == "old-model"
    assert producing_author(directory, "c" * 40)["model"] == rebound.author_model
    saved = (directory / "state.json").read_bytes()
    assert apply(tmp_path, rebound, "") == rebound
    assert (directory / "state.json").read_bytes() == saved
    assert not requested(tmp_path, "one")


def test_v021_rebind_eval_and_launch_provenance(tmp_path, rc1_record, selection, monkeypatch):
    import shutil

    from outerloop.dispatch import write_eval_job
    from outerloop.launchlog import append_submitted, read_ledger
    from outerloop.syscall import Launch

    directory, source = rc1_record()
    # Rebinding also has to retain the attribution of a candidate already sealed.
    monkeypatch.setattr(
        "outerloop.github.contract_at",
        lambda *a: (
            """
benchmarks: [{name: tsp, command: echo, metric: score, direction: max}]
budgets: {gpu_hours_per_run: 1, runs_per_week: 5}
scope: {allowed: [src/]}
roadmap: README.md
"""
        ),
    )
    shutil.copytree(source / "eval-provenance", directory / "eval-provenance")
    shutil.copyfile(source / "launches.jsonl", directory / "launches.jsonl")
    legacy_ledger = (directory / "launches.jsonl").read_bytes()
    assert not (directory / "eval-provenance/provenance.json").exists()
    (directory / "rebind.json").write_bytes((source / "rebind.json").read_bytes())
    apply(tmp_path, load_record(tmp_path, "one"), "")
    original = os.replace

    def fail(source, destination):
        if destination.name == "provenance.json":
            raise KeyboardInterrupt
        original(source, destination)

    def write_eval():
        write_eval_job(
            directory,
            "provenance",
            repo_root=directory / "ws",
            snapshot_sha="a" * 40,
            command="true",
            image="",
        )

    with monkeypatch.context() as patch:
        patch.setattr("outerloop.dispatch.os.replace", fail)
        with pytest.raises(KeyboardInterrupt):
            write_eval()
    write_eval()
    provenance = (directory / "eval-provenance/provenance.json").read_bytes()
    assert json.loads(provenance)["author"]["model"] == "old-model"
    write_eval()
    assert (directory / "eval-provenance/provenance.json").read_bytes() == provenance
    launch = (Launch("probe", "true", 1),)
    append_submitted(directory, sleep=1, launches=launch, job_ids=["501"], at=2, commit="a" * 40)
    assert (directory / "launches.jsonl").read_bytes() == legacy_ledger
    import outerloop.launchlog as ledger

    original_append = ledger._append

    def interrupted(*args):
        original_append(*args)
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(ledger, "_append", interrupted)
        with pytest.raises(KeyboardInterrupt):
            append_submitted(
                directory, sleep=2, launches=launch, job_ids=["502"], at=3, commit="a" * 40
            )
    for _ in range(2):
        append_submitted(
            directory, sleep=2, launches=launch, job_ids=["502"], at=3, commit="a" * 40
        )
    rows = read_ledger(directory)
    assert len(rows) == 2 and rows[1]["author"]["model"] == "old-model"
    assert (directory / "launches.jsonl").read_bytes().startswith(legacy_ledger)
