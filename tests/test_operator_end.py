"""Operator requests use the tick's terminal path, including live sessions."""

import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from fakes import RecordingDispatcher
from outerloop.attempt import end_on_request, publish
from outerloop.cli import main
from outerloop.endpoints import EndpointUnavailable
from outerloop.runstate import (
    END_REQUEST_NAME,
    ENDED,
    PARKED,
    RUNNING,
    RunRecord,
    acquire_lease,
    load_record,
    release_lease,
    request_end,
    run_dir,
    save_record,
)
from outerloop.tick import sweep


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr("outerloop.cli.env_file_values", lambda **kwargs: {})
    record = RunRecord(run_id="r1", target="owner/repo", task_title="Try a change", state=PARKED)
    save_record(tmp_path, record, 1)
    return record


def test_command_request_is_idempotent_and_atomic(tmp_path, run, monkeypatch, capsys):
    from outerloop import runstate

    path = run_dir(tmp_path, run.run_id) / END_REQUEST_NAME
    original = runstate.os.replace
    writes = []

    def replace_file(source, destination):
        assert not path.exists()
        assert source.parent == path.parent
        payload = json.loads(source.read_text())
        assert payload["note"] == "endpoint retired"
        assert payload["requested_at"] > 0
        writes.append(destination)
        original(source, destination)

    monkeypatch.setattr(runstate.os, "replace", replace_file)
    args = ["end", "r1", "--root", str(tmp_path), "--note", "endpoint retired"]
    assert main(args) == 0
    before = path.read_bytes()
    assert main([*args[:-1], "another reason"]) == 0
    assert path.read_bytes() == before
    assert writes == [path]
    assert load_record(tmp_path, "r1").state == PARKED
    assert "already requested" in capsys.readouterr().out


@pytest.mark.parametrize("run_id", ["missing", "../r1", ".", ".."])
def test_command_refuses_unknown_ids(tmp_path, run, run_id, capsys):
    assert main(["end", run_id, "--root", str(tmp_path)]) == 2
    assert "unknown run id" in capsys.readouterr().err
    assert not (run_dir(tmp_path, "r1") / END_REQUEST_NAME).exists()


def test_command_refuses_ended_run(tmp_path, run, capsys):
    save_record(tmp_path, replace(run, state=ENDED, ending="operator"), 2)
    assert main(["end", "r1", "--root", str(tmp_path)]) == 2
    assert "already ended" in capsys.readouterr().err


def test_failed_atomic_write_leaves_no_request(tmp_path, run, monkeypatch):
    def fail(*args):
        raise OSError("write failed")

    monkeypatch.setattr("outerloop.runstate.os.replace", fail)
    assert main(["end", "r1", "--root", str(tmp_path)]) == 2
    assert not list(run_dir(tmp_path, "r1").glob("*end-request*"))


@pytest.mark.parametrize("state", [PARKED, RUNNING])
@pytest.mark.parametrize("issue", [0, 12])
def test_tick_operator_ending(tmp_path, run, monkeypatch, state, issue):
    record = replace(run, state=state, issue_number=issue, stage={"launch_afterany": "afterany:7"})
    save_record(tmp_path, record, 2)
    # An unavailable endpoint must never be consulted to end a run.
    dispatcher = RecordingDispatcher()
    dispatch = Mock(side_effect=EndpointUnavailable("unavailable"))
    monkeypatch.setattr(dispatcher, "dispatch", dispatch)
    compute = Mock()
    compute.status.return_value = "PENDING"
    monkeypatch.setattr("outerloop.compute.compute_from_env", lambda: compute)
    github = Mock() if issue else None
    if state == RUNNING:
        assert acquire_lease(tmp_path, "r1", "session", "99", 2)
    assert request_end(tmp_path, "r1", "endpoint retired", 3)
    dry = sweep(tmp_path, compute, dispatcher, 4, github=github, dry_run=True)
    assert not dry.review_ended
    assert load_record(tmp_path, "r1").state == state
    if state == RUNNING:
        # The live session keeps its lease: the tick waits, and the session's
        # publish is refused while the request is pending.
        assert not sweep(tmp_path, compute, dispatcher, 5, github=github).review_ended
        assert load_record(tmp_path, "r1").state == RUNNING
        outcome = publish(
            result=Mock(),
            ws=Mock(),
            workspace=tmp_path,
            run_root=tmp_path,
            run_dir=run_dir(tmp_path, "r1"),
            run_id="r1",
            record=record,
            config=Mock(),
            contract=Mock(),
            github=Mock(),
            now=8,
            secrets=(),
            base_branch="main",
            base_sha="base",
            issue_number=issue,
            line_ref="",
            date="",
        )
        assert outcome.outcome == "publish-refused"
        release_lease(tmp_path, "r1")
    report = sweep(tmp_path, compute, dispatcher, 5, github=github)
    assert report.review_ended == (("r1", "operator"),)
    final = load_record(tmp_path, "r1")
    assert (final.state, final.ending, final.ending_note) == (ENDED, "operator", "endpoint retired")
    assert "operator: endpoint retired" in (run_dir(tmp_path, "r1") / "report.md").read_text()
    from outerloop.climbboard import collect_rows

    row = collect_rows(tmp_path, "owner/repo")["benchmark"][0]
    assert (row.outcome, row.note) == ("operator", "endpoint retired")
    dispatch.assert_not_called()
    compute.cancel.assert_called_once_with("7")
    if github:
        from outerloop.intake import RELEASE_MARKER

        assert RELEASE_MARKER in github.comment.call_args.args[2]
        github.get_pull_request.assert_not_called()
    # The request stays as an audit record, but neither a later tick nor a stale
    # session record can act on it twice.
    assert (run_dir(tmp_path, "r1") / END_REQUEST_NAME).exists()
    assert not sweep(tmp_path, compute, dispatcher, 6, github=github).review_ended
    assert end_on_request(tmp_path, record, github, 7) == ""
    compute.cancel.assert_called_once()
    if github:
        github.comment.assert_called_once()
    if state == RUNNING:
        save_record(tmp_path, record, 9)
        assert load_record(tmp_path, "r1") == final


def test_legacy_record_without_request_is_unchanged(tmp_path, run):
    assert end_on_request(tmp_path, run, None, 3) == ""
    assert load_record(tmp_path, "r1").state == PARKED


@pytest.mark.parametrize("note", [None, 3, ["x"]])
def test_non_text_note_is_recorded_empty(tmp_path, run, note):
    (run_dir(tmp_path, "r1") / END_REQUEST_NAME).write_text(json.dumps({"note": note}))
    assert end_on_request(tmp_path, load_record(tmp_path, "r1"), None, 10.0) == "operator"
    assert load_record(tmp_path, "r1").ending_note == ""


def test_damaged_request_still_ends_the_run_once(tmp_path, run):
    (run_dir(tmp_path, "r1") / END_REQUEST_NAME).write_text("{not json")
    assert end_on_request(tmp_path, load_record(tmp_path, "r1"), None, 10.0) == "operator"
    final = load_record(tmp_path, "r1")
    assert (final.state, final.ending, final.ending_note) == (ENDED, "operator", "")
    assert end_on_request(tmp_path, final, None, 11.0) == ""


def test_issue_run_waits_for_github_before_ending(tmp_path, run):
    save_record(tmp_path, replace(run, issue_number=12), 2)
    assert request_end(tmp_path, "r1", "", 3)
    assert end_on_request(tmp_path, load_record(tmp_path, "r1"), None, 4) == ""
    assert load_record(tmp_path, "r1").state == PARKED
    github = Mock()
    assert end_on_request(tmp_path, load_record(tmp_path, "r1"), github, 5) == "operator"
    github.comment.assert_called_once()


def test_queued_wake_exits_without_a_leg(tmp_path, run, monkeypatch):
    from outerloop import attempt

    assert request_end(tmp_path, "r1", "", 3)
    image = tmp_path / "image.sif"
    image.touch()
    argv = ["climb", "--resume", "r1", "--run-root", str(tmp_path), "--image", str(image)]
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setattr(attempt, "_lease_held_by_another_job", lambda *args: "")
    released = []
    monkeypatch.setattr(attempt, "_release_own_lease", lambda *args: released.append(args))
    monkeypatch.setattr(attempt, "resume_run", Mock(side_effect=AssertionError("leg started")))
    monkeypatch.setattr(attempt, "build_harness", Mock(side_effect=AssertionError("harness built")))
    assert attempt.main() == 0
    assert released == [(str(tmp_path), "r1")] or released == [(tmp_path, "r1")]
    assert load_record(tmp_path, "r1").state == PARKED


def test_issue_run_without_github_is_never_woken_while_it_waits(tmp_path, run, monkeypatch):
    record = replace(run, issue_number=12, experiment_job_id="7", deadline=10.0)
    save_record(tmp_path, record, 2)
    assert request_end(tmp_path, "r1", "retired", 3)
    compute = Mock()
    compute.status.return_value = "COMPLETED"
    monkeypatch.setattr("outerloop.compute.compute_from_env", lambda: compute)
    dispatcher = RecordingDispatcher()
    dispatch = Mock()
    monkeypatch.setattr(dispatcher, "dispatch", dispatch)
    for now in (100, 200, 300, 400):
        assert not sweep(tmp_path, compute, dispatcher, now, grace_s=0).review_ended
    dispatch.assert_not_called()
    waiting = load_record(tmp_path, "r1")
    assert (waiting.state, waiting.wake_attempts) == (PARKED, 0)
    github = Mock()
    report = sweep(tmp_path, compute, dispatcher, 500, grace_s=0, github=github)
    assert report.review_ended == (("r1", "operator"),)
    assert load_record(tmp_path, "r1").ending_note == "retired"


def test_dead_session_of_a_requested_run_ends_as_operator(tmp_path, run, monkeypatch):
    save_record(tmp_path, replace(run, state=RUNNING, run_job_id="55", issue_number=12), 2)
    assert acquire_lease(tmp_path, "r1", "session", "55", 2)
    assert request_end(tmp_path, "r1", "retired", 3)
    compute = Mock()
    compute.status.return_value = "FAILED"
    monkeypatch.setattr("outerloop.compute.compute_from_env", lambda: compute)
    dispatcher = RecordingDispatcher()
    sweep(tmp_path, compute, dispatcher, 10, grace_s=1)  # stamps the kill
    # Without GitHub the issue cannot be told, so the dead run waits.
    assert "r1" not in sweep(tmp_path, compute, dispatcher, 20, grace_s=1).running_ended
    assert load_record(tmp_path, "r1").state == RUNNING
    github = Mock()
    report = sweep(tmp_path, compute, dispatcher, 30, grace_s=1, github=github)
    assert "r1" in report.running_ended
    final = load_record(tmp_path, "r1")
    assert (final.state, final.ending, final.ending_note) == (ENDED, "operator", "retired")
    from outerloop.intake import RELEASE_MARKER

    assert RELEASE_MARKER in github.comment.call_args.args[2]
    assert "operator: retired" in (run_dir(tmp_path, "r1") / "report.md").read_text()


def test_request_during_wake_setup_still_prevents_the_leg(tmp_path, run, monkeypatch):
    from types import SimpleNamespace

    from outerloop import attempt

    image = tmp_path / "image.sif"
    image.touch()
    argv = ["climb", "--resume", "r1", "--run-root", str(tmp_path), "--image", str(image)]
    argv += ["--panel-skip", "test"]
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setenv("OUTERLOOP_AUTHOR_BACKEND", "claude")
    monkeypatch.setenv("OUTERLOOP_CLAUDE_MODEL", "claude-native")
    monkeypatch.setattr(attempt, "_lease_held_by_another_job", lambda *args: "")
    monkeypatch.setattr(attempt, "resolve_bot_auth", lambda *a: SimpleNamespace(token=lambda: "t"))
    monkeypatch.setattr(attempt, "model_key", lambda *args: "key")

    def limits(*args):
        request_end(tmp_path, "r1", "late", 4)  # lands during setup, after the first check
        return None

    monkeypatch.setattr(attempt, "bound_limits", limits)
    monkeypatch.setattr(attempt, "build_harness", lambda *a, **k: object())
    released = []
    monkeypatch.setattr(attempt, "_release_own_lease", lambda *args: released.append(args))
    monkeypatch.setattr(attempt, "resume_run", Mock(side_effect=AssertionError("leg started")))
    assert attempt.main() == 0
    assert released
