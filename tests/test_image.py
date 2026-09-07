"""The agent image on an adopter's machine (outerloop.image)."""

from __future__ import annotations

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


def test_download_streams_through_a_part_file(tmp_path: Path) -> None:
    payload = b"x" * (3 * (1 << 20) + 7)
    dest = tmp_path / "outerloop-images" / "agent-py312.sif"
    lines: list[str] = []
    got = img.download_image(
        "https://example/agent.sif",
        dest,
        opener=lambda url, timeout: _Resp(payload, len(payload)),
        report=lines.append,
    )
    assert got == dest and dest.read_bytes() == payload
    assert not dest.with_name(dest.name + ".part").exists()
    assert lines[0].startswith("downloading https://example/agent.sif")


def test_short_download_leaves_nothing_behind(tmp_path: Path) -> None:
    dest = tmp_path / "agent-py312.sif"
    with pytest.raises(OSError, match="short download"):
        img.download_image(
            "u", dest, opener=lambda url, timeout: _Resp(b"abc", 10), report=lambda s: None
        )
    assert not dest.exists() and not dest.with_name(dest.name + ".part").exists()


def test_ensure_image_paths(tmp_path: Path, monkeypatch: Any) -> None:
    fetched: list[Path] = []

    def fake_fetch(url: str, dest: Path, *, report: Any) -> Path:
        fetched.append(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("")
        return dest

    def quiet(s: str) -> None:
        pass

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
