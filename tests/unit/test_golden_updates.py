"""Verify explicit golden rewrites are loud and default runs assert; forbid live services."""

from pathlib import Path

import pytest

pytest_plugins = ["pytester"]


@pytest.mark.integration
def test_explicit_update_flag_and_default_assert(pytester: pytest.Pytester) -> None:
    root = Path(__file__).resolve().parents[2]
    pytester.makeconftest((root / "tests/conftest.py").read_text())
    pytester.makepyfile("""
from pathlib import Path
def test_golden(golden_file):
    golden_file(Path("fixture.json"), b"new expected bytes")
""")
    fixture = pytester.path / "fixture.json"
    fixture.write_bytes(b"old")
    rejected = pytester.runpytest_subprocess("-q", "-o", "addopts=")
    rejected.assert_outcomes(failed=1)
    assert fixture.read_bytes() == b"old"
    rewritten = pytester.runpytest_subprocess("-q", "--update-goldens", "-o", "addopts=")
    rewritten.assert_outcomes(passed=1)
    rewritten.stdout.fnmatch_lines(["*GOLDEN REWRITTEN:*fixture.json*3 -> 18 bytes*delta +15*"])
    assert fixture.read_bytes() == b"new expected bytes"
    pytester.runpytest_subprocess("-q", "-o", "addopts=").assert_outcomes(passed=1)
