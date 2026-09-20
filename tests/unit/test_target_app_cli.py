"""Validate local fixture entrypoints; forbid real network calls."""

from functools import partial

import httpx
import pytest
import typer
from typer.testing import CliRunner

from cua.target_app import __main__ as launcher
from cua.target_app import faults
from cua.target_app.models import FaultConfiguration, FaultKind


@pytest.mark.parametrize("command", ["set", "show", "clear"])
def test_fault_cli(command: str, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["cookie"] == "cua_session=synthetic-session-cookie"
        if request.method == "POST":
            config = FaultConfiguration.model_validate_json(request.content)
            if command == "set":
                assert config.faults[0].kind == FaultKind.SLOW_LOAD
                assert config.faults[0].remaining == 2
                assert config.faults[0].delay_ms == 15
            else:
                assert not config.faults
        else:
            config = FaultConfiguration()
        return httpx.Response(200, json=config.model_dump(mode="json"))

    monkeypatch.setattr(
        faults.httpx, "Client", partial(httpx.Client, transport=httpx.MockTransport(handler))
    )
    arguments = [command]
    if command == "set":
        arguments += ["slow_load", "--count", "2", "--delay-ms", "15"]
    arguments += ["--cookie", "synthetic-session-cookie"]
    result = CliRunner().invoke(faults.cli, arguments)
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert '"event": "fixture_faults"' in result.output
    assert "synthetic-session-cookie" not in result.output


def test_cli_rejects_remote_url() -> None:
    with pytest.raises(typer.BadParameter, match="loopback"):
        faults.send("unused", "https://example.com", None)


def test_cli_propagates_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(422))
    monkeypatch.setattr(faults.httpx, "Client", partial(httpx.Client, transport=transport))
    with pytest.raises(httpx.HTTPStatusError):
        faults.send("synthetic", "http://127.0.0.1:8099", None)


def test_server_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    ports: list[int] = []

    def run(
        app: str, *, host: str, port: int, workers: int, access_log: bool, log_config: None
    ) -> None:
        assert app == "cua.target_app.app:app"
        assert host == "127.0.0.1" and workers == 1
        assert not access_log and log_config is None
        ports.append(port)

    monkeypatch.setattr(launcher.uvicorn, "run", run)
    monkeypatch.setattr(launcher.logging, "disable", lambda level: None)
    launcher.main()
    assert ports == [8099]
