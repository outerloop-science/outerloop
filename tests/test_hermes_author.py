"""Author selection, wake routing and legacy state for Hermes."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from outerloop import attempt
from outerloop.attempt import author_config_error, resolve_author_key_file, resume_author
from outerloop.runstate import RECORD_NAME, RunRecord, load_record, run_dir, save_record
from outerloop.tick import ServiceSpec, _author_config_error


@pytest.fixture
def hermes_config(monkeypatch, tmp_path):
    monkeypatch.setattr("outerloop.hermes_install.hermes_ready", lambda repo: True)
    monkeypatch.setenv("REVIEW_HERMES_REPO", str(tmp_path / "hermes"))
    monkeypatch.setenv("REVIEW_HERMES_PROVIDER", "openai")
    monkeypatch.setenv("OUTERLOOP_AUTHOR_BACKEND", "hermes")
    monkeypatch.setenv("OUTERLOOP_AUTHOR_MODEL", "gpt-native")
    monkeypatch.delenv("OUTERLOOP_AUTHOR_ENDPOINT", raising=False)


def test_native_config_and_preflight(hermes_config, monkeypatch, tmp_path):
    spec = ServiceSpec(
        target="o/r", account="", partition="", run_root=tmp_path, image="image.sif", home=tmp_path
    )
    assert _author_config_error(spec) == ""
    assert "requires --image" in author_config_error("hermes", "gpt-native", "")
    assert "AUTHOR_MODEL" in author_config_error("hermes", "", "image.sif")
    monkeypatch.setenv("REVIEW_HERMES_PROVIDER", "unknown")
    assert "PROVIDER" in _author_config_error(spec)
    monkeypatch.setenv("REVIEW_HERMES_PROVIDER", "openrouter")
    assert author_config_error("hermes", "org/model", "image.sif") == ""
    monkeypatch.setenv("OUTERLOOP_HERMES_RESUME_MAX_CHARS", "0")
    assert "positive integer" in _author_config_error(spec)
    monkeypatch.delenv("OUTERLOOP_HERMES_RESUME_MAX_CHARS")
    monkeypatch.setattr("outerloop.hermes_install.hermes_ready", lambda repo: False)
    assert "pinned source and runtime" in _author_config_error(spec)


def test_hermes_key_and_parked_route(hermes_config, monkeypatch, tmp_path):
    key = tmp_path / "hermes_key"
    monkeypatch.setenv("OUTERLOOP_HERMES_KEY_FILE", str(key))
    monkeypatch.setenv("OUTERLOOP_CLAUDE_KEY_FILE", "/unrelated")
    assert resolve_author_key_file("hermes") == str(key)
    record = RunRecord(
        "hermes-run", "o/r", "task", "parked", author_backend="hermes", author_model="gpt-native"
    )
    for now in (1, 2, 3):
        save_record(tmp_path, record, now=now)
        record = load_record(tmp_path, record.run_id)
        assert resume_author(record, "claude-fleet", "claude") == ("hermes", "gpt-native", str(key))


@pytest.mark.parametrize("wake", [False, True])
def test_attempt_builds_effective_hermes_author(hermes_config, monkeypatch, tmp_path, wake):
    image = tmp_path / "image.sif"
    image.touch()
    argv = ["climb", "--run-root", str(tmp_path), "--image", str(image), "--panel-skip", "test"]
    if wake:
        save_record(
            tmp_path,
            RunRecord(
                "h",
                "o/r",
                "task",
                "parked",
                author_backend="hermes",
                author_model="gpt-native",
                stage={"phase": "author-sleep"},
            ),
            now=1,
        )
        argv += ["--resume", "h"]
        monkeypatch.setenv("OUTERLOOP_AUTHOR_BACKEND", "codex")
        monkeypatch.setenv("OUTERLOOP_AUTHOR_MODEL", "other-model")
    else:
        argv += ["--author-backend", "hermes", "--target", "o/r", "--benchmark", "b"]
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setattr(
        attempt, "resolve_bot_auth", lambda *a: SimpleNamespace(token=lambda: "bot")
    )
    monkeypatch.setattr(attempt, "model_key", lambda *a: "author-key")
    monkeypatch.setattr(attempt, "_lease_held_by_another_job", lambda *a: "")
    monkeypatch.setattr("outerloop.tick.dispatch_wake_armed", lambda *a: True)
    monkeypatch.setattr(
        "outerloop.disk.check_mount", lambda *a, **kw: SimpleNamespace(ok=lambda: True)
    )
    monkeypatch.setattr(attempt, "arm_self_deadline", lambda *a: 0)
    seen = {}

    class Built(Exception):
        pass

    def build(*args, **kwargs):
        seen.update(kwargs)
        raise Built

    monkeypatch.setattr(attempt, "build_harness", build)
    with pytest.raises(Built):
        attempt.main()
    assert seen["backend"] == "hermes"
    assert seen["model"] == "gpt-native"
    assert seen["hermes_provider"] == "openai"
    assert seen["hermes_repo"] == tmp_path / "hermes"
    assert seen["container_image"] == str(image)


@pytest.mark.parametrize("fixture", ["author_route_legacy.json", "author_route_missing_route.json"])
def test_legacy_author_records_remain_readable(fixture, tmp_path):
    data = json.loads((Path(__file__).parent / "fixtures" / fixture).read_text())
    directory = run_dir(tmp_path, data["run_id"])
    directory.mkdir(parents=True)
    path = directory / RECORD_NAME
    original = json.dumps(data)
    path.write_text(original)
    first = load_record(tmp_path, data["run_id"])
    from outerloop.harness import resume_config_block

    assert resume_config_block(first.stage) == ""  # legacy missing field is unblocked
    for _ in range(2):
        assert load_record(tmp_path, data["run_id"]) == first
    # Retry after an interrupted writer left an uninstalled temporary file.
    (directory / "state.json.tmp").write_text("{")
    assert load_record(tmp_path, data["run_id"]) == first
    assert path.read_text() == original


@pytest.mark.parametrize("judge", ["codex", "hermes"])
def test_panel_rejects_hermes_author_key(hermes_config, monkeypatch, tmp_path, judge):
    from outerloop.attempt import _judge_lens_key
    from outerloop.tick import _panel_preflight_error

    key = tmp_path / "author_key"
    key.write_text("secret")
    key.chmod(0o600)
    image = tmp_path / "image.sif"
    image.touch()
    env = f"OUTERLOOP_PANEL_{judge.upper()}_KEY_FILE"
    monkeypatch.setenv("OUTERLOOP_HERMES_KEY_FILE", str(key))
    monkeypatch.setenv(env, str(key))
    with pytest.raises(ValueError, match="hermes author key"):
        _judge_lens_key(
            backend=judge,
            key_file_env=env,
            author_backend="hermes",
            claude_panel_path=tmp_path / "panel",
            image=str(image),
        )
    spec = ServiceSpec(
        target="o/r",
        account="",
        partition="",
        run_root=tmp_path,
        image=str(image),
        home=tmp_path,
        panel=f"review:{judge}:gpt-native",
    )
    assert "hermes author key" in _panel_preflight_error(spec)


@pytest.mark.parametrize("judge", ["claude", "codex", "hermes"])
@pytest.mark.parametrize("duplicate", [False, True])
def test_effective_explicit_author_key_separation(
    hermes_config, monkeypatch, tmp_path, judge, duplicate
):
    author = tmp_path / "explicit-author"
    author.write_text("same-credential")
    author.chmod(0o600)
    key = tmp_path / "judge" if duplicate else author
    key.write_text("same-credential")
    key.chmod(0o600)
    monkeypatch.setenv(f"OUTERLOOP_PANEL_{judge.upper()}_KEY_FILE", str(key))
    monkeypatch.setattr(attempt, "build_harness", lambda *a, **k: pytest.fail("built session"))
    args = SimpleNamespace(
        panel=f"review:{judge}:{'claude-test' if judge == 'claude' else 'gpt-native'}",
        panel_key_file=str(key) if judge == "claude" else str(tmp_path / "unused"),
        author_backend="hermes",
        model="gpt-native",
        key_file=str(author),
        image="image.sif",
        claude_bin="claude",
        codex_bin="codex",
    )
    with pytest.raises(ValueError, match="role separation"):
        attempt._panel_lenses_from_args(args)


def test_invalid_resume_budget_does_not_break_judge_construction(monkeypatch):
    from outerloop.harness import HermesHarness

    monkeypatch.setenv("OUTERLOOP_HERMES_RESUME_MAX_CHARS", "invalid")
    assert HermesHarness(api_key="judge", repo_dir=Path("/opt/hermes")).resume_max_chars is None
