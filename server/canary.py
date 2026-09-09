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
import select
import stat
import time
from contextlib import asynccontextmanager
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


def create_chat_canary_app(*, executable: Path, probe_home: Path, probe_cwd: Path,
                           socket_path: Path, oauth_token: str) -> FastAPI:
    """Explicit, approval-bound bootstrap; never call through normal serve.

    Authorization must be supplied in memory by an approved resolver, not CLI
    arguments or a token file. Construction does not call the model. One app
    lifespan only; no automatic restart/retry or normal supervisor is created.
    This factory is not authorization to obtain production credentials.
    """
    from core.supervisor.canary import CanaryIPCService, ClaudeTextProbe
    from core.supervisor.ipc import IPCClient, IPCRequest

    roots = _validate_roots()
    paths = [*roots, probe_home.resolve(), probe_cwd.resolve(), socket_path.parent.resolve()]
    production = Path(os.environ["ANIMAWORKS_CANARY_PRODUCTION_ROOT"]).resolve()
    if any(_overlaps(p, production) for p in paths):
        raise ValueError("Probe overlaps production")
    if any(_overlaps(a, b) for i, a in enumerate(paths) for b in paths[i + 1:]):
        raise ValueError("Separate canary directories required")
    probe = ClaudeTextProbe(executable, probe_home, probe_cwd, oauth_token=oauth_token)
    probe._launch_spec()  # Validate without starting a process or exposing the spec.
    service = CanaryIPCService(socket_path, probe)
    ipc = IPCClient(socket_path)

    class ProbeSupervisor:
        active = False

        def is_bootstrapping(self, name):
            return not self.active or name != "h2-canary"

        async def send_request(self, *, anima_name, method, params, timeout):
            if not self.active or anima_name != "h2-canary":
                raise RuntimeError("Canary admission closed")
            try:
                result = await ipc.send_request(IPCRequest("probe", method, params), timeout=65)
                if result.error:
                    raise ValueError("Canary admission closed")
                return result.result
            except Exception:
                raise RuntimeError("Canary admission closed") from None

    supervisor = ProbeSupervisor()
    consumed = False

    @asynccontextmanager
    async def lifespan(app):
        nonlocal consumed
        if consumed:
            raise ValueError("Canary launch already consumed")
        consumed = True
        try:
            if _validate_roots() != roots:
                raise ValueError("Canary roots changed")
            await service.start()
            supervisor.active = True
            yield
        finally:
            supervisor.active = False
            await service.stop()
            probe._token = ""

    app = _create_canary_app(probe_supervisor=supervisor)
    app.router.lifespan_context = lifespan
    return app


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


def _read_authorization_pipe(fd: int) -> str:
    """Consume a bounded inherited pipe, never a credential file or terminal."""
    if fd < 3:
        raise ValueError("Dedicated authorization pipe required")
    try:
        if not stat.S_ISFIFO(os.fstat(fd).st_mode):
            raise ValueError("Dedicated authorization pipe required")
        os.set_inheritable(fd, False)
        os.set_blocking(fd, False)
        raw = bytearray()
        deadline = time.monotonic() + 5
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise ValueError("Authorization pipe unavailable")
            chunk = os.read(fd, 8193 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > 8192:
                raise ValueError("Authorization pipe unavailable")
        token = raw.decode("ascii")
        if not token or any(ord(c) <= 32 or ord(c) >= 127 for c in token):
            raise ValueError("Authorization pipe unavailable")
        return token
    except (OSError, UnicodeError):
        raise ValueError("Authorization pipe unavailable") from None
    finally:
        os.close(fd)


def main(argv=None) -> int:
    """Approval-bound direct entry; no resolver, restart, or normal CLI startup.

    An approved operator passes authorization through an inherited anonymous
    pipe, closes its write end, and supplies only the descriptor number in argv.
    This does not itself authorize resolving or copying actual credentials.
    Run with a cleared environment and the isolated HOME/DATA contract above.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Isolated one-shot upgrade canary")
    for name in ("executable", "probe-home", "probe-cwd", "socket-path"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--authorization-fd", required=True, type=int)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args(argv)
    token = ""
    try:
        if not 1024 <= args.port <= 65535:
            raise ValueError("Invalid port")
        _validate_roots()
        token = _read_authorization_pipe(args.authorization_fd)
        app = create_chat_canary_app(
            executable=args.executable, probe_home=args.probe_home,
            probe_cwd=args.probe_cwd, socket_path=args.socket_path, oauth_token=token,
        )
        token = ""
        import uvicorn

        uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1,
                    reload=False, proxy_headers=False, access_log=False,
                    log_config=None, log_level="critical", lifespan="on")
        return 0
    except (Exception, SystemExit):
        # Provider/ASGI setup exceptions must not expose authorization or config.
        return 2
    finally:
        token = ""


if __name__ == "__main__":
    raise SystemExit(main())
