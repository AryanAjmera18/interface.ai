# Synthetic bank console

This deliberately legacy-styled internal banking console stands in for a bank vendor's UI.
It is a **test fixture, not part of the automation system under test**. The automation packages
cannot import it; `.importlinter` enforces that boundary, including future `cua.*` packages.
An isolated import-graph test verifies that an actual forbidden import produces exit code 1.

We built this fixture instead of automating a public demo site for four reasons:

1. Deterministic, hermetic tests without external network services or rate limits.
2. Injectable runtime faults, necessary to demonstrate the assignment's error taxonomy.
3. Two configurations of the same product for a real cross-tenant reuse demonstration.
4. No public-site terms-of-service issues or real PII.

All records are synthetic. Authentication is deliberately fake: **any username and password,
including empty values, are accepted**. Credentials are neither read nor retained by the login
handler; no real credentials exist in this fixture or its repository configuration.

## Run

From the repository root, with uv, Python 3.12, and GNU Make available:

```sh
make install
make target-app
```

Open `http://127.0.0.1:8099/t/alpha/` or `http://127.0.0.1:8099/t/beta/`.
The launcher runs one Uvicorn worker, bound only to loopback. There is no JS framework,
build step, fetch, XHR, CDN, or remote asset. Navigation uses ordinary GETs and form POSTs.
The supplemental Faker dependency and its timezone data are exactly pinned in this directory's
`requirements.txt`; the Stage 1 file scope intentionally leaves the root dependency files alone.
`make install` and both CI jobs install these pins after the main locked environment.

## Intentional surface characteristics

- The shell contains Navigation and Content iframes. Content contains another Member workspace
  iframe, where all deep flows occur. Perception and actions must retain the full frame scope.
- Layout uses presentation tables; field text lives in a `<td>` adjacent to the control's `<td>`.
  There are no `<label for=...>` associations. The accounts table remains a real accessible data
  table, with a caption and column headers.
- Several submission actions are `javascript:void(0)` links with an `onclick` handler. Opening
  a sub-account uses a styled span. `requestSubmit()` performs ordinary form submission and
  retains native validation behavior. Link buttons support Space as well as native Enter;
  the span supports both keys and has a tab stop.
- Interactive elements have no IDs or `data-testid` attributes. Opaque CSS classes vary by
  tenant. Form names exist for the legacy submit handlers, not as automation contracts.
- Every interactive control has an accessible name and the appropriate native or explicit role.
  Each frame is titled, headings are structured, errors have alert semantics, and standalone
  dialog pages move focus to their only actionable control. The underlying destination is not
  rendered while an unexpected notice is pending.

**The accessibility tree is the intended perception channel.** The design argument is that
legacy enterprise apps often have hostile markup while retaining enough accessibility to be
operated. An automation strategy should therefore use roles, accessible names, state, and frame
scope rather than CSS classes or positional DOM selectors. This is a deliberate fixture model,
not a claim that every legacy product is accessible. Accessible names follow tenant-varying
visible labels. Roles stay equivalent; automation must explicitly reconcile the different
business wording rather than receiving canonical aliases from the fixture.

`TenantConfig` is the only source of tenant variation: branding, document titles, CSS class,
visible Member ID / Account Holder Number and Open Sub-Account / Add Deposit Product labels,
the order of Nickname and Initial deposit, and beta's extra confirmation dialog. The two
variants share handlers and templates.

## State and flow

Login → member search → search results → member detail and balances → four-field sub-account
form → review → confirmation. Beta inserts its extra dialog between review and submission.
Creation transfers the reviewed deposit from the selected funding account into the new account.
Reviewed values are held server-side; an opaque confirmation token makes retries idempotent.
No account changes occur during review or while a notice or beta confirmation is displayed.

Faker uses an instance-local seed of 731 and the pinned version. IDs are exactly 10001–10050;
10050 is restricted. Balances are deterministic Decimal values. IDs outside the range produce
the `record_not_found` business outcome, with HTTP 200, rather than an error page. Every visible
date is pinned to `2026-01-15`. References are deterministic per session, such as `ALPHA-000001`.

Sessions exist only in this process's memory. The HttpOnly, SameSite=Strict cookie contains an
opaque ID and an HMAC-SHA256 signature, never a member field or credential. Secure is disabled
only because this loopback fixture uses HTTP. A new app instance invalidates existing sessions.
Submitting login starts fresh seeded state for the selected tenant. Creation affects only that
session; it is the irreversible action within a run that the later policy engine will gate.
There is no database, file persistence, telemetry export, or banking-value logging. The launcher
disables server logging, including access logs. The fault CLI emits only structlog JSON containing
fault-control metadata, never its cookie or a banking value. This fixture does not import the
future observability/redaction implementation or implement domain ports for the system under test.

## Fault controls

Use the same cookie jar as the UI client. POST `/_test/faults` **replaces** the caller's active
configuration; GET returns the remaining rules without consuming any. An empty list clears all.
Different clients cannot configure or consume each other's faults. Counts are 1–100 and delays
are 0–10000 ms. Unknown fields, unknown kinds, duplicate kinds, and invalid bounds are rejected.

```json
{"faults":[{"kind":"slow_load","remaining":2,"delay_ms":500}]}
```

| Fault | Eligible request and rendered outcome |
| --- | --- |
| `record_not_found` | Next authenticated `/ui/` operation; forced not-found status independent of ID. |
| `validation_error` | Next POST to `/ui/review`; inline deposit alert, invalid state, and retained values. |
| `permission_denied` | Next authenticated `/ui/` operation; in-app 403 denial. |
| `unexpected_interstitial` | Next authenticated `/ui/` operation; System Notice dialog retains the pending GET/POST. POST to `/ui/dismiss` resumes that operation exactly once. Other navigation remains blocked. |
| `session_expired` | Next `/t/` request, including shell/frame navigation; renders login with an expiry notice and invalidates pending review/control state. |
| `slow_load` | Next `/t/` response, including shell/frame responses; delays delivery by `delay_ms`. |
| `server_error_500` | Next authenticated `/ui/` operation; HTTP 500 with an in-app host-error screen and no mutation. |

Each eligible firing decrements its rule once and removes it at zero. Control and metadata
endpoints never consume faults. Login/frames do not consume business faults. When several are
configured, expiry takes precedence; business priority is notice, server error, permission denial,
then not-found. Validation applies at review. A dismissed notice resumes its stored request without
consuming another business fault; remaining business faults wait for the next operation. Delay
can combine with another outcome. The permission-denied screen and business outcomes return
HTTP 200; the host failure returns HTTP 500 with usable HTML. An executor must classify the
visible UI rather than rely solely on HTTP status.

For a manual demo, sign in through the browser and copy the `cua_session` cookie value from
browser developer tools into the `CUA_TARGET_COOKIE` environment variable. Then run:

```sh
uv run --locked python -m cua.target_app.faults set unexpected_interstitial
uv run --locked python -m cua.target_app.faults set slow_load --count 2 --delay-ms 500
uv run --locked python -m cua.target_app.faults show
uv run --locked python -m cua.target_app.faults clear
```

`--cookie` is also accepted. `--url` defaults to `http://127.0.0.1:8099` and accepts only loopback
HTTP. Set a fault while the browser is idle, then take the next action. The `/_test` endpoints
are deliberately test-only and unauthenticated; this app is not a production banking service.

## Surface fingerprint

GET `/_meta/version` returns `{app_id, app_version, tenant_id, config_hash, ui_revision}` for the
signed-in tenant (alpha before login). `?tenant_id=beta` explicitly selects a configuration.
`config_hash` is SHA-256 over the frozen tenant model's deterministic JSON, independent of
process/session identity. Configuration edits change the hash; template-contract edits must
advance `ui_revision`. A later capability artifact can bind to these values to detect drift.

## Tests

```sh
make lint typecheck test
uv run --locked playwright install chromium
# Bash; in PowerShell set $env:CUA_BROWSER_SMOKE = '1' before the pytest command.
CUA_BROWSER_SMOKE=1 uv run --locked pytest tests/browser/test_target_app_smoke.py -m integration --no-cov
```

The default integration suite uses httpx with ASGITransport: no server sockets or external
network. Browser tests are skipped unless explicitly enabled. Their separate CI job installs
Chromium and checks the actual nested-frame ARIA snapshot in both tenants, then completes the
flow using accessible controls, including keyboard activation of the non-native buttons.

Stage 3 preflight removed shared ARIA labels on tenant-varying controls. Beta also reverses account rows through TenantConfig, testing semantic rather than positional extraction.
