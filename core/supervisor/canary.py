"""One-shot chat IPC admission for the isolated upgrade probe.

Not a launch entry point and not wired into the auth-only factory. The caller
must supply a separately verified text-only provider: this wrapper cannot stop
native tools inside an arbitrary provider. No normal Anima or scheduler is
constructed here. The spent latch is deliberately not reset after any outcome.
"""

from __future__ import annotations

import asyncio
import json
import os
import pwd
import signal
import socket
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path

from core.supervisor.ipc import IPCRequest, IPCResponse, IPCServer


class ClaudeTextProbe:
    """Single explicit CLI call; not a public launcher or credential resolver.

    Only use with operator-approved isolated credentials after critical review.
    The executable and managed machine policy remain trusted. CLI flag acceptance
    is not proof of native behavior; actual tool/retry acceptance is still needed.
    No normal executor, SDK auth retry, fallback, or inherited environment is used.
    """

    def __init__(self, executable: Path, home: Path, cwd: Path, *, oauth_token: str) -> None:
        self.executable, self.home, self.cwd = executable, home, cwd
        # Obtain only via the approved resolver. Never persist, log or put in argv.
        if not isinstance(oauth_token, str) or not oauth_token or "\x00" in oauth_token:
            raise ValueError("Explicit resolved probe authorization required")
        self._token = oauth_token
        self._spent = False

    def _launch_spec(self) -> tuple[list[str], dict[str, str]]:
        production = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".animaworks"
        for path in (self.home, self.cwd):
            info = path.lstat()
            if (
                not path.is_absolute() or path.resolve() != path
                or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or path == production
                or production in path.parents or path in production.parents
                or any(path.iterdir())
            ):
                raise ValueError("Empty private isolated probe directories required")
        if self.home == self.cwd or self.home in self.cwd.parents or self.cwd in self.home.parents:
            raise ValueError("Separate probe directories required")
        info = self.executable.lstat()
        if (
            not self.executable.is_absolute() or self.executable.resolve() != self.executable
            or not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
            or info.st_mode & 0o022 or not os.access(self.executable, os.X_OK)
        ):
            raise ValueError("Trusted absolute probe executable required")
        argv = [
            str(self.executable), "--print", "--output-format", "json",
            "--model", "claude-fable-5-1", "--max-turns", "1",
            "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--setting-sources", "", "--settings", '{"disableAllHooks":true}',
            "--disable-slash-commands", "--no-session-persistence",
            "--permission-mode", "dontAsk", "--system-prompt", "Reply with CANARY_OK only.",
        ]
        # In particular, never inherit proxy/base URL, NODE_OPTIONS, telemetry,
        # API keys, plugins, provider selection, or normal Anima data paths.
        env = {
            "PATH": "/usr/bin:/bin", "HOME": str(self.home),
            "CLAUDE_CONFIG_DIR": str(self.home),
            "CLAUDE_CODE_MAX_RETRIES": "0", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
            "CLAUDE_CODE_OAUTH_TOKEN": self._token,
        }
        return argv, env

    async def __call__(self, message: str) -> str:
        if self._spent or message != "Reply with CANARY_OK only.":
            raise ValueError("Canary admission closed")
        self._spent = True
        process = None
        try:
            argv, env = self._launch_spec()
            async with asyncio.timeout(50):
                spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                    *argv, cwd=self.cwd, env=env, start_new_session=True,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, limit=65536,
                ))
                try:
                    process = await asyncio.shield(spawning)
                except asyncio.CancelledError:
                    # Do not lose ownership if cancellation races process creation.
                    process = await spawning
                    raise
                process.stdin.write(message.encode())
                await process.stdin.drain()
                process.stdin.close()
                raw = bytearray()
                while chunk := await process.stdout.read(4096):
                    raw.extend(chunk)
                    if len(raw) > 65536:
                        raise ValueError("Probe output limit")
                code = await process.wait()
                result = json.loads(raw)
                if (
                    code != 0 or not isinstance(result, dict)
                    or result.get("type") != "result" or result.get("subtype") != "success"
                    or result.get("is_error") is not False
                    or type(result.get("num_turns")) is not int or result["num_turns"] != 1
                    or result.get("result") != "CANARY_OK"
                    or result.get("permission_denials") != []
                ):
                    raise ValueError("Probe unsuccessful")
                return "CANARY_OK"
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ValueError("Canary admission closed") from None
        finally:
            self._token = ""
            if process is not None and process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()


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
