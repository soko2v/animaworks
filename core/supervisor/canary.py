"""One-shot chat IPC admission for the isolated upgrade probe.

Not a launch entry point and not wired into the auth-only factory. The caller
must supply a separately verified text-only provider: this wrapper cannot stop
native tools inside an arbitrary provider. No normal Anima or scheduler is
constructed here. The spent latch is deliberately not reset after any outcome.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from core.supervisor.ipc import IPCRequest, IPCResponse


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
