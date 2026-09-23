"""Deterministic replay unit tests."""

import importlib
from typing import cast

import pytest
from pydantic import JsonValue

from cua.domain.common import EvidenceRef, StringValue
from cua.domain.observation import Observation
from cua.domain.ports import (
    ActionResult,
    CandidateAttempt,
    EvidencePayload,
    JournalEvent,
    Resolution,
    ResolvedTarget,
)
from cua.policy.engine import PolicyEngine
from cua.policy.models import PolicyConfig
from cua.replay.executor import ReplayExecutor, ReplayInput, ReplayOptions
from tests.unit.domain.samples import FixedClock, capability, observation


class FakeEvidence:
    def sensitive_bounds_targets(self, root: object) -> tuple[()]:
        return ()

    async def put(self, payload: EvidencePayload) -> EvidenceRef:
        return EvidenceRef(
            evidence_id="00000000000000000000000009",
            content_hash="9" * 64,
            media_type=payload.media_type,
        )

    def verify(self, reference: EvidenceRef) -> bool:
        return True


class FakeJournal:
    def __init__(self) -> None:
        self.events: list[JournalEvent] = []

    async def append(self, entry: JournalEvent) -> str:
        self.events.append(entry)
        return f"{len(self.events):064x}"


class FakeSurface:
    def __init__(
        self, current: Observation, *, denied: bool = False, degraded: bool = False
    ) -> None:
        self.current = current
        self.actions = 0
        self.denied = denied
        self.degraded = degraded

    async def observe(self) -> Observation:
        return self.current

    async def resolve(self, ladder: object) -> Resolution:
        candidate = capability().steps[0].target.candidates[0]  # type: ignore[union-attr]
        return Resolution(
            winning_candidate=candidate,
            index=0,
            match_count=0 if self.denied else 1,
            attempts=(
                CandidateAttempt(
                    candidate=candidate,
                    outcome="not_found" if self.denied else "matched",
                    detail="fixture",
                ),
            ),
            degraded=self.degraded,
            degradation_delta=1 if self.degraded else 0,
            target=None
            if self.denied
            else ResolvedTarget(handle="one", observation_hash=self.current.hash),
        )

    async def act(
        self, action: object, resolved_target: object = None, *, timing: object = None
    ) -> ActionResult:
        self.actions += 1
        return ActionResult.model_validate(
            {
                "attempted": action,
                "before_hash": self.current.hash,
                "after_hash": self.current.hash,
                "changed": False,
                "settle_outcome": "settled",
                "value": None,
            }
        )

    async def settle(self, timing: object) -> Observation:
        return self.current

    async def cede_control(self) -> object:
        raise AssertionError("not used")

    async def resume_control(self, token: object) -> None:
        raise AssertionError("not used")


def policy() -> PolicyEngine:
    parser = importlib.import_module("yaml")
    from pathlib import Path

    document = cast(
        JsonValue, parser.safe_load(Path("config/policy.yaml").read_text(encoding="utf-8"))
    )
    return PolicyEngine(PolicyConfig.model_validate(document))


@pytest.mark.asyncio
async def test_success_has_no_model_seam() -> None:
    surface = FakeSurface(observation())
    journal = FakeJournal()
    executor = ReplayExecutor(
        surface=surface,
        policy=policy(),
        evidence=FakeEvidence(),
        journal=journal,
        clock=FixedClock(),
    )
    result = await executor.execute(
        capability(),
        (ReplayInput(name="member_id", value=StringValue(value="10001")),),
        ReplayOptions(allow_draft=True, trace_id="trace-one"),
    )
    assert result.kind == "success"
    assert surface.actions == 1
    with pytest.raises(TypeError):
        ReplayExecutor(  # type: ignore[call-arg]
            surface=surface,
            policy=policy(),
            evidence=FakeEvidence(),
            journal=journal,
            clock=FixedClock(),
            llm=object(),
        )


@pytest.mark.asyncio
async def test_invalid_input_stops_before_action() -> None:
    surface = FakeSurface(observation())
    executor = ReplayExecutor(
        surface=surface,
        policy=policy(),
        evidence=FakeEvidence(),
        journal=FakeJournal(),
        clock=FixedClock(),
    )
    result = await executor.execute(
        capability(), (), ReplayOptions(allow_draft=True, trace_id="trace-two")
    )
    assert result.kind == "hard_failure"
    assert result.failure_kind == "invalid_input"
    assert surface.actions == 0


@pytest.mark.asyncio
async def test_locator_exhaustion_is_hard_failure() -> None:
    surface = FakeSurface(observation(), denied=True)
    executor = ReplayExecutor(
        surface=surface,
        policy=policy(),
        evidence=FakeEvidence(),
        journal=FakeJournal(),
        clock=FixedClock(),
    )
    result = await executor.execute(
        capability(),
        (ReplayInput(name="member_id", value=StringValue(value="10001")),),
        ReplayOptions(allow_draft=True, trace_id="trace-three"),
    )
    assert result.kind == "hard_failure"
    assert result.failure_kind == "locator"
    assert surface.actions == 0


@pytest.mark.asyncio
async def test_drift_is_journaled_without_model_fallback() -> None:
    surface = FakeSurface(observation(), degraded=True)
    journal = FakeJournal()
    executor = ReplayExecutor(
        surface=surface,
        policy=policy(),
        evidence=FakeEvidence(),
        journal=journal,
        clock=FixedClock(),
    )
    result = await executor.execute(
        capability(),
        (ReplayInput(name="member_id", value=StringValue(value="10001")),),
        ReplayOptions(allow_draft=True, trace_id="trace-drift"),
    )
    assert result.kind == "success"
    assert any(event.type == "DriftObserved" for event in journal.events)
    assert any(event.type == "LocatorResolved" for event in journal.events)


@pytest.mark.asyncio
async def test_draft_requires_explicit_caller_authorization() -> None:
    surface = FakeSurface(observation())
    executor = ReplayExecutor(
        surface=surface,
        policy=policy(),
        evidence=FakeEvidence(),
        journal=FakeJournal(),
        clock=FixedClock(),
    )
    result = await executor.execute(
        capability(),
        (ReplayInput(name="member_id", value=StringValue(value="10001")),),
        ReplayOptions(trace_id="trace-draft"),
    )
    assert result.kind == "hard_failure"
    assert result.failure_kind == "invalid_input"
    assert "allow_draft" in result.observed
    assert surface.actions == 0
