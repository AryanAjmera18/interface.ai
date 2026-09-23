"""Scope immutable run identity across tasks; forbid higher-layer imports."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from cua.domain.common import ULID, Digest, DomainModel
from cua.domain.ports import Clock, IdGenerator


class RunContext(DomainModel):
    run_id: ULID
    kind: Literal["discovery", "replay", "escalation", "verification"]
    trace_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    parent_run_id: ULID | None = None
    capability_ref: ULID | None = None
    capability_content_digest: Digest | None = None
    tenant_id: str | None = None
    started_at: AwareDatetime


_CURRENT: ContextVar[RunContext | None] = ContextVar("cua_run", default=None)


def current_run() -> RunContext:
    context = _CURRENT.get()
    if context is None:
        raise RuntimeError("An observability operation requires run_scope(context)")
    return context


@contextmanager
def run_scope(context: RunContext) -> Iterator[RunContext]:
    """Child tasks inherit a snapshot; resetting a child never changes its parent's context."""
    token = _CURRENT.set(context)
    try:
        yield context
    finally:
        _CURRENT.reset(token)


def new_run(
    kind: Literal["discovery", "replay", "escalation", "verification"],
    *,
    clock: Clock,
    ids: IdGenerator,
    parent: RunContext | None = None,
    capability_ref: str | None = None,
    capability_content_digest: str | None = None,
    tenant_id: str | None = None,
) -> RunContext:
    import hashlib

    run_id = ids.new()
    return RunContext(
        run_id=run_id,
        kind=kind,
        trace_id=parent.trace_id if parent else hashlib.sha256(run_id.encode()).hexdigest()[:32],
        parent_run_id=parent.run_id if parent else None,
        capability_ref=capability_ref,
        capability_content_digest=capability_content_digest,
        tenant_id=tenant_id,
        started_at=clock.now(),
    )
