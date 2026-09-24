"""Drive a ceded browser for scripted evidence; forbid policy and orchestration imports."""

from typing import Literal

from playwright.async_api import Frame, Page, async_playwright


async def _frame_with(page: Page, role: Literal["button", "link", "textbox"], name: str) -> Frame:
    for frame in page.frames:
        if await frame.get_by_role(role, name=name).count() == 1:
            return frame
    raise RuntimeError(f"Operator could not find {role} {name}")


async def confirm_sub_account(endpoint: str) -> None:
    """Click the reviewed irreversible control through the ceded CDP session."""
    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.connect_over_cdp(endpoint)
        page = browser.contexts[0].pages[0]
        frame = await _frame_with(page, "button", "Confirm sub-account")
        await frame.get_by_role("button", name="Confirm sub-account").click()
    finally:
        await playwright.stop()


async def restore_member_search(endpoint: str) -> None:
    """Return a replay session to the member-search checkpoint through CDP."""
    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.connect_over_cdp(endpoint)
        page = browser.contexts[0].pages[0]
        frame = await _frame_with(page, "link", "Member search")
        await frame.get_by_role("link", name="Member search").click()
    finally:
        await playwright.stop()
