"""Replay scripted model decisions offline; forbid provider SDK imports."""

import hashlib

from cua.domain.models import DecisionRequest, DecisionResult, Usage
from cua.domain.provenance import ModelRef


class FakeLLMClient:
    def __init__(self, decisions: tuple[DecisionResult, ...]) -> None:
        self.decisions = decisions
        self.requests: list[DecisionRequest] = []

    async def decide(self, request: DecisionRequest) -> DecisionResult:
        self.requests.append(request)
        if not self.decisions:
            return DecisionResult(
                decision_id=request.decision_id,
                intent="Request human help",
                action=None,
                target=None,
                model=ModelRef(
                    provider="fake",
                    model_id="deterministic-v1",
                    api_flavor="offline",
                    structured_output_mode="json_schema",
                ),
                prompt_template_id=request.prompt_template_id,
                prompt_hash=hashlib.sha256(request.instruction.encode()).hexdigest(),
                rationale_digest=None,
                tool_call_id="fake-exhausted",
                usage=Usage(input_tokens=0, output_tokens=0, cost_usd=0),
                latency_ms=0,
                finish_reason="refusal",
            )
        scripted, self.decisions = self.decisions[0], self.decisions[1:]
        return scripted.model_copy(
            update={
                "decision_id": request.decision_id,
                "prompt_template_id": request.prompt_template_id,
                "prompt_hash": hashlib.sha256(request.instruction.encode()).hexdigest(),
            }
        )
