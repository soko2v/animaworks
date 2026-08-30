from __future__ import annotations

# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
import logging

from fastapi.responses import StreamingResponse

from core.i18n import t
from server.routes.chat_chunk_handler import _format_sse
from server.stream_registry import StreamRegistry, format_sse_with_id

logger = logging.getLogger("animaworks.routes.chat")


def _handle_resume(
    registry: StreamRegistry,
    resume_id: str,
    last_event_id: str,
    anima_name: str,
    *,
    from_person: str = "human",
):
    """Handle SSE stream resume request."""
    logger.info(
        "[SSE-RESUME] request stream=%s anima=%s last_event_id=%s from=%s",
        resume_id,
        anima_name,
        last_event_id,
        from_person,
    )
    stream = registry.get(resume_id)
    if stream is None or stream.anima_name != anima_name or stream.from_person != from_person:
        logger.info(
            "[SSE-RESUME] NOT_FOUND stream=%s (exists=%s anima_match=%s from_match=%s)",
            resume_id,
            stream is not None,
            stream.anima_name == anima_name if stream else "N/A",
            stream.from_person == from_person if stream else "N/A",
        )

        async def _not_found():
            yield _format_sse("error", {"code": "STREAM_NOT_FOUND", "message": t("chat.stream_not_found")})

        return StreamingResponse(
            _not_found(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    # Parse last event ID to get sequence number
    after_seq = -1
    if last_event_id and ":" in last_event_id:
        try:
            after_seq = int(last_event_id.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            pass

    logger.info(
        "[SSE-RESUME] replaying stream=%s after_seq=%d complete=%s total_events=%d",
        resume_id,
        after_seq,
        stream.complete,
        stream.event_count,
    )
    terminal_at_resume = stream.complete

    async def _replay_events():
        current_seq = after_seq
        replay_count = 0
        for event in stream.events_after(after_seq):
            if terminal_at_resume and event.event == "error":
                yield format_sse_with_id(
                    "history",
                    {"event": event.event, "payload": event.payload},
                    event.event_id,
                )
            else:
                yield format_sse_with_id(event.event, event.payload, event.event_id)
            current_seq = event.seq
            replay_count += 1

        logger.info(
            "[SSE-RESUME] replayed stream=%s count=%d current_seq=%d complete=%s",
            resume_id,
            replay_count,
            current_seq,
            stream.complete,
        )

        if not stream.complete:
            wait_count = 0
            while not stream.complete:
                got_event = await stream.wait_new_event(timeout=30.0)
                if not got_event:
                    wait_count += 1
                    logger.info(
                        "[SSE-RESUME] keepalive stream=%s wait#%d current_seq=%d",
                        resume_id,
                        wait_count,
                        current_seq,
                    )
                    yield ": keepalive\n\n"
                    continue
                new_events = stream.events_after(current_seq)
                for event in new_events:
                    yield format_sse_with_id(event.event, event.payload, event.event_id)
                    current_seq = event.seq
                logger.info(
                    "[SSE-RESUME] new_events stream=%s count=%d current_seq=%d",
                    resume_id,
                    len(new_events),
                    current_seq,
                )
        logger.info("[SSE-RESUME] done stream=%s final_seq=%d", resume_id, current_seq)

    return StreamingResponse(
        _replay_events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
