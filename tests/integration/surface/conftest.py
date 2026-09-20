"""Run isolated local fixture servers and Chromium; forbid external service calls."""

import asyncio
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

import httpx
import pytest

from cua.surface.web import PlaywrightWebSurface, WebConfig
from tests.unit.domain.samples import FixedClock, SequenceIds


@pytest.fixture
async def bank_url() -> AsyncIterator[str]:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [sys.executable, "-m", "cua.target_app", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    try:
        async with asyncio.timeout(15), httpx.AsyncClient(trust_env=False, timeout=0.3) as client:
            while True:
                if process.poll() is not None:
                    pytest.fail("Local fixture server exited")
                try:
                    if (await client.get(url + "/_meta/version")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                with suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.Event().wait(), 0.05)
        yield url
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
async def surface(
    bank_url: str, tmp_path: Path, request: pytest.FixtureRequest
) -> AsyncIterator[PlaywrightWebSurface]:
    tenant = getattr(request, "param", "alpha")
    web = await PlaywrightWebSurface.launch(
        WebConfig(
            user_data_dir=str(tmp_path / "profile"),
            base_url=bank_url,
            tenant_id=tenant,
            action_timeout_ms=300,
        ),
        clock=FixedClock(),
        ids=SequenceIds(),
    )
    try:
        page = web._page
        await page.get_by_role("textbox", name="Username").fill("synthetic")
        await page.get_by_label("Password", exact=True).fill("synthetic")
        await page.get_by_role("button", name="Sign in", exact=True).click()
        workspace = page.frame_locator('iframe[title="Content"]').frame_locator(
            'iframe[title="Member workspace"]'
        )
        await workspace.get_by_role("textbox").wait_for()
        yield web
    finally:
        if web._ceded:
            assert web._token is not None
            await web.resume_control(web._token)
        await web.close()
