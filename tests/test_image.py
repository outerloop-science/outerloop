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
    monkeypatch.setattr(img, "containment_available", lambda: False)
    assert img.ensure_image(interactive=False, home=tmp_path, fetch=fake_fetch, report=quiet) == ""
    assert fetched == []
    monkeypatch.setattr(img, "containment_available", lambda: True)
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
    monkeypatch.setattr(img, "containment_available", lambda: True)
    lines: list[str] = []

    def failing(url: str, dest: Path, *, report: Any) -> Path:
        raise OSError("no route to host")

    assert (
        img.ensure_image(interactive=False, home=tmp_path, fetch=failing, report=lines.append) == ""
    )
    assert any("download failed" in line for line in lines)
