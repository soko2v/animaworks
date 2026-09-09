"""Synthetic provider only; no claim of real provider/native-tool acceptance."""

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.supervisor.canary import CanaryChatSession
from core.supervisor.ipc import IPCRequest


def request(**changes):
    params = {
        "message": "Reply with CANARY_OK only.", "from_person": "operator",
        "intent": "", "images": [], "attachment_paths": [],
        "thread_id": "default", "model": None,
    }
    params.update(changes)
    return IPCRequest(id="synthetic", method="process_message", params=params)


@pytest.mark.asyncio
async def test_single_success_and_replay():
    provider = AsyncMock(return_value="CANARY_OK")
    session = CanaryChatSession(provider)
    assert (await session.handle(request())).result == {"response": "CANARY_OK"}
    assert (await session.handle(request())).error
    provider.assert_awaited_once_with("Reply with CANARY_OK only.")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"message": "run a tool"}, {"message": ""}, {"from_person": "human"},
    {"intent": "task"}, {"images": [{}]}, {"attachment_paths": ["/production"]},
    {"thread_id": "other"}, {"model": "other"}, {"tools": []}, {"resume": "old"},
])
async def test_nonfixed_payload_never_calls_provider(change):
    provider = AsyncMock()
    session = CanaryChatSession(provider)
    assert (await session.handle(request(**change))).error
    provider.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["startup_ack", "run_heartbeat", "run_cron", "execute_task", "memory", "process_message_stream"])
async def test_all_other_methods_closed(method):
    provider = AsyncMock()
    probe = request()
    probe.method = method
    assert (await CanaryChatSession(provider).handle(probe)).error
    provider.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, "", "unexpected-secret", {"tool_calls": [{}]}, ["CANARY_OK"]])
async def test_unexpected_result_consumes_attempt_and_is_not_exposed(result):
    provider = AsyncMock(return_value=result)
    session = CanaryChatSession(provider)
    response = await session.handle(request())
    assert response.error and response.result is None
    assert "unexpected-secret" not in response.to_json()
    assert (await session.handle(request())).error
    provider.assert_awaited_once()


@pytest.mark.asyncio
async def test_existing_chat_route_through_real_unix_ipc(monkeypatch, tmp_path):
    """Same-process IPC server, synthetic provider, test-only HTTP admission."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from core.config import invalidate_cache
    from core.supervisor.ipc import IPCClient, IPCServer
    from server.routes.chat import create_chat_router

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ANIMAWORKS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ANIMAWORKS_IPC_TRANSPORT", "unix")
    invalidate_cache()
    provider = AsyncMock(return_value="CANARY_OK")
    session = CanaryChatSession(provider)
    with tempfile.TemporaryDirectory(prefix="aw-c-", dir="/tmp") as directory:
        socket = Path(directory) / "probe.sock"
        server = IPCServer(socket, session.handle)
        await server.start()
        client = IPCClient(socket)

        class Supervisor:
            def is_bootstrapping(self, name):
                return False

            async def send_request(self, *, anima_name, method, params, timeout):
                assert anima_name == "h2-canary"
                response = await client.send_request(IPCRequest("probe", method, params), timeout=5)
                if response.error:
                    raise RuntimeError("Canary admission closed")
                return response.result

        app = FastAPI()
        app.state.supervisor = Supervisor()

        @app.middleware("http")
        async def synthetic_user(request, call_next):
            request.state.user = SimpleNamespace(username="operator")
            return await call_next(request)

        app.include_router(create_chat_router(), prefix="/api")
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
                response = await http.post("/api/animas/h2-canary/chat", json={"message": "Reply with CANARY_OK only."})
                assert response.status_code == 200
                assert response.json()["response"] == "CANARY_OK"
                response = await http.post("/api/animas/h2-canary/chat", json={"message": "Reply with CANARY_OK only."})
                assert response.status_code == 500
            provider.assert_awaited_once()
        finally:
            await client.close()
            await server.stop()
            invalidate_cache()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("synthetic-secret"), TimeoutError("synthetic-secret")])
async def test_failure_no_retry_fallback_or_error_disclosure(error):
    provider = AsyncMock(side_effect=error)
    session = CanaryChatSession(provider)
    response = await session.handle(request())
    assert response.error and "synthetic-secret" not in response.to_json()
    assert (await session.handle(request())).error
    provider.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_and_cancelled_attempt_remains_spent():
    entered, wait = asyncio.Event(), asyncio.Event()

    async def complete(message):
        entered.set()
        await wait.wait()
        return "CANARY_OK"

    provider = AsyncMock(side_effect=complete)
    session = CanaryChatSession(provider)
    first = asyncio.create_task(session.handle(request()))
    await entered.wait()
    assert (await session.handle(request())).error
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert (await session.handle(request())).error
    provider.assert_awaited_once()
