"""Load local development environment values without overriding process configuration."""

from pathlib import Path

from dotenv import load_dotenv


def load_environment(path: Path = Path(".env")) -> bool:
    """Load `.env` deterministically; an existing process variable always wins."""
    return load_dotenv(dotenv_path=path, override=False)
