"""Real synthetic auth/session, fail-closed paths, no runtime activation."""

import json
import os
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from server.canary import create_auth_canary_app


@pytest.fixture
def canary(tmp_path, monkeypatch):
    home, data, production = (tmp_path / n for n in ("home", "data", "production"))
    for p in (home, data, production):
        p.mkdir(mode=0o700)
    marker = production / "task_queue.jsonl"
    marker.write_text('{"task_id":"held","status":"blocked"}\n')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ANIMAWORKS_DATA_DIR", str(data))
    monkeypatch.setenv("ANIMAWORKS_CANARY_PRODUCTION_ROOT", str(production))
    monkeypatch.setenv("ANIMAWORKS_DISABLE_EXTERNAL_SYNC", "1")
    from core.auth.manager import hash_password, save_auth
    from core.auth.models import AuthConfig, AuthUser
    from core.config import invalidate_cache

    invalidate_cache()
    (data / "config.json").write_text(json.dumps({"server": {"session_ttl_days": 1}}))
    (data / "config.json").chmod(0o600)
    save_auth(AuthConfig(
        auth_mode="password", trust_localhost=False,
        owner=AuthUser(username="operator", password_hash=hash_password("synthetic-password"), role="owner"),
    ))
    before = marker.read_bytes()
    yield home, data, production
    assert marker.read_bytes() == before
    invalidate_cache()


def client_for(app, host="127.0.0.1", headers=None):
    return AsyncClient(
        transport=ASGITransport(app=app, client=(host, 12345)),
        base_url="http://127.0.0.1:18502", headers=headers,
    )


@pytest.mark.asyncio
async def test_real_password_session_and_revocation_without_runtime(canary):
    from server.app import create_app  # import before patching; never invoke

    assert create_app
    forbidden = [
        "server.app.ProcessSupervisor", "server.app.create_app", "server.app._activate_runtime_services",
        "server.app._run_startup_initialization", "server.app._run_model_warmup",
        "core.config.migrate.migrate_person_to_anima", "core.config.migrate.migrate_all_cron",
    ]
    with ExitStack() as stack:
        mocks = [stack.enter_context(patch(p, side_effect=AssertionError(p))) for p in forbidden]
        app = create_auth_canary_app()
        async with app.router.lifespan_context(app), client_for(app) as client:
            assert (await client.get("/api/auth/me")).status_code == 401
            assert (await client.post("/api/auth/login", json={
                "username": "operator", "password": "wrong",
            })).status_code == 401
            login = await client.post("/api/auth/login", json={
                "username": "operator", "password": "synthetic-password",
            })
            assert login.status_code == 200
            assert "httponly" in login.headers["set-cookie"].lower()
            assert (await client.get("/api/auth/me")).json()["username"] == "operator"
            assert (await client.post("/api/animas/h2-canary/chat", json={"message": "probe"})).status_code == 503
            assert (await client.post("/api/auth/logout")).status_code == 200
            assert (await client.get("/api/auth/me")).status_code == 401
        for mock in mocks:
            mock.assert_not_called()
    assert {p.name for p in canary[1].iterdir()} == {"auth.json", "config.json"}


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [
    ("POST", "/health"), ("GET", "/docs"), ("GET", "/api/approve/token"),
    ("POST", "/api/setup/complete"), ("POST", "/api/webhooks/slack"),
    ("POST", "/api/animas/h2-canary/chat"), ("POST", "/api/animas/other/chat"),
    ("POST", "/api/tasks"), ("GET", "/release"), ("GET", "/api/auth/login"),
    ("OPTIONS", "/api/auth/login"), ("POST", "/api/auth/login/"),
])
async def test_non_allowlisted_routes_closed(canary, method, path):
    async with client_for(create_auth_canary_app()) as client:
        assert (await client.request(method, path)).status_code == 503


@pytest.mark.asyncio
async def test_health_not_ready_and_ws_closed(canary):
    app = create_auth_canary_app()
    async with client_for(app) as client:
        for path in ("/health", "/api/system/health"):
            response = await client.get(path)
            assert response.status_code == 200
            assert response.json()["ready"] is False
            assert response.json()["chat_enabled"] is False
    sent = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    await app({"type": "websocket", "path": "/ws"}, receive, send)
    assert sent == [{"type": "websocket.close", "code": 1013}]


@pytest.mark.parametrize("kind", [
    "missing", "production", "ancestor", "descendant", "symlink", "hardlink", "permissions",
    "run", "local_trust", "localhost_bypass", "config", "home_data", "dirty_home",
])
def test_bad_root_or_auth_refused_before_router(canary, monkeypatch, kind):
    home, data, production = canary
    if kind == "missing":
        monkeypatch.delenv("ANIMAWORKS_DATA_DIR")
    elif kind in {"production", "ancestor", "descendant"}:
        target = {"production": production, "ancestor": production.parent, "descendant": production / "child"}[kind]
        target.mkdir(mode=0o700, exist_ok=True)
        monkeypatch.setenv("ANIMAWORKS_DATA_DIR", str(target))
    elif kind in {"symlink", "hardlink"}:
        target = data / "auth.json"
        saved = home.parent / "external-auth"
        target.rename(saved)
        if kind == "symlink":
            target.symlink_to(saved)
        else:
            os.link(saved, target)
    elif kind == "permissions":
        data.chmod(0o755)
    elif kind == "run":
        (data / "run").mkdir()
    elif kind in {"local_trust", "localhost_bypass"}:
        path = data / "auth.json"
        value = json.loads(path.read_text())
        value["auth_mode" if kind == "local_trust" else "trust_localhost"] = "local_trust" if kind == "local_trust" else True
        path.write_text(json.dumps(value))
    elif kind == "config":
        (data / "config.json").write_text('{"credentials":{"test":"vault://test"}}')
    elif kind == "home_data":
        monkeypatch.setenv("HOME", str(data))
    elif kind == "dirty_home":
        (home / "config").write_text("unexpected")
    with (
        patch("server.routes.auth.create_auth_router", side_effect=AssertionError("router constructed")),
        pytest.raises(ValueError),
    ):
        create_auth_canary_app()


@pytest.mark.asyncio
@pytest.mark.parametrize("headers,host", [
    ({"host": "untrusted.invalid"}, "127.0.0.1"),
    ({"origin": "null"}, "127.0.0.1"),
    ({"x-forwarded-for": "127.0.0.1"}, "127.0.0.1"),
    ({"x-forwarded-proto": "https"}, "127.0.0.1"),
    ({}, "192.0.2.1"),
])
async def test_nonlocal_and_browser_requests_refused(canary, headers, host):
    async with client_for(create_auth_canary_app(), host, headers) as client:
        assert (await client.get("/health")).status_code == 503


@pytest.mark.asyncio
async def test_runtime_environment_change_fails_closed(canary, monkeypatch):
    app = create_auth_canary_app()
    monkeypatch.setenv("ANIMAWORKS_DATA_DIR", str(canary[2]))
    async with client_for(app) as client:
        assert (await client.post("/api/auth/login", json={
            "username": "operator", "password": "synthetic-password",
        })).status_code == 503


def test_fresh_process_factory_lifespan_no_network_or_children(canary):
    child = '''
import asyncio, socket, subprocess
attempts = []
def denied(*a, **k):
    attempts.append('denied')
    raise AssertionError('network or process attempted')
socket.socket.connect = denied
subprocess.Popen.__init__ = denied
asyncio.create_subprocess_exec = denied
from server.canary import create_auth_canary_app
from httpx import ASGITransport, AsyncClient
async def main():
    app = create_auth_canary_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://127.0.0.1:18502') as client:
            assert (await client.post('/api/auth/login', json={
                'username': 'operator', 'password': 'synthetic-password',
            })).status_code == 200
            assert (await client.get('/api/auth/me')).status_code == 200
    assert attempts == []
asyncio.run(main())
'''
    result = subprocess.run([sys.executable, "-B", "-c", child], env={
        "PATH": "/usr/bin:/bin", "HOME": str(canary[0]),
        "ANIMAWORKS_DATA_DIR": str(canary[1]),
        "ANIMAWORKS_CANARY_PRODUCTION_ROOT": str(canary[2]),
        "ANIMAWORKS_DISABLE_EXTERNAL_SYNC": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(Path(__file__).resolve().parents[3]),
    }, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
