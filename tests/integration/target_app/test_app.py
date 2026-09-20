"""Exercise banking screens and faults without a network server; forbid live-service imports."""

import re
from collections.abc import AsyncIterator
from decimal import Decimal
from time import perf_counter

import httpx
import pytest
from fastapi import FastAPI

from cua.target_app.app import create_app
from cua.target_app.config import ALPHA, BETA, SurfaceVersion, TenantCatalog, TenantConfig
from cua.target_app.models import FaultConfiguration, FaultKind, FaultRule, FormValues
from tests.integration.target_app.helpers import Surface

pytestmark = pytest.mark.integration


@pytest.fixture
def app() -> FastAPI:
    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://fixture"
    ) as c:
        yield c


async def login(client: httpx.AsyncClient, tenant: str = "alpha") -> None:
    response = await client.get(f"/t/{tenant}/")
    assert "Sign in" in response.text
    response = await client.post(f"/t/{tenant}/", data={"username": "fake", "password": "fake"})
    assert 'title="Navigation"' in response.text
    assert 'title="Content"' in response.text


async def test_real_fixture_redirects_off_allowlist(client: httpx.AsyncClient) -> None:
    response = await client.get("/t/alpha/_test/redirect-off-allowlist", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "https://evil.example/redirected"


def values() -> FormValues:
    return FormValues(
        member_id="10001",
        account_type="Savings",
        nickname="Holiday",
        initial_deposit="25.50",
        funding_source="10001-01",
    )


async def inject(
    client: httpx.AsyncClient, kind: FaultKind, count: int = 1, delay_ms: int = 0
) -> None:
    body = FaultConfiguration(faults=[FaultRule(kind=kind, remaining=count, delay_ms=delay_ms)])
    response = await client.post("/_test/faults", json=body.model_dump(mode="json"))
    assert response.status_code == 200


async def remaining(client: httpx.AsyncClient) -> int:
    response = await client.get("/_test/faults")
    body = FaultConfiguration.model_validate_json(response.content)
    return sum(rule.remaining for rule in body.faults)


@pytest.mark.parametrize("tenant", ["alpha", "beta"])
async def test_happy_path(client: httpx.AsyncClient, tenant: str) -> None:
    await login(client, tenant)
    base = f"/t/{tenant}"
    assert "Bank operations" in (await client.get(f"{base}/nav")).text
    assert 'title="Member workspace"' in (await client.get(f"{base}/content")).text
    assert "Member search" in (await client.get(f"{base}/ui/search")).text
    result = await client.post(f"{base}/ui/search", data={"member_id": "10001"})
    assert "Search results" in result.text
    assert 'aria-label="View member 10001"' in result.text
    before = await client.get(f"{base}/ui/detail?member_id=10001")
    assert "Identity summary" in before.text and "2026-01-15" in before.text
    assert "Member accounts" in before.text
    form = await client.get(f"{base}/ui/open?member_id=10001")
    assert 'aria-label="Sub-account details"' in form.text
    review = await client.post(f"{base}/ui/review", data=values().model_dump())
    assert "Review sub-account" in review.text
    for value in ("Savings", "Holiday", "25.50", "10001-01"):
        assert value in review.text
    token = Surface(review.text).tokens[0]
    result = await client.post(f"{base}/ui/confirm", data={"token": token})
    if tenant == "beta":
        assert 'role="dialog"' in result.text
        assert "10001-03" not in (await client.get(f"{base}/ui/detail?member_id=10001")).text
        result = await client.post(f"{base}/ui/commit", data={"token": token})
    assert f"{tenant.upper()}-000001" in result.text
    assert "Sub-account created" in result.text
    after = await client.get(f"{base}/ui/detail?member_id=10001")
    assert "10001-03" in after.text and "Holiday" in after.text
    repeated = await client.post(f"{base}/ui/confirm", data={"token": token})
    assert f"{tenant.upper()}-000001" in repeated.text
    assert (await client.get(f"{base}/ui/detail?member_id=10001")).text == after.text


@pytest.mark.parametrize("member_id", ["10000", "10051", "nonsense", ""])
async def test_not_found_is_business_outcome(client: httpx.AsyncClient, member_id: str) -> None:
    await login(client)
    result = await client.post("/t/alpha/ui/search", data={"member_id": member_id})
    assert result.status_code == 200
    assert 'data-outcome="record_not_found"' in result.text


@pytest.mark.parametrize("action", ["search", "detail", "open", "review"])
async def test_restricted_member(client: httpx.AsyncClient, action: str) -> None:
    await login(client)
    result = await client.post(f"/t/alpha/ui/{action}", data={"member_id": "10050"})
    assert result.status_code == 200
    assert 'data-outcome="permission_denied"' in result.text


@pytest.mark.parametrize(
    "kind", [FaultKind.RECORD_NOT_FOUND, FaultKind.PERMISSION_DENIED, FaultKind.SERVER_ERROR_500]
)
async def test_fault_counts(client: httpx.AsyncClient, kind: FaultKind) -> None:
    await login(client)
    await inject(client, kind, 2)
    for count in (1, 0):
        result = await client.get("/t/alpha/ui/detail?member_id=10001")
        assert result.status_code == (500 if kind == FaultKind.SERVER_ERROR_500 else 200)
        assert f'data-outcome="{kind.value}"' in result.text
        assert await remaining(client) == count
    assert "Identity summary" in (await client.get("/t/alpha/ui/detail?member_id=10001")).text


async def test_validation_fault_clears(client: httpx.AsyncClient) -> None:
    await login(client)
    await inject(client, FaultKind.VALIDATION_ERROR, 2)
    await client.get("/t/alpha/ui/open?member_id=10001")
    assert await remaining(client) == 2
    for count in (1, 0):
        result = await client.post("/t/alpha/ui/review", data=values().model_dump())
        assert 'data-outcome="validation_error"' in result.text
        assert 'aria-invalid="true"' in result.text
        assert 'value="Holiday"' in result.text
        assert await remaining(client) == count
    assert (
        "Review sub-account"
        in (await client.post("/t/alpha/ui/review", data=values().model_dump())).text
    )


async def test_interstitial_preserves_post_and_blocks_navigation(client: httpx.AsyncClient) -> None:
    await login(client)
    await inject(client, FaultKind.UNEXPECTED_INTERSTITIAL, 2)
    for count in (1, 0):
        result = await client.post("/t/alpha/ui/search", data={"member_id": "10001"})
        assert 'aria-label="System Notice"' in result.text
        assert await remaining(client) == count
        blocked = await client.get("/t/alpha/ui/detail?member_id=10002")
        assert 'role="dialog"' in blocked.text
        assert await remaining(client) == count
        result = await client.post("/t/alpha/ui/dismiss")
        assert "Search results" in result.text and "10001" in result.text
    assert "Member search" in (await client.get("/t/alpha/ui/search")).text


async def test_session_expiry_count(client: httpx.AsyncClient) -> None:
    await login(client)
    await inject(client, FaultKind.SESSION_EXPIRED, 2)
    for count in (1, 0):
        result = await client.get("/t/alpha/ui/search")
        assert "Your session has expired" in result.text
        assert await remaining(client) == count
    assert "Please sign in" in (await client.get("/t/alpha/ui/search")).text
    await login(client)
    assert "Member search" in (await client.get("/t/alpha/ui/search")).text


async def test_slow_load_count(client: httpx.AsyncClient) -> None:
    await login(client)
    await inject(client, FaultKind.SLOW_LOAD, 2, 35)
    for count in (1, 0):
        started = perf_counter()
        result = await client.get("/t/alpha/ui/search")
        assert perf_counter() - started >= 0.03
        assert "Member search" in result.text
        assert await remaining(client) == count
    assert "Member search" in (await client.get("/t/alpha/ui/search")).text


async def test_sessions_isolate_faults_and_mutations(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://fixture"
    ) as other:
        await login(client)
        await login(other)
        await inject(client, FaultKind.PERMISSION_DENIED)
        assert "Permission denied" in (await client.get("/t/alpha/ui/search")).text
        assert "Member search" in (await other.get("/t/alpha/ui/search")).text
        assert await remaining(other) == 0
        review = await client.post("/t/alpha/ui/review", data=values().model_dump())
        await client.post("/t/alpha/ui/confirm", data={"token": Surface(review.text).tokens[0]})
        assert "10001-03" in (await client.get("/t/alpha/ui/detail?member_id=10001")).text
        assert "10001-03" not in (await other.get("/t/alpha/ui/detail?member_id=10001")).text


@pytest.mark.parametrize("tenant", [ALPHA, BETA])
async def test_accessible_controls_and_tenant_variations(
    client: httpx.AsyncClient, tenant: TenantConfig
) -> None:
    await login(client, tenant.tenant_id)
    base = f"/t/{tenant.tenant_id}/ui"
    search = await client.get(f"{base}/search")
    assert tenant.member_label in search.text
    assert {(c.role, c.name) for c in Surface(search.text).controls} == {
        ("textbox", tenant.member_label),
        ("button", "Search members"),
    }
    detail = await client.get(f"{base}/detail?member_id=10001")
    assert tenant.open_account_label in detail.text
    assert {(c.role, c.name) for c in Surface(detail.text).controls} == {
        ("button", tenant.open_account_label)
    }
    form = await client.get(f"{base}/open?member_id=10001")
    controls = Surface(form.text).controls
    assert tuple(c.field for c in controls if c.field) == tenant.field_order
    assert {(c.role, c.name) for c in controls} == {
        ("combobox", "Account type"),
        ("textbox", "Nickname"),
        ("textbox", "Initial deposit"),
        ("combobox", "Funding source"),
        ("button", "Review sub-account"),
    }
    for html in (search.text, detail.text, form.text):
        assert 'class="' + tenant.css_class + '"' in html
        assert "data-testid" not in html and "<label" not in html
        for control in Surface(html).controls:
            assert "id" not in control.attributes
            assert control.name


async def test_fingerprint_stable_and_sensitive_to_config() -> None:
    versions: list[SurfaceVersion] = []
    for catalog in (
        TenantCatalog(),
        TenantCatalog(),
        TenantCatalog(tenants=(ALPHA.model_copy(update={"branding": "Changed"}), BETA)),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(create_app(catalog)), base_url="http://fixture"
        ) as client:
            versions.append(
                SurfaceVersion.model_validate_json(
                    (await client.get("/_meta/version?tenant_id=alpha")).content
                )
            )
    assert versions[0] == versions[1]
    assert versions[0].config_hash != versions[2].config_hash
    assert versions[0].config_hash != BETA.config_hash


@pytest.mark.parametrize("deposit", ["abc", "NaN", "Infinity", "0", "-1", "1.001", "999999999"])
async def test_invalid_deposits(client: httpx.AsyncClient, deposit: str) -> None:
    await login(client)
    payload = values().model_copy(update={"initial_deposit": deposit})
    result = await client.post("/t/alpha/ui/review", data=payload.model_dump())
    assert 'data-outcome="validation_error"' in result.text


@pytest.mark.parametrize(
    "field,value", [("funding_source", "10002-01"), ("account_type", "Loan"), ("nickname", "")]
)
async def test_invalid_account_fields(client: httpx.AsyncClient, field: str, value: str) -> None:
    await login(client)
    result = await client.post(
        "/t/alpha/ui/review", data=values().model_copy(update={field: value}).model_dump()
    )
    assert 'data-outcome="validation_error"' in result.text


async def test_unreviewed_confirmation_and_escaping(client: httpx.AsyncClient) -> None:
    await login(client)
    assert "No matching reviewed request" in (await client.post("/t/alpha/ui/confirm")).text
    payload = values().model_copy(update={"nickname": '<script>alert("x")</script>'})
    result = await client.post("/t/alpha/ui/review", data=payload.model_dump())
    assert "&lt;script&gt;" in result.text
    assert '<script>alert("x")</script>' not in result.text


@pytest.mark.parametrize(
    "payload",
    [
        {"faults": [{"kind": "unknown"}]},
        {"faults": [{"kind": "slow_load", "remaining": 0}]},
        {"faults": [{"kind": "record_not_found", "delay_ms": 1}]},
        {"faults": [{"kind": "slow_load"}, {"kind": "slow_load"}]},
        {"unexpected": True},
    ],
)
async def test_invalid_fault_config(client: httpx.AsyncClient, payload: object) -> None:
    response = await client.post("/_test/faults", json=payload)
    assert response.status_code == 422
    assert await remaining(client) == 0


async def test_cookie_tampering_and_cross_tenant_guard(client: httpx.AsyncClient) -> None:
    await login(client)
    assert "Please sign in" in (await client.get("/t/beta/ui/search")).text
    signed = client.cookies.get("cua_session")
    client.cookies.clear()
    assert signed is not None
    client.cookies.set("cua_session", signed + "x", domain="fixture.local", path="/")
    result = await client.get("/t/alpha/ui/search")
    assert "Please sign in" in result.text
    assert client.cookies.get("cua_session") != signed


async def test_unknown_routes_and_version_tenant(client: httpx.AsyncClient) -> None:
    for path in ("/t/nope/", "/t/nope/nav", "/t/nope/ui/search", "/_meta/version?tenant_id=nope"):
        assert (await client.get(path)).status_code == 404
    assert "Please sign in" in (await client.get("/t/alpha/nav")).text
    await login(client, "beta")
    assert (await client.get("/t/beta/unknown")).status_code == 404
    version = SurfaceVersion.model_validate_json((await client.get("/_meta/version")).content)
    assert version.tenant_id == "beta"


async def test_beta_requires_extra_confirmation_and_conserves_funds(
    client: httpx.AsyncClient,
) -> None:
    await login(client, "beta")
    before = (await client.get("/t/beta/ui/detail?member_id=10001")).text
    account_row = (
        r"<tr><td>(10001-\d+)</td><td>[^<]*</td><td>[^<]*</td>"
        r"<td>\$(\d+\.\d{2})</td>"
    )
    old_balances = {account: Decimal(value) for account, value in re.findall(account_row, before)}
    review = await client.post("/t/beta/ui/review", data=values().model_dump())
    token = Surface(review.text).tokens[0]
    premature = await client.post("/t/beta/ui/commit", data={"token": token})
    assert "Additional confirmation is required" in premature.text
    for _ in range(2):
        assert (
            "Submit deposit product?"
            in (await client.post("/t/beta/ui/confirm", data={"token": token})).text
        )
        assert (await client.get("/t/beta/ui/detail?member_id=10001")).text == before
    await client.post("/t/beta/ui/commit", data={"token": token})
    after = (await client.get("/t/beta/ui/detail?member_id=10001")).text
    new_balances = {account: Decimal(value) for account, value in re.findall(account_row, after)}
    assert len(new_balances) == 3
    assert sum(old_balances.values()) == sum(new_balances.values())
    assert new_balances["10001-01"] == old_balances["10001-01"] - Decimal("25.50")
    assert new_balances["10001-03"] == Decimal("25.50")


async def test_notice_defers_mutation_and_replays_once(client: httpx.AsyncClient) -> None:
    await login(client)
    review = await client.post("/t/alpha/ui/review", data=values().model_dump())
    token = Surface(review.text).tokens[0]
    await inject(client, FaultKind.UNEXPECTED_INTERSTITIAL)
    result = await client.post("/t/alpha/ui/confirm", data={"token": token})
    assert "System Notice" in result.text
    result = await client.post("/t/alpha/ui/dismiss")
    assert "ALPHA-000001" in result.text
    await client.post("/t/alpha/ui/dismiss")
    detail = (await client.get("/t/alpha/ui/detail?member_id=10001")).text
    assert "10001-03" in detail and "10001-04" not in detail


async def test_invalid_new_review_invalidates_old_token(client: httpx.AsyncClient) -> None:
    await login(client)
    review = await client.post("/t/alpha/ui/review", data=values().model_dump())
    token = Surface(review.text).tokens[0]
    forged = await client.post("/t/alpha/ui/confirm", data={"token": "\u2603"})
    assert "No matching reviewed request" in forged.text
    await inject(client, FaultKind.VALIDATION_ERROR)
    await client.post("/t/alpha/ui/review", data=values().model_dump())
    assert (
        "No matching reviewed request"
        in (await client.post("/t/alpha/ui/confirm", data={"token": token})).text
    )
    assert "10001-03" not in (await client.get("/t/alpha/ui/detail?member_id=10001")).text


async def test_restart_reseeds_identical_member_data() -> None:
    pages: list[str] = []
    for _ in range(2):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(create_app()), base_url="http://fixture"
        ) as client:
            await login(client)
            pages.append((await client.get("/t/alpha/ui/detail?member_id=10001")).text)
    assert pages[0] == pages[1]


async def test_receipt_keeps_original_balance_after_later_transfer(
    client: httpx.AsyncClient,
) -> None:
    await login(client)
    review = await client.post("/t/alpha/ui/review", data=values().model_dump())
    first_token = Surface(review.text).tokens[0]
    first_receipt = await client.post("/t/alpha/ui/confirm", data={"token": first_token})
    next_form = values().model_copy(
        update={"nickname": "Second", "initial_deposit": "5.00", "funding_source": "10001-03"}
    )
    review = await client.post("/t/alpha/ui/review", data=next_form.model_dump())
    await client.post("/t/alpha/ui/confirm", data={"token": Surface(review.text).tokens[0]})
    repeated = await client.post("/t/alpha/ui/confirm", data={"token": first_token})
    assert repeated.text == first_receipt.text
    assert "Opening balance: $25.50" in repeated.text
