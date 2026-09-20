"""Audit prompt provenance against committed evidence without live services."""

import hashlib
import json
from pathlib import Path
from typing import Any, cast

from cua.discovery.prompts import PromptRenderer
from cua.domain.observation import AxNode, FrameInfo, Observation, walk_ax
from cua.policy.models import Budget

ROOT = Path(__file__).resolve().parents[3]
GOAL = "look up member 10023 and read their current savings balance"
URL = "http://127.0.0.1:8099/t/alpha/"


def _records(path: Path) -> list[dict[str, Any]]:
    return [cast(dict[str, Any], json.loads(line)) for line in path.read_text().splitlines()]


def test_every_committed_prompt_id_has_a_template_file() -> None:
    for journal in sorted((ROOT / "evidence").glob("discovery-*/journal.ndjson")):
        for record in _records(journal):
            if record["type"] != "ModelDecided":
                continue
            decision = cast(dict[str, Any], cast(dict[str, Any], record["payload"])["decision"])
            template_id = str(decision["prompt_template_id"])
            assert (ROOT / "config/prompts" / f"{template_id}.txt").is_file(), (
                f"{journal}: missing prompt template {template_id}"
            )


def test_attempt_010_prompt_hashes_reproduce_from_committed_evidence() -> None:
    """Attempt 010 is reproducible because its redacted AX evidence is committed.

    Attempts 001-009 intentionally omit pre-fix AX blobs. Their rendered prompt inputs are absent,
    so their hashes cannot be recomputed; docs/discovery-attempts.md records that known limit.
    """
    root = ROOT / "evidence/discovery-010"
    records = _records(root / "journal.ndjson")
    renderer = PromptRenderer.from_file(ROOT / "config/prompts/discovery-planner.v1.txt")
    budget = Budget()
    observed: Observation | None = None
    checked = 0
    for record in records:
        payload = cast(dict[str, Any], record["payload"])
        if record["type"] == "Observed":
            reference = cast(dict[str, Any], payload["evidence_ref"])
            digest = str(reference["content_hash"])
            stored = cast(
                dict[str, Any],
                json.loads((root / "blobs" / digest[:2] / digest).read_text(encoding="utf-8")),
            )
            ax_root = AxNode.model_validate(stored["ax_root"])
            frame_paths = tuple(dict.fromkeys(node.frame_path for node in walk_ax(ax_root)))
            observed = Observation.model_construct(
                url=URL,
                frames=tuple(FrameInfo(frame_path=path, title="") for path in frame_paths),
                ax_root=ax_root,
            )
        elif record["type"] == "ModelDecided":
            assert observed is not None
            decision = cast(dict[str, Any], payload["decision"])
            rendered = renderer.render(GOAL, (), observed, budget)
            assert hashlib.sha256(rendered.encode()).hexdigest() == decision["prompt_hash"]
            usage = cast(dict[str, Any], decision["usage"])
            budget = budget.model_copy(
                update={
                    "llm_calls": budget.llm_calls + 1,
                    "tokens": budget.tokens
                    + int(usage["input_tokens"])
                    + int(usage["output_tokens"]),
                    "cost_usd": budget.cost_usd + float(usage["cost_usd"] or 0),
                }
            )
            checked += 1
        elif record["type"] == "ActionResult":
            budget = budget.model_copy(update={"steps": budget.steps + 1})
    assert checked == 6
