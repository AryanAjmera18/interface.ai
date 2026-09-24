"""Render versioned, bounded planner prompts; permit file I/O only at this adapter boundary."""

import hashlib
import json
from pathlib import Path

from cua.discovery.state import InputBinding
from cua.domain.common import Digest
from cua.domain.observation import AxNode, Observation
from cua.observability.redaction import json_value, redact, redact_ax_for_prompt
from cua.policy.models import Budget

TEMPLATE_ID = "discovery-planner.v3"


def _node(node: AxNode) -> str:
    states = ",".join(sorted(node.states))
    stable = "/".join((*node.frame_path, *(str(item) for item in node.node_path))) or "root"
    return f"{stable}|{node.role}|{node.name}|{states}"


def compact_ax(observation: Observation, max_chars: int = 8000) -> str:
    """Breadth-first keeps page structure and controls before deep noise; truncate at node lines."""
    queue = [observation.ax_root]
    lines: list[str] = []
    used = 0
    while queue:
        node = queue.pop(0)
        line = _node(node)
        if used + len(line) + 1 > max_chars:
            lines.append("[TRUNCATED: lower-priority descendants omitted]")
            break
        lines.append(line)
        used += len(line) + 1
        queue.extend(node.children)
    return "\n".join(lines)


class PromptRenderer:
    """Templates are versioned files because their bytes are provenance.

    Inline f-strings were rejected because their version would be inseparable from unrelated code.
    """

    def __init__(self, template: str) -> None:
        self.template = template

    @classmethod
    def from_file(cls, path: Path) -> "PromptRenderer":
        return cls(path.read_text(encoding="utf-8"))

    def render(
        self,
        goal: str,
        inputs: tuple[InputBinding, ...],
        observation: Observation,
        budget: Budget,
        previous_result: str = "none",
        outcomes: str = "none",
    ) -> str:
        safe_inputs = []
        for item in inputs:
            value = json_value(redact(item.value, sensitivity=item.sensitivity))
            safe_inputs.append({"name": item.name, "value": value, "sensitivity": item.sensitivity})
        return self.template.format(
            goal=goal,
            inputs=json.dumps(safe_inputs, sort_keys=True),
            url=observation.url or "none",
            frames=json.dumps([frame.frame_path for frame in observation.frames]),
            budget=budget.model_dump_json(),
            previous_result=previous_result,
            outcomes=outcomes,
            ax_tree=compact_ax(
                observation.model_copy(
                    update={"ax_root": redact_ax_for_prompt(observation.ax_root)}
                )
            ),
        )

    def digest(self, rendered: str) -> Digest:
        return hashlib.sha256(rendered.encode()).hexdigest()
