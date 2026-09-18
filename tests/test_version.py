from importlib.metadata import version

import outerloop


def test_installed_metadata_matches_package_version() -> None:
    assert version("outerloop-science") == outerloop.__version__


def test_release_version_is_pinned() -> None:
    # the release PR bumps this literal; a forgotten bump fails here
    assert outerloop.__version__ == "0.2.0"
