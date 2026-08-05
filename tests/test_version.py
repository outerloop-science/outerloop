from importlib.metadata import version

import autoresearch


def test_installed_metadata_matches_package_version() -> None:
    assert version("autoresearch") == autoresearch.__version__
