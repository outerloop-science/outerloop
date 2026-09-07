"""The agent image on an adopter's machine (outerloop.image)."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

import pytest

from outerloop import image as img


def test_find_image_prefers_the_new_location(tmp_path: Path) -> None:
    assert img.find_image(tmp_path) == ""
    old = tmp_path / "autoresearch-images" / "agent-py312.sif"
    old.parent.mkdir()
    old.write_text("")
    assert img.find_image(tmp_path) == str(old)
    new = tmp_path / "outerloop-images" / "agent-py312.sif"
    new.parent.mkdir()
    new.write_text("")
    assert img.find_image(tmp_path) == str(new)


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


def test_download_streams_through_a_part_file(tmp_path: Path) -> None:
    payload = b"x" * (3 * (1 << 20) + 7)
    dest = tmp_path / "outerloop-images" / "agent-py312.sif"
    lines: list[str] = []
    got = img.download_image(
        "https://example/agent.sif", dest, opener=_serving(payload), report=lines.append
    )
    assert got == dest and dest.read_bytes() == payload
    assert not dest.with_name(dest.name + ".part").exists()
    assert lines[0].startswith("downloading https://example/agent.sif")


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


def test_ensure_image_paths(tmp_path: Path, monkeypatch: Any) -> None:
    fetched: list[Path] = []

    def fake_fetch(url: str, dest: Path, *, report: Any) -> Path:
        fetched.append(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("")
        return dest

    # nothing to run it with: no download, no image
    monkeypatch.setattr(
        img, "containment_check", lambda: "apptainer is not installed (not on PATH)"
    )
    assert img.ensure_image(interactive=False, home=tmp_path, fetch=fake_fetch, report=quiet) == ""
    assert fetched == []
    monkeypatch.setattr(img, "containment_check", lambda: "")
    # opted out
    assert (
        img.ensure_image(
            interactive=False, want=False, home=tmp_path, fetch=fake_fetch, report=quiet
        )
        == ""
    )
    # interactive no
    assert (
        img.ensure_image(
            interactive=True, home=tmp_path, ask=lambda p: "n", fetch=fake_fetch, report=quiet
        )
        == ""
    )
    assert fetched == []
    # interactive Enter = yes: fetched into the image dir
    got = img.ensure_image(
        interactive=True, home=tmp_path, ask=lambda p: "", fetch=fake_fetch, report=quiet
    )
    assert got == str(tmp_path / "outerloop-images" / "agent-py312.sif") and len(fetched) == 1
    # found afterwards: returned without another fetch
    assert img.ensure_image(interactive=False, home=tmp_path, fetch=fake_fetch, report=quiet) == got
    assert len(fetched) == 1


def test_ensure_image_download_failure_is_a_warning(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(img, "containment_check", lambda: "")
    lines: list[str] = []

    def failing(url: str, dest: Path, *, report: Any) -> Path:
        raise OSError("no route to host")

    assert (
        img.ensure_image(interactive=False, home=tmp_path, fetch=failing, report=lines.append) == ""
    )
    assert any("download failed" in line for line in lines)


class _Proc:
    def __init__(self, rc: int, err: str = "") -> None:
        self.returncode = rc
        self.stderr = err


def test_containment_check_runs_a_container_not_just_which(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Being on PATH is not enough (Ubuntu 24.04 strips capabilities from a
    hand-installed apptainer's user namespace): the probe execs a container
    and reads the failure."""
    monkeypatch.setattr(img.sys, "platform", "linux")
    monkeypatch.setattr(img.shutil, "which", lambda name: None)
    assert "not installed" in img.containment_check()
    monkeypatch.setattr(img.shutil, "which", lambda name: "/usr/bin/apptainer")
    seen: list[list[str]] = []

    def working(argv: list[str], **kw: Any) -> _Proc:
        seen.append(argv)
        return _Proc(0)

    assert img.containment_check(runner=working) == ""
    assert seen and seen[0][0] == "/usr/bin/apptainer" and seen[0][1] == "exec"
    broken = _Proc(
        255,
        "ERROR  : Could not write info to setgroups: Permission denied\n"
        "ERROR  : Error while waiting event for user namespace mappings: no event received",
    )
    problem = img.containment_check(runner=lambda argv, **kw: broken)
    assert "cannot create containers" in problem and "user namespace" in problem
    other = _Proc(255, "FATAL:   container creation failed: something else")
    assert "apptainer exec failed" in img.containment_check(runner=lambda argv, **kw: other)
    monkeypatch.setattr(img.sys, "platform", "darwin")
    assert "macOS" in img.containment_check()


def test_install_hint_names_the_distribution(monkeypatch: Any) -> None:
    monkeypatch.setattr(img.sys, "platform", "linux")
    monkeypatch.setattr(img, "_linux_flavor", lambda: ("ubuntu", "24.04"))
    for machine in ("x86_64", "aarch64"):  # the PPA covers both (terra, #306)
        monkeypatch.setattr(img.platform, "machine", lambda m=machine: m)
        hint = img.install_hint()
        assert "ppa:apptainer/ppa" in hint and "AppArmor" in hint and "setgroups" in hint
        assert "outerloop init --force" in hint and ".deb" not in hint
    monkeypatch.setattr(img, "_linux_flavor", lambda: ("debian", "12"))
    monkeypatch.setattr(img.platform, "machine", lambda: "x86_64")
    assert "apptainer_1.5.3_amd64.deb" in img.install_hint()
    monkeypatch.setattr(img.platform, "machine", lambda: "aarch64")
    odd = img.install_hint()
    assert "No Apptainer Debian package" in odd and "aarch64" in odd and ".deb" not in odd
    monkeypatch.setattr(img, "_linux_flavor", lambda: ("fedora", "40"))
    assert "dnf install" in img.install_hint()
    monkeypatch.setattr(img, "_linux_flavor", lambda: ("arch", ""))
    assert "install-unprivileged.sh" in img.install_hint()
    monkeypatch.setattr(img.sys, "platform", "darwin")
    assert "macOS" in img.install_hint()


def test_ensure_image_explains_why_runs_are_uncontained(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(
        img, "containment_check", lambda: "apptainer is not installed (not on PATH)"
    )
    monkeypatch.setattr(img, "install_hint", lambda: "INSTALL STEPS HERE")
    lines: list[str] = []
    assert img.ensure_image(interactive=False, home=tmp_path, report=lines.append) == ""
    assert any("UNCONTAINED" in line and "not installed" in line for line in lines)
    assert any("INSTALL STEPS HERE" in line for line in lines)


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
