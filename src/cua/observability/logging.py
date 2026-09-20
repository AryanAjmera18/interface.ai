"""Render strict structured logs with run identity; forbid higher-layer imports."""

from typing import TextIO

import structlog
from opentelemetry.trace import get_current_span
from structlog.typing import EventDict, WrappedLogger

from cua.observability.context import current_run
from cua.observability.redaction import TaggedValue, checked_json, json_value, redact


def _context(logger: WrappedLogger, method: str, event: EventDict) -> EventDict:
    run = current_run()
    event.update(
        run_id=run.run_id,
        trace_id=run.trace_id,
        span_id=f"{get_current_span().get_span_context().span_id:016x}",
    )
    return event


class StructuredLogger:
    """Require sealed fields before rendering; static event names also pass the redactor.

    Non-strict production mode auto-redacts raw fields rather than turning redaction off.
    The wrapper owns its processors instead of changing global structlog configuration.
    """

    def __init__(self, stream: TextIO, *, strict: bool = True) -> None:
        self.strict = strict
        self.logger = structlog.wrap_logger(
            structlog.PrintLogger(file=stream),
            processors=[_context, structlog.processors.JSONRenderer(sort_keys=True)],
        )

    def info(self, event: str, **fields: TaggedValue) -> None:
        values = {key: checked_json(value, strict=self.strict) for key, value in fields.items()}
        safe = json_value(redact(values))
        if not isinstance(safe, dict):
            raise ValueError("Structured log fields must be an object")
        self.logger.info(json_value(redact(event)), **safe)
