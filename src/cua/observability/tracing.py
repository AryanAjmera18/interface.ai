"""Export redacted OTel spans; forbid surface and higher-layer imports.

LangSmith covers discovery. Deterministic replay contains no LLM: its evidence is the journal,
OTel spans, and run report. Both paths share one trace_id namespace. Local OTLP is always on;
remote export is an additional sink, never the only copy of evidence.
"""

import hashlib
import os
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, cast

from google.protobuf.json_format import MessageToDict
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.id_generator import IdGenerator as OTelIdGenerator
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import Link, Status, use_span
from opentelemetry.util.types import AttributeValue
from pydantic import JsonValue

from cua.domain.common import canonical_json
from cua.domain.models import DecisionResult
from cua.domain.ports import Clock, IdGenerator
from cua.observability._io import write_bytes
from cua.observability.context import current_run
from cua.observability.redaction import RedactionRequiredError, json_value, redact

SpanKind = Literal[
    "run",
    "agent_step",
    "observation",
    "model_call",
    "locator_resolution",
    "action",
    "checkpoint",
    "policy_decision",
    "replay_step",
    "control_transfer",
]


def _safe_text(value: str, path: str = "") -> str:
    cleaned = json_value(redact(value, field_path=path))
    return cleaned if isinstance(cleaned, str) else canonical_json(cleaned)


def _attributes(values: Mapping[str, AttributeValue] | None) -> dict[str, AttributeValue]:
    """Scrub keys as well as values; raw prompt/tool payloads are never useful trace metadata."""
    result: dict[str, AttributeValue] = {}
    for key, value in (values or {}).items():
        private = any(
            part in key.casefold()
            for part in ("prompt.text", "prompt_text", "tool.arguments", "tool_arguments")
        )
        source = (
            list(value) if isinstance(value, Sequence) and not isinstance(value, str) else value
        )
        safe = json_value(
            redact(
                cast(JsonValue, source),
                field_path="/" + key,
                sensitivity="secret" if private else None,
            )
        )
        result[_safe_text(key)] = (
            safe if isinstance(safe, (str, int, float, bool)) else canonical_json(safe)
        )
    return result


class SanitizedSpan(ReadableSpan):
    """Internal export token; only the processor creates it after sanitizing the entire span."""


def _sanitize(span: ReadableSpan) -> SanitizedSpan:
    scope = span.instrumentation_scope
    return SanitizedSpan(
        name=_safe_text(span.name),
        context=span.context,
        parent=span.parent,
        resource=Resource(_attributes(span.resource.attributes)),
        attributes=_attributes(span.attributes),
        kind=span.kind,
        events=[
            Event(_safe_text(event.name), _attributes(event.attributes), event.timestamp)
            for event in span.events
        ],
        links=[Link(link.context, _attributes(link.attributes)) for link in span.links],
        status=Status(
            span.status.status_code,
            _safe_text(span.status.description) if span.status.description else None,
        ),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=InstrumentationScope(
            _safe_text(scope.name),
            _safe_text(scope.version) if scope.version else None,
            _safe_text(scope.schema_url) if scope.schema_url else None,
            _attributes(cast(Mapping[str, AttributeValue], scope.attributes)),
        )
        if scope
        else None,
    )


def _require_spans(spans: Sequence[ReadableSpan]) -> None:
    if not all(isinstance(span, SanitizedSpan) for span in spans):
        raise RedactionRequiredError("Exporter rejected spans without boundary redaction")


class LocalOTLPExporter(SpanExporter):
    """Write one standard OTLP JSON request per line; no collector or model service required."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        write_bytes(path, redact(b""), append=True)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        _require_spans(spans)
        # Use the SDK's OTLP encoder, not a hand-maintained approximation of OTLP.
        document = cast(JsonValue, MessageToDict(encode_spans(spans)))
        with self._lock:
            write_bytes(self.path, redact((canonical_json(document) + "\n").encode()), append=True)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


class GuardedExporter(SpanExporter):
    def __init__(self, delegate: SpanExporter) -> None:
        self.delegate = delegate

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        _require_spans(spans)
        return self.delegate.export(spans)

    def shutdown(self) -> None:
        self.delegate.shutdown()


class RedactingSpanProcessor(SpanProcessor):
    def __init__(self, exporters: Sequence[SpanExporter]) -> None:
        self.exporters = tuple(GuardedExporter(exporter) for exporter in exporters)
        self.failures = 0
        self._identities: dict[int, tuple[str, str]] = {}
        self._lock = threading.Lock()

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._identities)

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        run = current_run()
        context = span.get_span_context()
        with self._lock:
            self._identities[context.span_id] = (run.run_id, f"{context.trace_id:032x}")
        span.set_attribute("run_id", run.run_id)
        span.set_attribute("trace_id", f"{context.trace_id:032x}")

    def on_end(self, span: ReadableSpan) -> None:
        if span.context is None:
            raise ValueError("Span lacks identity")
        with self._lock:
            run_id, trace_id = self._identities.pop(span.context.span_id)
        cleaned = _sanitize(span)
        # Prevent callers overwriting join keys after on_start.
        cleaned = SanitizedSpan(
            name=cleaned.name,
            context=cleaned.context,
            parent=cleaned.parent,
            resource=cleaned.resource,
            attributes={**(cleaned.attributes or {}), "run_id": run_id, "trace_id": trace_id},
            events=cleaned.events,
            links=cleaned.links,
            kind=cleaned.kind,
            status=cleaned.status,
            start_time=cleaned.start_time,
            end_time=cleaned.end_time,
            instrumentation_scope=cleaned.instrumentation_scope,
        )
        for exporter in self.exporters:
            try:
                if exporter.export((cleaned,)) != SpanExportResult.SUCCESS:
                    self.failures += 1
            except Exception:
                # Exporter exceptions can echo credentials/payloads. Count, never log raw errors.
                self.failures += 1

    def shutdown(self) -> None:
        for exporter in self.exporters:
            exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.failures == 0


class _Ids(OTelIdGenerator):
    def __init__(self, ids: IdGenerator) -> None:
        self.ids = ids

    def generate_span_id(self) -> int:
        return int(hashlib.sha256(self.ids.new().encode()).hexdigest()[:16], 16) or 1

    def generate_trace_id(self) -> int:
        return int(current_run().trace_id, 16) or 1


class Tracing:
    def __init__(
        self,
        root: Path,
        *,
        clock: Clock,
        ids: IdGenerator,
        extra_exporters: Sequence[SpanExporter] = (),
        remote: bool = True,
    ) -> None:
        run = current_run()
        self.clock = clock
        self.path = root / run.run_id / "spans.otlp.jsonl"
        exporters: list[SpanExporter] = [LocalOTLPExporter(self.path), *extra_exporters]
        key = os.environ.get("LANGSMITH_API_KEY") if remote else None
        if key:
            # Verified 2026-09-18: https://docs.langchain.com/langsmith/trace-with-opentelemetry
            # Explicit exporter endpoint needs /v1/traces; generic OTEL env base does not.
            exporters.append(
                OTLPSpanExporter(
                    endpoint="https://api.smith.langchain.com/otel/v1/traces",
                    headers={
                        "x-api-key": key,
                        "Langsmith-Project": os.environ.get("LANGSMITH_PROJECT", "cua"),
                    },
                    timeout=5,
                )
            )
        self.processor = RedactingSpanProcessor(exporters)
        self.provider = TracerProvider(
            resource=Resource({"service.name": "cua"}),
            id_generator=_Ids(ids),
            shutdown_on_exit=False,
        )
        self.provider.add_span_processor(self.processor)
        self.tracer = self.provider.get_tracer("cua.observability", "1")

    @contextmanager
    def span(self, kind: SpanKind) -> Iterator[Span]:
        started = int(self.clock.now().timestamp() * 1_000_000_000)
        span = self.tracer.start_span(kind, start_time=started)
        # Automatic exception recording can capture raw values before the boundary. Export
        # sanitization still covers explicit events; callers choose safe failure codes here.
        with use_span(
            span, end_on_exit=False, record_exception=False, set_status_on_exception=False
        ):
            try:
                yield cast(Span, span)
            finally:
                span.end(end_time=int(self.clock.now().timestamp() * 1_000_000_000))

    def model_metadata(self, span: Span, decision: DecisionResult) -> None:
        span.set_attributes(
            {
                "provider": decision.model.provider,
                "model_id": decision.model.model_id,
                "prompt_hash": decision.prompt_hash,
                "input_tokens": decision.usage.input_tokens,
                "output_tokens": decision.usage.output_tokens,
                "cached_input_tokens": decision.usage.cached_input_tokens,
                "reasoning_tokens": decision.usage.reasoning_tokens,
                "cost_available": decision.usage.cost_usd is not None,
                "latency_ms": decision.latency_ms,
                "finish_reason": decision.finish_reason,
            }
        )

    def close(self) -> None:
        self.provider.shutdown()
