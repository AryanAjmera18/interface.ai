"""Verify provider usage fixtures and evidence-backed pricing behavior."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from cua.discovery.anthropic import normalize_anthropic_usage
from cua.discovery.pricing import PricingEntry, load_pricing, price_usage
from cua.discovery.usage import normalize_openai_usage


def fixture(name: str) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads((Path("tests/fixtures/providers") / name).read_text(encoding="utf-8")),
    )


def test_recorded_provider_usage_is_normalized() -> None:
    anthropic = normalize_anthropic_usage(fixture("anthropic_usage.json"))
    openai = normalize_openai_usage(fixture("openai_usage.json"))
    assert (anthropic.input_tokens, anthropic.cached_input_tokens, anthropic.output_tokens) == (
        120,
        20,
        30,
    )
    assert (openai.input_tokens, openai.cached_input_tokens, openai.output_tokens) == (150, 50, 40)
    assert openai.reasoning_tokens == 12
    assert openai.output_tokens == 40  # Responses includes billed reasoning in output_tokens.
    assert anthropic.cost_usd is None and openai.cost_usd is None


def test_missing_rates_emit_null_instead_of_false_zero() -> None:
    config = load_pricing(Path("config/pricing.yaml"))
    usage = normalize_anthropic_usage(fixture("anthropic_usage.json"))
    assert price_usage(usage, config.entries[0]).cost_usd is None
    assert price_usage(usage, None).cost_usd is None


def test_dated_source_allows_cost_computation() -> None:
    usage = normalize_anthropic_usage(fixture("anthropic_usage.json"))
    entry = PricingEntry(
        provider="anthropic",
        model_id="fixture-model",
        input_per_mtok=Decimal("2"),
        output_per_mtok=Decimal("4"),
        cached_input_per_mtok=Decimal("1"),
        source_url="https://example.test/pricing",
        retrieved_on=date(2026, 9, 19),
    )
    assert price_usage(usage, entry).cost_usd == 0.00034
