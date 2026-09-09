"""Synthetic-only maintenance startup and admission contract."""

import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from server.maintenance import create_maintenance_app


def test_direct_factory_fresh_process_never_imports_runtime(tmp_path):
    child = """
import asyncio, sys
from pathlib import Path
from server.maintenance import create_maintenance_app
async def main():
    app = create_maintenance_app()
    async with app.router.lifespan_context(app):
        assert not app.routes
    assert 'server.app' not in sys.modules
    assert 'core.supervisor' not in sys.modules
    assert 'core.config' not in sys.modules
    assert not Path(sys.argv[1]).exists()
asyncio.run(main())
"""
    data = tmp_path / "not-created"
    result = subprocess.run(
        [sys.executable, "-c", child, str(data)],
        env={
            "PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"),
            "ANIMAWORKS_DATA_DIR": str(data), "ANIMAWORKS_DISABLE_EXTERNAL_SYNC": "1",
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(Path(__file__).resolve().parents[3]),
        },
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert not data.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,expected", [
    ("GET", "/health", 200), ("HEAD", "/health", 200),
    ("GET", "/api/system/health", 200), ("POST", "/health", 503),
    ("GET", "/", 503), ("GET", "/docs", 503),
    ("GET", "/api/approve/token", 503), ("POST", "/api/approve/token", 503),
    ("POST", "/api/setup/complete", 503), ("POST", "/api/auth/login", 503),
    ("POST", "/api/animas/synthetic/chat", 503),
    ("POST", "/api/webhooks/slack", 503), ("DELETE", "/api/animas/synthetic", 503),
    ("GET", "/health/../api/approve/token", 503),
    ("GET", "/prefix/health", 503), ("OPTIONS", "/api/setup", 503),
])
async def test_maintenance_admission(method, path, expected):
    app = create_maintenance_app()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.request(method, path)
    assert response.status_code == expected
    assert response.headers["cache-control"] == "no-store"
    if method != "HEAD":
        assert response.json() == {
            "status": "maintenance", "ready": False, "admission_enabled": False,
        }


@pytest.mark.asyncio
async def test_websocket_cannot_bypass_maintenance():
    sent = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    await create_maintenance_app()({"type": "websocket", "path": "/ws"}, receive, send)
    assert sent == [{"type": "websocket.close", "code": 1013}]


@pytest.mark.asyncio
async def test_factory_gate_precedes_config_migrations_and_runtime(tmp_path):
    from server.app import create_app

    data = tmp_path / "synthetic"
    data.mkdir()
    marker = data / "task_queue.jsonl"
    marker.write_text('{"task_id":"held","status":"blocked"}\n')
    before = {p.relative_to(data): p.read_bytes() for p in data.rglob("*") if p.is_file()}
    boundaries = [
        "server.app.load_config", "server.app.load_auth", "server.app.ProcessSupervisor",
        "core.config.migrate.migrate_person_to_anima", "core.config.migrate.migrate_all_cron",
        "server.app._activate_runtime_services", "server.app._run_startup_initialization",
        "server.app._run_model_warmup", "server.app._warm_voice_greets",
        "server.app.create_router", "server.app.create_setup_router",
    ]
    with ExitStack() as stack:
        mocks = [stack.enter_context(patch(name, side_effect=AssertionError(name))) for name in boundaries]
        app = create_app(data / "animas", data / "shared", maintenance=True)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            assert (await client.post("/api/setup/complete")).status_code == 503
        for mock in mocks:
            mock.assert_not_called()
    after = {p.relative_to(data): p.read_bytes() for p in data.rglob("*") if p.is_file()}
    assert after == before
    assert list(data.iterdir()) == [marker]
