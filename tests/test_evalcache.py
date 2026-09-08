"""The seed cache: warmed by the kernel, wheels only, once per lockfile."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from outerloop.evalcache import LOCK_HASH, lockfile_hash, seed_dir, warm


class _GitHub:
    def __init__(self, files: dict[str, str | None]) -> None:
        self.files = files
        self.asked: list[tuple[str, str, str]] = []

    def get_file_content(self, repo: str, path: str, ref: str) -> str | None:
        self.asked.append((repo, path, ref))
        return self.files.get(path)


class _Runner:
    def __init__(self, returncode: int = 0) -> None:
        self.calls: list[dict] = []
        self.returncode = returncode

    def __call__(self, argv, **kw):
        self.calls.append({"argv": argv, **kw})
        return SimpleNamespace(
            returncode=self.returncode, stdout="", stderr="boom\nresolution failed"
        )


FILES = {"pyproject.toml": "[project]\nname='t'\n", "uv.lock": "version = 1\n"}


def test_warm_runs_uv_with_the_seed_as_cache_and_no_build(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("OUTERLOOP_PAT_FILE", "/keys/pat")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    gh, run = _GitHub(dict(FILES)), _Runner()
    assert warm(tmp_path, "o/r", gh, "main", runner=run, uv="/bin/uv") == "warmed"
    assert gh.asked == [("o/r", "pyproject.toml", "main"), ("o/r", "uv.lock", "main")]
    (call,) = run.calls
    argv = call["argv"]
    assert argv[:3] == ["/bin/uv", "sync", "--frozen"]
    for flag in ("--no-install-project", "--no-build", "--all-extras"):
        assert flag in argv  # wheels only: nothing of the target's ever runs on the tick host
    assert argv[argv.index("--python") + 1] == "3.12"
    seed = seed_dir(tmp_path, "o/r")
    assert seed == tmp_path / "eval-cache" / "o__r"
    # warmed into a fresh directory beside the seed, then swapped in whole
    assert call["env"]["UV_CACHE_DIR"] != str(seed)
    assert call["env"]["UV_CACHE_DIR"].startswith(str(seed.parent / ".o__r.warm-"))
    assert call["env"]["UV_PROJECT_ENVIRONMENT"].startswith(call["cwd"])  # a throwaway env
    assert not Path(call["cwd"]).exists()  # the temp dir is gone
    assert (seed / LOCK_HASH).read_text() == lockfile_hash(FILES)
    assert [p.name for p in seed.parent.iterdir()] == ["o__r"]  # no leftovers beside it
    # the download process sees what it needs and no tick credential
    env = call["env"]
    assert env["HTTPS_PROXY"] == "http://proxy:3128" and "PATH" in env
    assert "GITHUB_TOKEN" not in env and "OUTERLOOP_PAT_FILE" not in env


def test_warm_is_a_no_op_until_the_lockfile_changes(tmp_path: Path) -> None:
    gh, run = _GitHub(dict(FILES)), _Runner()
    assert warm(tmp_path, "o/r", gh, "main", runner=run, uv="/bin/uv") == "warmed"
    assert warm(tmp_path, "o/r", gh, "main", runner=run, uv="/bin/uv") == "unchanged"
    assert len(run.calls) == 1
    stale = seed_dir(tmp_path, "o/r") / "wheels-v1" / "old.whl"
    stale.parent.mkdir()
    stale.write_text("x")
    gh.files["uv.lock"] = "version = 2\n"
    assert warm(tmp_path, "o/r", gh, "main", runner=run, uv="/bin/uv") == "warmed"
    assert len(run.calls) == 2
    # the seed is exactly the new lockfile's cache: nothing from before survives
    assert not stale.exists()
    changed = {**FILES, "uv.lock": "version = 2\n"}
    assert (seed_dir(tmp_path, "o/r") / LOCK_HASH).read_text() == lockfile_hash(changed)


def test_warm_skips_or_fails_loudly_and_records_nothing(tmp_path: Path, monkeypatch) -> None:
    run = _Runner()
    status = warm(tmp_path, "o/r", _GitHub({"pyproject.toml": "x"}), "main", runner=run)
    assert status.startswith("skipped: no uv.lock") and run.calls == []
    monkeypatch.setattr("outerloop.evalcache.shutil.which", lambda name: None)
    status = warm(tmp_path, "o/r", _GitHub(dict(FILES)), "main", runner=run)
    assert status.startswith("skipped: uv is not") and run.calls == []
    failing = _Runner(returncode=2)
    status = warm(tmp_path, "o/r", _GitHub(dict(FILES)), "main", runner=failing, uv="/bin/uv")
    assert status.startswith("failed: uv sync exited 2") and "resolution failed" in status
    assert not (seed_dir(tmp_path, "o/r") / LOCK_HASH).exists()  # the next tick tries again
    assert not any(p.name.startswith(".o__r.warm-") for p in (tmp_path / "eval-cache").iterdir())
