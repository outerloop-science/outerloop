"""The agent image on an adopter's machine (outerloop.image)."""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from outerloop import container
from outerloop import image as img
from outerloop.container import ApptainerRuntime, EnrootRuntime


@dataclass(frozen=True)
class _Runtime(ApptainerRuntime):
    """Apptainer whose probe answers `problem`."""

    binary: str = "apptainer"
    problem: str = ""

    def check(self, **kw: Any) -> str:
        return self.problem

    def install_hint(self) -> str:
        return "INSTALL STEPS HERE"


def test_find_image(tmp_path: Path) -> None:
    old = tmp_path / "autoresearch-images" / "agent-py312.sif"
    old.parent.mkdir()
    old.write_text("")  # an old image dir is not looked for
    assert img.find_image(tmp_path) == ""
    new = tmp_path / "outerloop-images" / "agent-py312.sif"
    new.parent.mkdir()
    new.mkdir()
    assert img.find_image(tmp_path) == ""
    new.rmdir()
    new.write_text("")
    assert img.find_image(tmp_path) == str(new)
    # each runtime looks for the image in its own format
    assert img.find_image(tmp_path, EnrootRuntime("enroot")) == ""
    sqsh = new.with_suffix(".sqsh")
    sqsh.write_text("")
    assert img.find_image(tmp_path, EnrootRuntime("enroot")) == str(sqsh)


def test_published_image_per_runtime_and_processor() -> None:
    apptainer, enroot = ApptainerRuntime("apptainer"), EnrootRuntime("enroot")
    assert img.published_name(apptainer, "x86_64") == "agent-py312.sif"
    assert img.published_name(apptainer, "aarch64") == "agent-py312-arm64.sif"
    assert img.published_name(enroot, "x86_64") == "agent-py312.sqsh"
    assert img.published_name(enroot, "arm64") == "agent-py312-arm64.sqsh"
    assert img.published_name(enroot, "ppc64le") == ""
    # the file in the image dir has no processor in its name
    assert img.image_name(apptainer) == "agent-py312.sif"
    assert img.image_name(enroot) == "agent-py312.sqsh"


class _Resp(io.BytesIO):
    def __init__(self, data: bytes, length: int | None) -> None:
        super().__init__(data)
        self.headers = {"Content-Length": str(length)} if length is not None else {}

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _serving(payload: bytes, checksum: str | None = None, *, length: int | None = None) -> Any:
    """An opener serving the image at any URL and its sha256 at <url>.sha256:
    None = the right one, "" = none published, anything else verbatim."""
    digest = checksum if checksum is not None else hashlib.sha256(payload).hexdigest()

    def opener(url: str, timeout: int) -> _Resp:
        if url.endswith(".sha256"):
            if checksum == "":
                raise OSError("404")
            return _Resp(f"{digest}  agent-py312.sif\n".encode(), None)
        return _Resp(payload, len(payload) if length is None else length)

    return opener


def quiet(s: str) -> None:
    pass


def test_download_streams_through_a_part_file(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(img.sys.stdout, "isatty", lambda: False)
    payload = b"x" * (3 * (1 << 20) + 7)
    dest = tmp_path / "outerloop-images" / "agent-py312.sif"
    lines: list[str] = []
    got = img.download_image(
        "https://example/agent.sif", dest, opener=_serving(payload), report=lines.append
    )
    assert got == dest and dest.read_bytes() == payload
    assert not dest.with_name(dest.name + ".part").exists()
    assert lines[0].startswith("downloading https://example/agent.sif")
    # the download drives the progress: every 10% line, then the summary and the checksum
    pct = [line.split("%")[0].strip() for line in lines if "%" in line]
    assert pct == [str(n) for n in range(10, 101, 10)]
    assert any(line.startswith("  downloaded") for line in lines)
    assert "  checksum verified" in lines and lines[-1].startswith("  saved ")


def test_interrupted_download_leaves_nothing_behind(tmp_path: Path) -> None:
    """A read error after some bytes were written (network drop, Ctrl-C) removes
    the .part file too (terra, #305)."""

    class Dropping(_Resp):
        def __init__(self) -> None:
            super().__init__(b"y" * 4096, 1 << 20)
            self.reads = 0

        def read(self, n: int | None = -1) -> bytes:
            self.reads += 1
            if self.reads > 1:
                raise ConnectionResetError("dropped")
            return super().read(n)

    dest = tmp_path / "agent-py312.sif"
    with pytest.raises(ConnectionResetError):
        img.download_image("u", dest, opener=lambda url, timeout: Dropping(), report=quiet)
    assert not dest.exists() and not dest.with_name(dest.name + ".part").exists()


def test_short_download_leaves_nothing_behind(tmp_path: Path) -> None:
    dest = tmp_path / "agent-py312.sif"
    with pytest.raises(OSError, match="short download"):
        img.download_image("u", dest, opener=_serving(b"abc", length=10), report=quiet)
    assert not dest.exists() and not dest.with_name(dest.name + ".part").exists()


def test_download_rejects_a_checksum_mismatch(tmp_path: Path) -> None:
    """The published sha256 gates installation: a replaced image never lands
    where the tick would run it with the bound harness and the run's keys."""
    dest = tmp_path / "agent-py312.sif"
    with pytest.raises(OSError, match="checksum mismatch"):
        img.download_image("u", dest, opener=_serving(b"abc", "0" * 64), report=quiet)
    assert not dest.exists() and not dest.with_name(dest.name + ".part").exists()


def test_download_requires_a_published_checksum(tmp_path: Path) -> None:
    dest = tmp_path / "agent-py312.sif"
    with pytest.raises(OSError, match="no checksum published"):
        img.download_image("u", dest, opener=_serving(b"abc", ""), report=quiet)
    assert not dest.exists() and not dest.with_name(dest.name + ".part").exists()
    with pytest.raises(OSError, match="unreadable checksum"):
        img.download_image("u", dest, opener=_serving(b"abc", "not-a-digest"), report=quiet)
    assert not dest.with_name(dest.name + ".part").exists()


def test_ensure_image_paths(tmp_path: Path) -> None:
    fetched: list[tuple[str, Path]] = []

    def fake_fetch(url: str, dest: Path, *, report: Any) -> Path:
        fetched.append((url, dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("")
        return dest

    working = _Runtime()

    def ensure(runtime: Any = working, **kw: Any) -> str:
        options: dict[str, Any] = {
            "interactive": False,
            "home": tmp_path,
            "fetch": fake_fetch,
            "report": quiet,
        }
        return img.ensure_image(runtime=runtime, machine="x86_64", **(options | kw))

    # nothing to run it with: no download, no image
    assert ensure(_Runtime(problem="apptainer is not installed (not on PATH)")) == ""
    assert fetched == []
    # opted out
    assert ensure(want=False) == ""
    # interactive no
    assert ensure(interactive=True, ask=lambda p: "n") == ""
    assert fetched == []
    # interactive Enter = yes: fetched into the image dir
    got = ensure(interactive=True, ask=lambda p: "")
    assert got == str(tmp_path / "outerloop-images" / "agent-py312.sif")
    assert fetched == [(f"{img.IMAGE_REPO}/agent-py312.sif", Path(got))]
    # found afterwards: returned without another fetch
    assert ensure() == got and len(fetched) == 1


def test_ensure_image_fetches_this_processors_image_in_the_runtimes_format(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(EnrootRuntime, "check", lambda self: "")
    urls: list[str] = []

    def fake_fetch(url: str, dest: Path, *, report: Any) -> Path:
        urls.append(url)
        return dest

    got = img.ensure_image(
        interactive=False,
        home=tmp_path,
        fetch=fake_fetch,
        report=quiet,
        runtime=EnrootRuntime("enroot"),
        machine="aarch64",
    )
    assert urls == [f"{img.IMAGE_REPO}/agent-py312-arm64.sqsh"]
    assert got == str(tmp_path / "outerloop-images" / "agent-py312.sqsh")
    lines: list[str] = []
    assert (
        img.ensure_image(
            interactive=False,
            home=tmp_path / "other",
            fetch=fake_fetch,
            report=lines.append,
            runtime=_Runtime(),
            machine="riscv64",
        )
        == ""
    )
    assert any("no published image for this processor (riscv64)" in line for line in lines)


def test_ensure_image_download_failure_is_a_warning(tmp_path: Path) -> None:
    lines: list[str] = []

    def failing(url: str, dest: Path, *, report: Any) -> Path:
        raise OSError("no route to host")

    assert (
        img.ensure_image(
            interactive=False,
            home=tmp_path,
            fetch=failing,
            report=lines.append,
            runtime=_Runtime(),
            machine="x86_64",
        )
        == ""
    )
    assert any("download failed" in line for line in lines)


def test_ensure_image_probes_the_deployments_runtime(tmp_path: Path, monkeypatch: Any) -> None:
    """Without a runtime handed in, `init` probes the one the deployment jails
    with, run as OUTERLOOP_APPTAINER_BIN."""
    monkeypatch.setenv("OUTERLOOP_APPTAINER_BIN", "/apps/apptainer")
    monkeypatch.setattr(container.sys, "platform", "linux")
    looked_up: list[str] = []
    monkeypatch.setattr(container.shutil, "which", lambda name: looked_up.append(name))
    lines: list[str] = []
    assert img.ensure_image(interactive=False, home=tmp_path, report=lines.append) == ""
    assert looked_up == ["/apps/apptainer"]
    assert any("apptainer is not installed (not on PATH)" in line for line in lines)


def test_ensure_image_explains_why_runs_are_uncontained(tmp_path: Path) -> None:
    lines: list[str] = []
    runtime = _Runtime(problem="apptainer is not installed (not on PATH)")
    assert (
        img.ensure_image(interactive=False, home=tmp_path, report=lines.append, runtime=runtime)
        == ""
    )
    assert any("UNCONTAINED" in line and "not installed" in line for line in lines)
    assert any("INSTALL STEPS HERE" in line for line in lines)


def test_ensure_image_probes_before_accepting_an_existing_image(tmp_path: Path) -> None:
    """An image on disk is not recorded when apptainer cannot run containers
    (terra, #306): a contained run would only fail later. Slurm skips the probe,
    since the login node cannot speak for the compute nodes."""
    existing = tmp_path / "outerloop-images" / "agent-py312.sif"
    existing.parent.mkdir()
    existing.write_text("")
    runtime = _Runtime(problem="apptainer cannot create containers here (x)")
    lines: list[str] = []
    assert (
        img.ensure_image(interactive=False, home=tmp_path, report=lines.append, runtime=runtime)
        == ""
    )
    assert any(
        str(existing) in line and "once the container runtime works" in line for line in lines
    )
    assert img.ensure_image(
        interactive=False, probe=False, home=tmp_path, report=lines.append, runtime=runtime
    ) == str(existing)


def test_progress_reports_ten_percent_steps_off_a_terminal(monkeypatch: Any) -> None:
    monkeypatch.setattr(img.sys.stdout, "isatty", lambda: False)
    lines: list[str] = []
    bar = img._Progress("u", lines.append)
    bar.start(1000)
    for done in (50, 250, 251, 999, 1000):
        bar.update(done)
    bar.finish(1000)
    pct = [line.split("%")[0].strip() for line in lines if "%" in line]
    assert pct == ["10", "20", "30", "40", "50", "60", "70", "80", "90", "100"]
    assert lines[-1].startswith("  downloaded")


def test_progress_draws_a_bar_on_a_terminal(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr(img.sys.stdout, "isatty", lambda: True)
    bar = img._Progress("u", lambda s: None)
    bar.start(4 << 20)
    bar.last_draw = -1.0
    bar.update(2 << 20)
    bar.finish(4 << 20)
    out = capsys.readouterr().out
    assert "[" in out and "50%" in out and "100%" in out and "ETA" in out
