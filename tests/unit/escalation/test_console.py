"""Test the minimal operator console through its ASGI boundary."""

import httpx

from cua.domain.observation import ax_digest
from cua.escalation.console import create_operator_app
from cua.escalation.coordinator import HandoffCoordinator, InMemoryInterventionStore
from cua.escalation.lease import LeaseManager
from tests.unit.domain.samples import FixedClock, SequenceIds, observation
from tests.unit.escalation.test_coordinator import Journal, Surface, _request


async def test_operator_console_lists_takes_and_returns_same_session() -> None:
    before = observation()
    root = before.ax_root.model_copy(update={"name": "operator changed page"})
    after = before.model_copy(update={"ax_root": root, "hash": ax_digest(root)})
    manager = LeaseManager(FixedClock(), SequenceIds())
    store = InMemoryInterventionStore()
    coordinator = HandoffCoordinator(
        surface=Surface(before, after), journal=Journal(), leases=manager, store=store
    )
    request = _request(manager)
    await coordinator.open(request, before)
    app = create_operator_app(store, coordinator)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://operator.test"
    ) as client:
        listing = await client.get("/operator")
        assert listing.status_code == 200 and request.request_id in listing.text
        detail = await client.get(f"/operator/{request.request_id}")
        assert "Take control" in detail.text and "No screenshot" in detail.text
        taken = await client.post(
            f"/operator/{request.request_id}/take", params={"actor": "scripted-operator"}
        )
        assert "operator_control" in taken.text
        owned = await client.get(f"/operator/{request.request_id}")
        assert "127.0.0.1:9333" in owned.text
        returned = await client.post(
            f"/operator/{request.request_id}/hand-back", params={"notes": "done"}
        )
        assert "resumed=true" in returned.text
        assert request.request_id not in (await client.get("/operator")).text
