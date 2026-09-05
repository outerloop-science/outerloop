"""The built distributions carry what the release publishes and nothing else."""

from __future__ import annotations

import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO_ONLY = {".github", "web", ".wrangler"}  # in the repo, never in the sdist


@pytest.fixture(scope="module")
def dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("uv") is None:
        pytest.skip("uv not on PATH")
    out = tmp_path_factory.mktemp("dist")
    subprocess.run(
        ["uv", "build", "--out-dir", str(out)], cwd=ROOT, check=True, capture_output=True
    )
    return out


def test_sdist_excludes_repo_only_dirs(dist: Path) -> None:
    (sdist,) = dist.glob("*.tar.gz")
    with tarfile.open(sdist) as tf:
        top = {m.name.split("/")[1] for m in tf.getmembers() if "/" in m.name}
    assert top & REPO_ONLY == set()
    assert {"src", "tests", "docs", "README.md", "LICENSE", "NOTICE"} <= top


def test_wheel_is_the_package_and_its_licenses(dist: Path) -> None:
    (wheel,) = dist.glob("*.whl")
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        (meta,) = [n for n in names if n.endswith("METADATA")]
        metadata = zf.read(meta).decode()
    assert all(n.startswith(("outerloop/", "outerloop_science-")) for n in names)
    assert "outerloop/cli.py" in names
    assert "Name: outerloop-science" in metadata
    assert "Project-URL: Repository, https://github.com/outerloop-science/outerloop" in metadata
    assert "License-File: LICENSE" in metadata and "License-File: NOTICE" in metadata
    assert (
        "outerloop = outerloop.cli:main"
        in zf.read(meta.replace("METADATA", "entry_points.txt")).decode()
    )
