from contextlib import asynccontextmanager
import logging

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.relay import relay
from app.services import request_log as request_log_module
from app.api.providers import router as provider_router
from app.api.health import router as health_router
from app.api.decision import router as decision_router
from app.api.chat import router as chat_router
from app.api.diagnostics import router as diagnostics_router
from app.api.openai import router as openai_router
from app.api.admin import router as admin_router
from app.api.keys import router as keys_router
from app.api.feedback import router as feedback_router
from app.security.auth import require_api_key
from app.api.middleware import BodySizeLimitMiddleware, MetricsMiddleware
from app.api.metrics import router as metrics_router

_logger = logging.getLogger("relay")


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    """
    Application lifecycle: starts the background health refresher and
    state flusher when enabled, and stops them gracefully on shutdown.
    The state flusher performs a final flush so no learned intelligence
    is lost when the process exits.
    """

    if settings.health_refresh_enabled:
        relay.health_refresher.start()

    if settings.provider_recovery_enabled:
        relay.provider_recovery.start()

    if relay.state_flusher is not None:
        relay.state_flusher.start()

    if relay.continuity_flusher is not None:
        relay.continuity_flusher.start()
        relay.continuity_flusher.prune_now()

    # P9d: startup reconciliation -- detect seq gaps / duplicates and
    # summary-ahead-of-turns anomalies, and mark conversations whose last
    # safe point is undeterminable as requiring recovery review. Best
    # effort: it reports, never repairs, and never breaks startup.
    if relay.continuity_recovery is not None:
        try:
            report = relay.continuity_recovery.reconcile()
            if report.get("requires_review"):
                _logger.warning(
                    "continuity reconcile: %d/%d conversations require "
                    "recovery review",
                    report["requires_review"],
                    report["scanned"],
                )
            else:
                _logger.info(
                    "continuity reconcile: %d conversations scanned, "
                    "%d healthy, %d recoverable",
                    report["scanned"],
                    report["healthy"],
                    report.get("recoverable", 0),
                )
        except Exception:
            _logger.exception("continuity reconcile failed")

    try:
        yield
    finally:
        # Stop provider recovery first: it mutates provider state and its
        # in-flight pass should never race shutdown of the other services.
        relay.provider_recovery.stop()

        relay.health_refresher.stop()

        if relay.state_flusher is not None:
            relay.state_flusher.stop()
            try:
                relay.state_flusher.flush()
            except Exception:
                _logger.exception("shutdown state flush failed")

        if relay.continuity_flusher is not None:
            relay.continuity_flusher.stop()
            try:
                relay.continuity_flusher.flush()
            except Exception:
                _logger.exception("shutdown continuity flush failed")

        # Drain the request-log write-behind buffer and release its SQLite
        # connection; without this the daemon flusher dies on process exit
        # with up to one flush interval of buffered rows never persisted.
        try:
            request_log_module.request_log().close()
        except Exception:
            _logger.exception("shutdown request-log flush failed")


app = FastAPI(
    title="Relay",
    lifespan=lifespan,
    dependencies=[Depends(require_api_key)],
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(MetricsMiddleware)
app.add_middleware(BodySizeLimitMiddleware)

app.include_router(provider_router)
app.include_router(health_router)
app.include_router(decision_router)
app.include_router(chat_router)
app.include_router(diagnostics_router)
app.include_router(openai_router)
app.include_router(metrics_router)
app.include_router(admin_router)
app.include_router(keys_router)
app.include_router(feedback_router)


def _format_request_validation_error(exc: RequestValidationError) -> str:
    """Collapse a pydantic validation error into a single human-readable line."""
    first = exc.errors()[0] if exc.errors() else {}
    loc = first.get("loc", ())
    field = ".".join(str(part) for part in loc if part not in ("body", "query", "path"))
    msg = first.get("msg", "Invalid request.")
    if field:
        return f"{field}: {msg}"
    return msg


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(request: Request, exc: RequestValidationError):
    """
    Reformats request-validation errors raised under /v1 to the OpenAI
    {"error": {...}} shape so OpenAI SDK clients can parse them in the same
    way they parse business errors (which already use that shape). Non-/v1
    routes keep FastAPI's default {"detail": ...} response.
    """
    if not request.url.path.startswith("/v1"):
        return JSONResponse(
            status_code=422,
            content={"detail": exc.errors()},
        )
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "message": _format_request_validation_error(exc),
                "type": "invalid_request_error",
                "code": "validation_error",
            }
        },
    )


@app.get("/docs", include_in_schema=False)
async def docs():
    """
    Swagger UI. Registered as a real endpoint so it inherits the global
    require_api_key dependency (the built-in docs routes cannot).
    """
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title="Relay API docs",
    )


@app.get("/redoc", include_in_schema=False)
async def redoc():
    """
    ReDoc UI. Registered as a real endpoint so it inherits the global
    require_api_key dependency.
    """
    return get_redoc_html(
        openapi_url="/openapi.json",
        title="Relay API docs",
    )


@app.get("/openapi.json", include_in_schema=False)
async def openapi_json():
    """
    OpenAPI schema. Registered as a real endpoint so it inherits the
    global require_api_key dependency.
    """
    return app.openapi()


@app.get("/")
def root():
    return {
        "name": "Relay",
        "status": "running",
    }
