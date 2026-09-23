"""Adapt Chromium to Surface; forbid policy, replay, discovery, and target_app imports.

Pinned Playwright Python 1.63.0 has no Page.accessibility. This adapter deliberately uses
public BrowserContext.new_cdp_session + Accessibility.getFullAXTree, not the removed snapshot
API. CDP supplies values and backend node identity that Locator.aria_snapshot YAML omits.
If CDP disappears, fail closed: a replacement ARIA-snapshot parser AND an identity bridge must
pass the same contract tests before being enabled; never infer a DOM tree as an AX substitute.
Sources: playwright.dev/python/docs/api/class-browsercontext#browser-context-new-cdp-session
and chromedevtools.github.io/devtools-protocol/tot/Accessibility/#method-getFullAXTree.
"""

import asyncio
import hashlib
import socket
import struct
import zlib
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal, cast

from playwright.async_api import (
    BrowserContext,
    CDPSession,
    Error,
    Locator,
    Page,
    Playwright,
    Request,
    async_playwright,
)
from pydantic import Field, JsonValue

from cua.domain.actions import (
    Action,
    Assert,
    Click,
    Dismiss,
    Navigate,
    ReadValue,
    Scroll,
    SelectOption,
    TypeText,
    WaitFor,
)
from cua.domain.common import DomainModel, EvidenceRef, LiteralRef, ValueRef, digest
from cua.domain.locators import (
    AxPathCandidate,
    CoordinatesCandidate,
    CssCandidate,
    LabelCandidate,
    LocatorCandidate,
    LocatorLadder,
    RoleNameCandidate,
    ScopedRoleNameCandidate,
    StructuralCandidate,
    VisibleTextCandidate,
    stability_key,
)
from cua.domain.observation import AxNode, Observation, SurfaceFingerprint, ax_digest, walk_ax
from cua.domain.ports import (
    ActionResult,
    CandidateAttempt,
    Clock,
    ControlToken,
    EvidencePayload,
    EvidenceSink,
    IdGenerator,
    NodeAddress,
    Resolution,
    ResolvedTarget,
    SettleFailure,
    SettleTimeout,
    ValueResolver,
)
from cua.domain.predicates import AmbiguousTargetError, AxTarget, evaluate
from cua.domain.steps import StepTiming
from cua.surface._cdp import (
    FrameCapture,
    accessible_element,
    attach_mask_bounds,
    backend_selector,
    capture_frames,
)
from cua.surface.base import merge_frames, tree_diff


def opaque_screenshot(png: bytes) -> bytes:
    """Keep a tested legacy full-viewport encoder for old evidence compatibility.

    New screenshots use observability's region mask. This helper remains for verifying
    historical blank-image semantics and is not used by the browser adapter.
    """
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Screenshot transport must be PNG")
    width, height = struct.unpack(">II", png[16:24])

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        png[:8]
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\0" + b"\0\0\0" * width) * height))
        + chunk(b"IEND", b"")
    )


class WebConfig(DomainModel):
    user_data_dir: str
    base_url: str
    tenant_id: str
    headless: bool = True
    debugging_port: int = Field(default=0, ge=0, le=65535)
    action_timeout_ms: int = Field(default=1000, gt=0)


class NoopEvidenceSink:
    """Return content identity without writing data; a durable redacted store comes later."""

    def sensitive_bounds_targets(self, root: AxNode) -> tuple[NodeAddress, ...]:
        return ()

    def __init__(self, ids: IdGenerator) -> None:
        self._ids = ids

    async def put(self, payload: EvidencePayload) -> EvidenceRef:
        return EvidenceRef(
            evidence_id=self._ids.new(),
            media_type=payload.media_type,
            content_hash=hashlib.sha256(payload.content).hexdigest(),
        )

    def verify(self, reference: EvidenceRef) -> bool:
        """The no-op adapter cannot prove persistence and therefore never verifies evidence."""
        return False


class LiteralValues:
    async def resolve_value(self, reference: ValueRef) -> str:
        if not isinstance(reference, LiteralRef):
            raise ValueError("Parameter/secret reference requires an injected ValueResolver")
        return str(reference.value.value)


class ControlCededError(RuntimeError):
    """Automation cannot observe, resolve or act while the human owns this session."""


class ActionBlockedError(RuntimeError):
    """The recorded target is stale, ambiguous or no longer actionable; do not dispatch."""


class _Resolved:
    def __init__(self, ladder: LocatorLadder, index: int) -> None:
        self.ladder, self.index = ladder, index


class PlaywrightWebSurface:
    """Only domain values cross this API. Browser resources stay private and are never recorded."""

    def __init__(
        self,
        config: WebConfig,
        clock: Clock,
        ids: IdGenerator,
        evidence: EvidenceSink,
        values: ValueResolver,
    ) -> None:
        self.config, self._clock, self._ids = config, clock, ids
        self._evidence, self._values = evidence, values
        self._lock = asyncio.Lock()
        self._ceded = False
        self._token: ControlToken | None = None
        self._requests: set[Request] = set()
        self._navigating: set[str] = set()
        self._handles: dict[str, _Resolved] = {}
        self._pulse = asyncio.Event()
        self._playwright: Playwright
        self._context: BrowserContext
        self._owner: BrowserContext
        self._page: Page
        self._cdp: CDPSession
        self._endpoint = ""
        self._last_observation: Observation | None = None
        self._reading_node: AxNode | None = None
        self._last_activity = 0.0

    @classmethod
    async def launch(
        cls,
        config: WebConfig,
        *,
        clock: Clock,
        ids: IdGenerator,
        evidence: EvidenceSink | None = None,
        values: ValueResolver | None = None,
    ) -> "PlaywrightWebSurface":
        self = cls(config, clock, ids, evidence or NoopEvidenceSink(ids), values or LiteralValues())
        port = config.debugging_port
        if not port:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
        self._endpoint = f"http://127.0.0.1:{port}"
        self._playwright = await async_playwright().start()
        try:
            self._owner = await self._playwright.chromium.launch_persistent_context(
                Path(config.user_data_dir),
                headless=config.headless,
                args=[f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1"],
                viewport={"width": 1440, "height": 1200},
                service_workers="block",
            )
            self._context = self._owner
            self._page = self._context.pages[0]
            await self._monitor()
            await self._page.goto(config.base_url + f"/t/{config.tenant_id}/", wait_until="load")
        except BaseException:
            await self._playwright.stop()
            raise
        return self

    async def configure_test_fault(self, kind: str, *, count: int = 1, delay_ms: int = 0) -> None:
        """Configure the synthetic fixture through its session cookie for integration evidence.

        This explicit test hook is not part of the Surface port and cannot be invoked by an
        artifact. Keeping it on the concrete adapter lets the real browser session own the fault
        while production replay remains unaware of target-app controls.
        """
        response = await self._context.request.post(
            self.config.base_url + "/_test/faults",
            data={"faults": [{"kind": kind, "remaining": count, "delay_ms": delay_ms}]},
        )
        if not response.ok:
            raise RuntimeError("Synthetic fixture rejected fault configuration")

    async def _monitor(self) -> None:
        self._cdp = await self._context.new_cdp_session(self._page)
        await self._cdp.send("Page.enable")
        self._cdp.on("Page.frameStartedLoading", self._navigation_start)
        self._cdp.on("Page.frameStoppedLoading", self._navigation_end)
        self._page.on("request", self._request_start)
        self._page.on("requestfinished", self._request_end)
        self._page.on("requestfailed", self._request_end)

    def _request_start(self, request: Request) -> None:
        self._last_activity = asyncio.get_running_loop().time()
        self._requests.add(request)
        self._pulse.set()

    def _request_end(self, request: Request) -> None:
        self._last_activity = asyncio.get_running_loop().time()
        self._requests.discard(request)
        self._pulse.set()

    def _navigation_start(self, event: dict[str, Any]) -> None:
        self._last_activity = asyncio.get_running_loop().time()
        self._navigating.add(str(event["frameId"]))
        self._pulse.set()

    def _navigation_end(self, event: dict[str, Any]) -> None:
        self._last_activity = asyncio.get_running_loop().time()
        self._navigating.discard(str(event["frameId"]))
        self._pulse.set()

    def _assert_control(self) -> None:
        if self._ceded:
            raise ControlCededError("Control is ceded; resume the issued token first")

    async def close(self) -> None:
        async with self._lock:
            self._assert_control()
            await self._owner.close()
            await self._playwright.stop()

    async def _observe(
        self, *, evidence: bool = False, capture: FrameCapture | None = None
    ) -> Observation:
        owns_capture = capture is None
        capture = capture or await capture_frames(self._context, self._page, self._cdp)
        try:
            tree = merge_frames(tuple(capture.snapshots))
            if evidence:
                targets = self._evidence.sensitive_bounds_targets(tree)
                await attach_mask_bounds(capture, targets)
                tree = merge_frames(tuple(capture.snapshots))
            # Read tag structure only, never persist full DOM/text/attribute values.
            structures = [
                await frame.evaluate(
                    "() => Array.from(document.querySelectorAll('*'), e => "
                    "[e.tagName, e.children.length])"
                )
                for frame in capture.frames.values()
            ]
        finally:
            if owns_capture:
                await capture.close()
        response = await self._context.request.get(
            self.config.base_url + "/_meta/version", params={"tenant_id": self.config.tenant_id}
        )
        fingerprint = SurfaceFingerprint.model_validate(
            {
                **await response.json(),
                "observed_at": self._clock.now(),
            }
        )
        observation_id = self._ids.new()
        screenshot_ref = None
        if evidence:
            # The sink owns masking; never write or export these transient raw bytes here.
            screenshot = await self._page.screenshot(animations="disabled")
            screenshot_ref = await self._evidence.put(
                EvidencePayload(
                    media_type="image/png",
                    content=screenshot,
                    observation_hash=ax_digest(tree),
                    observation_id=observation_id,
                    ax_root=tree,
                )
            )
        result = Observation(
            observation_id=observation_id,
            captured_at=self._clock.now(),
            surface_kind="web",
            url=self._page.url,
            title=await self._page.title(),
            ax_root=tree,
            frames=tuple(s.info for s in capture.snapshots),
            screenshot_ref=screenshot_ref,
            dom_digest=digest(cast(JsonValue, structures)),
            fingerprint=fingerprint,
        )
        self._last_observation = result
        return result

    async def observe(self) -> Observation:
        async with self._lock:
            self._assert_control()
            return await self._observe(evidence=True)

    async def _candidate_locator(
        self,
        candidate: LocatorCandidate,
        observation: Observation,
        capture: FrameCapture,
        *,
        first_match: bool = False,
    ) -> tuple[Locator | None, int]:
        frame = capture.frames[candidate.frame_path]
        nodes: tuple[AxNode, ...] = ()
        if isinstance(candidate, (RoleNameCandidate, ScopedRoleNameCandidate)):
            target = AxTarget(
                role=candidate.value.role,
                name_matcher=candidate.value.name_matcher,
                frame_path=candidate.frame_path,
                within=candidate.value.ancestor
                if isinstance(candidate, ScopedRoleNameCandidate)
                else None,
            )
            try:
                nodes = target.select(observation)
            except AmbiguousTargetError:
                # Ancestor resolution still fails closed. Only the leaf may opt into first_match.
                roots = (
                    target.within.select(observation) if target.within else (observation.ax_root,)
                )
                nodes = tuple(
                    n
                    for root in roots
                    for n in walk_ax(root)
                    if n.role == target.role
                    and target.name_matcher.matches(n.name)
                    and n.frame_path == candidate.frame_path
                )
        elif isinstance(candidate, AxPathCandidate):
            nodes = tuple(
                n
                for n in walk_ax(observation.ax_root)
                if n.frame_path == candidate.frame_path and n.node_path == candidate.value.path
            )
        elif isinstance(candidate, (LabelCandidate, VisibleTextCandidate)):
            nodes = tuple(
                n
                for n in walk_ax(observation.ax_root)
                if n.frame_path == candidate.frame_path
                and candidate.value.matcher.matches(n.name)
                and (
                    (isinstance(candidate, VisibleTextCandidate) and n.role == "text")
                    or (
                        isinstance(candidate, LabelCandidate)
                        and n.role in {"textbox", "combobox", "checkbox", "radio"}
                    )
                )
            )
        elif isinstance(candidate, CssCandidate):
            return await self._fallback_locator(capture, candidate, candidate.value.selector)
        elif isinstance(candidate, StructuralCandidate):
            selector = "html" + "".join(f" > :nth-child({i + 1})" for i in candidate.value.path)
            return await self._fallback_locator(capture, candidate, selector)
        elif isinstance(candidate, CoordinatesCandidate):
            # Coordinates are frame-local CSS pixels, not global screen coordinates.
            css = await frame.evaluate(
                """p => {
                let e = document.elementFromPoint(p.x, p.y), parts = [];
                while (e && e.nodeType === 1) {
                    let i = 1, s = e.previousElementSibling;
                    while(s) { i++; s=s.previousElementSibling; }
                    parts.unshift(e.localName+':nth-child('+i+')'); e=e.parentElement;
                } return parts.join(' > ');
            }""",
                {"x": candidate.value.x, "y": candidate.value.y},
            )
            if not css:
                return None, 0
            return await self._fallback_locator(capture, candidate, str(css))
        if not nodes:
            return None, 0
        if len(nodes) > 1 and not first_match:
            return None, len(nodes)
        self._reading_node = nodes[0]
        binding = capture.bindings[(nodes[0].frame_path, nodes[0].node_path)]
        if binding.backend_id is None:
            return None, 0
        selector = await backend_selector(
            capture.sessions[candidate.frame_path], binding.backend_id
        )
        return frame.locator(selector), len(nodes)

    async def _fallback_locator(
        self, capture: FrameCapture, candidate: LocatorCandidate, selector: str
    ) -> tuple[Locator | None, int]:
        locator = capture.frames[candidate.frame_path].locator(selector)
        count = await locator.count()
        if count:
            document = capture.bindings[(candidate.frame_path, ())].backend_id
            if document is not None:
                raw = await accessible_element(
                    capture.sessions[candidate.frame_path], document, selector
                )
                if raw is not None:
                    self._reading_node = AxNode(
                        role=raw.role, name=raw.name, value=raw.value, description=raw.description
                    )
        return locator, count

    async def _actionable(self, locator: Locator) -> bool:
        if (
            not await locator.count()
            or not await locator.is_enabled()
            or not await locator.is_visible()
        ):
            return False
        # Trial click checks viewport, nested frame clipping, overlays and event interception,
        # and works for span[role=button] without dispatching an input event.
        try:
            await locator.click(trial=True, timeout=self.config.action_timeout_ms)
            return await locator.bounding_box() is not None
        except Error:
            return False

    async def _resolve(self, ladder: LocatorLadder) -> tuple[Resolution, Locator | None]:
        capture = await capture_frames(self._context, self._page, self._cdp)
        attempts: list[CandidateAttempt] = []
        final_count = 0
        try:
            observation = await self._observe(capture=capture)
            for index, candidate in enumerate(ladder.candidates):
                self._reading_node = None
                final_count = 0
                outcome: LiteralOutcome = "not_found"
                detail = "No node matched the recorded candidate"
                locator: Locator | None = None
                if candidate.frame_path not in capture.frames:
                    outcome, detail = "wrong_frame", "Recorded frame path is absent"
                else:
                    try:
                        locator, final_count = await self._candidate_locator(
                            candidate,
                            observation,
                            capture,
                            first_match=ladder.match_policy == "first_match",
                        )
                        if final_count > 1 and ladder.match_policy == "require_unique":
                            outcome, detail = (
                                "ambiguous",
                                "Multiple nodes match; require_unique rejected them",
                            )
                        elif locator is not None and final_count:
                            locator = locator.first
                            if await self._actionable(locator):
                                outcome, detail = (
                                    "matched",
                                    "Unique actionable candidate"
                                    if final_count == 1
                                    else "Explicit first_match policy",
                                )
                            else:
                                outcome, detail = (
                                    "not_actionable",
                                    "Node is disabled, occluded or outside the actionable viewport",
                                )
                        # The fixture replaces the pending destination with a modal. A blocking
                        # dialog takes precedence over not_found; we do not claim hidden existence.
                        elif any(
                            n.role in {"dialog", "alertdialog"}
                            for n in walk_ax(observation.ax_root)
                        ):
                            outcome, detail = (
                                "not_actionable",
                                "Modal blocks the pending destination; dismiss it before resolving",
                            )
                    except AmbiguousTargetError:
                        outcome, detail = (
                            "ambiguous",
                            "Semantic target or ancestor matches multiple nodes",
                        )
                        # No leaf count is known when the ancestor itself is ambiguous.
                        final_count = 0
                    except Error:
                        outcome, detail = "not_found", "Node detached or candidate is invalid"
                attempts.append(
                    CandidateAttempt(candidate=candidate, outcome=outcome, detail=detail)
                )
                if outcome == "matched":
                    handle = self._ids.new()
                    self._handles[handle] = _Resolved(ladder, index)
                    delta = float(
                        stability_key(ladder.candidates[0])[0] - stability_key(candidate)[0]
                    )
                    return Resolution(
                        winning_candidate=candidate,
                        index=index,
                        match_count=final_count,
                        attempts=tuple(attempts),
                        degraded=index > 0,
                        degradation_delta=delta,
                        target=ResolvedTarget(handle=handle, observation_hash=observation.hash),
                    ), locator
            return Resolution(
                winning_candidate=None,
                index=None,
                match_count=final_count,
                attempts=tuple(attempts),
                degraded=False,
                degradation_delta=0,
                target=None,
            ), None
        finally:
            await capture.close()

    async def resolve(self, ladder: LocatorLadder) -> Resolution:
        async with self._lock:
            self._assert_control()
            result, _ = await self._resolve(ladder)
            return result

    async def _settle(self, timing: StepTiming) -> Observation:
        """Wait on a timer/event race, never sleep. Network AND AX stability AND navigation gate.

        A poll interval separates consecutive samples; the stability window also applies to
        network quiescence. Events wake the scheduler but never permit back-to-back AX samples.
        The deadline encloses observation I/O as well, so a hung snapshot cannot defeat timeout.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timing.timeout_ms / 1000
        previous = last = self._last_observation
        stable_since = loop.time()
        next_poll = loop.time() + timing.poll_interval_ms / 1000
        try:
            async with asyncio.timeout_at(deadline):
                while True:
                    try:
                        previous = last = await self._observe()
                        break
                    except Error:
                        self._pulse.clear()
                        with suppress(TimeoutError):
                            await asyncio.wait_for(self._pulse.wait(), 0.05)
                while True:
                    self._pulse.clear()
                    with suppress(TimeoutError):
                        await asyncio.wait_for(
                            self._pulse.wait(), max(0.001, next_poll - loop.time())
                        )
                    if self._requests or self._navigating:
                        stable_since = loop.time()
                    if loop.time() < next_poll:
                        continue
                    try:
                        current = await self._observe()
                    except Error:
                        if not self._requests and not self._navigating:
                            raise
                        next_poll = loop.time() + timing.poll_interval_ms / 1000
                        continue
                    previous, last = last, current
                    if previous.hash != last.hash:
                        stable_since = loop.time()
                    if (
                        not self._requests
                        and not self._navigating
                        and previous.hash == last.hash
                        and loop.time() - max(stable_since, self._last_activity)
                        >= timing.stability_window_ms / 1000
                    ):
                        return last
                    next_poll = loop.time() + timing.poll_interval_ms / 1000
        except TimeoutError as exc:
            raise SettleTimeout(
                SettleFailure(
                    last_hashes=(
                        previous.hash if previous else digest(None),
                        last.hash if last else digest(None),
                    ),
                    diff=tree_diff(previous, last) if previous and last else (),
                    pending_requests=len(self._requests),
                    pending_navigation=bool(self._navigating),
                )
            ) from exc

    async def settle(self, timing: StepTiming) -> Observation:
        async with self._lock:
            self._assert_control()
            return await self._settle(timing)

    async def act(
        self,
        action: Action,
        resolved_target: ResolvedTarget | None = None,
        *,
        timing: StepTiming | None = None,
    ) -> ActionResult:
        async with self._lock:
            self._assert_control()
            before = await self._observe()
            locator = None
            if resolved_target is not None:
                entry = self._handles.get(resolved_target.handle)
                if entry is None or resolved_target.observation_hash != before.hash:
                    raise ActionBlockedError("Resolved target is stale; observe and resolve again")
                resolution, locator = await self._resolve(entry.ladder)
                if (
                    resolution.index != entry.index
                    or locator is None
                    or resolution.target is None
                    or resolution.target.observation_hash != before.hash
                ):
                    raise ActionBlockedError("Target is no longer uniquely actionable")
            if (
                isinstance(action, (Click, Dismiss, TypeText, SelectOption, ReadValue))
                and locator is None
            ):
                raise ActionBlockedError("This action requires an actionable resolved target")
            value = None
            if isinstance(action, Navigate):
                await self._page.goto(action.url, wait_until="commit")
            elif isinstance(action, (Click, Dismiss)):
                assert locator is not None
                await locator.click(timeout=self.config.action_timeout_ms, no_wait_after=True)
            elif isinstance(action, (TypeText, SelectOption)):
                assert locator is not None
                text = await self._values.resolve_value(action.value_ref)
                if isinstance(action, TypeText):
                    await locator.fill(text, timeout=self.config.action_timeout_ms)
                else:
                    await locator.select_option(label=text, timeout=self.config.action_timeout_ms)
            elif isinstance(action, ReadValue):
                assert locator is not None
                if self._reading_node is None:
                    raise ActionBlockedError("Read target has no browser-computed AX attributes")
                value = getattr(self._reading_node, action.attribute)
            elif isinstance(action, WaitFor):
                await self._wait_predicate(
                    action,
                    timing
                    or StepTiming(
                        settle_strategy="ax_stable", timeout_ms=self.config.action_timeout_ms
                    ),
                )
            elif isinstance(action, Assert):
                result = evaluate(action.predicate, before)
                if not result.satisfied:
                    raise ActionBlockedError("Surface assertion is not satisfied")
            elif isinstance(action, Scroll):
                amount = action.amount if action.direction in {"down", "right"} else -action.amount
                await self._page.mouse.wheel(
                    amount if action.direction in {"left", "right"} else 0,
                    amount if action.direction in {"up", "down"} else 0,
                )
            after = (
                await self._settle(timing)
                if timing and timing.settle_strategy != "none"
                else await self._observe()
            )
            return ActionResult(
                attempted=action,
                before_hash=before.hash,
                after_hash=after.hash,
                changed=before.hash != after.hash,
                value=value,
                settle_outcome="settled"
                if timing and timing.settle_strategy != "none"
                else "not_requested",
            )

    async def _wait_predicate(self, action: WaitFor, timing: StepTiming) -> None:
        previous = last = await self._observe()
        try:
            async with asyncio.timeout(timing.timeout_ms / 1000):
                while not evaluate(action.predicate, last).satisfied:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(
                            asyncio.Event().wait(), timing.poll_interval_ms / 1000
                        )
                    previous, last = last, await self._observe()
        except TimeoutError as exc:
            raise SettleTimeout(
                SettleFailure(
                    last_hashes=(previous.hash, last.hash),
                    diff=tree_diff(previous, last),
                    pending_requests=len(self._requests),
                    pending_navigation=bool(self._navigating),
                )
            ) from exc

    async def cede_control(self) -> ControlToken:
        async with self._lock:
            self._assert_control()
            info = (await self._cdp.send("Target.getTargetInfo"))["targetInfo"]
            token = ControlToken(
                cdp_endpoint=self._endpoint,
                context_id=info.get("browserContextId", "default"),
                page_guid=info["targetId"],
                issued_at=self._clock.now(),
            )
            self._page.remove_listener("request", self._request_start)
            self._page.remove_listener("requestfinished", self._request_end)
            self._page.remove_listener("requestfailed", self._request_end)
            await self._cdp.detach()
            self._token, self._ceded = token, True
            self._handles.clear()
            return token

    async def resume_control(self, token: ControlToken) -> None:
        async with self._lock:
            if not self._ceded or token != self._token:
                raise ControlCededError("Resume requires the exact outstanding control token")
            browser = await self._playwright.chromium.connect_over_cdp(token.cdp_endpoint)
            for context in browser.contexts:
                for page in context.pages:
                    session = await context.new_cdp_session(page)
                    info = (await session.send("Target.getTargetInfo"))["targetInfo"]
                    await session.detach()
                    if (
                        info["targetId"] == token.page_guid
                        and info.get("browserContextId", "default") == token.context_id
                    ):
                        self._context, self._page = context, page
                        self._requests.clear()
                        self._navigating.clear()
                        await self._monitor()
                        self._ceded, self._token = False, None
                        return
            raise ControlCededError(
                "Original page/context no longer exists; refusing a new session"
            )


LiteralOutcome = Literal["matched", "not_found", "ambiguous", "wrong_frame", "not_actionable"]
