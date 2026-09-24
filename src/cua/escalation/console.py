"""Serve the minimal operator handoff console; forbid discovery and replay imports."""

from collections.abc import Callable
from html import escape

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response

from cua.domain.common import EvidenceRef
from cua.escalation.coordinator import HandoffCoordinator, InMemoryInterventionStore
from cua.escalation.models import InterventionRequest


def create_operator_app(
    store: InMemoryInterventionStore,
    coordinator: HandoffCoordinator,
    *,
    evidence_reader: Callable[[EvidenceRef], bytes] | None = None,
) -> FastAPI:
    """Expose polling rather than co-browsing, a deliberate take-home scope cut."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def request_or_404(request_id: str) -> InterventionRequest:
        try:
            return store.get(request_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail="Unknown request") from error

    @app.get("/operator", response_class=HTMLResponse)
    async def requests() -> str:
        rows = "".join(
            f'<li><a href="/operator/{escape(item.request_id)}">'
            f"{escape(item.reason)} — {escape(item.status)}</a></li>"
            for item in store.open()
        )
        return f"<h1>Open intervention requests</h1><ul>{rows}</ul>"

    @app.get("/operator/{request_id}", response_class=HTMLResponse)
    async def detail(request_id: str) -> str:
        item = request_or_404(request_id)
        screenshot = (
            (
                f'<img alt="Latest redacted screenshot" '
                f'src="/operator/{escape(request_id)}/screenshot">'
            )
            if item.screenshot_ref is not None
            else "<p>No screenshot available.</p>"
        )
        endpoint = (
            f"<p>CDP endpoint: <code>{escape(item.cdp_endpoint)}</code></p>"
            if item.cdp_endpoint
            else ""
        )
        return (
            '<meta http-equiv="refresh" content="2">'
            f"<h1>{escape(item.reason)}</h1><p>{escape(item.detail)}</p>{screenshot}{endpoint}"
            f'<form method="post" action="/operator/{escape(request_id)}/take?actor=operator">'
            '<button type="submit">Take control</button></form>'
            f'<form method="post" action="/operator/{escape(request_id)}/hand-back?notes=done">'
            '<button type="submit">Hand back</button></form>'
        )

    @app.get("/operator/{request_id}/screenshot")
    async def screenshot(request_id: str) -> Response:
        item = request_or_404(request_id)
        if item.screenshot_ref is None or evidence_reader is None:
            raise HTTPException(status_code=404, detail="Screenshot unavailable")
        return Response(evidence_reader(item.screenshot_ref), media_type="image/png")

    @app.post("/operator/{request_id}/take", response_class=HTMLResponse)
    async def take(request_id: str, actor: str = "operator") -> str:
        item = await coordinator.take_control(request_id, actor)
        return f"<p>{escape(item.status)}</p><a href=/operator/{escape(request_id)}>Return</a>"

    @app.post("/operator/{request_id}/hand-back", response_class=HTMLResponse)
    async def hand_back(request_id: str, notes: str = "") -> str:
        result = await coordinator.hand_back(request_id, notes)
        return f"<p>resumed={str(result.resumed).lower()}</p><a href=/operator>Requests</a>"

    return app
