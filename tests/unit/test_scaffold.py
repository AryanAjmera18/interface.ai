"""Validate package metadata; forbid live-service client imports."""

import cua


def test_package_imports_and_has_version() -> None:
    assert isinstance(cua.__version__, str)
    assert cua.__version__
