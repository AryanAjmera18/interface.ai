# 1. Architecture

The system separates one-time discovery from repeat execution. A Playwright surface converts the
legacy application into normalized accessibility-tree observations. A LangGraph discovery graph
observes, asks a model for a typed action, checks policy, acts, and verifies. LangGraph makes
terminal edges, budget stops, policy denial, and escalation inspectable control flow. LangSmith is
optional telemetry; local OTel spans are always available.

The journal is the source transcript. A deterministic compiler builds the capability from accepted
actions, observations, policy decisions, and extractor results rather than asking a model to write
the artifact. Domain, policy, and replay are framework-free. One OpenAI model discovered the flow,
Luna selected it from the catalog, and replay used no model.

This costs more engineering than replaying generated code, but yields a reviewable boundary. At
scale, adapters and stores would become deployable services while the domain contracts remain
stable.

# 2. Artifact schema

A capability has typed scalar parameters, sensitivity on every input and output, an ordered step
list, predicates expressed as data, named outcomes, and full provenance. Each locator ladder
carries multiple evidence-backed candidates ordered by a pure stability function. Steps record
intent, timing, risk, checkpoints, outcome handling, and the observation and decision that caused
them.

Approval binds the canonical content digest while excluding status and the approval record itself.
A changed artifact therefore becomes stale rather than retaining silent approval. Provider-strict
schemas use entry lists instead of open-keyed maps. Tenant overrides are separate, limited to
locators and checkpoints, and bound to the base digest.

The trade-off is visible complexity. Dropping provenance, sensitivity, or outcome types would make
the file shorter but remove the facts reviewers need. At scale, authoring tools should present
semantic diffs rather than raw JSON.

# 3. Determinism & error handling

Replay validates inputs, checks policy, resolves a recorded ladder, acts, settles by an explicit
strategy, detects outcomes, then evaluates the checkpoint. No model fallback exists. The observed
alpha success executed six steps; beta failed on an alpha heading without an override and succeeded
with the reviewed override.

Business outcomes such as no member and permission denial return typed answers. Recoverable
conditions such as an interstitial remain journal history and lead to a terminal result after a
bounded recovery. Locator exhaustion, policy denial, invalid input, timeout, server failure, and
checkpoint failure produce HardFailure with step, expectation, observation, trace, and evidence.

Determinism is bounded by the external UI and browser. Fingerprints, lower-ranked locator use, and
settle outcomes make drift visible rather than eliminating it. At scale, capability promotion
would use repeated deterministic canary replays across supported surface versions.

# 4. Heterogeneity & multi-tenant

The Surface protocol isolates observe, resolve, act, settle, and control transfer. Browser-specific
DOM and CDP details remain in the Playwright adapter. The desktop stub maps the same concepts to
UIA or AX; CDP-to-CSS fallback cannot port and needs a platform-native equivalent.

Alpha and beta share one target codebase but vary labels, branding, field order, and confirmation
behavior. The approved alpha lookup failed on beta as expected. A named human override added beta
labels and checkpoints without changing action, risk, outcomes, inputs, or outputs; its successful
run retained fingerprint drift and a verified parent digest.

Overrides reduce duplicate artifacts but require governance. At scale, supported tenant matrices,
expiry rules, and recorded beta observations should replace manual alias maintenance.

# 5. Escalation & handoff

Discovery escalates dead ends, explicit help requests, and irreversible actions. Replay can
escalate a hard failure through a framework-free port. SessionLease tracks RUNNING, PAUSED,
OPERATOR_CONTROL, HANDBACK_PENDING, and terminal abort, including actor and expiry.

The evidence transfers the same Chromium session over CDP to a minimal operator surface. The
scripted-operator actor is explicit. Handback records the human action and AX change, then
automation re-observes before continuing. LangGraph checkpoints preserve graph data; the lease
preserves authority and browser continuity.

A real operator co-browsing interface was outside scope. At scale, leases need durable storage,
authenticated identities, authorization, and audited remote session transport.

# 6. Safety

The URL allowlist compares parsed scheme, normalized host, explicit wildcard labels, port, and
decoded path. Tests cover userinfo, suffix, query-string, encoded traversal, IDN, case, and default
ports. Policy derives risk from action, route, and target semantics at planner time and immediately
before surface action. Approved status, recorded irreversible risk, and an explicit caller flag
are all required for irreversible replay.

Two escalation attempts exposed concrete policy defects. First, an irreversible rule named a
route the fixture never served, so a broad safe-click rule won; configuration now self-checks the
real review and beta-extra routes. Second, the outer frameset URL stayed fixed while its content
frame navigated, so policy now classifies against the target-owning frame URL.

AX text is untrusted prompt input, and policy contains a model that follows injected text.
Sensitive fields pass one redaction choke point; screenshots use AX bounds and fall back to full
viewport masking. The chain detects ordering changes but does not authenticate a same-process
rewrite. A sensitive output returned to a calling model is provider egress controlled by
OutputSpec sensitivity. The catalog evidence deliberately keeps the raw balance local.

# 7. Cuts

- journals and manifests are the evidence format; an HTML viewer over them is next.
- Operator co-browsing is scripted; next, add authenticated remote control with durable leases.
- The desktop surface is a typed stub; next, implement UIA and macOS AX adapters.
- The hand-built OpenAI httpx transport owns retries and errors; next, migrate to the official SDK.
- Journal heads are local; next, anchor signed heads in an external append-only store.
- Early attempts retain blank screenshots or omit pre-mask blobs; next runs use region masking.
- Only OpenAI has live evidence; next, validate the provider-neutral seam with Anthropic.
- No MCP server was added because the assignment rewards a narrow agent-facing catalog.
