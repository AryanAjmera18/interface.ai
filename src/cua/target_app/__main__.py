"""Run the loopback-only fixture server; forbid automation-package imports."""

import logging

import typer
import uvicorn


def main(port: int = 8099) -> None:
    """Suppress access/error logs so form values and URLs cannot escape through server logging."""
    logging.disable(logging.CRITICAL)
    uvicorn.run(
        "cua.target_app.app:app",
        host="127.0.0.1",
        port=port,
        workers=1,
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":
    typer.run(main)
