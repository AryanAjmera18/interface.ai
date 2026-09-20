"""Validate dated provider pricing and compute normalized usage cost."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, model_validator

from cua.domain.common import DomainModel
from cua.domain.models import Usage

ModelProvider = Literal["anthropic", "openai", "fake"]


class PricingEntry(DomainModel):
    provider: ModelProvider
    model_id: str = Field(min_length=1)
    input_per_mtok: Decimal | None = Field(default=None, ge=0)
    output_per_mtok: Decimal | None = Field(default=None, ge=0)
    cached_input_per_mtok: Decimal | None = Field(default=None, ge=0)
    currency: str = "USD"
    source_url: HttpUrl | None = None
    retrieved_on: date | None = None

    @model_validator(mode="after")
    def source_and_date_are_atomic(self) -> "PricingEntry":
        if (self.source_url is None) != (self.retrieved_on is None):
            raise ValueError("Pricing source_url and retrieved_on must be supplied together")
        return self


class PricingConfig(DomainModel):
    entries: tuple[PricingEntry, ...]

    @model_validator(mode="after")
    def unique_provider_model_pairs(self) -> "PricingConfig":
        keys = [(entry.provider, entry.model_id) for entry in self.entries]
        if len(keys) != len(set(keys)):
            raise ValueError("Pricing entries must be unique by (provider, model_id)")
        return self

    def get(self, provider: ModelProvider, model_id: str) -> PricingEntry | None:
        return next(
            (e for e in self.entries if e.provider == provider and e.model_id == model_id), None
        )


def load_pricing(path: Path) -> PricingConfig:
    """Perform YAML I/O in discovery, never in the framework-free domain layer."""
    import importlib

    parser = importlib.import_module("yaml")
    return PricingConfig.model_validate(parser.safe_load(path.read_text(encoding="utf-8")))


def price_usage(usage: Usage, entry: PricingEntry | None) -> Usage:
    """Return null cost unless all applicable rates have dated source evidence."""
    if (
        entry is None
        or entry.source_url is None
        or entry.retrieved_on is None
        or entry.input_per_mtok is None
        or entry.output_per_mtok is None
        or (usage.cached_input_tokens and entry.cached_input_per_mtok is None)
    ):
        return usage.model_copy(update={"cost_usd": None})
    uncached = max(usage.input_tokens - usage.cached_input_tokens, 0)
    total = Decimal(uncached) * entry.input_per_mtok
    total += Decimal(usage.output_tokens) * entry.output_per_mtok
    total += Decimal(usage.cached_input_tokens) * (entry.cached_input_per_mtok or Decimal(0))
    return usage.model_copy(update={"cost_usd": float(total / Decimal(1_000_000))})
