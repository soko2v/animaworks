"""One-shot chat IPC admission for the isolated upgrade probe.

Not a launch entry point and not wired into the auth-only factory. The caller
must supply a separately verified text-only provider: this wrapper cannot stop
native tools inside an arbitrary provider. No normal Anima or scheduler is
constructed here. The spent latch is deliberately not reset after any outcome.
"""

from __future__ import annotations

import asyncio
import os
import pwd
import socket
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path

from core.supervisor.ipc import IPCRequest, IPCResponse, IPCServer


class CanaryChatSession:
    """Admit one fixed synthetic message, without retry or task dispatch.

    Lifetime is one isolated runner instance. This is not a durable cross-process
    execution ledger; restarting a runner requires a new operator authorization.
    """

    def __init__(self, complete: Callable[[str], Awaitable[str]]) -> None:
        self._complete = complete
        self._spent = False

    async def handle(self, request: IPCRequest) -> IPCResponse:
        denied = IPCResponse(id=request.id, error={"code": "canary_closed", "message": "Canary admission closed"})
        expected = {
            "message": "Reply with CANARY_OK only.",
            "from_person": "operator", "intent": "", "images": [],
            "attachment_paths": [], "thread_id": "default", "model": None,
        }
        if request.method != "process_message" or request.params != expected or self._spent:
            return denied
        # No await between admission and consumption: parallel/replayed requests
        # cannot invoke the provider twice in this runner's event loop.
        self._spent = True
        try:
            async with asyncio.timeout(60):
                result = await self._complete(expected["message"])
            if not isinstance(result, str) or result.strip() != "CANARY_OK":
                return denied
            return IPCResponse(id=request.id, result={"response": "CANARY_OK"})
        except asyncio.CancelledError:
            # Disconnect/cancellation does not authorize another provider call.
            raise
        except Exception:
            # Never expose provider errors, credentials or unexpected tool output.
            return denied


class CanaryIPCService:
    """Dedicated Unix listener, without normal runner/Anima initialization.

    No CLI entry point or real provider is selected here. A trusted caller owns
    the child process and its verified provider. Closing consumes this service;
    a failed start cannot be retried on the same instance. Never adopt or unlink
    an existing endpoint, and never fall back to TCP. Not a same-UID sandbox.
    """

    def __init__(self, path: Path, complete: Callable[[str], Awaitable[str]]) -> None:
        self.path = path
        self._ipc = IPCServer(path, CanaryChatSession(complete).handle)
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task] = set()
        self._identity: tuple[int, int] | None = None
        self._started = False
        self._closed = False

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if self._closed:
            writer.close()
            await writer.wait_closed()
            return
        self._connections.add(task)
        try:
            await self._ipc._handle_connection(reader, writer)
        finally:
            self._connections.discard(task)

    async def start(self) -> None:
        if self._started or self._closed:
            raise ValueError("Canary service already consumed")
        self._started = True
        parent = self.path.parent
        production = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".animaworks"
        info = parent.lstat()
        if (
            not self.path.is_absolute() or parent.resolve() != parent
            or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or len(os.fsencode(self.path)) >= 104
            or parent == production or production in parent.parents
        ):
            raise ValueError("Private isolated short Unix endpoint required")
        if os.path.lexists(self.path):
            raise ValueError("Canary endpoint already exists")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # Bind directly: asyncio's path-based helper may remove an existing
            # socket. Kernel bind is the exclusive claim and fails on conflicts.
            listener.bind(str(self.path))
            info = self.path.lstat()
            self._identity = (info.st_dev, info.st_ino)
            self.path.chmod(0o600)
            listener.setblocking(False)
            self._server = await asyncio.start_unix_server(self._connection, sock=listener, limit=8192)
        except BaseException:
            listener.close()
            await self.stop()
            raise

    async def stop(self) -> None:
        self._closed = True
        if self._server is not None:
            self._server.close()
        tasks = list(self._connections)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()
        if self._identity is not None:
            try:
                info = self.path.lstat()
                if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == self._identity:
                    self.path.unlink()
            except FileNotFoundError:
                pass
            self._identity = None
