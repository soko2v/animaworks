"""Isolated authentication stage of the upgrade canary (NOT chat readiness).

Direct factory: server.canary:create_auth_canary_app. Never use normal start/serve.
Only an operator-prepared private HOME and DATA, outside the production tree, are
accepted. DATA contains auth.json and a minimal config.json, not copied Animas.
No supervisor/lifespan activation is constructed. Chat stays closed until the
runner and native/provider tool boundaries have separately passed acceptance.
This is not an OS sandbox against a malicious same-UID operator or root.
"""

from __future__ import annotations

import json
import os
import pwd
import stat
from pathlib import Path

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def _private(path: Path, *, directory: bool) -> None:
    info = path.lstat()
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not correct_type or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Canary paths must be private, owned, and not symlinks")
    if not directory and info.st_nlink != 1:
        raise ValueError("Canary files must not be hardlinks")


def _validate_roots() -> tuple[Path, Path]:
    values = [os.environ.get(k, "") for k in (
        "HOME", "ANIMAWORKS_DATA_DIR", "ANIMAWORKS_CANARY_PRODUCTION_ROOT",
    )]
    if any(not v or not Path(v).is_absolute() for v in values):
        raise ValueError("Explicit absolute HOME, DATA and production root required")
    home, data, production = (Path(v).resolve(strict=True) for v in values)
    default_production = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".animaworks"
    for root in (home, data):
        if any(_overlaps(root, p.resolve()) for p in (production, default_production)):
            raise ValueError("Canary root overlaps production")
        _private(root, directory=True)
    if _overlaps(home, data) or list(home.iterdir()):
        raise ValueError("Canary HOME must be empty and separate from DATA")
    if {p.name for p in data.iterdir()} != {"auth.json", "config.json"}:
        raise ValueError("Auth canary DATA must contain only auth.json and config.json")
    for name in ("auth.json", "config.json"):
        _private(data / name, directory=False)
    # Do not load arbitrary provider settings or resolve Vault references here.
    config = json.loads((data / "config.json").read_text())
    if config != {"server": {"session_ttl_days": 1}}:
        raise ValueError("Auth canary requires the minimal one-day session config")
    return home, data


class _AuthCanaryGate:
    def __init__(self, app: ASGIApp, roots: tuple[Path, Path]) -> None:
        self.app = app
        self.roots = roots

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1013})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        response = JSONResponse({"error": "Canary admission closed"}, status_code=503)
        try:
            if _validate_roots() != self.roots:
                raise ValueError("Canary roots changed")
            request = Request(scope)
            client, server = scope.get("client"), scope.get("server")
            if not client or not server or client[0] != "127.0.0.1" or server[0] != "127.0.0.1":
                raise ValueError("Loopback-only canary")
            if request.headers.get("host") != f"127.0.0.1:{server[1]}":
                raise ValueError("Unexpected host")
            if any(k in request.headers for k in ("origin", "forwarded", "x-forwarded-for", "x-forwarded-proto")):
                raise ValueError("Browser/proxy requests are not admitted")
            from core.auth.manager import find_user, load_auth, validate_session

            auth = load_auth()
            if auth.auth_mode != "password" or auth.trust_localhost or not auth.owner or not auth.owner.password_hash:
                raise ValueError("Password authentication without localhost bypass required")
            method, path = scope["method"], scope["path"]
            if method in {"GET", "HEAD"} and path in {"/health", "/api/system/health"}:
                response = JSONResponse({
                    "status": "auth_canary", "ready": False, "admission_enabled": False,
                    "chat_enabled": False,
                })
            elif (method, path) in {
                ("POST", "/api/auth/login"), ("POST", "/api/auth/logout"), ("GET", "/api/auth/me"),
            }:
                session = validate_session(request.cookies.get("session_token"))
                scope.setdefault("state", {})["user"] = find_user(auth, session.username) if session else None
                await self.app(scope, receive, send)
                return
        except (ValueError, OSError):
            # Do not include settings, request contents or secrets in diagnostics.
            pass
        response.headers["Cache-Control"] = "no-store"
        await response(scope, receive, send)


def create_auth_canary_app() -> FastAPI:
    """Authentication-only preparation; no model/runner or release endpoint."""
    roots = _validate_roots()
    from core.auth.manager import load_auth

    auth = load_auth()
    if auth.auth_mode != "password" or auth.trust_localhost or not auth.owner or not auth.owner.password_hash:
        raise ValueError("Explicit password authentication required")
    from server.routes.auth import create_auth_router

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(create_auth_router(), prefix="/api")
    app.add_middleware(_AuthCanaryGate, roots=roots)
    return app
