# 1. Architecture

The design starts from a provenance claim: every artifact field traces to an observation, a model
decision, a deterministic compiler rule, or a named human edit. `cua verify` checks those links,
the journal chain, recompilation, evidence digests, and content-bound approval.

One-time discovery uses a LangGraph loop to observe, decide, check policy, act, and verify. A
deterministic compiler turns the accepted journal records into a typed capability. Replay reads
that artifact without an LLM. Domain, policy, and replay stay framework-free; Playwright,
LangGraph, model clients, and storage sit behind ports.

The accessibility tree is the perception channel because it survives the target's hostile markup:
interactive elements have no IDs, layout uses tables, and some buttons are styled spans or
JavaScript links. Role and accessible name carry semantics that pixels lack. AX values can be
redacted field by field, while screenshots require pixel masking. The normalized tree also maps
to Windows UIA and macOS AX, which is why the desktop seam can share the same domain shape.

The target app is self-built so tests are deterministic and hermetic, runtime faults are
injectable, two tenant configurations can exercise reuse, and no public site's terms or real PII
are involved. Discovery-010 used `gpt-6-astra`: six decisions, 18,090 input tokens including
2,742 cached tokens, 1,152 output tokens, and $0.213822. The catalog evidence used
`gpt-5.6-luna`. Total recorded live spend is $3.7975124, including retained escalation-discovery
attempts. Replay makes no model call.

# 2. Artifact schema

A capability contains typed scalar inputs with sensitivity, typed outputs and extractors, ordered
steps, data-only predicates, named outcomes, timing, risk, and provenance. Approval binds a digest
of canonical content; changing reviewed content makes approval stale. Provider-strict schemas use
typed entry lists in place of arbitrary-key maps.

Locator ladders encode a stability argument rather than model preference. Semantic role and name
come first because they survive restyling and most tenant branding. Positional addressing is
allowed only inside a semantically identified container. Coordinates rank last and are never valid
alone. Recorded uniqueness affects ordering, and replay uses `require_unique`; ambiguity fails
closed instead of silently choosing the first match. The savings extractor demonstrates this:
it finds the currency cell inside the row matching the Savings account, itself inside the named
Member accounts table.

Outcome classes are enforced by Pydantic validators. A recoverable outcome must name a bounded
recovery action; a business outcome may not have one. “Member not found” can therefore return as
an answer, while an interstitial can be dismissed only within its declared bound.

# 3. Determinism & error handling

Replay validates inputs and artifact approval, checks the surface fingerprint, evaluates policy,
resolves the recorded ladder, acts, settles, detects outcomes, and evaluates the checkpoint in a
fixed order. The web settle rule requires network quiet, no pending frame navigation, and two
equal consecutive AX snapshots across the stability window. It waits on events and poll timers,
not fixed sleeps. Its typed timeout retains the last hashes, structural diff, pending request
count, and navigation state.

Business outcomes return typed `BusinessOutcome` values. Recovery is journal history rather than
a terminal caller result. The committed capability dismisses an unexpected interstitial at most
twice and re-authenticates an expired session once. Exhausted locators, policy denial, invalid
input, timeout, server failure, and failed checkpoints return `HardFailure` with the step,
expectation, observation, trace, journal head, and evidence reference.

The browser and external UI still introduce timing and version variation. Hashes, fingerprints,
fallback use, settle records, and failures make that variation observable; they do not pretend to
remove it.

# 4. Heterogeneity & multi-tenant

The target app is the legacy-web case: nested iframes, table layout, non-semantic controls, no test
IDs, and tenant-specific labels and field order. The surface therefore observes each frame
separately, records frame paths and frame URLs, then merges them into one portable AX tree. This
was required both for locating controls and for applying policy to the content frame rather than
the unchanged outer frameset URL.

Alpha and beta share one application but have different configuration hashes. Replay compares
`app_version` and `config_hash` in the surface fingerprint, while policy canonicalizes dynamic
routes such as `/member/:id`. The approved alpha capability records drift and fails on beta's
different labels without an override. The reviewed beta override may patch only locator ladders
and checkpoints—never actions, outcomes, risk, inputs, or outputs. It binds the base capability ID
and content digest, so a changed base makes it stale. The committed before and after runs show the
failure and successful reuse.

The Surface protocol also has a desktop stub for UIA/AX. Browser CDP identity and CSS fallback do
not port; a production desktop adapter needs native element identity while preserving the domain
contract.

# 5. Escalation & handoff

Discovery declares `dead_end` after configurable N consecutive actions produce no observation
hash change, or after the same action-target pair occurs three times. It also escalates explicit
help requests, policy denials, budget exhaustion, surface errors, and irreversible confirmation.
The journal records the reason, terminal edge, and triggering observation hash. The concrete
intervention request carries the capability or goal, optional step ID and intent, reason and
detail, redacted inputs, screenshot and AX references, trace ID, resume token, and lease.

A `SessionLease` moves authority through RUNNING, PAUSED, OPERATOR_CONTROL, HANDBACK_PENDING, and
resume or abort. The operator attaches to the same Chromium context over CDP. On handback,
automation re-observes and rechecks the checkpoint instead of trusting the operator's assertion.
The committed evidence used the named `scripted-operator` actor for repeatability. A person uses
the same handoff through the operator console: take control, act in the visible browser, and hand
back before replay resumes.

LangGraph checkpoints persist graph data; the lease protects authority over the live browser.
Production use still needs durable leases, authenticated operators, authorization, and audited
remote transport.

# 6. Safety

The URL allowlist compares parsed scheme, normalized host, explicit wildcard labels, normalized
port, and decoded path. Tests cover userinfo, suffix, query-string, encoded traversal, IDN, case,
and default-port attacks. Risk comes from named policy rules, not the model, and is checked before
planner dispatch and immediately before surface action.

Irreversible discovery actions escalate. Blocking every such action would make the banking flow
unusable; flagging and continuing would permit a bank action without review. Escalation keeps a
named human on the decision. Replay separately requires approved content, recorded irreversible
risk, and the caller's explicit flag.

The retained attempts exposed two real defects. In attempt 3, an irreversible submit ran without
a human because the rule named a route the app never served, allowing the broad safe-click rule
to win. Route self-checks now cover the actual review and beta-extra paths. Attempt 4 showed that
the outer frameset URL remained fixed while the action belonged to a changing content frame;
policy now uses the target-owning frame URL.

Page text is untrusted input; if the model follows injected instructions, policy still refuses
anything outside the allowlist. Persisted sensitive values pass through the observability
redaction choke point. Current replay AX nodes include bounds, and evidence shows region-masked
screenshots. Missing sensitive bounds fail closed to full-viewport masking; discovery-010 retains
one legacy full-viewport image.

Limits remain. Typed username values can appear in later AX snapshots after form entry.
Discovery-010's historical journal contains one raw synthetic balance recorded before output
masking. `member_id` is configured as `internal`; changing that rule to `pii` makes its persisted
representation a hash marker. The local hash-chain head detects editing but cannot authenticate a
same-process rewrite of both journal and anchor.

# 7. Cuts

- Journals and manifests are the evidence format; an HTML viewer over them is next.
- Screenshot masking uses AX bounds when present and full-viewport masking when a sensitive node
  has none; semantic AX snapshots remain the primary failure evidence.
- Operator co-browsing is a local console; durable leases, authentication, authorization, and
  remote transport remain production work.
- The desktop surface is a typed stub; UIA and macOS AX adapters remain unimplemented.
- Journal heads are local; an external append-only signed anchor is not implemented.
- The hand-built OpenAI transport owns retries and error mapping; migration to the official SDK
  remains open.
- Only OpenAI has live evidence; Anthropic compatibility is schema-tested rather than live-tested.
- Stretch goals were limited to two (cross-tenant overrides, agent catalog); MCP, multi-run
  stability and code generation were left out.