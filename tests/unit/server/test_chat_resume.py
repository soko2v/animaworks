from __future__ import annotations

import pytest

from server.routes.chat_resume import _handle_resume
from server.stream_registry import StreamRegistry


async def _collect_sse(response) -> str:
    return "".join([chunk async for chunk in response.body_iterator])


@pytest.mark.asyncio
async def test_terminal_error_replays_as_history_not_current_error() -> None:
    registry = StreamRegistry()
    stream = registry.register("alice", from_person="human")
    stream.add_event("error", {"code": "CONNECTION_LOST", "message": "old failure"})
    registry.mark_complete(stream.response_id, done=False)

    result = _handle_resume(
        registry,
        stream.response_id,
        "",
        "alice",
        from_person="human",
    )
    combined = await _collect_sse(result)

    assert "event: history\n" in combined
    assert "event: error\n" not in combined
    assert "CONNECTION_LOST" in combined


@pytest.mark.asyncio
async def test_error_from_active_stream_remains_error() -> None:
    registry = StreamRegistry()
    stream = registry.register("alice", from_person="human")
    result = _handle_resume(
        registry,
        stream.response_id,
        "",
        "alice",
        from_person="human",
    )
    stream.add_event("error", {"code": "CONNECTION_LOST", "message": "current failure"})
    registry.mark_complete(stream.response_id, done=False)

    combined = await _collect_sse(result)
    assert "event: error\n" in combined
    assert "event: history\n" not in combined
