"""Endpoint visibility persists across ticks without changing scheduling."""

import json
import logging
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from outerloop.endpoint_wait import recovered, session_url, unavailable, waiting
from outerloop.endpoints import EndpointProfile, EndpointUnavailable
from outerloop.runstate import RunRecord, load_record, save_record


@pytest.fixture
def profile(tmp_path):
    key = tmp_path / "key"
    key.write_text("endpoint-secret")
    key.chmod(0o600)
    return EndpointProfile("local", "", key, "open-model", ("chat",), tmp_path / "address")


def record(root, run_id="one"):
    item = RunRecord(run_id, "owner/repo", "Check endpoint", "parked", stage={"custom": 42})
    save_record(root, item, 1)
    workspace = root / "runs" / run_id / "ws"
    workspace.mkdir(exist_ok=True)
    return workspace


def down(name="local"):
    return EndpointUnavailable("server unavailable", name)


def test_waits_status_and_shared_outage_logs(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    record(tmp_path)
    record(tmp_path, "two")
    for tick in range(100, 120):
        unavailable(tmp_path, "one", down("LOCAL"), tick)
        unavailable(tmp_path, "two", down(), tick + 0.5)
    assert waiting(tmp_path, "one") == {"endpoint": "local", "since": 100}
    assert waiting(tmp_path, "two")["since"] == 100.5
    assert load_record(tmp_path, "one").stage["custom"] == 42
    assert len(caplog.records) == 1
    recovered(tmp_path, "one", "local", 130)
    assert not waiting(tmp_path, "one")
    assert waiting(tmp_path, "two")  # This run has not resumed yet.
    recovered(tmp_path, "two", "LOCAL", 131)
    recovered(tmp_path, "two", "local", 132)
    assert not waiting(tmp_path, "two")
    assert len(caplog.records) == 2
    assert "30.0s" in caplog.records[1].message
    assert "one, two" in caplog.records[1].message
    unavailable(tmp_path, "one", down(), 200)
    assert len(caplog.records) == 3


def test_healthy_session_clears_wait_without_extra_probe(tmp_path, profile, monkeypatch):
    workspace = record(tmp_path)
    profile.url_file.write_text("http://localhost:8000/v1")
    connection = Mock()
    connection.getresponse.return_value.status = 200
    factory = Mock(return_value=connection)
    monkeypatch.setattr("outerloop.endpoints.HTTPConnection", factory)
    original = (tmp_path / "runs/one/state.json").read_bytes()
    assert session_url(profile, workspace) == "http://localhost:8000/v1"
    assert (tmp_path / "runs/one/state.json").read_bytes() == original
    assert not (tmp_path / "endpoint-waits").exists()
    factory.assert_called_once_with("localhost", 8000, timeout=3)
    unavailable(tmp_path, "one", down(), 100)
    session_url(profile, workspace)
    assert not waiting(tmp_path, "one")
    assert connection.request.call_count == 2


@pytest.mark.parametrize("ended", [False, True])
def test_legacy_fixture_repeat_interruption_retry(tmp_path, monkeypatch, ended):
    directory = tmp_path / "runs/legacy-author"
    directory.mkdir(parents=True)
    directory.joinpath("state.json").write_text(
        Path("tests/fixtures/author_route_legacy.json").read_text()
    )
    old = load_record(tmp_path, "legacy-author")
    if ended:
        save_record(tmp_path, replace(old, state="ended", ending="aborted"), 90)
    for _ in range(2):
        assert waiting(tmp_path, old.run_id) == {}
        recovered(tmp_path, old.run_id, "local", 100)
    original = __import__("os").replace

    def interrupted(src, dst):
        if Path(dst).parent.name == "endpoint-waits":
            raise OSError("interrupted before atomic rename")
        original(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr("outerloop.endpoint_wait.os.replace", interrupted)
        with pytest.raises(OSError):
            unavailable(tmp_path, old.run_id, down(), 110)
    for now in (120, 130):
        unavailable(tmp_path, old.run_id, down(), now)
    if ended:
        assert not waiting(tmp_path, old.run_id)
    else:
        assert waiting(tmp_path, old.run_id)["since"] == 110
        recovered(tmp_path, old.run_id, "local", 140)
        assert not waiting(tmp_path, old.run_id)
    assert load_record(tmp_path, old.run_id).author_model == old.author_model


@pytest.mark.parametrize(
    "metadata",
    [
        {"model": "wrong-model"},
        {"model": None},
        {"expires_at": 0},
        {"expires_at": 100},
        {"expires_at": True},
        {"expires_at": "tomorrow"},
        {"expires_at": float("nan")},
        {"expires_at": float("inf")},
        {"expires_at": 10**400},
    ],
)
def test_invalid_metadata_is_unavailable(tmp_path, profile, monkeypatch, metadata):
    profile.url_file.write_text(json.dumps({"url": "http://localhost/v1", **metadata}))
    monkeypatch.setattr("outerloop.endpoints.time.time", lambda: 100)
    with pytest.raises(EndpointUnavailable) as error:
        profile.session_url()
    assert error.value.endpoint == "local"


def test_valid_metadata_and_size_bound(tmp_path, profile, monkeypatch):
    monkeypatch.setattr("outerloop.endpoints.time.time", lambda: 100)
    profile.url_file.write_text(
        json.dumps(
            {
                "url": "http://localhost/v1",
                "model": "open-model",
                "expires_at": 101,
            }
        )
    )
    assert profile.url == "http://localhost/v1"
    profile.url_file.write_text(" " * 65537)
    with pytest.raises(EndpointUnavailable, match="record too large"):
        _ = profile.url


def test_wake_deferral_records_wait_and_refunds_retry(tmp_path, monkeypatch, profile):
    from outerloop.attempt import defer_endpoint_wake

    record(tmp_path)
    current = replace(load_record(tmp_path, "one"), wake_attempts=2)
    save_record(tmp_path, current, 50)
    monkeypatch.setattr("outerloop.attempt.resolve_endpoint", lambda *_: (profile.model, profile))
    monkeypatch.setattr("outerloop.attempt.time.time", lambda: 100)
    assert defer_endpoint_wake(tmp_path, current)
    assert load_record(tmp_path, "one").wake_attempts == 1
    assert waiting(tmp_path, "one") == {"endpoint": "local", "since": 100}


def test_dry_run_and_intake_do_not_write(tmp_path):
    unavailable(None, "one", down(), 100)
    unavailable(tmp_path, "", down(), 100)
    assert list(tmp_path.iterdir()) == []


def test_parked_sweep_uses_shared_wait_across_ticks(tmp_path, profile, monkeypatch, caplog):
    from outerloop.tick import _sweep_one

    monkeypatch.setattr("outerloop.endpoints.resolve_endpoint", lambda *_: (profile.model, profile))
    for run_id in ("one", "two"):
        record(tmp_path, run_id)
        save_record(tmp_path, replace(load_record(tmp_path, run_id), deadline=10), 1)
    wake = Mock()
    for now in range(100, 105):
        deferred: list[str] = []
        for run_id in ("one", "two"):
            _sweep_one(
                tmp_path,
                Mock(),
                Mock(),
                now,
                60,
                60,
                False,
                load_record(tmp_path, run_id),
                "holder",
                wake,
                deferred,
                [],
                [],
            )
        assert deferred == ["one", "two"]
    assert len(caplog.records) == 1
    assert waiting(tmp_path, "one")["since"] == 100
    assert waiting(tmp_path, "two")["since"] == 100
    wake.assert_not_called()


def test_checkpoint_preserves_wait_and_does_not_resurrect_after_recovery(tmp_path):
    from outerloop.attempt import _clear_stage, _park_run
    from outerloop.orchestrator import RunParked

    record(tmp_path)
    stale = load_record(tmp_path, "one")
    unavailable(tmp_path, "one", down(), 100)
    parked = RunParked(
        phase="author-sleep",
        afterany="",
        base_sha="base",
        seed=0,
        suite_seed=0,
        candidate_sha="candidate",
        capacity_wait=True,
    )
    _park_run(tmp_path, stale, parked, "keep", 1, 101, ())
    assert waiting(tmp_path, "one")["since"] == 100
    stale = load_record(tmp_path, "one")
    recovered(tmp_path, "one", "local", 110)
    cleared = _clear_stage(stale, tmp_path)
    save_record(tmp_path, cleared, 111)
    assert not waiting(tmp_path, "one")


def test_intake_preflight_is_transient_without_claiming_run(tmp_path, profile, monkeypatch):
    from outerloop.endpoint_wait import EndpointWaitReason
    from outerloop.tick import _author_config_error

    monkeypatch.setattr("outerloop.tick._selected_author", lambda *_: ("hermes", profile.model))
    monkeypatch.setattr("outerloop.endpoints.resolve_endpoint", lambda *_: (profile.model, profile))
    for _ in range(3):
        assert isinstance(_author_config_error(Mock(image="/opt/agent.sif")), EndpointWaitReason)
    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "endpoint-waits").exists()


def test_resume_after_interrupted_first_outage_write(tmp_path, monkeypatch):
    record(tmp_path)
    original = __import__("os").replace

    def interrupted(src, dst):
        if Path(dst).parent.name == "endpoint-waits":
            raise OSError("interrupted")
        original(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr("outerloop.endpoint_wait.os.replace", interrupted)
        with pytest.raises(OSError):
            unavailable(tmp_path, "one", down(), 100)
    assert waiting(tmp_path, "one")
    recovered(tmp_path, "one", "local", 110)
    assert not waiting(tmp_path, "one")
