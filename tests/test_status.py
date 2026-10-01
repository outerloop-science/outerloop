import json
from dataclasses import replace
from pathlib import Path

import pytest

from outerloop.cli import main
from outerloop.endpoint_wait import recovered, unavailable
from outerloop.endpoints import EndpointUnavailable
from outerloop.runstate import RunRecord, save_record


def test_status_wait_and_outage_read_only(tmp_path, capsys, monkeypatch):
    run = RunRecord(
        "one",
        "owner/repo",
        "Research",
        "parked",
        author_backend="hermes",
        author_model="open-model[endpoint=local]",
        author_overridden=True,
        stage={"phase": "candidate", "gpu_hours_used": 2.5},
    )
    save_record(tmp_path, run, 1)
    save_record(tmp_path, replace(run, run_id="ended", state="ended", ending="aborted"), 1)
    workspace = tmp_path / "runs/one/ws"
    workspace.mkdir()
    (workspace / ".outerloop.yaml").write_text(
        """
benchmarks:
  - name: benchmark
    command: python evaluate.py
    metric: score
    direction: max
budgets:
  gpu_hours_per_run: 8
  runs_per_week: 10
scope:
  allowed: [src/]
roadmap: README.md
"""
    )
    unavailable(tmp_path, "one", EndpointUnavailable("down", "local"), 100)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    def forbidden(*args, **kwargs):
        pytest.fail("status must not call subprocesses or the network")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("socket.socket", forbidden)
    assert main(["status", "--root", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert len(text.splitlines()) == 2
    assert "one target=owner/repo agent=agent-01 state=parked phase=candidate" in text
    assert "author=hermes/open-model[endpoint=local] overridden=yes GPU-hours=2.5/8.0" in text
    assert "waiting for endpoint local since 1970-01-01T00:01:40+00:00" in text
    assert "Endpoint outage local since 1970-01-01T00:01:40+00:00; waiting runs: one" in text
    assert main(["status", "--root", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "runs": [
            {
                "run_id": "one",
                "target": "owner/repo",
                "agent": "agent-01",
                "state": "parked",
                "phase": "candidate",
                "author_backend": "hermes",
                "author_model": "open-model[endpoint=local]",
                "author_overridden": True,
                "gpu_hours_used": 2.5,
                "gpu_hours_budget": 8.0,
                "endpoint_wait": {"endpoint": "local", "since": 100},
            }
        ],
        "outages": [{"endpoint": "local", "since": 100, "waiting_runs": ["one"]}],
    }
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    recovered(tmp_path, "one", "local", 110)
    assert main(["status", "--root", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outages"] == []
    assert payload["runs"][0]["endpoint_wait"] is None


@pytest.mark.parametrize("exists", [True, False])
def test_status_empty_root(tmp_path, capsys, exists):
    root = tmp_path / "state"
    if exists:
        root.mkdir()
    assert main(["status", "--root", str(root)]) == 0
    assert capsys.readouterr().out == "No active runs.\nNo endpoint outages.\n"
    assert main(["status", "--root", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"runs": [], "outages": []}
    assert root.exists() == exists
    assert list(root.glob("*")) == []


def test_status_legacy_and_root_precedence(tmp_path, monkeypatch, capsys):
    root = tmp_path / "configured"
    directory = root / "runs/legacy-author"
    directory.mkdir(parents=True)
    directory.joinpath("state.json").write_bytes(
        Path("tests/fixtures/author_route_legacy.json").read_bytes()
    )
    monkeypatch.delenv("OUTERLOOP_ROOT", raising=False)
    monkeypatch.setattr("outerloop.cli.env_file_values", lambda **_: {"OUTERLOOP_ROOT": str(root)})
    assert main(["status", "--json"]) == 0
    run = json.loads(capsys.readouterr().out)["runs"][0]
    assert run["endpoint_wait"] is None
    assert run["gpu_hours_budget"] is None
    assert run["author_backend"] == "codex"
    assert run["author_overridden"] is False
    monkeypatch.setenv("OUTERLOOP_ROOT", str(tmp_path / "empty"))
    assert main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["runs"] == []
    assert main(["status", "--root", str(root), "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["runs"]) == 1
    monkeypatch.delenv("OUTERLOOP_ROOT")
    monkeypatch.setattr("outerloop.cli.env_file_values", lambda **_: {})
    monkeypatch.setattr("outerloop.cli.DEFAULT_LOCAL_ROOT", root)
    assert main(["status", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["runs"]) == 1
