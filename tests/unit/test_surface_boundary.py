"""Prove Playwright stays inside surface adapters; forbid live service calls."""

import configparser
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("violation", [False, True])
def test_playwright_import_contract(tmp_path: Path, violation: bool) -> None:
    root = Path(__file__).resolve().parents[2]
    config = configparser.ConfigParser()
    config.read(root / ".importlinter")
    section = "importlinter:contract:playwright-boundary"
    sources = config[section]["source_modules"].split()
    actual = {
        f"cua.{p.name}"
        for p in (root / "src/cua").iterdir()
        if p.is_dir() and (p / "__init__.py").exists() and p.name != "surface"
    }
    assert set(sources) == actual, "New package must join the Playwright boundary contract"
    for name in tuple(config.sections()):
        if name not in {"importlinter", section}:
            config.remove_section(name)
    config_path = tmp_path / ".importlinter"
    with config_path.open("w") as stream:
        config.write(stream)
    package = tmp_path / "cua"
    package.mkdir()
    (package / "__init__.py").write_text("")
    for module in [*sources, "cua.surface"]:
        directory = package / module.split(".")[-1]
        directory.mkdir()
        (directory / "__init__.py").write_text("")
    (package / ("catalog" if violation else "surface") / "adapter.py").write_text(
        "from playwright.async_api import Page\n"
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
