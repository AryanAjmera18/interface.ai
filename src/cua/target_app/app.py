"""Serve the isolated synthetic banking fixture; forbid automation-package imports."""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.responses import Response

from cua.target_app.config import SurfaceVersion, TenantCatalog, TenantConfig
from cua.target_app.models import (
    ApiError,
    FaultConfiguration,
    FaultKind,
    FormValues,
    PendingAction,
    Screen,
    Session,
    View,
)
from cua.target_app.state import SessionStore, seed_members
from cua.target_app.workflow import find_member, workflow

COOKIE_NAME = "cua_session"


def create_app(catalog: TenantCatalog | None = None, *, secret: bytes | None = None) -> FastAPI:
    """Keep sessions per app instance; only synthetic in-memory values reach the rendered UI."""
    tenants = catalog if catalog is not None else TenantCatalog()
    sessions = SessionStore(secret)
    templates = Environment(
        loader=FileSystemLoader(Path(__file__).parent / "templates"),
        autoescape=select_autoescape(("html",)),
    )
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def render(tenant: TenantConfig, view: View) -> HTMLResponse:
        """Convert typed view objects to template arguments only at the Jinja adapter."""
        return HTMLResponse(
            templates.get_template(f"{view.screen}.html").render(
                tenant=tenant, page=view, base=f"/t/{tenant.tenant_id}"
            ),
            status_code=500 if view.screen == "server_error" else 200,
        )

    @app.middleware("http")
    async def session_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        session = sessions.resolve(request.cookies.get(COOKIE_NAME))
        request.state.fixture_session = session
        response: Response
        tenant_path = request.url.path.startswith("/t/")
        if tenant_path and session.faults.consume(FaultKind.SESSION_EXPIRED):
            session.authenticated = False
            session.draft = None
            session.pending = None
            tenant = tenants.get(request.url.path.split("/")[2])
            if tenant is None:
                return HTMLResponse("Unknown tenant", status_code=404)
            response = render(tenant, View(screen="login", notice="Your session has expired."))
        else:
            response = await call_next(request)
        if tenant_path:
            slow = session.faults.consume(FaultKind.SLOW_LOAD)
            if slow is not None:
                await asyncio.sleep(slow.delay_ms / 1000)
        response.set_cookie(
            COOKIE_NAME,
            sessions.cookie(session),
            httponly=True,
            samesite="strict",
            secure=False,
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_body(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Do not echo rejected request values through FastAPI's default validation response."""
        return JSONResponse(
            ApiError(error="Invalid fault configuration or tenant selection.").model_dump(), 422
        )

    @app.get("/_test/faults", response_model=FaultConfiguration)
    async def get_faults(request: Request) -> FaultConfiguration:
        return cast(Session, request.state.fixture_session).faults

    @app.post("/_test/faults", response_model=FaultConfiguration)
    async def set_faults(request: Request, configuration: FaultConfiguration) -> FaultConfiguration:
        session = cast(Session, request.state.fixture_session)
        session.faults = configuration.model_copy(deep=True)
        return session.faults

    @app.get("/_meta/version", response_model=SurfaceVersion)
    async def version(request: Request, tenant_id: str | None = None) -> Response | SurfaceVersion:
        session = cast(Session, request.state.fixture_session)
        tenant = tenants.get(tenant_id or session.tenant_id)
        if tenant is None:
            return JSONResponse(ApiError(error="Unknown tenant").model_dump(), 404)
        return SurfaceVersion(tenant_id=tenant.tenant_id, config_hash=tenant.config_hash)

    @app.get("/t/{tenant_id}/_test/redirect-off-allowlist")
    async def redirect_off_allowlist(tenant_id: str) -> Response:
        if tenants.get(tenant_id) is None:
            return HTMLResponse("Unknown tenant", status_code=404)
        return RedirectResponse("https://evil.example/redirected", status_code=302)

    @app.api_route("/t/{tenant_id}/", methods=["GET", "POST"])
    async def login(request: Request, tenant_id: str) -> Response:
        tenant = tenants.get(tenant_id)
        if tenant is None:
            return HTMLResponse("Unknown tenant", status_code=404)
        session = cast(Session, request.state.fixture_session)
        if request.method == "POST":
            # Fake authentication deliberately never reads or retains credentials.
            session.tenant_id = tenant.tenant_id
            session.authenticated = True
            session.members = seed_members()
            session.draft = None
            session.pending = None
            session.receipts.clear()
        screen: Screen = (
            "shell" if session.authenticated and session.tenant_id == tenant_id else "login"
        )
        return render(tenant, View(screen=screen))

    @app.get("/t/{tenant_id}/{frame}")
    async def frame_page(request: Request, tenant_id: str, frame: str) -> Response:
        tenant = tenants.get(tenant_id)
        if tenant is None:
            return HTMLResponse("Unknown tenant", status_code=404)
        session = cast(Session, request.state.fixture_session)
        if not session.authenticated or session.tenant_id != tenant_id:
            return render(tenant, View(screen="login", notice="Please sign in."))
        if frame not in {"nav", "content"}:
            return HTMLResponse("Unknown page", status_code=404)
        return render(tenant, View(screen="nav" if frame == "nav" else "content"))

    @app.api_route("/t/{tenant_id}/ui/{action}", methods=["GET", "POST"])
    async def ui(request: Request, tenant_id: str, action: str) -> Response:
        tenant = tenants.get(tenant_id)
        if tenant is None:
            return HTMLResponse("Unknown tenant", status_code=404)
        session = cast(Session, request.state.fixture_session)
        if not session.authenticated or session.tenant_id != tenant_id:
            return render(tenant, View(screen="login", notice="Please sign in."))
        body = await request.body()
        if len(body) > 16_384:
            return render(tenant, View(screen="invalid", error="Form is too large."))
        encoded = (
            body.decode("utf-8", errors="replace")
            if request.method == "POST"
            else (request.url.query)
        )
        fields = parse_qs(encoded, keep_blank_values=True)
        form = FormValues.model_validate({name: values[-1] for name, values in fields.items()})
        method = request.method
        if session.pending is not None:
            if action != "dismiss" or method != "POST":
                return render(tenant, View(screen="notice"))
            pending = session.pending
            session.pending = None
            action, method, form = pending.action, pending.method, pending.form
        else:
            if session.faults.consume(FaultKind.UNEXPECTED_INTERSTITIAL):
                session.pending = PendingAction(action=action, method=method, form=form)
                return render(tenant, View(screen="notice"))
            if session.faults.consume(FaultKind.SERVER_ERROR_500):
                return render(tenant, View(screen="server_error"))
            if session.faults.consume(FaultKind.PERMISSION_DENIED):
                return render(tenant, View(screen="denied"))
            if session.faults.consume(FaultKind.RECORD_NOT_FOUND):
                return render(tenant, View(screen="not_found", form=form))
        rejected = (
            action == "review"
            and method == "POST"
            and bool(session.faults.consume(FaultKind.VALIDATION_ERROR))
        )
        if rejected and find_member(session, form.member_id) is None:
            return render(tenant, View(screen="not_found", form=form))
        return render(
            tenant, workflow(session, tenant, action, method, form, reject_deposit=rejected)
        )

    return app


app = create_app()
