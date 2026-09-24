"""Coordinate one live-browser handoff; forbid discovery and replay framework dependencies."""

from cua.domain.actions import Action
from cua.domain.observation import Observation
from cua.domain.ports import (
    ControlReturned,
    ControlToken,
    ControlTransferred,
    HumanAction,
    HumanChangeSummary,
    JournalSink,
    Surface,
)
from cua.escalation.lease import LeaseManager
from cua.escalation.models import HandoffResult, InterventionRequest
from cua.surface.base import tree_diff


class InMemoryInterventionStore:
    """Keep the demo operator surface small; a durable queue is intentionally out of scope."""

    def __init__(self) -> None:
        self._items: dict[str, InterventionRequest] = {}

    def add(self, request: InterventionRequest) -> None:
        self._items[request.request_id] = request

    def get(self, request_id: str) -> InterventionRequest:
        try:
            return self._items[request_id]
        except KeyError as error:
            raise LookupError("Unknown intervention request") from error

    def replace(self, request: InterventionRequest) -> None:
        if request.request_id not in self._items:
            raise LookupError("Unknown intervention request")
        self._items[request.request_id] = request

    def open(self) -> tuple[InterventionRequest, ...]:
        return tuple(
            item
            for item in self._items.values()
            if item.status in {"open", "operator_control", "handback_pending"}
        )


class HandoffCoordinator:
    """Hold browser and graph concerns apart across an operator intervention.

    The surface's ControlToken preserves the live CDP session. A graph checkpointer separately
    preserves discovery state. Handback always resumes the token, observes the current page, and
    records an AX diff; it never treats restored graph state as proof of browser continuity.
    """

    def __init__(
        self,
        *,
        surface: Surface,
        journal: JournalSink,
        leases: LeaseManager,
        store: InMemoryInterventionStore,
    ) -> None:
        self.surface, self.journal, self.leases, self.store = surface, journal, leases, store
        self._tokens: dict[str, ControlToken] = {}
        self._before: dict[str, Observation] = {}

    async def open(self, request: InterventionRequest, observation: Observation) -> None:
        paused = self.leases.pause(request.lease)
        self._before[request.request_id] = observation
        self.store.add(request.model_copy(update={"lease": paused, "status": "open"}))

    async def take_control(self, request_id: str, actor: str) -> InterventionRequest:
        request = self.store.get(request_id)
        lease = self.leases.take_control(request.lease)
        if lease.state == "ABORTED":
            aborted = request.model_copy(update={"lease": lease, "status": "aborted"})
            self.store.replace(aborted)
            return aborted
        token = await self.surface.cede_control()
        self._tokens[request_id] = token
        updated = request.model_copy(
            update={
                "lease": lease,
                "status": "operator_control",
                "operator_actor": actor,
                "cdp_endpoint": token.cdp_endpoint,
            }
        )
        self.store.replace(updated)
        await self.journal.append(
            ControlTransferred(observation_hash=self._before[request_id].hash, actor=actor)
        )
        return updated

    async def record_action(self, request_id: str, action: Action) -> None:
        request = self.store.get(request_id)
        if request.status != "operator_control" or request.operator_actor is None:
            raise RuntimeError("Human action requires operator control")
        await self.journal.append(
            HumanAction(
                observation_hash=self._before[request_id].hash,
                actor=request.operator_actor,
                action=action,
            )
        )

    async def hand_back(self, request_id: str, notes: str) -> HandoffResult:
        request = self.store.get(request_id)
        if request.operator_actor is None:
            raise RuntimeError("No named operator owns this request")
        pending = self.leases.request_handback(request.lease)
        self.store.replace(
            request.model_copy(update={"lease": pending, "status": "handback_pending"})
        )
        token = self._tokens.pop(request_id)
        await self.surface.resume_control(token)
        after = await self.surface.observe()
        before = self._before.pop(request_id)
        changes = tree_diff(before, after)
        await self.journal.append(
            ControlReturned(observation_hash=after.hash, actor=request.operator_actor)
        )
        await self.journal.append(
            HumanChangeSummary(
                observation_hash=after.hash,
                actor=request.operator_actor,
                before_hash=before.hash,
                after_hash=after.hash,
                changes=changes,
                notes=notes,
            )
        )
        running = self.leases.resume(pending, request.resume_token)
        self.store.replace(
            request.model_copy(
                update={
                    "lease": running,
                    "status": "resolved",
                    "operator_notes": notes,
                    "cdp_endpoint": None,
                }
            )
        )
        return HandoffResult(
            request_id=request.request_id,
            resumed=True,
            aborted=False,
            actor=request.operator_actor,
            notes=notes,
            changes=changes,
            at=self.leases.clock.now(),
        )
