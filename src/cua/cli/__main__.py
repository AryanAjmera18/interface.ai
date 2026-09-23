"""Emit public schemas and load model configuration; forbid target-app imports."""

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from pydantic import JsonValue

from cua.discovery.anthropic import AnthropicLLMClient
from cua.discovery.environment import load_environment
from cua.discovery.fake import FakeLLMClient
from cua.discovery.graph import DiscoveryAgent, build_graph, default_renderer
from cua.discovery.openai import OpenAILLMClient, ProviderResponseError
from cua.discovery.pricing import load_pricing
from cua.discovery.state import DiscoveryState, InputBinding
from cua.domain.common import LiteralRef, ParamRef, SecretRef, ValueRef
from cua.domain.models import ModelProfile, ModelRegistry, ModelRole, fake_registry
from cua.domain.ports import LLMClient, ProviderFailure, RunEnded
from cua.domain.provenance import ModelRef
from cua.domain.schemas import public_schemas
from cua.observability.context import new_run, run_scope
from cua.observability.evidence import EvidenceStore, SurfaceEvidenceSink
from cua.observability.journal import RunJournal
from cua.observability.manifest import ModelEntry, PricingEvidence, write_manifest
from cua.observability.redaction import (
    FieldRule,
    RedactionPolicy,
    json_value,
    redact,
)
from cua.observability.tracing import Tracing
from cua.policy.engine import PolicyEngine
from cua.policy.models import PolicyConfig
from cua.surface.web import PlaywrightWebSurface, WebConfig

app = typer.Typer()
schema = typer.Typer()
app.add_typer(schema, name="schema")
load_environment()


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class UlidGenerator:
    def new(self) -> str:
        import ulid

        return str(ulid.ULID())


class RuntimeValues:
    """Resolve values only at dispatch; synthetic fixture credentials never enter model context."""

    def __init__(self, params: list[str]) -> None:
        self.params = {
            item.split("=", 1)[0]: item.split("=", 1)[1] for item in params if "=" in item
        }

    async def resolve_value(self, reference: ValueRef) -> str:
        if isinstance(reference, LiteralRef):
            return str(reference.value.value)
        if isinstance(reference, ParamRef):
            if reference.name not in self.params:
                raise ValueError(f"No runtime parameter named {reference.name}")
            return self.params[reference.name]
        if isinstance(reference, SecretRef) and reference.name in {"username", "password"}:
            return "synthetic"
        raise ValueError(f"No runtime secret named {reference.name}")


@schema.command()
def emit(output: Path = Path("docs/schema")) -> None:
    """Only public schema metadata reaches disk; no runtime field values are accepted here."""
    output.mkdir(parents=True, exist_ok=True)
    for document in public_schemas():
        (output / f"{document.name}.json").write_text(
            document.text(), encoding="utf-8", newline="\n"
        )


def load_model_registry(path: Path = Path("config/models.yaml")) -> ModelRegistry:
    """Read YAML at the CLI boundary; role overrides are JSON ModelRefs, validated as a whole.

    CUA_MODEL_DISCOVERY_PLANNER, CUA_MODEL_EXTRACTOR and CUA_MODEL_CATALOG_AGENT replace model
    identity atomically; changing only provider while retaining another vendor's ID was rejected.
    JSON is valid YAML, but the shipped human-readable YAML needs the already-installed parser.
    """
    if path.exists():
        # PyYAML has no bundled stubs; use its dynamically loaded parser only at this I/O boundary.
        import importlib

        parser = importlib.import_module("yaml")
        registry = ModelRegistry.model_validate(parser.safe_load(path.read_text(encoding="utf-8")))
    else:
        registry = fake_registry()
    profiles = []
    for profile in registry.profiles:
        override = os.environ.get(f"CUA_MODEL_{profile.role.name}")
        profiles.append(
            ModelProfile(
                role=profile.role,
                max_output_tokens=profile.max_output_tokens,
                model=ModelRef.model_validate_json(override) if override else profile.model,
            )
        )
    return ModelRegistry(profiles=tuple(profiles))


@app.command()
def doctor(
    target: Annotated[str, typer.Option()] = "http://127.0.0.1:8099",
) -> None:
    """Report configuration readiness without exposing credential metadata."""
    import urllib.error
    import urllib.request

    registry = load_model_registry()
    pricing = load_pricing(Path("config/pricing.yaml"))
    typer.echo(f"OPENAI_API_KEY={'present' if os.environ.get('OPENAI_API_KEY') else 'absent'}")
    typer.echo(
        f"ANTHROPIC_API_KEY={'present' if os.environ.get('ANTHROPIC_API_KEY') else 'absent'}"
    )
    for profile in registry.profiles:
        model = profile.model
        effort = model.reasoning_effort or "default"
        typer.echo(f"{profile.role.value}={model.provider}/{model.model_id} reasoning={effort}")
        entry = pricing.get(model.provider, model.model_id)
        filled = bool(
            entry
            and entry.input_per_mtok is not None
            and entry.output_per_mtok is not None
            and entry.source_url is not None
            and entry.retrieved_on is not None
        )
        state = "filled" if filled else "missing"
        typer.echo(f"pricing[{model.provider}/{model.model_id}]={state}")
    try:
        with urllib.request.urlopen(f"{target}/_meta/version", timeout=2) as response:
            reachable = response.status == 200
    except (OSError, urllib.error.URLError):
        reachable = False
    typer.echo(f"target_app={'reachable' if reachable else 'unreachable'}")


@app.command()
def discover(
    goal: Annotated[str, typer.Option()],
    target: Annotated[str, typer.Option()],
    tenant: Annotated[str, typer.Option()] = "alpha",
    param: Annotated[list[str] | None, typer.Option()] = None,
) -> None:
    """Run offline discovery by default; provider adapters may replace the fake client."""
    asyncio.run(_discover(goal, target, tenant, param or []))


async def _discover(goal: str, target: str, tenant: str, params: list[str]) -> None:
    import importlib

    parser = importlib.import_module("yaml")
    policy = PolicyConfig.model_validate(
        parser.safe_load(Path("config/policy.yaml").read_text(encoding="utf-8"))
    )
    clock, ids = SystemClock(), UlidGenerator()
    context = new_run("discovery", clock=clock, ids=ids, tenant_id=tenant)
    root = Path(os.environ.get("CUA_EVIDENCE_DIR", "evidence")) / context.run_id
    root.parent.mkdir(parents=True, exist_ok=True)
    with run_scope(context):
        evidence = EvidenceStore(root.parent, context.run_id, clock=clock, ids=ids)
        journal = RunJournal(root.parent, context.run_id, clock=clock)
        tracing = Tracing(root.parent, clock=clock, ids=ids)
        surface = await PlaywrightWebSurface.launch(
            WebConfig(
                user_data_dir=str(root / "browser-profile"), base_url=target, tenant_id=tenant
            ),
            clock=clock,
            ids=ids,
            evidence=SurfaceEvidenceSink(evidence),
            values=RuntimeValues(params),
        )
        try:
            bindings = tuple(
                InputBinding(
                    name=item.split("=", 1)[0], value=item.split("=", 1)[1], sensitivity="internal"
                )
                for item in params
                if "=" in item
            )
            profile = load_model_registry().get(ModelRole.DISCOVERY_PLANNER)
            pricing_entry = load_pricing(Path("config/pricing.yaml")).get(
                profile.model.provider, profile.model.model_id
            )
            llm: LLMClient
            if profile.model.provider == "anthropic":
                llm = AnthropicLLMClient(
                    profile.model,
                    max_output_tokens=profile.max_output_tokens,
                    pricing=pricing_entry,
                )
            elif profile.model.provider == "openai":
                llm = OpenAILLMClient(
                    profile.model,
                    max_output_tokens=profile.max_output_tokens,
                    pricing=pricing_entry,
                )
            elif profile.model.provider == "fake":
                llm = FakeLLMClient(())
            else:
                raise ValueError(f"No discovery adapter configured for {profile.model.provider}")
            agent = DiscoveryAgent(
                surface=surface,
                llm=llm,
                policy=PolicyEngine(policy),
                journal=journal,
                evidence=evidence,
                ids=ids,
                renderer=default_renderer(),
            )
            failure: Exception | None = None
            terminal_status = "complete"
            try:
                with tracing.span("run"):
                    graph_result = await build_graph(agent).ainvoke(
                        DiscoveryState(goal=goal, inputs=bindings, run_id=context.run_id)
                    )
                    terminal_status = DiscoveryState.model_validate(graph_result).status
            except Exception as exc:
                failure = exc
                provider_error = (
                    ProviderFailure(
                        code=exc.code,
                        safe_message=(f"HTTP {exc.status}; provider type {exc.error_type}"),
                    )
                    if isinstance(exc, ProviderResponseError)
                    else None
                )
                journal.record(
                    RunEnded(
                        result="hard_failure",
                        summary="provider_error" if provider_error else type(exc).__name__,
                        failure_kind=(
                            "provider_request_rejected" if provider_error else type(exc).__name__
                        ),
                        failure_reason=(
                            provider_error.safe_message if provider_error else type(exc).__name__
                        ),
                        provider_error=provider_error,
                    )
                )
            finally:
                tracing.close()
                write_manifest(
                    journal=journal,
                    evidence=evidence,
                    tracing=tracing,
                    clock=clock,
                    inputs=redact(
                        {
                            "goal": json_value(redact(goal, sensitivity="internal")),
                            "parameters": [
                                {
                                    "name": item.name,
                                    "value": json_value(
                                        redact(item.value, sensitivity=item.sensitivity)
                                    ),
                                    "sensitivity": item.sensitivity,
                                }
                                for item in bindings
                            ],
                        }
                    ),
                    result_summary=redact(
                        {"status": "hard_failure" if failure else terminal_status}
                    ),
                    models=(
                        ModelEntry(
                            role=profile.role.value,
                            provider=profile.model.provider,
                            model_id=profile.model.model_id,
                            reasoning_effort=profile.model.reasoning_effort,
                            source="configured",
                        ),
                    ),
                    pricing=(
                        PricingEvidence(
                            provider=profile.model.provider,
                            model_id=profile.model.model_id,
                            source_url=(
                                str(pricing_entry.source_url)
                                if pricing_entry and pricing_entry.source_url
                                else None
                            ),
                            retrieved_on=pricing_entry.retrieved_on if pricing_entry else None,
                        ),
                    ),
                )
            if failure is not None:
                raise failure
        finally:
            await surface.close()
            evidence.close()
            journal.close()
    typer.echo(f"run_id={context.run_id}")
    typer.echo(f"manifest={root / 'manifest.json'}")


def _run_directory(run_id: str, root: Path = Path("evidence")) -> Path:
    for journal in root.glob("*/journal.ndjson"):
        first = journal.read_text(encoding="utf-8").splitlines()[0]
        if f'"run_id":"{run_id}"' in first:
            return journal.parent
    raise typer.BadParameter(f"No evidence run found for {run_id}")


@app.command("compile")
def compile_capability(
    run_dir: Path,
    output: Annotated[Path, typer.Option("-o", "--output")],
) -> None:
    """Deterministically derive a draft capability from recorded evidence."""
    from cua.discovery.compiler import capability_bytes, compile_run
    from cua.domain.capability import capability_content_digest
    from cua.domain.ports import CapabilityCompiled

    capability = compile_run(run_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(capability_bytes(capability))
    journal = RunJournal(
        run_dir.parent,
        capability.provenance.derived_from_run_id,
        clock=SystemClock(),
        directory=run_dir,
    )
    try:
        journal.record(
            CapabilityCompiled(
                capability_id=capability.capability_id,
                content_digest=capability_content_digest(capability),
            )
        )
    finally:
        journal.close()
    typer.echo(str(output))


@app.command("verify")
def verify_capability(artifact: Path) -> None:
    from cua.discovery.verifier import RunProvenanceVerifier
    from cua.domain.capability import Capability

    capability = Capability.model_validate_json(artifact.read_bytes())
    report = RunProvenanceVerifier(
        _run_directory(capability.provenance.derived_from_run_id)
    ).verify(capability)
    for check in report.checks:
        typer.echo(f"{'PASS' if check.passed else 'FAIL'} {check.asserted}: {check.found}")
    if not report.verified:
        raise typer.Exit(1)


@app.command("approve")
def approve_capability(
    artifact: Path,
    actor: Annotated[str, typer.Option()],
    reason: Annotated[str, typer.Option()],
) -> None:
    from cua.discovery.compiler import capability_bytes
    from cua.domain.capability import Capability, capability_content_digest
    from cua.domain.ports import CapabilityApproved
    from cua.domain.provenance import ApprovalRecord

    capability = Capability.model_validate_json(artifact.read_bytes())
    reviewed = capability_content_digest(capability)
    approved = capability.model_copy(
        update={
            "status": "approved",
            "provenance": capability.provenance.model_copy(
                update={
                    "approval": ApprovalRecord(
                        actor=actor,
                        at=SystemClock().now(),
                        reviewed_digest=reviewed,
                        reason=reason,
                    )
                }
            ),
        }
    )
    approved = Capability.model_validate(approved.model_dump(mode="json"))
    run_dir = _run_directory(capability.provenance.derived_from_run_id)
    journal = RunJournal(
        run_dir.parent,
        capability.provenance.derived_from_run_id,
        clock=SystemClock(),
        directory=run_dir,
    )
    try:
        journal.record(
            CapabilityApproved(
                capability_id=capability.capability_id,
                actor=actor,
                reason=reason,
                reviewed_digest=reviewed,
            )
        )
    finally:
        journal.close()
    artifact.write_bytes(capability_bytes(approved))
    typer.echo(f"approved {artifact} digest={reviewed}")


@app.command("diff")
def diff_capabilities(left: Path, right: Path) -> None:
    """Print semantic changes instead of a formatting-sensitive JSON diff."""
    from cua.domain.capability import Capability, capability_content_digest

    before = Capability.model_validate_json(left.read_bytes())
    after = Capability.model_validate_json(right.read_bytes())
    typer.echo(f"status: {before.status} -> {after.status}")
    typer.echo(f"steps: {len(before.steps)} -> {len(after.steps)}")
    for old, new in zip(before.steps, after.steps, strict=False):
        if old != new:
            old_count = len(old.target.candidates) if old.target else 0
            new_count = len(new.target.candidates) if new.target else 0
            typer.echo(
                f"step {old.ordinal}: {old.action.kind}/{old_count} -> "
                f"{new.action.kind}/{new_count} locators"
            )
    typer.echo(f"outcomes: {len(before.outcomes)} -> {len(after.outcomes)}")
    typer.echo(
        f"content_digest: {capability_content_digest(before)} -> {capability_content_digest(after)}"
    )


@app.command("replay")
def replay_capability(
    artifact: Path,
    tenant: Annotated[str, typer.Option()] = "alpha",
    input_value: Annotated[list[str] | None, typer.Option("--input")] = None,
    allow_draft: Annotated[bool, typer.Option()] = False,
    allow_irreversible: Annotated[bool, typer.Option()] = False,
    fault: Annotated[str | None, typer.Option()] = None,
    fault_delay_ms: Annotated[int, typer.Option()] = 0,
    output: Annotated[Path, typer.Option()] = Path("evidence/replay-success"),
) -> None:
    """Replay a capability through its recorded data with no model in the decision loop."""
    asyncio.run(
        _replay_capability(
            artifact,
            tenant,
            input_value or [],
            allow_draft,
            allow_irreversible,
            fault,
            fault_delay_ms,
            output,
        )
    )


async def _replay_capability(
    artifact: Path,
    tenant: str,
    input_values: list[str],
    allow_draft: bool,
    allow_irreversible: bool,
    fault: str | None,
    fault_delay_ms: int,
    output: Path,
) -> None:
    import importlib

    from cua.domain.capability import Capability, capability_content_digest
    from cua.domain.common import StringValue
    from cua.domain.ports import RunStarted
    from cua.observability.journal import JournalAdapter
    from cua.observability.manifest import OutputEntry
    from cua.replay.executor import ReplayExecutor, ReplayInput, ReplayOptions

    capability = Capability.model_validate_json(artifact.read_bytes())
    parser = importlib.import_module("yaml")
    policy = PolicyConfig.model_validate(
        parser.safe_load(Path("config/policy.yaml").read_text(encoding="utf-8"))
    )
    values = {item.split("=", 1)[0]: item.split("=", 1)[1] for item in input_values if "=" in item}
    inputs = tuple(
        ReplayInput(name=name, value=StringValue(value=value))
        for name, value in sorted(values.items())
    )
    clock, ids = SystemClock(), UlidGenerator()
    context = new_run(
        "replay",
        clock=clock,
        ids=ids,
        capability_ref=capability.capability_id,
        capability_content_digest=capability_content_digest(capability),
        tenant_id=tenant,
    ).model_copy(update={"parent_run_id": capability.provenance.derived_from_run_id})
    output.mkdir(parents=True, exist_ok=True)
    with run_scope(context):
        evidence = EvidenceStore(
            output.parent,
            context.run_id,
            clock=clock,
            ids=ids,
            directory=output,
        )
        journal = RunJournal(output.parent, context.run_id, clock=clock, directory=output)
        tracing = Tracing(output.parent, clock=clock, ids=ids, remote=False, directory=output)
        surface = await PlaywrightWebSurface.launch(
            WebConfig(
                user_data_dir=str(output / "browser-profile"),
                base_url="http://127.0.0.1:8099",
                tenant_id=tenant,
            ),
            clock=clock,
            ids=ids,
            evidence=SurfaceEvidenceSink(evidence),
            values=RuntimeValues(input_values),
        )
        if fault is not None:
            await surface.configure_test_fault(fault, delay_ms=fault_delay_ms)
        result = None
        try:
            journal.record(
                RunStarted(kind="replay", parent_run_id=capability.provenance.derived_from_run_id)
            )
            result = await ReplayExecutor(
                surface=surface,
                policy=PolicyEngine(policy),
                evidence=SurfaceEvidenceSink(evidence),
                journal=JournalAdapter(
                    journal,
                    RedactionPolicy(
                        fields=(
                            FieldRule(path="/result/value", sensitivity="pii"),
                            FieldRule(
                                path="/result/attempted/value_ref/value",
                                sensitivity="pii",
                            ),
                        )
                    ),
                ),
                clock=clock,
            ).execute(
                capability,
                inputs,
                ReplayOptions(
                    allow_draft=allow_draft,
                    allow_irreversible=allow_irreversible,
                    trace_id=context.trace_id,
                ),
            )
            terminal = (
                "success"
                if result.kind == "success"
                else "business"
                if result.kind == "business_outcome"
                else "hard_failure"
            )
            journal.record(
                RunEnded.model_validate(
                    {
                        "result": terminal,
                        "summary": result.kind,
                        "failure_kind": (
                            result.failure_kind if result.kind == "hard_failure" else None
                        ),
                        "failure_reason": (
                            result.observed if result.kind == "hard_failure" else None
                        ),
                        "failure_step": result.step_id if result.kind == "hard_failure" else None,
                    }
                )
            )
        finally:
            await surface.close()
            tracing.close()
            assert result is not None
            output_specs = {item.name: item for item in capability.outputs}
            output_entries: list[JsonValue] = []
            if result.kind == "success":
                for name, value in result.outputs.items():
                    spec = output_specs[name]
                    output_entries.append(
                        OutputEntry(
                            name=name,
                            value=json_value(redact(value, sensitivity=spec.sensitivity)),
                            sensitivity=spec.sensitivity,
                        ).model_dump(mode="json")
                    )
            parameter_entries: list[JsonValue] = [
                {
                    "name": item.name,
                    "value": json_value(redact(item.value.value, sensitivity="pii")),
                    "sensitivity": "internal",
                }
                for item in inputs
            ]
            write_manifest(
                journal=journal,
                evidence=evidence,
                tracing=tracing,
                clock=clock,
                inputs=redact(
                    {
                        "goal": None,
                        "parameters": parameter_entries,
                    }
                ),
                result_summary=redact({"status": terminal, "outputs": output_entries}),
            )
            journal.close()
            evidence.close()
    typer.echo(f"{result.kind}: {output / 'manifest.json'}")
    typer.echo(f"capability_digest={capability_content_digest(capability)}")


if __name__ == "__main__":
    app()
