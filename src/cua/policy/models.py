"""Define validated guardrail configuration and decisions; forbid I/O and frameworks."""

from typing import Literal, Self

from pydantic import Field, model_validator

from cua.domain.common import DomainModel, NonEmpty, Sensitivity

ActionKind = Literal[
    "navigate",
    "click",
    "type_text",
    "select_option",
    "read_value",
    "wait_for",
    "assert",
    "dismiss",
    "scroll",
]
Risk = Literal["safe", "reversible", "irreversible"]


class UrlPattern(DomainModel):
    """Match URL components independently; a whole-URL regex obscures trust boundaries."""

    scheme: Literal["http", "https"]
    host: NonEmpty
    port: int | None = Field(default=None, ge=1, le=65535)
    path: NonEmpty


class RouteActions(DomainModel):
    route: UrlPattern
    action_kinds: tuple[ActionKind, ...] = Field(min_length=1)


class TargetRule(DomainModel):
    role: str | None = None
    name: str | None = None


class RiskRule(DomainModel):
    rule_id: NonEmpty
    action_kinds: tuple[ActionKind, ...] = Field(min_length=1)
    route: UrlPattern
    target: TargetRule | None = None
    risk: Risk


class AllowlistConfig(DomainModel):
    url_patterns: tuple[UrlPattern, ...] = Field(min_length=1)
    allowed_frames: tuple[tuple[str, ...], ...] = Field(min_length=1)
    action_kinds_by_route: tuple[RouteActions, ...] = Field(min_length=1)


class FieldSensitivityOverride(DomainModel):
    field_path: NonEmpty
    sensitivity: Sensitivity


class DataPolicy(DomainModel):
    field_sensitivity_overrides: tuple[FieldSensitivityOverride, ...] = ()
    max_chars_extracted: int = Field(gt=0)
    screenshot_policy: Literal["deny", "masked_only", "allow"]


class Limits(DomainModel):
    max_steps: int = Field(gt=0)
    max_wall_clock_s: float = Field(gt=0)
    max_llm_calls: int = Field(ge=0)
    max_cost_usd: float = Field(ge=0)
    max_tokens: int = Field(ge=0)


class PolicyConfig(DomainModel):
    allowlist: AllowlistConfig
    risk_rules: tuple[RiskRule, ...] = Field(min_length=1)
    irreversible_policy: Literal["block", "require_human_confirmation", "flag_and_continue"]
    data: DataPolicy
    limits: Limits

    @model_validator(mode="after")
    def unique_rule_ids(self) -> Self:
        ids = [rule.rule_id for rule in self.risk_rules]
        if len(ids) != len(set(ids)):
            raise ValueError("risk rule_id values must be unique")
        return self


class Budget(DomainModel):
    """Immutable run-budget snapshot carried in policy context and updated by the run owner.

    The policy layer cannot mutate RunContext without coupling the pure engine to orchestration.
    Carrying the snapshot in that context makes every limit decision deterministic and journalable.
    """

    steps: int = Field(ge=0, default=0)
    wall_clock_s: float = Field(ge=0, default=0)
    llm_calls: int = Field(ge=0, default=0)
    cost_usd: float = Field(ge=0, default=0)
    tokens: int = Field(ge=0, default=0)


class TargetSemantics(DomainModel):
    role: str | None = None
    name: str | None = None
    frame_path: tuple[str, ...] = ()


class PolicyContext(DomainModel):
    phase: Literal["discovery", "replay"]
    url: NonEmpty
    budget: Budget = Field(default_factory=Budget)
    capability_status: Literal["draft", "candidate", "approved"] | None = None
    step_recorded_risk: Risk | None = None
    allow_irreversible: bool = False
    model_suggested_risk: Risk | None = None


class PolicyDecision(DomainModel):
    verdict: Literal["allow", "deny", "escalate"]
    rule_id: NonEmpty
    reason: NonEmpty
    risk: Risk
    model_suggested_risk: Risk | None = None
