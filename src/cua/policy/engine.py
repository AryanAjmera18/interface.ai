"""Derive fail-closed action policy decisions; forbid I/O and frameworks."""

from typing import Literal

from cua.domain.actions import Action, Navigate
from cua.domain.names import normalize_name
from cua.policy.models import (
    Budget,
    PolicyConfig,
    PolicyContext,
    PolicyDecision,
    Risk,
    RiskRule,
    TargetRule,
    TargetSemantics,
)
from cua.policy.url_matcher import url_matches


def _decision(
    verdict: Literal["allow", "deny", "escalate"],
    rule_id: str,
    reason: str,
    risk: Risk,
    context: PolicyContext,
) -> PolicyDecision:
    return PolicyDecision(
        verdict=verdict,
        rule_id=rule_id,
        reason=reason,
        risk=risk,
        model_suggested_risk=context.model_suggested_risk,
    )


def _budget(config: PolicyConfig, budget: Budget, context: PolicyContext) -> PolicyDecision | None:
    values = (
        (budget.steps + 1, config.limits.max_steps, "max_steps"),
        (budget.wall_clock_s, config.limits.max_wall_clock_s, "max_wall_clock_s"),
        (budget.llm_calls, config.limits.max_llm_calls, "max_llm_calls"),
        (budget.cost_usd, config.limits.max_cost_usd, "max_cost_usd"),
        (budget.tokens, config.limits.max_tokens, "max_tokens"),
    )
    for actual, limit, name in values:
        if actual > limit:
            return _decision("deny", f"budget.{name}", f"Run would exceed {name}", "safe", context)
    return None


def _target_matches(rule: TargetRule | None, target: TargetSemantics | None) -> bool:
    if rule is None:
        return True
    if target is None:
        return False
    if rule.role is not None and normalize_name(rule.role) != normalize_name(target.role or ""):
        return False
    return rule.name is None or normalize_name(rule.name) == normalize_name(target.name or "")


def _risk_rule(
    rules: tuple[RiskRule, ...], action: Action, url: str, target: TargetSemantics | None
) -> RiskRule | None:
    return next(
        (
            rule
            for rule in rules
            if action.kind in rule.action_kinds
            and url_matches(rule.route, url)
            and _target_matches(rule.target, target)
        ),
        None,
    )


class PolicyEngine:
    """Make a named deterministic decision; unmatched input is denied.

    Risk comes only from ordered policy rules. A model that can be talked into performing an
    action can also be talked into calling it safe, so model_suggested_risk is comparison-only.
    AX names are untrusted page input, including prompt injection text; they can select a named
    rule but cannot create permissions the allowlist and rule table did not grant.
    """

    def __init__(self, config: PolicyConfig) -> None:
        self.config = config

    def evaluate(
        self, action: Action, target: TargetSemantics | None, context: PolicyContext
    ) -> PolicyDecision:
        budget = _budget(self.config, context.budget, context)
        if budget is not None:
            return budget
        url = action.url if isinstance(action, Navigate) else context.url
        if not any(url_matches(pattern, url) for pattern in self.config.allowlist.url_patterns):
            return _decision(
                "deny", "allowlist.url", "URL is outside the structural allowlist", "safe", context
            )
        if not isinstance(action, Navigate) and (
            target is None or target.frame_path not in self.config.allowlist.allowed_frames
        ):
            return _decision(
                "deny", "allowlist.frame", "Target frame is not allowed", "safe", context
            )
        route = next(
            (
                item
                for item in self.config.allowlist.action_kinds_by_route
                if url_matches(item.route, url) and action.kind in item.action_kinds
            ),
            None,
        )
        if route is None:
            return _decision(
                "deny",
                "allowlist.action_route",
                "Action kind is not allowed on this route",
                "safe",
                context,
            )
        rule = _risk_rule(self.config.risk_rules, action, url, target)
        if rule is None:
            return _decision(
                "deny", "default.deny", "No risk rule matched; policy fails closed", "safe", context
            )
        if rule.risk != "irreversible":
            return _decision(
                "allow", rule.rule_id, "Named risk rule permits action", rule.risk, context
            )
        return self._irreversible(rule, context)

    def _irreversible(self, rule: RiskRule, context: PolicyContext) -> PolicyDecision:
        """Require authority at recording and replay, at the cost of an unattended bank demo."""
        if self.config.irreversible_policy == "block":
            return _decision(
                "deny", "irreversible.block", "Irreversible actions are blocked", rule.risk, context
            )
        if context.phase == "discovery":
            verdict: Literal["allow", "escalate"] = (
                "allow" if self.config.irreversible_policy == "flag_and_continue" else "escalate"
            )
            return _decision(
                verdict,
                rule.rule_id,
                "Irreversible discovery action needs human confirmation",
                rule.risk,
                context,
            )
        if context.capability_status != "approved":
            return _decision(
                "deny",
                "irreversible.capability_status",
                "Replay capability is not approved",
                rule.risk,
                context,
            )
        if context.step_recorded_risk != "irreversible":
            return _decision(
                "deny",
                "irreversible.recorded_risk",
                "Replay step was not recorded as irreversible",
                rule.risk,
                context,
            )
        if not context.allow_irreversible:
            return _decision(
                "deny",
                "irreversible.caller_flag",
                "Caller did not allow irreversible execution",
                rule.risk,
                context,
            )
        return _decision(
            "allow", rule.rule_id, "All irreversible replay gates hold", rule.risk, context
        )
