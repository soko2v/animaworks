"""Non-admitting ASGI factory for an explicitly approved maintenance launch.

Invoke directly with uvicorn's --factory, NOT through the normal start CLI:
that CLI has side effects before creating the application. No runtime state,
authentication, business routes, migrations or background services are loaded.
This verifies HTTP liveness only, never operational readiness or writer quiescence.
There is deliberately no HTTP endpoint that switches to normal operation.
"""

from fastapi import FastAPI
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class _MaintenanceGate:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1013})
            return
        if scope["type"] == "http":
            health = scope.get("method") in {"GET", "HEAD"} and scope.get("path") in {
                "/health", "/api/system/health",
            }
            response = JSONResponse(
                {"status": "maintenance", "ready": False, "admission_enabled": False},
                status_code=200 if health else 503,
                headers={"Cache-Control": "no-store", "Retry-After": "5"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_maintenance_app() -> FastAPI:
    """Create a state-independent liveness shell; normal startup is not imported."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(_MaintenanceGate)
    return app
