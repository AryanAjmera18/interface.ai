"""Validate domain decisions, error paths and exhaustive results; forbid live-service imports."""

import ast
import importlib
import inspect
import json
from pathlib import Path
from typing import Any, assert_never

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

from cua.domain import actions as a
from cua.domain import capability as c
from cua.domain import common as v
from cua.domain import locators as l
from cua.domain import migrations as m
from cua.domain import models as model
from cua.domain import observation as o
from cua.domain import ports, schemas
from cua.domain import predicates as p
from cua.domain import provenance as prov
from cua.domain import results as r
from cua.domain import steps as s
from cua.domain.names import NameMatcher, normalize_name
from tests.unit.domain.samples import (
    HASH,
    IDENTIFIER,
    NOW,
    FixedClock,
    SequenceIds,
    capability,
    ladder,
    observation,
)

ROOT = Path(__file__).resolve().parents[3]


def model_samples() -> list[v.DomainModel]:
    cap = capability()
    obs = observation()
    target = p.AxTarget(role="textbox", name_matcher=NameMatcher(value="Member ID"))
    pred = p.AxNodeExists(role="textbox", name_matcher=NameMatcher(value="Member ID"))
    evidence = ladder().candidates[0].evidence_ref
    base = ladder().candidates[0].model_dump(exclude={"strategy", "value"})
    candidates = [
        l.ScopedRoleNameCandidate(
            value=l.ScopedRoleNameValue(
                role="textbox", name_matcher=NameMatcher(value="Member ID"), ancestor=target
            ),
            **base,
        ),
        l.AxPathCandidate(value=l.PathValue(path=(2,)), **base),
        l.LabelCandidate(value=l.TextValue(matcher=NameMatcher(value="Member ID")), **base),
        l.VisibleTextCandidate(value=l.TextValue(matcher=NameMatcher(value="Find")), **base),
        l.StructuralCandidate(value=l.PathValue(path=(2,)), **base),
        l.CssCandidate(value=l.CssValue(selector="input"), **base),
        l.CoordinatesCandidate(value=l.CoordinatesValue(x=10, y=20), **base),
    ]
    predicates = [
        pred,
        p.TextMatches(scope=p.TextScope(), regex="Member"),
        p.UrlMatches(pattern="alpha"),
        p.ElementState(target=target, state="disabled", expected=False),
        p.ExtractMatches(output_name="reference", regex="REF"),
        p.AllOf(predicates=(pred,)),
        p.AnyOf(predicates=(pred,)),
        p.NoneOf(predicates=(pred,)),
    ]
    action_values = [
        a.Navigate(url="http://localhost"),
        a.Click(),
        a.SelectOption(value_ref=v.SecretRef(name="key")),
        a.ReadValue(output_name="reference"),
        a.WaitFor(predicate=pred),
        a.Assert(predicate=pred),
        a.Dismiss(),
        a.Scroll(direction="down", amount=100),
        a.TypeText(value_ref=v.LiteralRef(value=v.StringValue(value="synthetic"))),
    ]
    request = model.DecisionRequest(
        decision_id=IDENTIFIER,
        role=model.ModelRole.DISCOVERY_PLANNER,
        instruction="Synthetic decision",
        observation=obs,
        prompt_template_id="fixture.v1",
        tool_schema_hash=HASH,
    )
    decision = model.DecisionResult(
        decision_id=IDENTIFIER,
        intent="Search for the member",
        action=a.Click(),
        target=target,
        model=cap.provenance.models_used[0],
        prompt_template_id="discovery.v1",
        prompt_hash=HASH,
        rationale_digest=HASH,
        tool_call_id="fake-call",
        usage=model.Usage(input_tokens=1, output_tokens=2, cost_usd=0),
        latency_ms=0,
        finish_reason="completed",
    )
    result_values = [
        r.Success(
            outputs={"nested": {"items": [1, True, None]}},
            evidence_ref=evidence,
            steps_executed=1,
            duration_ms=1,
        ),
        r.BusinessOutcome(
            code="missing",
            caller_message="No member",
            detail="Not found",
            partial_outputs={},
            evidence_ref=evidence,
        ),
        r.HardFailure(
            step_id="one",
            step_intent="Find",
            expected="One member",
            observed="None",
            failure_kind="checkpoint",
            evidence_ref=evidence,
            trace_id="trace",
            journal_head_hash=HASH,
        ),
        r.Recovered(
            event_id=IDENTIFIER,
            run_id=IDENTIFIER,
            at=NOW,
            step_id="one",
            outcome_code="expired",
            recovery_kind="reauthenticate",
            attempt=1,
            evidence_ref=evidence,
        ),
        r.JournalEntry(
            event_id=IDENTIFIER,
            run_id=IDENTIFIER,
            at=NOW,
            kind="test",
            payload={"ok": True},
            previous_hash=None,
            hash=HASH,
        ),
    ]
    return [
        cap,
        obs,
        *candidates,
        *predicates,
        *action_values,
        *result_values,
        a.ActionInput(action=a.Click()),
        model.fake_registry(),
        request,
        decision,
        v.NumberValue(value=1),
        v.BooleanValue(value=True),
        v.NullValue(),
        l.CandidateBase(**base),
        p.RegexPredicate(regex=".*"),
        p.EvaluationContext(
            parameters=(p.Binding(name="member_id", value=v.StringValue(value="10001")),)
        ),
        p.evaluate(pred, obs),
        c.RegexValidation(pattern="[0-9]+"),
        c.RangeValidation(minimum=0, maximum=100),
        c.EnumValidation(values=(v.StringValue(value="x"),)),
        c.RecoveryAction(kind="retry", max_attempts=1),
        c.OutputSpec(
            name="x",
            json_type="string",
            produced_by_step_id="one",
            extractor=c.ExtractorSpec(target=target, attribute="value"),
            nullable=False,
            sensitivity="internal",
            description="x",
        ),
        prov.HumanRef(actor="reviewer"),
        prov.HumanEdit(
            actor="reviewer",
            at=NOW,
            field_path="/intent",
            before_hash=HASH,
            after_hash="b" * 64,
            reason="Clarify intent",
        ),
        prov.ApprovalRecord(actor="reviewer", at=NOW, reviewed_digest=HASH, reason="Reviewed"),
        prov.LineageRef(
            capability_id=IDENTIFIER, version="1.0.0", content_hash=HASH, relationship="forked"
        ),
        ports.ExtractedValue(value="synthetic", evidence_ref=evidence),
        ports.VerificationReport(
            verified=True,
            checks=(ports.VerificationCheck(asserted="hash", found="hash", passed=True),),
        ),
        ports.EvidencePayload(media_type="text/plain", content=b"synthetic"),
        ports.EvidenceRelabeled(
            evidence_id=IDENTIFIER,
            original_kind="ax_snapshot",
            corrected_kind="screenshot",
            original_media_type="image/png;base64;redaction=full-viewport",
            corrected_media_type="image/png",
            reason="Historical metadata was misclassified",
        ),
        ports.ManifestRegenerated(
            attempt_directory="discovery-002",
            reason="Correct unknown provider usage",
        ),
        ports.ProviderFailure(code="invalid_schema", safe_message="HTTP 400; invalid schema"),
        ports.TargetMetadataReconstructed(
            fingerprint=obs.fingerprint,
            entry_point="http://127.0.0.1:8099/t/alpha/",
            source_commit="fixture-revision",
            reason="Original journal lacked target metadata",
        ),
        ports.ActionResult(
            attempted=a.Click(),
            before_hash=HASH,
            after_hash=HASH,
            changed=False,
            settle_outcome="settled",
        ),
        ports.Resolution(
            winning_candidate=ladder().candidates[0],
            index=0,
            match_count=1,
            attempts=(
                ports.CandidateAttempt(
                    candidate=ladder().candidates[0], outcome="matched", detail="Unique"
                ),
            ),
            degraded=False,
            degradation_delta=0,
            target=ports.ResolvedTarget(handle="opaque", observation_hash=HASH),
        ),
        ports.ControlToken(
            cdp_endpoint="http://127.0.0.1:8099",
            context_id="context",
            page_guid="page",
            issued_at=NOW,
        ),
        ports.SettleFailure(
            last_hashes=(HASH, HASH),
            pending_requests=1,
            pending_navigation=False,
            diff=(ports.TreeChange(frame_path=(), node_path=(), fields=("name",)),),
        ),
        ports.RunStarted(kind="discovery"),
        ports.PageEvent(observation_hash=HASH),
        ports.Observed(observation_hash=HASH, observation_id=IDENTIFIER, evidence_ref=evidence),
        ports.ModelDecided(observation_hash=HASH, decision=decision),
        ports.PolicyDecision(observation_hash=HASH, allowed=True, rule="allow", reason="safe"),
        ports.ActionAttempted(
            observation_hash=HASH,
            step_id="one",
            intent="Search",
            action=a.Click(),
            target=target,
            locator_ladder=ladder(),
        ),
        ports.LocatorResolved(
            observation_hash=HASH,
            step_id="one",
            winning_index=0,
            attempts=(
                ports.CandidateAttempt(
                    candidate=cap.steps[0].target.candidates[0],
                    outcome="matched",
                    detail="unique",
                ),
            ),
        ),
        ports.ActionResultEvent(
            observation_hash=HASH,
            step_id="one",
            result=ports.ActionResult(
                attempted=a.Click(),
                before_hash=HASH,
                after_hash=HASH,
                changed=False,
                settle_outcome="settled",
            ),
        ),
        ports.CheckpointEvaluated(
            observation_hash=HASH, step_id="one", result=p.evaluate(pred, obs)
        ),
        ports.OutcomeDetected(observation_hash=HASH, code="not_found", classification="business"),
        ports.RecoveredEvent(
            observation_hash=HASH,
            step_id="one",
            outcome_code="expired",
            recovery_kind="reauthenticate",
            attempt=1,
        ),
        ports.DriftObserved(
            observation_hash=HASH, expected_hash=HASH, detail="label", degradation_delta=0.1
        ),
        ports.EscalationRaised(observation_hash=HASH, escalation_id=IDENTIFIER, reason="operator"),
        ports.ControlTransferred(observation_hash=HASH, actor="reviewer"),
        ports.HumanAction(observation_hash=HASH, actor="reviewer", action=a.Click()),
        ports.ControlReturned(observation_hash=HASH, actor="reviewer"),
        ports.CapabilityCompiled(capability_id=cap.capability_id, content_digest=HASH),
        ports.DraftReplayAuthorized(capability_id=cap.capability_id),
        ports.CapabilityApproved(
            capability_id=cap.capability_id,
            actor="Aryan Ajmera (reviewer)",
            reason="Reviewed",
            reviewed_digest=HASH,
        ),
        ports.CostAmended(
            provider="openai",
            model_id="gpt-6-astra",
            input_per_mtok="10",
            cached_input_per_mtok="1",
            output_per_mtok="50",
            currency="USD",
            source_url="https://developers.openai.com/api/docs/pricing",
            retrieved_on="2026-09-20",
            usage=model.Usage(input_tokens=1, output_tokens=2, cost_usd=0.00011),
            reason="pricing entry filled after the run",
        ),
        ports.RunEnded(result="success", summary="complete"),
        m.ArtifactDocument(payload=json.loads(cap.model_dump_json())),
        m.load_capability(cap.model_dump_json(), clock=FixedClock(), ids=SequenceIds()),
        *schemas.public_schemas(),
    ]


def test_every_model_roundtrips_and_forbids_extra() -> None:
    seen: set[type[v.DomainModel]] = set()

    def visit(value: Any) -> None:
        if isinstance(value, v.DomainModel):
            cls = type(value)
            seen.add(cls)
            encoded = value.model_dump_json()
            assert cls.model_validate_json(encoded).model_dump_json() == encoded
            with pytest.raises(ValidationError, match="extra_forbidden"):
                cls.model_validate_json(encoded[:-1] + ',"unrecognized":true}')
            for name in cls.model_fields:
                visit(getattr(value, name))
        elif isinstance(value, (tuple, list)):
            for item in value:
                visit(item)

    for value in model_samples():
        visit(value)
    expected = set()
    for path in (ROOT / "src/cua/domain").glob("*.py"):
        module = importlib.import_module(f"cua.domain.{path.stem}")
        expected.update(
            cls
            for _, cls in inspect.getmembers(module, inspect.isclass)
            if issubclass(cls, v.DomainModel) and cls is not v.DomainModel
        )
    assert expected <= seen, {item.__name__ for item in expected - seen}


@pytest.mark.parametrize(
    "payload,fragment",
    [
        ({"kind": "launch_shell"}, "union_tag_invalid"),
        ({"url": "x"}, "union_tag_not_found"),
        ({"kind": "type_text", "value_ref": {"kind": "secret_ref"}}, "name"),
    ],
)
def test_action_union_errors(payload: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ValidationError, match=fragment):
        TypeAdapter(a.Action).validate_python(payload)


@pytest.mark.parametrize("tenant", ["alpha", "beta"])
def test_committed_tenant_ax_predicates(tenant: str) -> None:
    obs = o.Observation.model_validate_json((ROOT / f"tests/golden/ax-{tenant}.json").read_text())
    assert p.evaluate(
        p.AxNodeExists(role="textbox", name_matcher=NameMatcher(value=" MEMBER-id "), max_count=1),
        obs,
    ).satisfied
    aliases = NameMatcher(mode="one_of", alternatives=("member id", "account holder number"))
    assert p.evaluate(p.AxNodeExists(role="text", name_matcher=aliases), obs).satisfied
    assert not NameMatcher(value="Member ID").matches("Account Holder Number")
    assert p.evaluate(p.TextMatches(scope=p.TextScope(), regex="Member"), obs).satisfied


def test_all_predicate_branches_and_fail_closed_bindings() -> None:
    obs = observation()
    target = p.AxTarget(role="textbox", name_matcher=NameMatcher(value="Member ID"), node_path=(2,))
    yes = p.AxNodeExists(role="textbox", name_matcher=NameMatcher(value="Member ID"))
    no = p.AxNodeExists(role="button", name_matcher=NameMatcher(value="missing"))
    predicates = [
        p.AllOf(predicates=(yes, yes)),
        p.AnyOf(predicates=(no, yes)),
        p.NoneOf(predicates=(no,)),
        p.ElementState(target=target, state="disabled", expected=False),
        p.UrlMatches(pattern="alpha"),
        p.TextMatches(
            scope=p.TextScope(target=target, frame_path=target.frame_path), regex="10001"
        ),
        p.FieldValueEquals(
            target=target, value_ref=v.LiteralRef(value=v.StringValue(value="10001"))
        ),
    ]
    for predicate in predicates:
        result = p.evaluate(predicate, obs)
        assert result.satisfied and result.explanation
    value_pred = p.FieldValueEquals(target=target, value_ref=v.ParamRef(name="member_id"))
    assert not p.evaluate(value_pred, obs).satisfied
    context = p.EvaluationContext(
        parameters=(p.Binding(name="member_id", value=v.StringValue(value="10001")),),
        outputs=(p.Binding(name="reference", value=v.StringValue(value="REF-1")),),
    )
    assert p.evaluate(value_pred, obs, context).satisfied
    assert p.evaluate(
        p.ExtractMatches(output_name="reference", regex="REF"), obs, context
    ).satisfied
    assert not p.evaluate(
        p.ExtractMatches(output_name="missing", regex=".*"), obs, context
    ).satisfied
    assert not p.evaluate(
        p.FieldValueEquals(target=target, value_ref=v.SecretRef(name="member_id")), obs, context
    ).satisfied
    assert not p.evaluate(
        p.UrlMatches(pattern=".*"), obs.model_copy(update={"url": None})
    ).satisfied
    assert not p.evaluate(p.AllOf(predicates=(yes, no)), obs).satisfied
    assert not p.evaluate(p.AnyOf(predicates=(no,)), obs).satisfied
    assert not p.evaluate(p.NoneOf(predicates=(yes,)), obs).satisfied
    assert NameMatcher(mode="exact", value="X").matches("X")
    assert NameMatcher(mode="regex", value="^X").matches("XYZ")
    assert normalize_name("  MEMBER—ID: ") == "member id"


@pytest.mark.parametrize(
    "constructor",
    [
        lambda: NameMatcher(mode="regex", value="["),
        lambda: NameMatcher(mode="one_of"),
        lambda: NameMatcher(),
        lambda: p.TextMatches(scope=p.TextScope(), regex="["),
        lambda: p.UrlMatches(pattern="["),
        lambda: p.AxNodeExists(
            role="x", name_matcher=NameMatcher(value="x"), min_count=2, max_count=1
        ),
    ],
)
def test_invalid_predicates(constructor: Any) -> None:
    with pytest.raises(ValidationError):
        constructor()


@given(st.floats(min_value=-10000, max_value=10000, allow_nan=False, allow_infinity=False))
def test_hash_excludes_bounds_and_focus(position: float) -> None:
    obs = observation()
    child = obs.ax_root.children[2].model_copy(
        update={
            "bounds": o.Bounds(x=position, y=position, w=1, h=1),
            "states": frozenset({"focused"}),
        }
    )
    children = (*obs.ax_root.children[:2], child, obs.ax_root.children[3])
    assert o.ax_digest(obs.ax_root.model_copy(update={"children": children})) == obs.hash


@given(st.text(min_size=1, max_size=30).filter(lambda name: name != "Member search"))
def test_hash_detects_labels(label: str) -> None:
    root = observation().ax_root
    assert o.ax_digest(root.model_copy(update={"name": label})) != o.ax_digest(root)


def test_hash_metadata_exclusions_and_tampering() -> None:
    obs = observation()
    data = obs.model_dump(mode="json")
    data.update(
        observation_id="0" * 26,
        captured_at="2027-01-01T00:00:00Z",
        url=None,
        title="Changed",
        frames=[],
    )
    assert o.Observation.model_validate(data).hash == obs.hash
    data["hash"] = "b" * 64
    with pytest.raises(ValidationError, match="normalized AX tree"):
        o.Observation.model_validate(data)


@given(st.integers(min_value=0, max_value=10), st.floats(min_value=0, max_value=1, allow_nan=False))
def test_locator_rank_ignores_model_preference(count: int, confidence: float) -> None:
    candidate = ladder().candidates[0]
    changed = candidate.model_copy(
        update={"uniqueness_at_record": count, "confidence": confidence, "source": "model"}
    )
    baseline = candidate.model_copy(update={"uniqueness_at_record": count})
    assert l.stability_key(changed) == l.stability_key(baseline)
    items = [candidate, baseline]
    ordered = tuple(sorted(items, key=l.stability_key, reverse=True))
    assert l.LocatorLadder(candidates=ordered).candidates == ordered


def test_locator_rejections() -> None:
    original = ladder().candidates[0]
    coordinate = l.CoordinatesCandidate(
        **original.model_dump(exclude={"strategy", "value"}), value=l.CoordinatesValue(x=1, y=1)
    )
    with pytest.raises(ValidationError, match="Coordinates"):
        l.LocatorLadder(candidates=(coordinate,))
    with pytest.raises(ValidationError, match="Coordinates"):
        l.LocatorLadder(candidates=(coordinate, original))
    with pytest.raises(ValidationError, match="AX-tree or DOM"):
        l.LocatorLadder(candidates=(original.model_copy(update={"source": "model"}),))
    better = original.model_copy(update={"uniqueness_at_record": 1})
    worse = original.model_copy(update={"uniqueness_at_record": 2})
    with pytest.raises(ValidationError, match="stability_key"):
        l.LocatorLadder(candidates=(worse, better))
    assert l.LocatorLadder(candidates=(original, coordinate))


@pytest.mark.parametrize("actor", ["model", "compiler", "human"])
def test_provenance_positive_negative(actor: str) -> None:
    cap = capability()
    data = json.loads(cap.model_dump_json())
    attribution = data["steps"][0]["provenance"]
    if actor == "model":
        attribution["observation_hash"] = None
    elif actor == "compiler":
        attribution["decided_by"] = "compiler"
    else:
        attribution["decided_by"] = {"kind": "human", "actor": "reviewer"}
    with pytest.raises(ValidationError, match="unattributable"):
        c.Capability.model_validate(data)
    invalid = prov.StepProvenance.model_validate(attribution)
    with pytest.raises(prov.UnattributableStepError, match="attach journal evidence"):
        prov.assert_step_attributable("enter-member", invalid)
    if actor == "model":
        attribution["observation_hash"] = observation().hash
    elif actor == "compiler":
        attribution["compiler_rule_id"] = "emit-field.v1"
    else:
        attribution["edits"] = [
            prov.HumanEdit(
                actor="reviewer",
                at=NOW,
                field_path="/intent",
                before_hash=HASH,
                after_hash="b" * 64,
                reason="Name the field",
            ).model_dump(mode="json")
        ]
    assert c.Capability.model_validate(data)


@pytest.mark.parametrize("classification", ["business", "recoverable", "hard"])
@pytest.mark.parametrize("recovery", [False, True])
def test_outcome_matrix(classification: str, recovery: bool) -> None:
    data = capability().outcomes[0].model_dump()
    data.update(
        classification=classification,
        recovery=c.RecoveryAction(kind="retry", max_attempts=1) if recovery else None,
    )
    if recovery == (classification == "recoverable"):
        assert c.OutcomeSpec.model_validate(data)
    else:
        with pytest.raises(ValidationError):
            c.OutcomeSpec.model_validate(data)


def result_branch(result: r.ReplayResult) -> str:
    match result:
        case r.Success():
            return "success"
        case r.BusinessOutcome():
            return "business"
        case r.HardFailure():
            return "hard"
        case unreachable:
            assert_never(unreachable)


def test_result_exhaustiveness_and_recovered_exclusion() -> None:
    found = [
        item
        for item in model_samples()
        if isinstance(item, (r.Success, r.BusinessOutcome, r.HardFailure))
    ]
    assert {result_branch(item) for item in found} == {"success", "business", "hard"}
    recovered = next(item for item in model_samples() if isinstance(item, r.Recovered))
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        TypeAdapter(r.ReplayResult).validate_json(recovered.model_dump_json())


def test_outcome_mapping_and_named_reference_error() -> None:
    step = capability().steps[0]
    assert step.outcome_map is step.outcome_map
    with pytest.raises(TypeError):
        step.outcome_map["new"] = s.StepOutcomeHandling(kind="return")  # type: ignore[index]
    duplicate = step.model_dump()
    duplicate["on_outcome"] *= 2
    with pytest.raises(ValidationError, match="unique"):
        s.Step.model_validate(duplicate)
    data = json.loads(capability().model_dump_json())
    data["steps"][0]["on_outcome"][0]["code"] = "typo"
    with pytest.raises(ValidationError, match="undeclared") as caught:
        c.Capability.model_validate(data)
    assert isinstance(caught.value.errors()[0]["ctx"]["error"], c.UndeclaredOutcomeError)
    second = s.OutcomeHandlingEntry(code="aaa", handling=s.StepOutcomeHandling(kind="fail"))
    changed = s.Step.model_validate({**step.model_dump(), "on_outcome": [*step.on_outcome, second]})
    assert [entry["code"] for entry in changed.model_dump(mode="json")["on_outcome"]] == [
        "aaa",
        "member_not_found",
    ]
    assert "outcome_map" not in changed.model_dump()


@pytest.mark.parametrize(
    "change",
    [
        {"default": {"kind": "number", "value": 1}},
        {"example": {"kind": "boolean", "value": True}},
        {"json_type": "object"},
        {"sensitivity": "secret"},
        {"validation": {"kind": "regex", "pattern": "^Z"}},
    ],
)
def test_parameter_literals_reject_mismatches(change: dict[str, Any]) -> None:
    data = capability().inputs[0].model_dump(mode="json")
    data.update(change)
    with pytest.raises(ValidationError):
        c.ParamSpec.model_validate(data)


def test_migrations_record_identity_with_injected_dependencies() -> None:
    text = schemas.artifact_json(capability())
    loaded = m.load_capability(text, clock=FixedClock(), ids=SequenceIds())
    assert schemas.artifact_json(loaded.capability) == text
    assert loaded.migrations[0].from_version == loaded.migrations[0].to_version == 2
    assert loaded.migrations[0].before_hash == loaded.migrations[0].after_hash
    assert loaded.migrations[0].at == NOW
    for data in ([], {"schema_version": 3}, {"schema_version": 0}, {"schema_version": True}):
        with pytest.raises(ValueError):
            m.load_capability(json.dumps(data), clock=FixedClock(), ids=SequenceIds())


def test_domain_has_no_effect_imports() -> None:
    for path in (ROOT / "src/cua/domain").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not module.startswith("cua.") or module.startswith("cua.domain")
                assert module.split(".")[0] not in {
                    "os",
                    "pathlib",
                    "socket",
                    "httpx",
                    "requests",
                    "subprocess",
                }
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"open", "eval", "exec", "print"}
