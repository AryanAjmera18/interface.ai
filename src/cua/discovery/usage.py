"""Normalize provider response counters into the shared domain usage contract."""

from typing import Any

from cua.domain.models import Usage


def normalize_openai_usage(payload: dict[str, Any]) -> Usage:
    """Map Responses API usage, including cached input, while leaving USD unknown."""
    details = payload.get("input_tokens_details")
    cached = details.get("cached_tokens", 0) if isinstance(details, dict) else 0
    output_details = payload.get("output_tokens_details")
    reasoning = output_details.get("reasoning_tokens", 0) if isinstance(output_details, dict) else 0
    return Usage(
        input_tokens=int(payload.get("input_tokens", 0)),
        cached_input_tokens=int(cached),
        output_tokens=int(payload.get("output_tokens", 0)),
        reasoning_tokens=int(reasoning),
        cost_usd=None,
    )
