"""Deployment author experiments retain their route without changing judges."""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from outerloop import attempt
from outerloop.author_overrides import parse_overrides, select_override
from outerloop.runstate import RunRecord, load_record, save_record
from outerloop.tick import ServiceSpec, _climb_author_argv, _panel_preflight_error


@pytest.mark.parametrize("backend", ["claude", "codex", "hermes"])
def test_slot_matching(monkeypatch, backend):
    monkeypatch.setenv(
        "OUTERLOOP_AUTHOR_OVERRIDES",
        json.dumps(
            {
                "owner/repo": {
                    "backend": backend,
                    "model": "served-model[endpoint=onprem]",
                    "slots": ["agent-05"],
                },
                "owner/all": {"backend": backend, "model": "served-model"},
            }
        ),
    )
    selected = select_override("owner/repo", "agent-05")
    assert selected is not None and selected.backend == backend
    assert select_override("owner/repo", "agent-04") is None
    assert select_override("other/repo", "agent-05") is None
    selected = select_override("owner/all", "agent-99")
    assert selected is not None and selected.backend == backend


@pytest.mark.parametrize(
    "value",
    [
        "oops",
        "[]",
        "null",
        '{"repo": {}}',
        '{"owner/repo": []}',
        '{"owner/repo": {"backend": "other", "model": "x"}}',
        '{"owner/repo": {"backend": "claude"}}',
        *[
            json.dumps({"owner/repo": {"backend": "claude", "model": "x", "slots": slots}})
            for slots in cast(
                list[Any],
                [
                    None,
                    [],
                    "agent-01",
                    ["agent-00"],
                    ["agent-1"],
                    ["agent-001"],
                    ["agent-01", "agent-01"],
                    [1],
                    [{}],
                ],
            )
        ],
        '{"owner/repo": {"backend": "claude", "model": "x", "endpoint": "onprem"}}',
    ],
)
def test_invalid(value):
    with pytest.raises(ValueError, match="OUTERLOOP_AUTHOR_OVERRIDES"):
        parse_overrides(value)


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    def key(name, value):
        path = tmp_path / name
        path.write_text(value)
        path.chmod(0o600)
        return str(path)

    image = tmp_path / "image.sif"
    image.touch()
    monkeypatch.setenv("OUTERLOOP_AUTHOR_BACKEND", "claude")
    monkeypatch.setenv("OUTERLOOP_AUTHOR_MODEL", "claude-fleet")
    monkeypatch.delenv("OUTERLOOP_AUTHOR_ENDPOINT", raising=False)
    monkeypatch.setenv("OUTERLOOP_CLAUDE_KEY_FILE", key("fleet-key", "fleet-secret"))
    monkeypatch.setenv("OUTERLOOP_CODEX_KEY_FILE", key("codex-key", "codex-secret"))
    monkeypatch.setenv("OUTERLOOP_HERMES_KEY_FILE", key("hermes-key", "hermes-secret"))
    monkeypatch.setenv("OUTERLOOP_ENDPOINT_ONPREM_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("OUTERLOOP_ENDPOINT_ONPREM_MODEL", "served-model")
    monkeypatch.setenv("OUTERLOOP_ENDPOINT_ONPREM_API", "anthropic,responses,chat")
    monkeypatch.setenv("OUTERLOOP_ENDPOINT_ONPREM_KEY_FILE", key("endpoint-key", "endpoint-secret"))
    monkeypatch.setenv(
        "OUTERLOOP_AUTHOR_OVERRIDES",
        json.dumps(
            {
                "owner/repo": {
                    "backend": "codex",
                    "model": "served-model[endpoint=onprem]",
                    "slots": ["agent-05"],
                }
            }
        ),
    )
    return ServiceSpec(
        account="",
        partition="",
        run_root=tmp_path,
        home=tmp_path,
        target="owner/repo",
        image=str(image),
        panel="verify,review",
        panel_key_file=key("panel-key", "judge-secret"),
    )


def panel_args(spec, **kwargs):
    return SimpleNamespace(
        panel=spec.panel,
        panel_key_file=spec.panel_key_file,
        image=spec.image,
        claude_bin="claude",
        codex_bin="codex",
        **kwargs,
    )


def test_panel_independence(deployment):
    ordinary, secrets = attempt._panel_lenses_from_args(
        panel_args(deployment, author_backend="claude", model="claude-fleet")
    )
    overridden, override_secrets = attempt._panel_lenses_from_args(
        panel_args(
            deployment,
            author_backend="codex",
            model="served-model[endpoint=onprem]",
            author_overridden=True,
        )
    )
    assert [(x.kind, x.harness) for x in ordinary] == [(x.kind, x.harness) for x in overridden]
    assert secrets == override_secrets
    assert _panel_preflight_error(deployment, "agent-05") == ""


@pytest.mark.parametrize("same_path", [False, True])
def test_override_credential_separation(deployment, same_path):
    endpoint_key = deployment.home / "endpoint-key"
    if same_path:
        deployment = replace(deployment, panel_key_file=str(endpoint_key))
    else:
        Path(deployment.panel_key_file).write_text(endpoint_key.read_text())
    assert "role separation" in _panel_preflight_error(deployment, "agent-05")
    assert _panel_preflight_error(deployment, "agent-04") == ""
    with pytest.raises(ValueError, match="role separation"):
        attempt._panel_lenses_from_args(
            panel_args(
                deployment,
                author_backend="codex",
                model="served-model[endpoint=onprem]",
                author_overridden=True,
            )
        )


@pytest.mark.parametrize("backend", ["claude", "codex", "hermes"])
@pytest.mark.parametrize("queued", [False, True])
def test_binding_survives_setting_change(deployment, monkeypatch, backend, queued):
    monkeypatch.setenv(
        "OUTERLOOP_AUTHOR_OVERRIDES",
        json.dumps(
            {
                "owner/repo": {
                    "backend": backend,
                    "model": "served-model[endpoint=onprem]",
                    "slots": ["agent-05"],
                }
            }
        ),
    )
    monkeypatch.setenv("REVIEW_HERMES_REPO", str(deployment.home / "hermes"))
    monkeypatch.setattr("outerloop.hermes_install.hermes_ready", lambda path: True)
    argv = _climb_author_argv(deployment, "agent-05") if queued else []
    if queued:
        monkeypatch.setenv(
            "OUTERLOOP_AUTHOR_OVERRIDES", '{"owner/repo":{"backend":"claude","model":"claude-new"}}'
        )
    seen = {}
    monkeypatch.setattr(
        attempt, "resolve_bot_auth", lambda *a: SimpleNamespace(token=lambda: "bot")
    )
    monkeypatch.setattr(attempt, "_dispatch_settings", lambda *a: None)
    monkeypatch.setattr(attempt, "build_harness", lambda *a, **kw: seen.update(kw) or object())

    real_launch = attempt.live_attempt

    class AfterRecord(BaseException):
        pass

    def stop_before_network(*args, **kwargs):
        raise AfterRecord

    monkeypatch.setattr(attempt.Workspace, "clone", stop_before_network)

    def launch(**kw):
        with pytest.raises(AfterRecord):
            real_launch(**kw)
        record = load_record(deployment.run_root, kw["run_id"])
        record = replace(record, state="parked", stage={"phase": "author-sleep"})
        save_record(deployment.run_root, record, 1)
        seen["record"] = record
        return attempt.AttemptOutcome(run_id=record.run_id, outcome="parked")

    monkeypatch.setattr(attempt, "live_attempt", launch)
    monkeypatch.setattr(
        "sys.argv",
        [
            "attempt",
            "--target",
            "owner/repo",
            "--benchmark",
            "bench",
            "--agent-id",
            "agent-05",
            "--run-root",
            str(deployment.run_root),
            "--image",
            deployment.image,
            "--min-free-gb",
            "0",
            *argv,
        ],
    )
    assert attempt.main() == 0
    assert (seen["backend"], seen["model"]) == (backend, "served-model[endpoint=onprem]")
    record = load_record(deployment.run_root, seen["record"].run_id)
    assert record.author_overridden
    assert attempt.resume_author(record, "claude-new", "claude")[:2] == (
        backend,
        "served-model[endpoint=onprem]",
    )
    monkeypatch.setenv("OUTERLOOP_AUTHOR_OVERRIDES", "{}")
    # Exercise the actual dispatched wake entrypoint, including its harness.
    monkeypatch.setattr(attempt, "_lease_held_by_another_job", lambda *a: "")
    monkeypatch.setattr("outerloop.tick.dispatch_wake_armed", lambda *a: True)
    monkeypatch.setattr(attempt, "_release_own_lease", lambda *a: None)
    monkeypatch.setattr(
        attempt,
        "resume_run",
        lambda *a, **kw: attempt.AttemptOutcome(run_id=record.run_id, outcome="parked"),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "attempt",
            "--resume",
            record.run_id,
            "--run-root",
            str(deployment.run_root),
            "--image",
            deployment.image,
        ],
    )
    seen.pop("backend")
    assert attempt.main() == 0
    assert (seen["backend"], seen["model"]) == (backend, "served-model[endpoint=onprem]")


def test_no_setting_keeps_launch_bytes(deployment, monkeypatch):
    monkeypatch.delenv("OUTERLOOP_AUTHOR_OVERRIDES")
    assert _climb_author_argv(deployment, "agent-05") == []
    monkeypatch.setenv("OUTERLOOP_AUTHOR_OVERRIDES", "{}")
    assert _climb_author_argv(deployment, "agent-05") == []


@pytest.mark.parametrize("state", ["parked", "ended"])
def test_legacy_record_tolerated_idempotently(tmp_path, state):
    data = json.loads(Path("tests/fixtures/author_route_legacy.json").read_text())
    data["state"] = state
    if state == "ended":
        data["ending"] = "stuck"
    directory = tmp_path / "runs" / data["run_id"]
    directory.mkdir(parents=True)
    (directory / "state.json").write_text(json.dumps(data))
    for _ in range(3):  # first read, idempotent pass, interrupted writer retry
        record = load_record(tmp_path, data["run_id"])
        assert not record.author_overridden
        assert attempt.resume_author(record, "other", "claude") == (
            "codex",
            "gpt-5.6-terra",
            "/keys/author",
        )
        (directory / ".state.json.interrupted.tmp").write_text('{"author_overridden":')
        save_record(tmp_path, record, 2)


def test_wake_preflight_uses_bound_credential(deployment, monkeypatch):
    record = RunRecord(
        run_id="bound",
        target=deployment.target,
        task_title="trial",
        state="parked",
        agent_id="agent-05",
        author_backend="codex",
        author_model="served-model[endpoint=onprem]",
        author_overridden=True,
    )
    monkeypatch.delenv("OUTERLOOP_AUTHOR_OVERRIDES")
    Path(deployment.panel_key_file).write_text("endpoint-secret")
    assert _panel_preflight_error(deployment) == ""
    assert "role separation" in _panel_preflight_error(deployment, record=record)


def test_unmatched_submission_is_also_bound(deployment):
    argv = _climb_author_argv(deployment, "agent-04")
    assert "--author-bound" in argv and "--author-overridden" not in argv
    assert argv[argv.index("--model") + 1] == "claude-fleet"
    assert argv[argv.index("--author-backend") + 1] == "claude"


def test_no_override_record_keeps_wire_bytes(tmp_path):
    original = Path("tests/fixtures/author_route_legacy.json").read_text()
    data = json.loads(original)
    record = RunRecord(**data)
    save_record(tmp_path, record, 1.0)
    assert (tmp_path / "runs" / record.run_id / "state.json").read_text() == original


def test_status_exposes_author(deployment):
    from outerloop.climbboard import collect_status

    record = RunRecord(
        run_id="bound",
        target=deployment.target,
        task_title="trial",
        state="parked",
        author_backend="codex",
        author_model="served-model[endpoint=onprem]",
        author_overridden=True,
    )
    status = collect_status(deployment.run_root, deployment.target, 1, records=[record])
    assert status["runs"][0]["author_backend"] == "codex"
    assert status["runs"][0]["author_model"] == "served-model[endpoint=onprem]"
    assert status["runs"][0]["author_overridden"] is True


def test_ci_reviewer_unchanged(deployment, monkeypatch):
    from outerloop.review_agent_cli import resolve_reviewer_harness
    from outerloop.roles import reviewer_spec

    monkeypatch.setenv("REVIEW_BACKEND", "claude")
    monkeypatch.setenv("REVIEW_MODEL", "claude-fleet")
    monkeypatch.setenv("ANTHROPIC_REVIEWER_KEY", "judge-secret")
    overridden = resolve_reviewer_harness(reviewer_spec())
    monkeypatch.delenv("OUTERLOOP_AUTHOR_OVERRIDES")
    assert overridden == resolve_reviewer_harness(reviewer_spec())


def test_parse_once():
    raw = '{"owner/repo":{"backend":"claude","model":"claude-trial"}}'
    first = parse_overrides(raw)
    assert parse_overrides(raw) is first
    with pytest.raises(TypeError):
        first["owner/other"] = first["owner/repo"]  # type: ignore[index]


def test_native_override_ignores_fleet_endpoint(deployment, monkeypatch):
    from outerloop.tick import _author_config_error, _selected_author

    monkeypatch.setenv("OUTERLOOP_AUTHOR_ENDPOINT", "onprem")
    monkeypatch.setenv(
        "OUTERLOOP_AUTHOR_OVERRIDES", '{"owner/repo":{"backend":"codex","model":"gpt-trial"}}'
    )
    assert _selected_author(deployment, "agent-05") == ("codex", "gpt-trial")
    assert _author_config_error(deployment, "agent-05") == ""


def test_validation_names_setting(deployment, monkeypatch):
    import os

    from outerloop.author_overrides import validate_overrides

    monkeypatch.setenv(
        "OUTERLOOP_AUTHOR_OVERRIDES", '{"owner/repo":{"backend":"codex","model":"claude-wrong"}}'
    )
    with pytest.raises(ValueError, match="OUTERLOOP_AUTHOR_OVERRIDES: owner/repo"):
        validate_overrides(os.environ, deployment.image)


def test_endpoint_model_default_is_bound(deployment, monkeypatch):
    monkeypatch.setenv(
        "OUTERLOOP_AUTHOR_OVERRIDES",
        '{"owner/repo":{"backend":"claude","model":"[endpoint=onprem]"}}',
    )
    argv = _climb_author_argv(deployment, "agent-05")
    assert argv[argv.index("--model") + 1] == "served-model[endpoint=onprem]"


def test_a_target_may_list_overrides_for_different_slots(monkeypatch):
    model = "served-model[endpoint=onprem]"
    raw = json.dumps(
        {
            "owner/repo": [
                {"backend": "claude", "model": model, "slots": ["agent-04"]},
                {"backend": "codex", "model": model, "slots": ["agent-03"]},
            ]
        }
    )
    parsed = parse_overrides(raw)["owner/repo"]
    assert [o.backend for o in parsed] == ["claude", "codex"]
    monkeypatch.setenv("OUTERLOOP_AUTHOR_OVERRIDES", raw)
    four, three = (
        select_override("owner/repo", "agent-04"),
        select_override("owner/repo", "agent-03"),
    )
    assert four is not None and four.backend == "claude"
    assert three is not None and three.backend == "codex"
    assert select_override("owner/repo", "agent-01") is None


@pytest.mark.parametrize(
    "entries",
    [
        [],  # an empty list
        [{"backend": "claude", "model": "m"}],  # a list entry must name its slots
        [
            {"backend": "claude", "model": "m", "slots": ["agent-03"]},
            {"backend": "codex", "model": "m", "slots": ["agent-03"]},
        ],  # one slot claimed twice
    ],
)
def test_listed_overrides_are_validated(entries):
    with pytest.raises(ValueError, match=r"^OUTERLOOP_AUTHOR_OVERRIDES:"):
        parse_overrides(json.dumps({"owner/repo": entries}))


def test_startup_validation_uses_the_image_the_tick_runs(monkeypatch):
    # A deployment that leaves OUTERLOOP_IMAGE unset runs codex sessions on the default
    # image; startup validation of a codex override must use that same image, or the
    # tick refuses to start at all.
    import os

    from outerloop import tick
    from outerloop.author_overrides import validate_overrides

    monkeypatch.delenv("OUTERLOOP_IMAGE", raising=False)
    monkeypatch.setenv(
        "OUTERLOOP_AUTHOR_OVERRIDES",
        json.dumps(
            {"owner/repo": {"backend": "codex", "model": "some-codex-model", "slots": ["agent-03"]}}
        ),
    )
    assert tick.startup_image() == tick._default_image()
    with pytest.raises(ValueError, match="requires --image"):
        validate_overrides(os.environ, "")  # the old startup behaviour
    try:
        validate_overrides(os.environ, tick.startup_image())
    except ValueError as exc:
        assert "requires --image" not in str(exc)


def test_tick_startup_validates_with_startup_image():
    # The tick's entry point must validate overrides with the image sessions run with.
    import inspect

    from outerloop import tick

    src = inspect.getsource(tick.main)
    assert "validate_overrides(os.environ, startup_image())" in src


@pytest.mark.parametrize(
    ("env_value", "expected"), [(None, "DEFAULT"), ("", ""), ("/img.sif", "/img.sif")]
)
def test_start_validates_with_the_tick_image(monkeypatch, tmp_path, env_value, expected):
    # outerloop start must validate overrides with exactly the image the launched tick uses:
    # absent -> the default image, explicit empty -> no image, set -> that image.
    import contextlib

    from outerloop import cli, tick

    seen = []
    monkeypatch.setattr(
        "outerloop.author_overrides.validate_overrides", lambda env, image: seen.append(image)
    )
    monkeypatch.setattr(tick, "_default_image", lambda: "DEFAULT")
    if env_value is None:
        monkeypatch.delenv("OUTERLOOP_IMAGE", raising=False)
    else:
        monkeypatch.setenv("OUTERLOOP_IMAGE", env_value)
    env_file = tmp_path / "settings.env"
    env_file.write_text("")
    env_file.chmod(0o600)
    monkeypatch.setattr(cli, "ENV_FILE", env_file)
    monkeypatch.setenv("OUTERLOOP_ENV_FILE", str(env_file))
    with contextlib.suppress(SystemExit):
        cli.main(["start", "--dry-run", "--root", str(tmp_path / "state")])
    assert seen and seen[0] == expected
