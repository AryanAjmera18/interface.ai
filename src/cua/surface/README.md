# Surface adapter

`Surface` separates normalized observations and typed actions from transport. Public methods
return domain models declared in `domain/ports.py`; no Playwright object crosses that boundary.
`PlaywrightWebSurface.launch` takes WebConfig, Clock, IdGenerator, EvidenceSink and ValueResolver.
The default value resolver accepts literal refs only; parameter/secret resolution is injected.
The desktop implementation is an explicit typed stub, with UIA/AX mappings in its docstring.

## Perception choice

The installed and pinned Python Playwright version is 1.63.0. Local API inspection confirmed
`Page.accessibility` is absent, while `Locator.aria_snapshot` returns a string. The web adapter
uses `Accessibility.getFullAXTree` through the public `new_cdp_session` API for values and DOM
node identity. A removed CDP API fails closed; an ARIA YAML parser plus a tested identity bridge
would be needed before enabling a replacement. There is no silent DOM-as-AX fallback.

Sources: [Playwright CDP sessions](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-new-cdp-session),
[ARIA snapshots](https://playwright.dev/python/docs/api/class-locator#locator-aria-snapshot),
[CDP Accessibility](https://chromedevtools.github.io/devtools-protocol/tot/Accessibility/).

Each frame is captured separately, sharing its parent's CDP session when Chromium puts them in
the same process. Child documents are spliced at their observed iframe owner. Paths use frame
titles/names; unnamed frames use structural paths and duplicate sibling names fail closed.
Original accessible names remain in AxNode.name. A private matching index stores folded names.
Presentational wrappers are flattened before per-frame node paths are assigned. Table rows
without names receive their cell names joined in order; this convention needs equivalent UIA/AX
grid support. Domain observation hashing is the only semantic hash implementation.

## Resolution and actions

Every attempted candidate records matched, not_found, wrong_frame, ambiguous or not_actionable.
`require_unique` never silently chooses a first node. `first_match` is an explicit ladder policy;
an ambiguous semantic ancestor still fails closed. Degradation delta is the difference in the
documented strategy ranks, not a model confidence score.

Targets are opaque adapter handles bound to the observation hash. Act reobserves and resolves
again, then rejects changed state or a different winning candidate. Actionability uses Chromium's
trial-click checks for visibility, enabled state, viewport reachability and event interception.
Coordinates are frame-local CSS pixels and require a real bounding box. ReadValue returns
browser-computed AX attributes, including accessible names/descriptions that differ from text.

The temporary CDP-to-DOM bridge generates a CSS path for the observed backend node. That path
is never stored in the capability or described as a semantic locator. It has not been qualified
for closed shadow roots, custom embedded controls, cross-origin out-of-process frames, or frame
titles that change mid-run. Those cases need adapter tests and may currently fail closed.

The fixture's interstitial replaces its pending destination. A modal therefore takes precedence
over a missing target: resolution reports not_actionable and requires dismissal. This does not
claim that the absent control has been observed behind the dialog. Untagged legacy overlays
are caught when they intercept an existing control, but a replacement screen without modal
semantics cannot be classified this way automatically.

## Settling and handoff

Settling requires network quiescence, no pending frame navigation, and equal consecutive AX
hashes separated by StepTiming.poll_interval_ms for StepTiming.stability_window_ms. A deadline
also bounds snapshot I/O. The scheduler waits on events/timers, never fixed sleeps. A timeout
contains the last two hashes and changed field/path metadata, not raw financial values. If no
snapshot has ever completed, the missing hash is the digest of JSON null.

Chromium uses a persistent profile and loopback CDP port. Cede waits for any current operation,
detaches the monitor and blocks all automated observation/actions. Resume checks the exact
issued token and reconnects to the original browser-context ID and target ID; it does not create
a fresh session. Tests use a second Playwright connection to edit the same unfinished form and
verify cookies and typed content survive. This proves mechanics, not an authenticated operator
lease; Stage 9 must protect token ownership and approval identity.

## Evidence and verification

DOM evidence is a digest of tag/child-count structure, never a persisted DOM dump. Screenshots are taken for every recorded observation and pass through the injected evidence
sink. The central redactor masks AX-identified identity regions using viewport bounds; if a
masked node has no bounds, it blanks that screenshot and records the fallback reason. This is
not OCR and does not prove that arbitrary pixels outside the AX tree are free of sensitive data.
The default no-op sink retains nothing. Historical attempts 001–010 keep their original blank
screenshots and are labeled accordingly in their indexes.

`pytest tests/integration/surface` runs real Chromium against loopback servers. Install the pinned
browser with `uv run playwright install chromium` first. The earlier CI tenant smoke job installs
Chromium separately. Adding the same setup to the main checks job needs the requested CI scope
extension; until then a fresh checks runner must provision Chromium before the default suite.
No external site, model API, or banking service is contacted by the tests.
