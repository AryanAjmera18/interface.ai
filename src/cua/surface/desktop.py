"""Declare the desktop adapter roadmap; forbid Playwright and higher-layer imports.

observe -> Windows UIAutomation tree walk / macOS AXUIElement hierarchy.
resolve -> AutomationElement property conditions / AXUIElementCopyAttributeValue, with the same
semantic ancestor scopes and fail-closed ambiguity policy as the web adapter.
act -> InvokePattern and ValuePattern / AXUIElementPerformAction and attribute setters.
settle -> repeated UIA/AX tree stability under a deadline; there is no network-quiescence signal.
cede_control -> release the input lease while retaining the native window/process reference.
resume_control -> reacquire that lease and revalidate the original window identity.

URL and frames do not port; desktop observations use url=None and an empty frame inventory.
Coordinates carry more weight for custom-drawn widgets. A future versioned control token must
replace web CDP attachment coordinates with native process/window references, never fake a URL.
Every method intentionally raises NotImplementedError rather than pretending desktop is supported.
"""

from cua.domain.actions import Action
from cua.domain.locators import LocatorLadder
from cua.domain.observation import Observation
from cua.domain.ports import ActionResult, ControlToken, Resolution, ResolvedTarget
from cua.domain.steps import StepTiming


class DesktopSurface:
    async def observe(self) -> Observation:
        raise NotImplementedError("Desktop perception requires a UIA/AX backend")

    async def resolve(self, ladder: LocatorLadder) -> Resolution:
        raise NotImplementedError("Desktop resolution requires a UIA/AX backend")

    async def act(
        self,
        action: Action,
        resolved_target: ResolvedTarget | None = None,
        *,
        timing: StepTiming | None = None,
    ) -> ActionResult:
        raise NotImplementedError("Desktop actions require a UIA/AX backend")

    async def settle(self, timing: StepTiming) -> Observation:
        raise NotImplementedError("Desktop settling requires UIA/AX tree stability")

    async def cede_control(self) -> ControlToken:
        raise NotImplementedError("Desktop handoff requires a native window token")

    async def resume_control(self, token: ControlToken) -> None:
        raise NotImplementedError("Desktop resume requires a native window token")
