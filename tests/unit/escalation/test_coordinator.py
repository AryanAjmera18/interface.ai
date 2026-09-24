"""Test same-session control transfer and unbroken typed history."""

from datetime import UTC, datetime

from cua.domain.actions import Click
from cua.domain.observation import Observation, ax_digest
from cua.domain.ports import ActionResult, ControlToken, JournalEvent, Resolution
from cua.escalation.coordinator import HandoffCoordinator, InMemoryInterventionStore
from cua.escalation.lease import LeaseManager
from cua.escalation.models import InterventionRequest
from tests.unit.domain.samples import FixedClock, SequenceIds, observation


class Journal:
    def __init__(self) -> None:
        self.events: list[JournalEvent] = []

    async def append(self, entry: JournalEvent) -> str:
        self.events.append(entry)
        return f"{len(self.events):064x}"


class Surface:
    def __init__(self, before: Observation, after: Observation) -> None:
        self.before, self.after = before, after
        self.ceded = False
        self.token = ControlToken(
            cdp_endpoint="http://127.0.0.1:9333",
            context_id="context",
            page_guid="page",
            issued_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    async def observe(self) -> Observation:
        return self.after if not self.ceded else self.before

    async def cede_control(self) -> ControlToken:
        self.ceded = True
        return self.token

    async def resume_control(self, token: ControlToken) -> None:
        assert token == self.token
        self.ceded = False

    async def resolve(self, ladder: object) -> Resolution:
        raise AssertionError("not used")

    async def act(
        self, action: object, resolved_target: object = None, *, timing: object = None
    ) -> ActionResult:
        raise AssertionError("not used")

    async def settle(self, timing: object) -> Observation:
        raise AssertionError("not used")


def _request(manager: LeaseManager) -> InterventionRequest:
    lease = manager.create("dead end")
    return InterventionRequest(
        request_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        capability_or_goal="test goal",
        reason="dead_end",
        detail="No control matched",
        trace_id="1" * 32,
        resume_token=lease.resume_token,
        lease=lease,
    )


async def test_handoff_reuses_token_reobserves_and_records_diff() -> None:
    before = observation()
    root = before.ax_root.model_copy(update={"name": "changed"})
    after = before.model_copy(update={"ax_root": root, "hash": ax_digest(root)})
    clock, ids = FixedClock(), SequenceIds()
    manager = LeaseManager(clock, ids)
    surface, journal, store = Surface(before, after), Journal(), InMemoryInterventionStore()
    coordinator = HandoffCoordinator(surface=surface, journal=journal, leases=manager, store=store)
    request = _request(manager)
    await coordinator.open(request, before)
    owned = await coordinator.take_control(request.request_id, "scripted-operator")
    assert owned.cdp_endpoint == surface.token.cdp_endpoint and surface.ceded
    await coordinator.record_action(request.request_id, Click())
    result = await coordinator.hand_back(request.request_id, "completed fixture action")
    assert result.resumed and result.changes and not surface.ceded
    assert [event.type for event in journal.events] == [
        "ControlTransferred",
        "HumanAction",
        "ControlReturned",
        "HumanChangeSummary",
    ]
    assert all(getattr(event, "actor", "") == "scripted-operator" for event in journal.events)
    assert store.get(request.request_id).lease.holder == "automation"
