import re

import outerloop


def test_package_imports() -> None:
    assert hasattr(outerloop, "__version__")


def test_version_is_pep440() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+(\.(dev|post)\d+)?([ab]|rc\d+)?", outerloop.__version__)
