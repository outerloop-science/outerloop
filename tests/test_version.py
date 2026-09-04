from importlib.metadata import version

import outerloop


def test_installed_metadata_matches_package_version() -> None:
    assert version("autoresearch") == outerloop.__version__
