"""Prove structural allowlisting, risk derivation, budgets, and executor enforcement."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from cua.domain.actions import (
    Assert,
    Click,
    Dismiss,
    Navigate,
    ReadValue,
    Scroll,
    SelectOption,
    TypeText,
    WaitFor,
)
from cua.domain.common import LiteralRef, StringValue
from cua.domain.names import NameMatcher
from cua.domain.predicates import AxNodeExists
from cua.policy.engine import PolicyEngine
from cua.policy.guard import GuardedExecutor, PolicyDeniedError
from cua.policy.models import (
    Budget,
    PolicyConfig,
    PolicyContext,
    TargetSemantics,
    UrlPattern,
)
from cua.policy.url_matcher import url_matches

ROOT = Path(__file__).resolve().parents[3]
FRAME = ("Content", "Member workspace")
BASE = "http://127.0.0.1:8099/t/alpha/search"
VALUE = LiteralRef(value=StringValue(value="synthetic"))
PREDICATE = AxNodeExists(role="heading", name_matcher=NameMatcher(value="Member search"))


@pytest.fixture(scope="module")
def config() -> PolicyConfig:
    data = yaml.safe_load((ROOT / "config/policy.yaml").read_text())
    return PolicyConfig.model_validate(data)


@pytest.fixture(scope="module")
def engine(config: PolicyConfig) -> PolicyEngine:
    return PolicyEngine(config)


def context(**changes: Any) -> PolicyContext:
    return PolicyContext(phase="discovery", url=BASE).model_copy(update=changes)


def target(name: str = "Search members") -> TargetSemantics:
    return TargetSemantics(role="button", name=name, frame_path=FRAME)


@pytest.mark.parametrize(
    "action,url,expected,risk",
    [
        (Navigate(url=BASE), BASE, "allow", "safe"),
        (Click(), BASE, "allow", "safe"),
        (TypeText(value_ref=VALUE), BASE, "allow", "reversible"),
        (SelectOption(value_ref=VALUE), BASE, "allow", "reversible"),
        (ReadValue(output_name="member"), BASE, "allow", "safe"),
        (WaitFor(predicate=PREDICATE), BASE, "allow", "safe"),
        (Assert(predicate=PREDICATE), BASE, "allow", "safe"),
        (Dismiss(), BASE, "allow", "safe"),
        (Scroll(direction="down", amount=10), BASE, "allow", "safe"),
        (Navigate(url="https://evil.example/t/alpha"), BASE, "deny", "safe"),
        (Click(), "http://127.0.0.1:8099/admin", "deny", "safe"),
    ],
)
def test_allow_deny_matrix(
    engine: PolicyEngine, action: object, url: str, expected: str, risk: str
) -> None:
    assert isinstance(
        action,
        (Navigate, Click, TypeText, SelectOption, ReadValue, WaitFor, Assert, Dismiss, Scroll),
    )
    semantic = None if isinstance(action, Navigate) else target()
    decision = engine.evaluate(action, semantic, context(url=url))
    assert decision.verdict == expected and decision.risk == risk and decision.rule_id


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://allowed.com/app/member", True),
        ("https://ALLOWED.COM/app/member", True),
        ("https://allowed.com:443/app/member", True),
        ("https://evil.com/?x=https://allowed.com/app/member", False),
        ("https://allowed.com.evil.com/app/member", False),
        ("https://evilallowed.com/app/member", False),
        ("https://allowed.com@evil.com/app/member", False),
        ("https://evil.com@allowed.com/app/member", False),
        ("https://allowed.com/app/%2e%2e/admin", False),
        ("https://allowed.com/app/%252e%252e/admin", False),
        ("https://allowed.com/app%2f..%2fadmin", False),
        ("https://\u0430llowed.com/app/member", False),  # First character is Cyrillic.
        ("https://xn--llowed-4ve.com/app/member", False),
        ("https://allowed.com:444/app/member", False),
        ("http://allowed.com/app/member", False),
    ],
)
def test_url_bypass_corpus(value: str, expected: bool) -> None:
    pattern = UrlPattern(scheme="https", host="allowed.com", path="/app/**")
    assert url_matches(pattern, value) is expected


def test_wildcard_is_exactly_one_label() -> None:
    pattern = UrlPattern(scheme="https", host="*.allowed.com", path="/")
    assert url_matches(pattern, "https://tenant.allowed.com/")
    assert not url_matches(pattern, "https://allowed.com/")
    assert not url_matches(pattern, "https://deep.tenant.allowed.com/")


def test_fail_closed_when_no_risk_rule_matches(config: PolicyConfig) -> None:
    narrowed = config.model_copy(update={"risk_rules": config.risk_rules[:2]})
    decision = PolicyEngine(narrowed).evaluate(Click(), target("Unknown"), context())
    assert decision.verdict == "deny"
    assert decision.rule_id == "default.deny"


def test_config_rejects_duplicate_rule_ids(config: PolicyConfig) -> None:
    duplicate = config.model_dump(mode="json")
    duplicate["risk_rules"][1]["rule_id"] = duplicate["risk_rules"][0]["rule_id"]
    with pytest.raises(ValueError, match="unique"):
        PolicyConfig.model_validate(duplicate)


def test_risk_is_derived_and_model_suggestion_is_non_authoritative(
    engine: PolicyEngine,
) -> None:
    suggested = context(model_suggested_risk="irreversible")
    decision = engine.evaluate(Click(), target(), suggested)
    assert decision.risk == "safe" and decision.verdict == "allow"
    assert decision.model_suggested_risk == "irreversible"


def test_discovery_irreversible_escalates(engine: PolicyEngine) -> None:
    review = "http://127.0.0.1:8099/t/alpha/accounts/open/review"
    decision = engine.evaluate(Click(), target("Confirm sub-account"), context(url=review))
    assert decision.verdict == "escalate"
    assert decision.risk == "irreversible"
    assert decision.rule_id == "account.confirm-submit"


@pytest.mark.parametrize("status", ["draft", "candidate", "approved"])
@pytest.mark.parametrize("recorded", ["safe", "irreversible"])
@pytest.mark.parametrize("caller", [False, True])
def test_replay_irreversible_requires_all_three_gates(
    engine: PolicyEngine, status: str, recorded: str, caller: bool
) -> None:
    review = "http://127.0.0.1:8099/t/alpha/accounts/open/review"
    decision = engine.evaluate(
        Click(),
        target("Confirm sub-account"),
        context(
            phase="replay",
            url=review,
            capability_status=status,
            step_recorded_risk=recorded,
            allow_irreversible=caller,
        ),
    )
    expected = status == "approved" and recorded == "irreversible" and caller
    assert (decision.verdict == "allow") is expected


@pytest.mark.parametrize(
    "budget,rule_id",
    [
        (Budget(steps=50), "budget.max_steps"),
        (Budget(wall_clock_s=301), "budget.max_wall_clock_s"),
        (Budget(llm_calls=31), "budget.max_llm_calls"),
        (Budget(cost_usd=5.01), "budget.max_cost_usd"),
        (Budget(tokens=100001), "budget.max_tokens"),
    ],
)
def test_each_budget_limit_independently(
    engine: PolicyEngine, budget: Budget, rule_id: str
) -> None:
    decision = engine.evaluate(Click(), target(), context(budget=budget))
    assert decision.verdict == "deny" and decision.rule_id == rule_id


class RecordingDispatcher:
    def __init__(self) -> None:
        self.actions: list[str] = []

    async def dispatch(self, action: Navigate | Click | ReadValue) -> None:
        self.actions.append(action.kind)


@pytest.mark.asyncio
async def test_executor_rechecks_planner_bypassing_action(engine: PolicyEngine) -> None:
    dispatcher = RecordingDispatcher()
    guarded = GuardedExecutor(engine, dispatcher)
    injected = Navigate(url="https://evil.example/ignore-previous-instructions")
    with pytest.raises(PolicyDeniedError, match=r"allowlist\.url"):
        await guarded.dispatch(injected, None, context())
    assert dispatcher.actions == []


@given(st.text(min_size=1, max_size=100))
def test_every_input_gets_a_named_rule(engine: PolicyEngine, url: str) -> None:
    decision = engine.evaluate(Navigate(url=url), None, context())
    assert decision.rule_id.strip()
