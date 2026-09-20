"""Expose deliberate fixture updates; forbid live services and silent golden replacement.

Goldens are never hand-edited. Updating after a failure requires an intentional change recorded
in the commit message. Historical schema snapshots/manifests are never updated by this option.
"""

from collections.abc import Callable
from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-goldens",
        action="store_true",
        default=False,
        help="Explicitly rewrite current artifact goldens; report each file and byte delta.",
    )


@pytest.fixture
def golden_file(request: pytest.FixtureRequest) -> Callable[[Path, bytes], bytes]:
    def check(path: Path, expected: bytes) -> bytes:
        previous = path.read_bytes() if path.exists() else b""
        if request.config.getoption("--update-goldens"):
            path.write_bytes(expected)
            reporter = request.config.pluginmanager.get_plugin("terminalreporter")
            if reporter is not None:
                reporter.write_line(
                    f"GOLDEN REWRITTEN: {path} | {len(previous)} -> {len(expected)} bytes "
                    f"(delta {len(expected) - len(previous):+d})",
                    yellow=True,
                    bold=True,
                )
        else:
            assert previous == expected, f"Golden drift: {path}; review the intentional change."
        return expected if request.config.getoption("--update-goldens") else previous

    return check
