"""Synthetic provider only; no claim of real provider/native-tool acceptance."""

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.supervisor.canary import CanaryChatSession, CanaryIPCService, ClaudeTextProbe
from core.supervisor.ipc import IPCRequest


def request(**changes):
    params = {
        "message": "Reply with CANARY_OK only.", "from_person": "operator",
        "intent": "", "images": [], "attachment_paths": [],
        "thread_id": "default", "model": None,
    }
    params.update(changes)
    return IPCRequest(id="synthetic", method="process_message", params=params)


@pytest.fixture
def cli_probe(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    home.mkdir(mode=0o700)
    cwd.mkdir(mode=0o700)
    executable = tmp_path / "synthetic-cli"
    executable.write_text("#!/bin/sh\nexit 1\n")
    executable.chmod(0o700)
    return ClaudeTextProbe(executable, home, cwd, oauth_token="synthetic-not-a-credential")


def test_cli_exact_toolless_spec_and_no_ambient_env(cli_probe, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-must-not-leak")
    monkeypatch.setenv("NODE_OPTIONS", "untrusted")
    args, env = cli_probe._launch_spec()
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "--strict-mcp-config" in args and "--disable-slash-commands" in args
    assert args[args.index("--setting-sources") + 1] == ""
    assert args[args.index("--settings") + 1] == '{"disableAllHooks":true}'
    assert args[args.index("--max-turns") + 1] == "1"
    assert "--fallback-model" not in args and "--resume" not in args
    assert env["CLAUDE_CODE_MAX_RETRIES"] == "0"
    assert "ANTHROPIC_API_KEY" not in env and "NODE_OPTIONS" not in env
    assert cli_probe._token not in " ".join(args)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [False, True])
async def test_real_synthetic_cli_one_shot(cli_probe, bad):
    import json

    result = {"type": "result", "subtype": "success", "is_error": False,
              "num_turns": 1, "result": "CANARY_OK", "permission_denials": []}
    if bad:
        result["permission_denials"] = [{"tool": "Bash"}]
    cli_probe.executable.write_text("#!/bin/sh\ncat >/dev/null\nprintf '%s' '" + json.dumps(result) + "'\n")
    if bad:
        with pytest.raises(ValueError, match="Canary admission closed"):
            await cli_probe("Reply with CANARY_OK only.")
    else:
        assert await cli_probe("Reply with CANARY_OK only.") == "CANARY_OK"
    assert cli_probe._token == ""
    with pytest.raises(ValueError):
        await cli_probe("Reply with CANARY_OK only.")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["public_home", "nonempty_cwd", "linked_cli", "public_cli"])
async def test_cli_rejects_unsafe_roots_before_spawn(cli_probe, kind, monkeypatch):
    if kind == "public_home":
        cli_probe.home.chmod(0o755)
    elif kind == "nonempty_cwd":
        (cli_probe.cwd / ".mcp.json").write_text("{}")
    elif kind == "public_cli":
        cli_probe.executable.chmod(0o777)
    else:
        link = cli_probe.executable.with_name("link")
        link.symlink_to(cli_probe.executable)
        cli_probe.executable = link
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(ValueError):
        await cli_probe("Reply with CANARY_OK only.")
    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_cli_cancel_during_spawn_reaps_owned_process(cli_probe, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    process = SimpleNamespace(pid=123456789, returncode=None, wait=AsyncMock(return_value=-9))

    async def spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        return process

    kills = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: kills.append(pid))
    task = asyncio.create_task(cli_probe("Reply with CANARY_OK only."))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert kills == [process.pid]
    process.wait.assert_awaited_once()
    assert cli_probe._token == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("script", [
    "printf 'not-json'", "printf '[]'", "exit 1",
    "dd if=/dev/zero bs=4096 count=20 2>/dev/null",
])
async def test_cli_errors_bounded_and_never_retried(cli_probe, script):
    cli_probe.executable.write_text("#!/bin/sh\ncat >/dev/null\n" + script + "\n")
    for _ in range(2):
        with pytest.raises(ValueError, match="Canary admission closed"):
            await cli_probe("Reply with CANARY_OK only.")


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
async def test_private_service_conflict_preserves_original_and_no_tcp(monkeypatch):
    monkeypatch.setenv("ANIMAWORKS_IPC_TRANSPORT", "tcp")
    with tempfile.TemporaryDirectory(prefix="aw-c-", dir="/tmp") as directory:
        path = Path(directory).resolve() / "probe.sock"
        provider = AsyncMock(return_value="CANARY_OK")
        first = CanaryIPCService(path, provider)
        second = CanaryIPCService(path, provider)
        await first.start()
        identity = path.stat().st_ino
        try:
            with pytest.raises((OSError, ValueError)):
                await second.start()
            assert path.stat().st_ino == identity
            reader, writer = await asyncio.open_unix_connection(path)
            writer.write((request().to_json() + "\n").encode())
            await writer.drain()
            assert b"CANARY_OK" in await reader.readline()
            writer.close()
            await writer.wait_closed()
        finally:
            await first.stop()
        assert not path.exists()
        with pytest.raises(ValueError):
            await first.start()
        provider.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["file", "symlink", "public_parent", "long_path"])
async def test_private_service_rejects_unsafe_endpoint(kind):
    with tempfile.TemporaryDirectory(prefix="aw-c-", dir="/tmp") as directory:
        parent = Path(directory).resolve()
        path = parent / "probe.sock"
        if kind == "file":
            path.write_text("do not overwrite")
        elif kind == "symlink":
            path.symlink_to(parent / "missing")
        elif kind == "public_parent":
            parent.chmod(0o755)
        else:
            path = parent / ("x" * 110)
        service = CanaryIPCService(path, AsyncMock())
        with pytest.raises((ValueError, OSError)):
            await service.start()
        await service.stop()
        if kind == "file":
            assert path.read_text() == "do not overwrite"
        if kind == "symlink":
            assert path.is_symlink()


@pytest.mark.asyncio
async def test_service_stop_cancels_inflight_provider():
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def complete(message):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with tempfile.TemporaryDirectory(prefix="aw-c-", dir="/tmp") as directory:
        path = Path(directory).resolve() / "probe.sock"
        service = CanaryIPCService(path, complete)
        await service.start()
        reader, writer = await asyncio.open_unix_connection(path)
        try:
            writer.write((request().to_json() + "\n").encode())
            await writer.drain()
            await asyncio.wait_for(entered.wait(), 3)
            await asyncio.wait_for(service.stop(), 3)
            assert cancelled.is_set()
            assert not service._connections
            assert await reader.read() == b""
        finally:
            writer.close()
            await writer.wait_closed()
            await service.stop()


@pytest.mark.asyncio
async def test_real_child_private_ipc_without_normal_runtime():
    """Real child lifecycle, synthetic provider, no actual credentials/model."""
    script = '''
import asyncio, builtins, sys
from pathlib import Path
original_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.startswith(("core.anima", "core.agent", "core.execution", "core.supervisor.runner", "core.lifecycle")):
        raise AssertionError("Normal runtime import attempted")
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded
from core.supervisor.canary import CanaryIPCService
from core.supervisor.manager import ProcessSupervisor
from core.supervisor.process_handle import ProcessHandle
from apscheduler.schedulers.asyncio import AsyncIOScheduler
def denied(*args, **kwargs):
    raise AssertionError("Normal runtime activation attempted")
ProcessSupervisor.__init__ = denied
ProcessHandle.__init__ = denied
AsyncIOScheduler.start = denied
async def main():
    calls = 0
    async def complete(message):
        nonlocal calls
        calls += 1
        return "CANARY_OK"
    service = CanaryIPCService(Path(sys.argv[1]), complete)
    await service.start()
    print("READY", flush=True)
    try:
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await service.stop()
    assert calls == 1
    assert not service._connections
    print("STOPPED", flush=True)
asyncio.run(main())
'''
    with tempfile.TemporaryDirectory(prefix="aw-c-", dir="/tmp") as directory:
        parent = Path(directory).resolve()
        path = parent / "probe.sock"
        env = {"PATH": "/usr/bin:/bin", "HOME": str(parent / "home"),
               "ANIMAWORKS_DATA_DIR": str(parent / "data"),
               "ANIMAWORKS_DISABLE_EXTERNAL_SYNC": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-B", "-c", script, str(path), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            ready = await asyncio.wait_for(child.stdout.readline(), 10)
            assert ready == b"READY\n", ready if ready else (await child.stderr.read()).decode()
            assert child.pid != os.getpid()
            reader, writer = await asyncio.open_unix_connection(path)
            try:
                methods = ("run_heartbeat", "run_cron", "startup_ack", "execute_task", "process_message", "process_message")
                for index, method in enumerate(methods):
                    probe = request()
                    probe.method = method
                    writer.write((probe.to_json() + "\n").encode())
                    await writer.drain()
                    result = await asyncio.wait_for(reader.readline(), 3)
                    if method != "process_message":
                        assert b"canary_closed" in result
                    if index == 4:
                        assert b'"response": "CANARY_OK"' in result
                assert b"canary_closed" in result  # replay denied
            finally:
                writer.close()
                await writer.wait_closed()
            child.stdin.write(b"stop\n")
            await child.stdin.drain()
            stdout, stderr = await asyncio.wait_for(child.communicate(), 10)
            assert child.returncode == 0, stderr.decode()
            assert stdout == b"STOPPED\n"
            assert not path.exists()
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()


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
