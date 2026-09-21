"""Build inspectable LangGraph discovery control flow; forbid provider-specific decisions.

A graph is used because stopping conditions, policy gates, and human interrupts are control flow.
Representing those edges as data makes them inspectable and independently testable; a while loop
would hide the exact route by which an action reached execution. SQLite restores graph state, not
the live browser session. Session continuity belongs to the later SessionLease; conflating these
two kinds of persistence would resume decisions against an unrelated UI session.
"""

from pathlib import Path
from typing import Any, Literal, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from cua.discovery.candidates import locator_ladder, target_semantics
from cua.discovery.prompts import TEMPLATE_ID, PromptRenderer
from cua.discovery.state import DiscoveryState, EscalationRequest, OutputBinding, TurnRecord
from cua.discovery.tools import action_schema
from cua.domain.actions import Navigate, ReadValue
from cua.domain.common import EvidenceRef, canonical_json, digest
from cua.domain.models import DecisionRequest, ModelRole
from cua.domain.observation import Observation
from cua.domain.ports import (
    ActionAttempted,
    ActionResultEvent,
    CheckpointEvaluated,
    EscalationRaised,
    IdGenerator,
    LLMClient,
    ModelDecided,
    Observed,
    RunEnded,
    Surface,
)
from cua.domain.ports import (
    PolicyDecision as PolicyEvent,
)
from cua.domain.predicates import PredicateResult
from cua.domain.steps import StepTiming
from cua.observability.evidence import EvidenceStore
from cua.observability.journal import RunJournal
from cua.observability.redaction import (
    FieldRule,
    RedactionPolicy,
    redact,
    redacted_ax_payload,
    redaction_policy,
)
from cua.policy.engine import PolicyEngine
from cua.policy.models import PolicyContext


class DiscoveryAgent:
    def __init__(
        self,
        *,
        surface: Surface,
        llm: LLMClient,
        policy: PolicyEngine,
        journal: RunJournal,
        evidence: EvidenceStore,
        ids: IdGenerator,
        renderer: PromptRenderer,
        dead_end_limit: int = 3,
    ) -> None:
        self.surface, self.llm, self.policy = surface, llm, policy
        self.journal, self.evidence, self.ids = journal, evidence, ids
        self.renderer, self.dead_end_limit = renderer, dead_end_limit
        self._last_evidence: EvidenceRef | None = None

    def _record_observation(self, observation: Observation) -> None:
        stored = self.evidence.put(
            redact(
                canonical_json(redacted_ax_payload(observation.ax_root, observation.hash)).encode()
            ),
            "application/json",
            kind="ax_snapshot",
            observation_hash=observation.hash,
            observation_id=observation.observation_id,
        )
        self._last_evidence = stored.to_artifact_ref()
        self.journal.record(
            Observed(
                observation_hash=observation.hash,
                observation_id=observation.observation_id,
                evidence_ref=self._last_evidence,
            )
        )

    async def observe(self, state: DiscoveryState) -> dict[str, Any]:
        try:
            observation = await self.surface.observe()
            self._record_observation(observation)
            return {"last_observation": observation}
        except Exception:
            return self._terminal_update("surface_error", "Surface observation failed")

    async def decide(self, state: DiscoveryState) -> dict[str, Any]:
        observation = state.last_observation
        if observation is None:
            return self._terminal_update("surface_error", "No observation available")
        prompt = self.renderer.render(state.goal, state.inputs, observation, state.budget)
        request = DecisionRequest(
            decision_id=self.ids.new(),
            role=ModelRole.DISCOVERY_PLANNER,
            instruction=prompt,
            observation=observation,
            prompt_template_id=TEMPLATE_ID,
            tool_schema_hash=digest(action_schema()),
        )
        decision = await self.llm.decide(request)
        self.journal.record(ModelDecided(observation_hash=observation.hash, decision=decision))
        budget = state.budget.model_copy(
            update={
                "llm_calls": state.budget.llm_calls + 1,
                "tokens": state.budget.tokens
                + decision.usage.input_tokens
                + decision.usage.output_tokens,
                "cost_usd": state.budget.cost_usd + (decision.usage.cost_usd or 0),
            }
        )
        if decision.action is None:
            edge = (
                "human_help_requested" if decision.finish_reason == "refusal" else "surface_error"
            )
            return {
                **self._terminal_update(edge, f"Model finished with {decision.finish_reason}"),
                "budget": budget,
                "pending_decision": decision,
            }
        semantic = target_semantics(decision.target)
        ladder = (
            locator_ladder(decision.target, observation, self._last_evidence)
            if self._last_evidence is not None
            else None
        )
        return {
            "budget": budget,
            "pending_decision": decision,
            "pending_action": decision.action,
            "pending_target": semantic,
            "pending_ladder": ladder,
        }

    async def check_policy(self, state: DiscoveryState) -> dict[str, Any]:
        if state.pending_action is None or state.last_observation is None:
            return self._terminal_update("surface_error", "Planner produced no executable action")
        context = PolicyContext(
            phase="discovery", url=state.last_observation.url or "about:blank", budget=state.budget
        )
        decision = self.policy.evaluate(state.pending_action, state.pending_target, context)
        self.journal.record(
            PolicyEvent(
                observation_hash=state.last_observation.hash,
                allowed=decision.verdict == "allow",
                verdict=decision.verdict,
                rule=decision.rule_id,
                reason=decision.reason,
            )
        )
        if decision.verdict == "deny":
            edge = "budget_exhausted" if decision.rule_id.startswith("budget.") else "policy_denied"
            return {**self._terminal_update(edge, decision.reason), "pending_policy": decision}
        if decision.verdict == "escalate":
            return {
                **self._terminal_update("human_help_requested", decision.reason),
                "pending_policy": decision,
            }
        return {"pending_policy": decision}

    async def act(self, state: DiscoveryState) -> dict[str, Any]:
        observation, decision = state.last_observation, state.pending_decision
        if observation is None or decision is None or state.pending_action is None:
            return self._terminal_update("surface_error", "Action state is incomplete")
        # Executor-time independent recheck: planner approval is never reused as authority.
        execution = self.policy.evaluate(
            state.pending_action,
            state.pending_target,
            PolicyContext(
                phase="discovery", url=observation.url or "about:blank", budget=state.budget
            ),
        )
        if execution.verdict != "allow":
            return self._terminal_update("policy_denied", execution.reason)
        resolved = (
            await self.surface.resolve(state.pending_ladder) if state.pending_ladder else None
        )
        self.journal.record(
            ActionAttempted(
                observation_hash=observation.hash,
                step_id=decision.decision_id,
                intent=decision.intent,
                action=state.pending_action,
                target=decision.target,
                locator_ladder=state.pending_ladder,
            )
        )
        try:
            result = await self.surface.act(
                state.pending_action,
                resolved.target if resolved else None,
                timing=StepTiming(settle_strategy="ax_stable", timeout_ms=3000),
            )
            # ReadValue outputs are regulated account data even when they do not match a
            # generic PII regex. The journal records a stable hash marker, never the value.
            with redaction_policy(
                RedactionPolicy(fields=(FieldRule(path="/result/value", sensitivity="pii"),))
            ):
                self.journal.record(
                    ActionResultEvent(
                        observation_hash=observation.hash,
                        step_id=decision.decision_id,
                        result=result,
                    )
                )
            return {"budget": state.budget.model_copy(update={"steps": state.budget.steps + 1})}
        except Exception:
            return self._terminal_update("surface_error", "Surface action failed")

    async def verify(self, state: DiscoveryState) -> dict[str, Any]:
        before, decision = state.last_observation, state.pending_decision
        if before is None or decision is None or state.pending_action is None:
            return self._terminal_update("surface_error", "Verification state is incomplete")
        after = await self.surface.observe()
        self._record_observation(after)
        if isinstance(state.pending_action, Navigate) and after.url:
            landed = self.policy.evaluate(
                Navigate(url=after.url),
                None,
                PolicyContext(phase="discovery", url=after.url, budget=state.budget),
            )
            self.journal.record(
                PolicyEvent(
                    observation_hash=after.hash,
                    allowed=landed.verdict == "allow",
                    verdict=landed.verdict,
                    rule=f"redirect.{landed.rule_id}",
                    reason=landed.reason,
                )
            )
            if landed.verdict != "allow":
                return {
                    **self._terminal_update("policy_denied", "Landed URL denied after navigation"),
                    "last_observation": after,
                }
        changed = before.hash != after.hash
        no_change = 0 if changed else state.consecutive_no_change + 1
        key = (
            state.pending_action.kind,
            state.pending_target.model_dump_json() if state.pending_target else "none",
        )
        prior = [
            (turn.action.kind, turn.target.model_dump_json() if turn.target else "none")
            for turn in state.history
        ]
        repeated = 1 + sum(item == key for item in prior[-2:])
        turn = TurnRecord(
            observation_hash=before.hash,
            intent=decision.intent,
            action=state.pending_action,
            target=state.pending_target,
            locator_ladder=state.pending_ladder,
            policy=cast(Any, state.pending_policy),
            after_hash=after.hash,
        )
        self.journal.record(
            CheckpointEvaluated(
                observation_hash=after.hash,
                step_id=decision.decision_id,
                result=PredicateResult(
                    satisfied=changed,
                    explanation="Observation changed" if changed else "No AX change",
                ),
            )
        )
        updates: dict[str, Any] = {
            "last_observation": after,
            "history": (*state.history, turn),
            "consecutive_no_change": no_change,
            "repeated_action_count": repeated,
        }
        if isinstance(state.pending_action, ReadValue):
            updates.update(
                status="goal_reached",
                outputs=(OutputBinding(name=state.pending_action.output_name, value="captured"),),
            )
        elif no_change >= self.dead_end_limit or repeated >= 3:
            updates.update(self._terminal_update("dead_end", "Repeated action made no progress"))
        return updates

    async def terminal(self, state: DiscoveryState) -> dict[str, Any]:
        observation_hash = state.last_observation.hash if state.last_observation else "0" * 64
        if state.status != "goal_reached":
            request = state.escalation or EscalationRequest(
                reason=state.status, terminal_edge=cast(Any, state.status)
            )
            self.journal.record(
                EscalationRaised(
                    observation_hash=observation_hash,
                    escalation_id=self.ids.new(),
                    reason=f"{request.terminal_edge}: {request.reason}",
                )
            )
        result: Literal["success", "hard_failure"] = (
            "success" if state.status == "goal_reached" else "hard_failure"
        )
        self.journal.record(RunEnded(result=result, summary=state.status))
        return {}

    def _terminal_update(self, edge: str, reason: str) -> dict[str, Any]:
        return {
            "status": edge,
            "escalation": EscalationRequest(reason=reason, terminal_edge=cast(Any, edge)),
        }


def build_graph(
    agent: DiscoveryAgent,
    checkpointer: Any = None,
    *,
    interrupt_after: list[str] | None = None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    graph = StateGraph(DiscoveryState)
    for name, node in (
        ("observe", agent.observe),
        ("decide", agent.decide),
        ("check_policy", agent.check_policy),
        ("act", agent.act),
        ("verify", agent.verify),
    ):
        graph.add_node(name, node)
    terminal_names = (
        "goal_reached",
        "budget_exhausted",
        "dead_end",
        "policy_denied",
        "human_help_requested",
        "surface_error",
    )
    for name in terminal_names:
        graph.add_node(name, agent.terminal)
        graph.add_edge(name, END)
    graph.add_edge(START, "observe")
    graph.add_edge("observe", "decide")
    graph.add_conditional_edges(
        "decide",
        lambda state: state.status,
        {"running": "check_policy", **{name: name for name in terminal_names}},
    )
    graph.add_conditional_edges(
        "check_policy",
        lambda state: state.status,
        {"running": "act", **{name: name for name in terminal_names}},
    )
    graph.add_conditional_edges(
        "act",
        lambda state: state.status,
        {"running": "verify", **{name: name for name in terminal_names}},
    )
    graph.add_conditional_edges(
        "verify",
        lambda state: state.status,
        {"running": "decide", **{name: name for name in terminal_names}},
    )
    return graph.compile(checkpointer=checkpointer, interrupt_after=interrupt_after)


def default_renderer(root: Path = Path("config/prompts")) -> PromptRenderer:
    return PromptRenderer.from_file(root / "discovery-planner.v1.txt")
