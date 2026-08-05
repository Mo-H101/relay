from contextlib import asynccontextmanager
import logging

from fastapi import Depends, FastAPI
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html

from app.core.config import settings
from app.core.relay import relay
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
from app.api.middleware import MetricsMiddleware
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

    if relay.state_flusher is not None:
        relay.state_flusher.start()

    try:
        yield
    finally:
        relay.health_refresher.stop()

        if relay.state_flusher is not None:
            relay.state_flusher.stop()
            try:
                relay.state_flusher.flush()
            except Exception:
                _logger.exception("shutdown state flush failed")


app = FastAPI(
    title="Relay",
    lifespan=lifespan,
    dependencies=[Depends(require_api_key)],
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(MetricsMiddleware)

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
