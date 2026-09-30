"""Scheduler identity and legacy production compatibility."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from outerloop import cli
from outerloop.instance import job_name
from outerloop.runstate import acquire_tick_lease, release_tick_lease, tick_lease_holder
from outerloop.tick import write_heartbeat


@pytest.mark.parametrize("base", ["outerloop-resident", "outerloop-tick"])
def test_legacy_settings_without_root_keep_names(tmp_path, monkeypatch, base):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OUTERLOOP_ENV_FILE", raising=False)
    config = tmp_path / ".config/outerloop/.env"
    config.parent.mkdir(parents=True)
    # Legacy settings: the root was supplied at start and inherited by the job.
    config.write_text("OUTERLOOP_TARGET=owner/repo\n")
    config.chmod(0o600)
    monkeypatch.setenv("OUTERLOOP_ROOT", str(tmp_path / "production"))
    for _ in range(3):
        assert job_name(base) == base
    run = Mock(return_value=Mock(returncode=0, stdout="12\n"))
    monkeypatch.setattr(cli.subprocess, "run", run)
    assert cli._resident_jobs() == ["12"]
    assert "--name=outerloop-resident" in run.call_args.args[0]


def test_selected_settings_have_stable_names(tmp_path, monkeypatch):
    import hashlib
    import os

    monkeypatch.setenv("HOME", str(tmp_path))
    selected = tmp_path / "sandbox.env"
    selected.touch()
    alias = tmp_path / "alias.env"
    alias.symlink_to(selected)
    digest = hashlib.sha256(os.fsencode(selected.resolve())).hexdigest()[:12]
    for path in [selected, alias, selected]:
        monkeypatch.setenv("OUTERLOOP_ENV_FILE", str(path))
        for root in ["first", "second"]:
            monkeypatch.setenv("OUTERLOOP_ROOT", str(tmp_path / root))
            for base in ["outerloop-resident", "outerloop-tick"]:
                assert job_name(base) == f"{base}-{digest}"
    monkeypatch.setenv("OUTERLOOP_ENV_FILE", str(tmp_path / "other.env"))
    assert job_name() != f"outerloop-resident-{digest}"


@pytest.mark.parametrize("alias", [False, True])
def test_resolved_default_settings_keep_names(tmp_path, monkeypatch, alias):
    monkeypatch.setenv("HOME", str(tmp_path))
    default = tmp_path / ".config/outerloop/.env"
    default.parent.mkdir(parents=True)
    default.touch()
    selected = default.parent / ".." / "outerloop" / ".env"
    if alias:
        selected = tmp_path / "alias.env"
        selected.symlink_to(default)
    monkeypatch.setenv("OUTERLOOP_ENV_FILE", str(selected))
    monkeypatch.setenv("OUTERLOOP_ROOT", str(tmp_path / "other-root"))
    assert job_name() == "outerloop-resident"
    assert job_name("outerloop-tick") == "outerloop-tick"


def test_lease_and_heartbeat_are_per_root(tmp_path: Path):
    first, second = tmp_path / "first", tmp_path / "second"
    a = acquire_tick_lease(first, "host:1", 100, 30, settle_s=0)
    b = acquire_tick_lease(second, "host:2", 100, 30, settle_s=0)
    try:
        assert tick_lease_holder(first, 100, 30, "host") == "host:1"
        assert tick_lease_holder(second, 100, 30, "host") == "host:2"
        write_heartbeat(first, 100)
        assert not (second / "heartbeat.json").exists()
        with pytest.raises(RuntimeError):
            acquire_tick_lease(first, "host:3", 100, 30, settle_s=0)
    finally:
        release_tick_lease(a)
        release_tick_lease(b)


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_settings_selector_is_default_instance(monkeypatch, value):
    # An empty selector must never fail the chain: it means the default instance.
    monkeypatch.setenv("OUTERLOOP_ENV_FILE", value)
    assert job_name() == "outerloop-resident"
    assert job_name("outerloop-tick") == "outerloop-tick"


def test_empty_settings_selector_reads_default_file(monkeypatch, tmp_path):
    from outerloop import paths

    monkeypatch.setenv("OUTERLOOP_ENV_FILE", "")
    assert paths.env_file(tmp_path / "default.env") == tmp_path / "default.env"
