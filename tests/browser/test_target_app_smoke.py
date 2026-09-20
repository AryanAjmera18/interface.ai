"""Check actual Chromium accessibility trees; forbid external-service traffic."""

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import expect, sync_playwright

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("CUA_BROWSER_SMOKE") != "1",
        reason="Opt in with CUA_BROWSER_SMOKE=1; requires local Chromium.",
    ),
]


@pytest.fixture(scope="module")
def server_url() -> Iterator[str]:
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
        with httpx.Client(trust_env=False, timeout=0.5) as client:
            for _ in range(100):
                if process.poll() is not None:
                    pytest.fail("Fixture server exited during startup")
                try:
                    if client.get(url + "/_meta/version").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.05)
            else:
                pytest.fail("Fixture server did not start")
        yield url
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.mark.parametrize("tenant", ["alpha", "beta"])
def test_member_search_accessibility_snapshot(server_url: str, tenant: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(f"{server_url}/t/{tenant}/")
            page.get_by_role("textbox", name="Username").fill("synthetic")
            page.get_by_label("Password", exact=True).fill("synthetic")
            page.get_by_role("button", name="Sign in", exact=True).press("Enter")
            workspace = page.frame_locator('iframe[title="Content"]').frame_locator(
                'iframe[title="Member workspace"]'
            )
            label = "Member ID" if tenant == "alpha" else "Account Holder Number"
            expect(workspace.get_by_role("textbox", name=label, exact=True)).to_be_visible()
            snapshot = workspace.locator("body").aria_snapshot()
            assert 'heading "Member search" [level=1]' in snapshot
            assert f'textbox "{label}"' in snapshot
            assert 'button "Search members"' in snapshot
            label = "Member ID" if tenant == "alpha" else "Account Holder Number"
            expect(workspace.get_by_text(label, exact=True)).to_be_visible()
            workspace.get_by_role("textbox", name=label, exact=True).fill("10001")
            workspace.get_by_role("button", name="Search members", exact=True).press("Space")
            workspace.get_by_role("link", name="View member 10001").click()
            workspace.get_by_role(
                "button",
                name="Open Sub-Account" if tenant == "alpha" else "Add Deposit Product",
                exact=True,
            ).press("Enter")
            workspace.get_by_role("textbox", name="Nickname", exact=True).fill("Holiday")
            workspace.get_by_role("textbox", name="Initial deposit", exact=True).fill("25.50")
            workspace.get_by_role("button", name="Review sub-account", exact=True).click()
            workspace.get_by_role("button", name="Confirm sub-account", exact=True).click()
            if tenant == "beta":
                expect(
                    workspace.get_by_role("dialog", name="Confirm deposit product")
                ).to_be_visible()
                workspace.get_by_role("button", name="Submit deposit product").press("Enter")
            expect(workspace.get_by_role("status")).to_contain_text(f"{tenant.upper()}-000001")
        finally:
            browser.close()
