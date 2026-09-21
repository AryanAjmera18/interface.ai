"""Emit public schemas and load model configuration; forbid target-app imports."""

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

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
from cua.observability.redaction import json_value, redact
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


if __name__ == "__main__":
    app()
