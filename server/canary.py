"""Isolated authentication stage of the upgrade canary (NOT chat readiness).

Direct factory: server.canary:create_auth_canary_app. Never use normal start/serve.
Only an operator-prepared private HOME and DATA, outside the production tree, are
accepted. DATA contains auth.json and a minimal config.json, not copied Animas.
No supervisor/lifespan activation is constructed. Chat stays closed until the
runner and native/provider tool boundaries have separately passed acceptance.
This is not an OS sandbox against a malicious same-UID operator or root.
"""

from __future__ import annotations

import asyncio
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
    def __init__(self, app: ASGIApp, roots: tuple[Path, Path], *, chat_probe: bool = False) -> None:
        self.app = app
        self.roots = roots
        self.chat_probe = chat_probe
        self.chat_spent = False

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
            elif self.chat_probe and (method, path) == ("POST", "/api/animas/h2-canary/chat"):
                session = validate_session(request.cookies.get("session_token"))
                user = find_user(auth, session.username) if session else None
                if user is None or user.username != "operator" or self.chat_spent:
                    raise ValueError("Authenticated operator required")
                # Bound the raw body before the normal route can save attachments
                # or normalize away unexpected fields. Only this fixed probe is admitted.
                body = bytearray()
                async with asyncio.timeout(5):
                    while True:
                        message = await receive()
                        if message["type"] != "http.request":
                            raise ValueError("Incomplete request")
                        body.extend(message.get("body", b""))
                        if len(body) > 256:
                            raise ValueError("Oversized probe")
                        if not message.get("more_body", False):
                            break
                if json.loads(body) != {"message": "Reply with CANARY_OK only."}:
                    raise ValueError("Fixed probe required")
                # Re-check after body receipt: concurrent requests must not both
                # pass, and logout/environment changes while receiving must close.
                current = validate_session(request.cookies.get("session_token"))
                if self.chat_spent or current is None or current.username != "operator":
                    raise ValueError("Probe consumed or session revoked")
                if _validate_roots() != self.roots:
                    raise ValueError("Canary roots changed")
                self.chat_spent = True
                scope.setdefault("state", {})["user"] = user
                delivered = False

                async def replay() -> dict:
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {"type": "http.request", "body": bytes(body), "more_body": False}
                    return await receive()

                await self.app(scope, replay, send)
                return
        except (ValueError, OSError, TimeoutError):
            # Do not include settings, request contents or secrets in diagnostics.
            pass
        response.headers["Cache-Control"] = "no-store"
        await response(scope, receive, send)


def create_auth_canary_app() -> FastAPI:
    """Authentication-only preparation; no model/runner or release endpoint."""
    return _create_canary_app()


def _create_canary_app(*, probe_supervisor=None) -> FastAPI:
    """Internal integration seam, NOT a production launch factory.

    A caller-supplied adapter is trusted code, not a verified provider sandbox.
    The public zero-argument auth factory remains chat-closed. Do not expose a
    CLI/env toggle until the real provider and child ownership pass review.
    """
    roots = _validate_roots()
    from core.auth.manager import load_auth

    auth = load_auth()
    if auth.auth_mode != "password" or auth.trust_localhost or not auth.owner or not auth.owner.password_hash:
        raise ValueError("Explicit password authentication required")
    from server.routes.auth import create_auth_router

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(create_auth_router(), prefix="/api")
    if probe_supervisor is not None:
        from server.routes.chat import create_chat_router

        app.state.supervisor = probe_supervisor
        app.include_router(create_chat_router(), prefix="/api")
    app.add_middleware(_AuthCanaryGate, roots=roots, chat_probe=probe_supervisor is not None)
    return app
