"""Real synthetic auth/session, fail-closed paths, no runtime activation."""

import json
import os
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

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


@pytest.mark.parametrize("payload", [b"synthetic-only", b"", b"bad\nvalue", b"x" * 8193, b"\xff"])
def test_authorization_pipe_consumed_and_bounded(payload):
    from server.canary import _read_authorization_pipe

    read_fd, write_fd = os.pipe()
    # Large payload uses a writer thread so pipe capacity cannot deadlock setup.
    import threading

    def write():
        try:
            os.write(write_fd, payload)
        except BrokenPipeError:
            pass
        finally:
            os.close(write_fd)

    writer = threading.Thread(target=write)
    writer.start()
    try:
        if payload == b"synthetic-only":
            assert _read_authorization_pipe(read_fd) == "synthetic-only"
        else:
            with pytest.raises(ValueError):
                _read_authorization_pipe(read_fd)
        with pytest.raises(OSError):
            os.fstat(read_fd)
    finally:
        writer.join(timeout=5)
        assert not writer.is_alive()


def test_authorization_rejects_file_and_stdin(tmp_path):
    from server.canary import _read_authorization_pipe

    file = tmp_path / "synthetic"
    file.write_text("synthetic-only")
    fd = os.open(file, os.O_RDONLY)
    with pytest.raises(ValueError):
        _read_authorization_pipe(fd)
    with pytest.raises(OSError):
        os.fstat(fd)
    with pytest.raises(ValueError):
        _read_authorization_pipe(0)


def test_authorization_timeout_does_not_wait_for_eof(monkeypatch):
    from server.canary import _read_authorization_pipe

    read_fd, write_fd = os.pipe()
    monkeypatch.setattr("server.canary.select.select", lambda *args: ([], [], []))
    try:
        with pytest.raises(ValueError):
            _read_authorization_pipe(read_fd)
    finally:
        os.close(write_fd)


@pytest.mark.parametrize("failure", [False, True, "no_verdict"])
def test_direct_launcher_fixed_listener_and_redaction(canary, monkeypatch, capsys, failure):
    import server.canary as module

    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"synthetic-only")
    os.close(write_fd)
    from types import SimpleNamespace

    app = SimpleNamespace(state=SimpleNamespace(canary_passed=lambda: failure is False))
    calls = []

    def factory(**kwargs):
        assert kwargs["oauth_token"] == "synthetic-only"
        if failure is True:
            raise RuntimeError("synthetic-only must not be printed")
        return app

    monkeypatch.setattr(module, "create_chat_canary_app", factory)
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: calls.append((args, kwargs)))
    result = module.main([
        "--executable", "/synthetic-cli", "--probe-home", "/synthetic-home",
        "--probe-cwd", "/synthetic-cwd", "--socket-path", "/synthetic-socket",
        "--authorization-fd", str(read_fd), "--port", "18502",
    ])
    assert result == (2 if failure else 0)
    if failure is not True:
        assert calls == [((app,), dict(host="127.0.0.1", port=18502, workers=1,
                                      reload=False, proxy_headers=False, access_log=False,
                                      log_config=None, log_level="critical", lifespan="on"))]
    else:
        assert not calls
    assert "synthetic-only" not in str(capsys.readouterr())


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["success", "failure", "occupied", "overlap"])
async def test_explicit_chat_factory_lifecycle(canary, case):
    from server.canary import create_chat_canary_app

    root = canary[0].parent
    home, cwd = root / "probe-home", root / "probe-cwd"
    home.mkdir(mode=0o700)
    cwd.mkdir(mode=0o700)
    executable = root / "synthetic-cli"
    reply = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                        "num_turns": 1, "result": "CANARY_OK", "permission_denials": []})
    executable.write_text("#!/bin/sh\ncat >/dev/null\nprintf '%s' '" + reply + "'\n"
                          + ("exit 1\n" if case == "failure" else ""))
    executable.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix="aw-f-", dir="/tmp") as directory:
        endpoint = Path(directory).resolve() / "probe.sock"
        if case == "occupied":
            endpoint.write_text("preserve")
        if case == "overlap":
            with pytest.raises(ValueError, match="Separate"):
                create_chat_canary_app(executable=executable, probe_home=canary[0],
                                       probe_cwd=cwd, socket_path=endpoint, oauth_token="synthetic")
            return
        app = create_chat_canary_app(executable=executable, probe_home=home,
                                    probe_cwd=cwd, socket_path=endpoint, oauth_token="synthetic")
        if case == "occupied":
            with pytest.raises(ValueError, match="already exists"):
                async with app.router.lifespan_context(app):
                    pytest.fail("Existing endpoint must not be adopted")
            assert endpoint.read_text() == "preserve"
        else:
            assert not endpoint.exists()
            assert not app.state.canary_passed()
            async with app.router.lifespan_context(app):
                assert endpoint.is_socket()
                async with client_for(app) as client:
                    assert (await client.post("/api/auth/login", json={
                        "username": "operator", "password": "synthetic-password",
                    })).status_code == 200
                    response = await client.post("/api/animas/h2-canary/chat", json={
                        "message": "Reply with CANARY_OK only.",
                    })
                    assert response.status_code == (200 if case == "success" else 500)
                    assert (await client.post("/api/animas/h2-canary/chat", json={
                        "message": "Reply with CANARY_OK only.",
                    })).status_code == 503
                    assert (await client.get("/health")).json()["ready"] is False
            assert not endpoint.exists()
            assert app.state.canary_passed() is (case == "success")
        with pytest.raises(ValueError, match="already consumed"):
            async with app.router.lifespan_context(app):
                pytest.fail("Must not restart")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "parallel", "provider_error", "cancelled", "native_cli"])
async def test_authenticated_route_real_ipc_one_shot(canary, outcome):
    """Real synthetic password session and Unix IPC, no real provider."""
    import asyncio

    from core.supervisor.canary import CanaryIPCService, ClaudeTextProbe
    from core.supervisor.ipc import IPCClient, IPCRequest
    from server.canary import _create_canary_app

    provider = AsyncMock(return_value="CANARY_OK")
    if outcome == "provider_error":
        provider.side_effect = RuntimeError("synthetic-secret")
    if outcome == "cancelled":
        provider.side_effect = asyncio.CancelledError()
    if outcome == "native_cli":
        # Exercise the actual adapter and OS subprocess from authenticated HTTP.
        # Only the executable/result/token are synthetic; no provider is called.
        root = canary[0].parent
        probe_home, probe_cwd = root / "cli-home", root / "cli-cwd"
        probe_home.mkdir(mode=0o700)
        probe_cwd.mkdir(mode=0o700)
        executable = root / "synthetic-cli"
        reply = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                            "num_turns": 1, "result": "CANARY_OK", "permission_denials": []})
        executable.write_text("#!/bin/sh\ncat >/dev/null\nprintf '%s' '" + reply + "'\n")
        executable.chmod(0o700)
        probe = ClaudeTextProbe(executable, probe_home, probe_cwd, oauth_token="synthetic-only")
        provider.side_effect = probe.__call__
    with tempfile.TemporaryDirectory(prefix="aw-c-", dir="/tmp") as directory:
        path = Path(directory).resolve() / "probe.sock"
        service = CanaryIPCService(path, provider)
        ipc = IPCClient(path)

        class ProbeSupervisor:
            def is_bootstrapping(self, name):
                assert name == "h2-canary"
                return False

            async def send_request(self, *, anima_name, method, params, timeout):
                assert anima_name == "h2-canary"
                try:
                    result = await ipc.send_request(IPCRequest("probe", method, params), timeout=1)
                    if result.error:
                        raise RuntimeError("Canary admission closed")
                    return result.result
                except Exception:
                    raise RuntimeError("Canary admission closed") from None

        await service.start()
        try:
            app = _create_canary_app(probe_supervisor=ProbeSupervisor())
            async with client_for(app) as client:
                url = "/api/animas/h2-canary/chat"
                payload = {"message": "Reply with CANARY_OK only."}
                assert (await client.post(url, json=payload)).status_code == 503
                provider.assert_not_called()
                assert (await client.post("/api/auth/login", json={
                    "username": "operator", "password": "synthetic-password",
                })).status_code == 200
                for bad in [
                    {**payload, "model": "other"}, {**payload, "images": [{}]},
                    {**payload, "from_person": "other"}, {**payload, "extra": True},
                    {"message": "x" * 300}, {"message": "run a tool"},
                ]:
                    assert (await client.post(url, json=bad)).status_code == 503
                for suffix in ["/stream", "/../greet"]:
                    assert (await client.post(url + suffix, json=payload)).status_code == 503
                provider.assert_not_called()
                if outcome == "parallel":
                    responses = await asyncio.gather(client.post(url, json=payload), client.post(url, json=payload))
                    assert sorted(r.status_code for r in responses) == [200, 503]
                    response = next(r for r in responses if r.status_code == 200)
                else:
                    response = await client.post(url, json=payload)
                assert response.status_code == (200 if outcome in {"success", "parallel", "native_cli"} else 500)
                assert "synthetic-secret" not in response.text
                if outcome in {"success", "parallel", "native_cli"}:
                    assert response.json()["response"] == "CANARY_OK"
                assert (await client.post(url, json=payload)).status_code == 503
                assert (await client.post("/api/auth/logout")).status_code == 200
                assert (await client.post(url, json=payload)).status_code == 503
                provider.assert_awaited_once()
                if outcome == "native_cli":
                    assert probe._spent and probe._token == ""
        finally:
            await ipc.close()
            await service.stop()


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
