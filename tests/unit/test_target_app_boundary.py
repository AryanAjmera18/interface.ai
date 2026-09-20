"""Check the fixture import boundary; never import live services."""

import configparser
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("violation", [False, True])
def test_target_app_import_boundary(tmp_path: Path, violation: bool) -> None:
    config = configparser.ConfigParser()
    config.read(Path(__file__).resolve().parents[2] / ".importlinter")
    section = "importlinter:contract:target-app-boundary"
    for name in tuple(config.sections()):
        if name not in {"importlinter", section}:
            config.remove_section(name)
    config_path = tmp_path / ".importlinter"
    with config_path.open("w", encoding="utf-8") as stream:
        config.write(stream)
    package = tmp_path / "cua"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    for name in ("target_app", "future_package"):
        directory = package / name
        directory.mkdir()
        (directory / "__init__.py").write_text("", encoding="utf-8")
    (package / "target_app" / "internal.py").write_text("", encoding="utf-8")
    (package / "target_app" / "__init__.py").write_text(
        "from cua.target_app import internal\n", encoding="utf-8"
    )
    if violation:
        (package / "future_package" / "nested.py").write_text(
            "from cua.target_app import internal\n", encoding="utf-8"
        )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from importlinter.cli import lint_imports; sys.exit(lint_imports())",
            "--config",
            str(config_path),
            "--no-cache",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == (1 if violation else 0), result.stdout + result.stderr
    assert ("BROKEN" if violation else "KEPT") in result.stdout
