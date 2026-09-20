"""Verify deterministic dotenv precedence without exposing credential values."""

import os
from pathlib import Path

from cua.discovery.environment import load_environment


def test_environment_wins_over_dotenv(tmp_path: Path, monkeypatch: object) -> None:
    from pytest import MonkeyPatch

    patch = monkeypatch
    assert isinstance(patch, MonkeyPatch)
    patch.setenv("OPENAI_API_KEY", "process-value")
    dotenv = tmp_path / ".env"
    dotenv.write_text("OPENAI_API_KEY=file-value\nANTHROPIC_API_KEY=file-only\n", encoding="utf-8")
    assert load_environment(dotenv)
    assert os.environ["OPENAI_API_KEY"] == "process-value"
    assert os.environ["ANTHROPIC_API_KEY"] == "file-only"
