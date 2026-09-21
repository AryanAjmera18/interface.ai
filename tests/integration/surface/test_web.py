"""Exercise real Chromium through the domain seam; forbid external services."""

import asyncio
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from playwright.async_api import Error, Route, async_playwright

from cua.domain.actions import Click, ReadValue, TypeText
from cua.domain.common import LiteralRef, StringValue
from cua.domain.locators import (
    AxPathCandidate,
    CoordinatesCandidate,
    CoordinatesValue,
    CssCandidate,
    CssValue,
    LabelCandidate,
    LocatorLadder,
    PathValue,
    RoleNameCandidate,
    RoleNameValue,
    ScopedRoleNameCandidate,
    ScopedRoleNameValue,
    StructuralCandidate,
    TextValue,
    VisibleTextCandidate,
)
from cua.domain.names import NameMatcher
from cua.domain.observation import walk_ax
from cua.domain.ports import SettleTimeout, Surface
from cua.domain.predicates import AxTarget
from cua.domain.steps import StepTiming
from cua.policy.models import UrlPattern
from cua.policy.url_matcher import url_matches
from cua.surface.web import ActionBlockedError, ControlCededError, PlaywrightWebSurface
from tests.unit.domain.samples import FixedClock, SequenceIds, ladder

pytestmark = pytest.mark.integration
FRAME = ("Content", "Member workspace")
TIMING = StepTiming(
    settle_strategy="ax_stable", timeout_ms=6000, poll_interval_ms=50, stability_window_ms=100
)


async def test_real_redirect_lands_outside_allowlist(
    surface: PlaywrightWebSurface,
) -> None:
    """Use the real target route; interception avoids making an external network request."""
    requests: list[str] = []
    surface._page.on("request", lambda request: requests.append(request.url))
    with pytest.raises(Error, match="ERR_NAME_NOT_RESOLVED"):
        await surface._page.goto(surface.config.base_url + "/t/alpha/_test/redirect-off-allowlist")
    landed = next(url for url in requests if url.startswith("https://evil.example/"))
    port = int(surface.config.base_url.rsplit(":", 1)[1])
    allowed = UrlPattern(scheme="http", host="127.0.0.1", port=port, path="/t/*/**")
    assert not url_matches(allowed, landed)


def target(role: str, name: str, *, frame: tuple[str, ...] = FRAME) -> LocatorLadder:
    base = ladder().candidates[0].model_dump(exclude={"strategy", "value", "frame_path"})
    return LocatorLadder(
        candidates=(
            RoleNameCandidate(
                value=RoleNameValue(role=role, name_matcher=NameMatcher(value=name)),
                frame_path=frame,
                **base,
            ),
        )
    )


async def detail(web: PlaywrightWebSurface) -> None:
    field = "Member ID" if web.config.tenant_id == "alpha" else "Account Holder Number"
    result = await web.resolve(target("textbox", field))
    assert result.target is not None
    await web.act(
        TypeText(value_ref=LiteralRef(value=StringValue(value="10001"))),
        result.target,
        timing=TIMING,
    )
    result = await web.resolve(target("button", "Search members"))
    assert result.target is not None
    await web.act(Click(), result.target, timing=TIMING)
    result = await web.resolve(target("link", "View member 10001"))
    assert result.target is not None
    await web.act(Click(), result.target, timing=TIMING)


@pytest.mark.parametrize("surface", ["alpha", "beta"], indirect=True)
async def test_observe_frames_names_and_scoped_balance(surface: PlaywrightWebSurface) -> None:
    seam: Surface = surface
    observed = await seam.observe()
    nodes = walk_ax(observed.ax_root)
    label = "Member ID" if surface.config.tenant_id == "alpha" else "Account Holder Number"
    field = next(n for n in nodes if n.role == "textbox")
    assert field.name == label and field.frame_path == FRAME
    assert {f.frame_path for f in observed.frames} == {(), ("Navigation",), ("Content",), FRAME}
    assert observed.screenshot_ref is not None and observed.dom_digest is not None
    assert observed.fingerprint.tenant_id == surface.config.tenant_id
    await detail(surface)
    row = AxTarget(
        role="row", name_matcher=NameMatcher(mode="regex", value=r"\bSavings\b"), frame_path=FRAME
    )
    balance = AxTarget(
        role="cell",
        name_matcher=NameMatcher(mode="regex", value=r"^\$[0-9,.]+$"),
        within=row,
        frame_path=FRAME,
    )
    obs = await seam.observe()
    selected = balance.select(obs)
    assert len(selected) == 1
    # Alpha Savings follows Checking; beta reverses the account data rows.
    assert row.select(obs)[0].node_path[-1] == (3 if surface.config.tenant_id == "alpha" else 2)
    candidate = ScopedRoleNameCandidate(
        **ladder().candidates[0].model_dump(exclude={"strategy", "value"}),
        value=ScopedRoleNameValue(role="cell", name_matcher=balance.name_matcher, ancestor=row),
    )
    resolution = await seam.resolve(LocatorLadder(candidates=(candidate,)))
    assert resolution.index == 0 and resolution.match_count == 1
    result = await seam.act(ReadValue(output_name="savings", attribute="name"), resolution.target)
    assert result.value == selected[0].name
    opened = await seam.resolve(
        target(
            "button",
            "Open Sub-Account" if surface.config.tenant_id == "alpha" else "Add Deposit Product",
        )
    )
    assert opened.target is not None
    await seam.act(Click(), opened.target, timing=TIMING)
    assert any(
        n.role == "textbox" and n.name == "Nickname"
        for n in walk_ax((await seam.observe()).ax_root)
    )


async def test_ladder_misses_and_ambiguity(surface: PlaywrightWebSurface) -> None:
    primary = target("textbox", "Missing").candidates[0]
    wrong = primary.model_copy(update={"frame_path": ("Wrong",)})
    css = CssCandidate(
        **primary.model_dump(exclude={"strategy", "value"}),
        value=CssValue(selector='input[name="member_id"]'),
    )
    result = await surface.resolve(LocatorLadder(candidates=(wrong, primary, css)))
    assert [attempt.outcome for attempt in result.attempts] == [
        "wrong_frame",
        "not_found",
        "matched",
    ]
    assert result.index == 2 and result.degraded and result.degradation_delta > 0
    frame = surface._page.frame(name="workspace")
    assert frame is not None
    await frame.evaluate(
        "document.querySelector('input').after(document.querySelector('input').cloneNode())"
    )
    ambiguous = await surface.resolve(target("textbox", "Member ID"))
    assert ambiguous.target is None and ambiguous.attempts[0].outcome == "ambiguous"
    assert ambiguous.match_count == 2
    first = await surface.resolve(
        target("textbox", "Member ID").model_copy(update={"match_policy": "first_match"})
    )
    assert first.target is not None and first.match_count == 2


async def test_interstitial_blocks_instead_of_not_found(surface: PlaywrightWebSurface) -> None:
    await surface._context.request.post(
        surface.config.base_url + "/_test/faults",
        data={
            "faults": [{"kind": "unexpected_interstitial", "remaining": 1, "delay_ms": 0}],
        },
    )
    frame = surface._page.frame(name="workspace")
    assert frame is not None
    await frame.goto(surface.config.base_url + "/t/alpha/ui/search")
    blocked = await surface.resolve(target("textbox", "Member ID"))
    assert blocked.attempts[0].outcome == "not_actionable"
    dismiss = await surface.resolve(target("button", "Dismiss notice"))
    assert dismiss.target is not None
    await surface.act(Click(), dismiss.target, timing=TIMING)
    assert (await surface.resolve(target("textbox", "Member ID"))).target is not None


@pytest.mark.parametrize("delay", [0, 250, 2000])
async def test_settle_waits_for_slow_responses(surface: PlaywrightWebSurface, delay: int) -> None:
    await surface._context.request.post(
        surface.config.base_url + "/_test/faults",
        data={
            "faults": [{"kind": "slow_load", "remaining": 1, "delay_ms": delay}],
        },
    )
    frame = surface._page.frame(name="workspace")
    assert frame is not None
    started = asyncio.get_running_loop().time()
    navigating = asyncio.create_task(frame.goto(surface.config.base_url + "/t/alpha/ui/search"))
    await surface._page.wait_for_event("request")
    observed = await surface.settle(TIMING)
    await navigating
    assert asyncio.get_running_loop().time() - started >= delay / 1000
    assert observed.hash == (await surface.observe()).hash


async def test_hung_response_has_typed_timeout(surface: PlaywrightWebSurface) -> None:
    released = asyncio.Event()

    async def hang(route: Route) -> None:
        await released.wait()
        await route.fulfill(body="done")

    await surface._page.route("**/hung", hang)
    request = asyncio.create_task(surface._page.evaluate("fetch('/hung')"))
    await surface._page.wait_for_event("request")
    try:
        with pytest.raises(SettleTimeout) as caught:
            await surface.settle(TIMING.model_copy(update={"timeout_ms": 300}))
        assert len(caught.value.detail.last_hashes) == 2
        assert caught.value.detail.pending_requests >= 1
        assert caught.value.detail.diff == ()  # Hung network with unchanged AX is distinguishable.
    finally:
        released.set()
        await request


async def test_real_control_transfer_preserves_session_and_form(
    surface: PlaywrightWebSurface,
) -> None:
    before_cookies = await surface._context.cookies()
    token = await surface.cede_control()
    with pytest.raises(ControlCededError):
        await surface.observe()
    with pytest.raises(ControlCededError):
        await surface.resolve(target("textbox", "Member ID"))
    with pytest.raises(ControlCededError):
        await surface.act(Click())
    with pytest.raises(ControlCededError):
        await surface.resume_control(token.model_copy(update={"page_guid": "wrong-page"}))
    async with async_playwright() as operator:
        browser = await operator.chromium.connect_over_cdp(token.cdp_endpoint)
        page = browser.contexts[0].pages[0]
        frame = page.frame(name="workspace")
        assert frame is not None
        await frame.get_by_role("textbox", name="Member ID").fill("10042")
        assert await browser.contexts[0].cookies() == before_cookies
        # Exiting the operator connection disconnects it, without closing Chromium.
    await surface.resume_control(token)
    after = await surface.observe()
    assert next(n for n in walk_ax(after.ax_root) if n.role == "textbox").value == "10042"
    assert await surface._context.cookies() == before_cookies
    assert (await surface.resolve(target("textbox", "Member ID"))).target is not None


@settings(
    max_examples=4, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    st.text(
        alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ -", min_size=1, max_size=25
    )
)
async def test_live_hash_stability_and_label_drift(
    surface: PlaywrightWebSurface, label: str
) -> None:
    frame = surface._page.frame(name="workspace")
    assert frame is not None
    await frame.locator("input").evaluate("(e, name) => e.setAttribute('aria-label', name)", label)
    before = await surface.observe()
    repeated = await surface.observe()
    assert before.hash == repeated.hash
    await frame.locator("input").evaluate(
        "(e, name) => e.setAttribute('aria-label', name)", label + "X"
    )
    assert (await surface.observe()).hash != before.hash


async def test_stale_target_is_not_used(surface: PlaywrightWebSurface) -> None:
    resolved = await surface.resolve(target("textbox", "Member ID"))
    frame = surface._page.frame(name="workspace")
    assert frame is not None
    await frame.locator("input").evaluate("e => e.setAttribute('aria-label','Changed')")
    with pytest.raises(ActionBlockedError, match="stale"):
        await surface.act(Click(), resolved.target)
    with pytest.raises(ActionBlockedError, match="requires"):
        await surface.act(Click())


async def test_disabled_and_occluded_controls_are_not_actionable(
    surface: PlaywrightWebSurface,
) -> None:
    frame = surface._page.frame(name="workspace")
    assert frame is not None
    await frame.locator("input").evaluate("e => e.disabled = true")
    result = await surface.resolve(target("textbox", "Member ID"))
    assert result.attempts[0].outcome == "not_actionable"
    await frame.locator("input").evaluate("e => e.disabled = false")
    await frame.evaluate("""() => {
        const cover = document.createElement('div');
        cover.setAttribute('role','dialog'); cover.setAttribute('aria-modal','true');
        cover.style='position:fixed;inset:0;z-index:999;background:white';
        cover.textContent='Blocking notice'; document.body.append(cover);
    }""")
    result = await surface.resolve(target("textbox", "Member ID"))
    assert result.attempts[0].outcome == "not_actionable"


async def test_timeout_carries_changed_tree_diff(surface: PlaywrightWebSurface) -> None:
    frame = surface._page.frame(name="workspace")
    assert frame is not None
    await frame.evaluate("""() => {
        let n = 0;
        window.churn = setInterval(() => document.querySelector('input').setAttribute(
            'aria-label', 'Changing ' + (++n)), 20);
    }""")
    try:
        with pytest.raises(SettleTimeout) as caught:
            await surface.settle(TIMING.model_copy(update={"timeout_ms": 650}))
        assert caught.value.detail.last_hashes[0] != caught.value.detail.last_hashes[1]
        assert any("name" in change.fields for change in caught.value.detail.diff)
    finally:
        await frame.evaluate("clearInterval(window.churn)")


async def test_snapshot_stall_obeys_deadline(
    surface: PlaywrightWebSurface, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cua.domain.observation import Observation

    original = await surface.observe()

    async def blocked_observation(**kwargs: object) -> Observation:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(surface, "_observe", blocked_observation)
    with pytest.raises(SettleTimeout) as caught:
        await surface.settle(TIMING.model_copy(update={"timeout_ms": 60}))
    assert caught.value.detail.last_hashes == (original.hash, original.hash)


async def test_accessible_name_and_description_are_not_inner_text(
    surface: PlaywrightWebSurface,
) -> None:
    frame = surface._page.frame(name="workspace")
    assert frame is not None
    await frame.locator("input").evaluate(
        "e => e.setAttribute('aria-description','Synthetic description')"
    )
    resolved = await surface.resolve(target("textbox", "Member ID"))
    result = await surface.act(ReadValue(output_name="name", attribute="name"), resolved.target)
    assert result.value == "Member ID"
    resolved = await surface.resolve(target("textbox", "Member ID"))
    result = await surface.act(
        ReadValue(output_name="description", attribute="description"), resolved.target
    )
    assert result.value == "Synthetic description"


async def test_each_fallback_strategy_and_ancestor_ambiguity(surface: PlaywrightWebSurface) -> None:
    frame = surface._page.frame(name="workspace")
    assert frame is not None
    absent = target("textbox", "Absent").candidates[0]
    meta = absent.model_dump(exclude={"strategy", "value"})
    obs = await surface.observe()
    node = next(n for n in walk_ax(obs.ax_root) if n.role == "textbox")
    structure = await frame.locator("input").evaluate("""e => {
        const path = []; while(e.parentElement) {
            path.unshift(Array.from(e.parentElement.children).indexOf(e)); e=e.parentElement;
        } return path;
    }""")
    point = await frame.locator("input").evaluate(
        "e => { const b=e.getBoundingClientRect(); return {x:b.x+4, y:b.y+4}; }"
    )
    candidates = (
        AxPathCandidate(value=PathValue(path=node.node_path), **meta),
        LabelCandidate(value=TextValue(matcher=NameMatcher(value="Member ID")), **meta),
        StructuralCandidate(value=PathValue(path=tuple(structure)), **meta),
        CoordinatesCandidate(value=CoordinatesValue(**point), **meta),
    )
    for candidate in candidates:
        resolution = await surface.resolve(LocatorLadder(candidates=(absent, candidate)))
        assert resolution.index == 1 and resolution.target is not None
        result = await surface.act(
            ReadValue(output_name="field", attribute="name"), resolution.target
        )
        assert result.value == "Member ID"
    visible = VisibleTextCandidate(value=TextValue(matcher=NameMatcher(value="Find")), **meta)
    assert (await surface.resolve(LocatorLadder(candidates=(absent, visible)))).target is not None
    bad_coordinates = CoordinatesCandidate(value=CoordinatesValue(x=-20, y=-20), **meta)
    assert (
        await surface.resolve(LocatorLadder(candidates=(absent, bad_coordinates)))
    ).target is None
    await detail(surface)
    await frame.locator('table[aria-label="Member accounts"]').evaluate(
        "e => e.after(e.cloneNode(true))"
    )
    ancestor = AxTarget(
        role="row", name_matcher=NameMatcher(mode="regex", value="Savings"), frame_path=FRAME
    )
    scoped = ScopedRoleNameCandidate(
        value=ScopedRoleNameValue(
            role="cell",
            name_matcher=NameMatcher(mode="regex", value=r"^\$"),
            ancestor=ancestor,
        ),
        **meta,
    )
    ambiguous = await surface.resolve(LocatorLadder(candidates=(scoped,)))
    assert ambiguous.attempts[0].outcome == "ambiguous" and ambiguous.target is None


async def test_wait_select_assert_and_navigation_actions(surface: PlaywrightWebSurface) -> None:
    from cua.domain.actions import Assert, Navigate, Scroll, SelectOption, WaitFor
    from cua.domain.predicates import AxNodeExists

    frame = surface._page.frame(name="workspace")
    assert frame is not None
    pred = AxNodeExists(role="textbox", name_matcher=NameMatcher(value="Ready"))
    await frame.evaluate(
        "setTimeout(() => document.querySelector('input').setAttribute('aria-label','Ready'), 100)"
    )
    await surface.act(WaitFor(predicate=pred), timing=TIMING)
    await surface.act(Assert(predicate=pred))
    missing = AxNodeExists(role="heading", name_matcher=NameMatcher(value="Never present"))
    with pytest.raises(ActionBlockedError):
        await surface.act(Assert(predicate=missing))
    with pytest.raises(SettleTimeout):
        await surface.act(
            WaitFor(predicate=missing), timing=TIMING.model_copy(update={"timeout_ms": 120})
        )
    await frame.goto(surface.config.base_url + "/t/alpha/ui/open?member_id=10001")
    select = await surface.resolve(target("combobox", "Account type"))
    await surface.act(
        SelectOption(value_ref=LiteralRef(value=StringValue(value="Checking"))), select.target
    )
    assert await frame.get_by_role("combobox", name="Account type").input_value() == "Checking"
    await surface.act(Scroll(direction="down", amount=50))
    await surface.act(Navigate(url=surface.config.base_url + "/t/alpha/"), timing=TIMING)
    assert (await surface.resolve(target("textbox", "Member ID"))).target is not None


async def test_tenant_ax_shapes_match_but_names_do_not(
    surface: PlaywrightWebSurface, tmp_path: Path
) -> None:
    alpha = await surface.observe()
    beta_surface = await PlaywrightWebSurface.launch(
        surface.config.model_copy(
            update={"tenant_id": "beta", "user_data_dir": str(tmp_path / "beta")}
        ),
        clock=FixedClock(),
        ids=SequenceIds(),
    )
    try:
        page = beta_surface._page
        await page.get_by_role("textbox", name="Username").fill("synthetic")
        await page.get_by_label("Password", exact=True).fill("synthetic")
        await page.get_by_role("button", name="Sign in", exact=True).click()
        await (
            page.frame_locator('iframe[title="Content"]')
            .frame_locator('iframe[title="Member workspace"]')
            .get_by_role("textbox")
            .wait_for()
        )
        beta = await beta_surface.observe()
        left, right = walk_ax(alpha.ax_root), walk_ax(beta.ax_root)
        assert [(n.role, n.frame_path, n.node_path) for n in left] == [
            (n.role, n.frame_path, n.node_path) for n in right
        ]
        assert next(n.name for n in left if n.role == "textbox") == "Member ID"
        assert next(n.name for n in right if n.role == "textbox") == "Account Holder Number"
        assert alpha.hash != beta.hash
    finally:
        await beta_surface.close()


async def test_real_screenshot_masks_identity_region(
    surface: PlaywrightWebSurface, tmp_path: Path
) -> None:
    from cua.observability.evidence import EvidenceStore, SurfaceEvidenceSink
    from cua.observability.redaction import masked_ax_nodes

    store = EvidenceStore(tmp_path / "evidence", "0" * 26, clock=FixedClock(), ids=SequenceIds())
    surface._evidence = SurfaceEvidenceSink(store)
    try:
        await detail(surface)
        observation = await surface.observe()
        masked = masked_ax_nodes(observation.ax_root)
        assert masked
        assert all(node.bounds is not None for node in masked)
        screenshots = [entry for entry in store.index.entries if entry.kind == "screenshot"]
        assert len(screenshots) == 1
        assert screenshots[0].observation_hash == observation.hash
        assert screenshots[0].redaction == "region_masked"
        assert screenshots[0].masked_regions
    finally:
        store.close()
