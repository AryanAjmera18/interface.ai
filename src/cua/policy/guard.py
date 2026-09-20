"""Enforce the same policy immediately before dispatch; forbid I/O and frameworks."""

from typing import Protocol

from cua.domain.actions import Action
from cua.policy.engine import PolicyEngine
from cua.policy.models import PolicyContext, PolicyDecision, TargetSemantics


class ActionDispatcher(Protocol):
    async def dispatch(self, action: Action) -> None: ...


class PolicyDeniedError(PermissionError):
    def __init__(self, decision: PolicyDecision) -> None:
        self.decision = decision
        super().__init__(f"Policy {decision.rule_id}: {decision.verdict}")


class GuardedExecutor:
    """Recheck policy immediately before dispatch without trusting a planner decision.

    A check only in an agent loop is bypassed by a bug, prompt injection, or future path that
    constructs actions directly. Both planner and executor call the same engine independently.
    """

    def __init__(self, engine: PolicyEngine, dispatcher: ActionDispatcher) -> None:
        self.engine = engine
        self.dispatcher = dispatcher

    async def dispatch(
        self, action: Action, target: TargetSemantics | None, context: PolicyContext
    ) -> PolicyDecision:
        decision = self.engine.evaluate(action, target, context)
        if decision.verdict != "allow":
            raise PolicyDeniedError(decision)
        await self.dispatcher.dispatch(action)
        return decision
