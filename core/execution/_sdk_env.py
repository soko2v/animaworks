"""Serialize SDK startup while excluding inherited subscription auth keys."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

_startup_lock = asyncio.Lock()
_AUTH_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


@asynccontextmanager
async def sdk_client_context(client_type: Any, options: Any) -> AsyncIterator[Any]:
    """Connect an SDK client without leaking parent API keys into Max auth.

    All framework SDK starts share the lock, including API-auth starts. Only
    connection/startup runs with a temporarily filtered parent environment;
    restoration precedes query execution and also runs on error/cancellation.
    Explicit API, Bedrock and Vertex environments retain their auth behavior.
    """
    env = options.env
    subscription = not (
        env.get("ANTHROPIC_API_KEY") or env.get("CLAUDE_CODE_USE_BEDROCK") or env.get("CLAUDE_CODE_USE_VERTEX")
    )
    async with AsyncExitStack() as stack:
        async with _startup_lock:
            saved: dict[str, str] = {}
            if subscription:
                for key in _AUTH_KEYS:
                    env.pop(key, None)
                    if key in os.environ:
                        saved[key] = os.environ.pop(key)
            try:
                client = await stack.enter_async_context(client_type(options=options))
            finally:
                os.environ.update(saved)
        yield client
