"""Control session-scoped test faults over loopback HTTP; forbid automation-package imports."""

from typing import Annotated
from urllib.parse import urlparse

import httpx
import structlog
import typer

from cua.target_app.models import FaultConfiguration, FaultKind, FaultRule

cli = typer.Typer()
structlog.configure(processors=[structlog.processors.JSONRenderer()])
log = structlog.get_logger()


def send(cookie: str, url: str, configuration: FaultConfiguration | None) -> FaultConfiguration:
    """Never log the session cookie or banking values; output only typed fault-control metadata."""
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise typer.BadParameter("Fault control is restricted to loopback HTTP.")
    with httpx.Client(base_url=url, cookies={"cua_session": cookie}, trust_env=False) as client:
        response = (
            client.get("/_test/faults")
            if configuration is None
            else client.post("/_test/faults", json=configuration.model_dump(mode="json"))
        )
        response.raise_for_status()
        return FaultConfiguration.model_validate_json(response.content)


@cli.command("set")
def set_fault(
    kind: FaultKind,
    cookie: Annotated[str, typer.Option(envvar="CUA_TARGET_COOKIE", show_envvar=True)],
    count: int = 1,
    delay_ms: int = 0,
    url: str = "http://127.0.0.1:8099",
) -> None:
    active = send(
        cookie,
        url,
        FaultConfiguration(faults=[FaultRule(kind=kind, remaining=count, delay_ms=delay_ms)]),
    )
    log.info("fixture_faults", active=active.model_dump(mode="json"))


@cli.command()
def show(
    cookie: Annotated[str, typer.Option(envvar="CUA_TARGET_COOKIE")],
    url: str = "http://127.0.0.1:8099",
) -> None:
    log.info("fixture_faults", active=send(cookie, url, None).model_dump(mode="json"))


@cli.command()
def clear(
    cookie: Annotated[str, typer.Option(envvar="CUA_TARGET_COOKIE")],
    url: str = "http://127.0.0.1:8099",
) -> None:
    log.info(
        "fixture_faults", active=send(cookie, url, FaultConfiguration()).model_dump(mode="json")
    )


if __name__ == "__main__":
    cli()
