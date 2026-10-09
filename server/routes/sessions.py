from __future__ import annotations

# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Request

from core.chat_attachments import describe_attachment_paths
from core.memory.activity.logger import ActivityLogger
from core.memory.conversation.memory import ConversationMemory
from core.memory.conversation.shortterm import ShortTermMemory
from core.memory.manager import MemoryManager
from core.skills.activation import validate_thread_id

logger = logging.getLogger("animaworks.routes.sessions")


def _thread_file_conversation_view(anima_dir, thread_id: str) -> dict | None:
    """Build a conversation view from the thread's conversation file.

    Threads persist their turns in ``state/conversations/{thread_id}.json``
    while the activity log only holds entries within its retention window
    (and older threads predate thread_id tagging entirely). When the
    activity-based view is missing turns, this file is the durable source.
    Returns None when the file is absent or unreadable.
    """
    conv_file = anima_dir / "state" / "conversations" / f"{thread_id}.json"
    if not conv_file.is_file():
        return None
    try:
        data = json.loads(conv_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, TypeError):
        return None

    turns = data.get("turns", [])
    if not isinstance(turns, list):
        return None

    messages = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role", "")
        if role not in ("human", "assistant"):
            continue
        tool_calls = []
        for rec in turn.get("tool_records", []) or []:
            if not isinstance(rec, dict):
                continue
            tool_calls.append(
                {
                    "tool_use_id": rec.get("tool_id", ""),
                    "tool_name": rec.get("tool_name", ""),
                    "input": rec.get("input_summary", ""),
                    "result": rec.get("result_summary", ""),
                    "is_error": bool(rec.get("is_error", False)),
                }
            )
        message = {
            "ts": turn.get("timestamp", ""),
            "role": role,
            "content": turn.get("content", ""),
            "from_person": "",
            "to_person": "",
            "source_key": "chat",
            "tool_calls": tool_calls,
        }
        raw_attachments = turn.get("attachments")
        if role == "human" and isinstance(raw_attachments, list) and raw_attachments:
            attachments = describe_attachment_paths(anima_dir, raw_attachments)
            if attachments:
                message["attachments"] = attachments
        messages.append(message)

    if not messages:
        return None

    sessions = ActivityLogger._group_into_sessions(messages, 10)
    return {"sessions": sessions, "has_more": False, "next_before": None}


def _count_view_messages(view: dict) -> int:
    return sum(len(s.get("messages", [])) for s in view.get("sessions", []))


def create_sessions_router() -> APIRouter:
    router = APIRouter()

    @router.get("/animas/{name}/sessions")
    async def list_sessions(name: str, request: Request):
        """List all available sessions: active conversation, archives, episodes."""
        animas_dir = request.app.state.animas_dir
        anima_dir = animas_dir / name
        if not anima_dir.exists():
            raise HTTPException(status_code=404, detail=f"Anima not found: {name}")

        from core.config.models import load_model_config

        model_config = load_model_config(anima_dir)

        # Active conversation
        conv = ConversationMemory(anima_dir, model_config)
        conv_state = conv.load()
        active_conv = None
        if conv_state.turns or conv_state.compressed_summary:
            active_conv = {
                "exists": True,
                "turn_count": len(conv_state.turns),
                "total_turn_count": conv_state.total_turn_count,
                "last_timestamp": (conv_state.turns[-1].timestamp if conv_state.turns else ""),
                "first_timestamp": (conv_state.turns[0].timestamp if conv_state.turns else ""),
                "has_summary": bool(conv_state.compressed_summary),
            }

        # Thread conversations
        threads = []
        conv_dir = anima_dir / "state" / "conversations"
        if conv_dir.is_dir():
            for conv_file in sorted(conv_dir.glob("*.json"), reverse=True):
                try:
                    data = json.loads(conv_file.read_text(encoding="utf-8"))
                    turns = data.get("turns", [])
                    threads.append(
                        {
                            "thread_id": conv_file.stem,
                            "turn_count": len(turns),
                            "total_turn_count": len(turns) + data.get("compressed_turn_count", 0),
                            "last_timestamp": turns[-1].get("timestamp", "") if turns else "",
                            "has_summary": bool(data.get("compressed_summary", "")),
                        }
                    )
                except (json.JSONDecodeError, TypeError):
                    pass

        # Archived sessions — metadata only (no full content read)
        stm = ShortTermMemory(anima_dir)
        archived = []
        archive_dir = stm._archive_dir
        if archive_dir.exists():
            for json_file in sorted(archive_dir.glob("*.json"), reverse=True):
                try:
                    data = json.loads(json_file.read_text(encoding="utf-8"))
                    ts_str = json_file.stem
                    archived.append(
                        {
                            "id": ts_str,
                            "timestamp": data.get("timestamp", ts_str),
                            "trigger": data.get("trigger", ""),
                            "turn_count": data.get("turn_count", 0),
                            "context_usage_ratio": data.get(
                                "context_usage_ratio",
                                0,
                            ),
                            "original_prompt_preview": data.get(
                                "original_prompt",
                                "",
                            )[:200],
                            "has_markdown": (archive_dir / f"{ts_str}.md").exists(),
                        }
                    )
                except (json.JSONDecodeError, TypeError):
                    pass

        # Episodes — partial read (first 200 chars only, no full file load)
        memory = MemoryManager(anima_dir)
        episodes = []
        for stem in memory.list_episode_files():
            ep_path = memory.episodes_dir / f"{stem}.md"
            preview = ""
            if ep_path.exists():
                with ep_path.open(encoding="utf-8") as f:
                    preview = f.read(200)
            episodes.append({"date": stem, "preview": preview})

        # Transcripts — count lines without loading full content
        transcripts = []
        transcript_dir = anima_dir / "transcripts"
        if transcript_dir.exists():
            for tf in sorted(transcript_dir.glob("*.jsonl"), reverse=True):
                line_count = sum(1 for line in tf.read_text(encoding="utf-8").splitlines() if line.strip())
                transcripts.append({"date": tf.stem, "message_count": line_count})

        return {
            "anima": name,
            "active_conversation": active_conv,
            "threads": threads,
            "archived_sessions": archived,
            "episodes": episodes,
            "transcripts": transcripts,
        }

    # ── Conversation history (activity_log-based) ──────────

    @router.get("/animas/{name}/conversation/history")
    async def get_conversation_history(
        name: str,
        limit: int = 50,
        before: str | None = None,
        thread_id: str = "default",
        strict_thread: bool = False,
        request: Request = None,
    ):
        """Get conversation history from activity log.

        Returns sessions with messages, tool calls nested into assistant
        messages, and cursor-based pagination.
        """
        animas_dir = request.app.state.animas_dir
        anima_dir = animas_dir / name
        if not anima_dir.exists():
            raise HTTPException(status_code=404, detail=f"Anima not found: {name}")

        limit = max(1, min(limit, 200))
        activity = ActivityLogger(anima_dir)
        # Log scanning is sync file I/O + JSON parsing; keep it off the
        # event loop (UI polls this endpoint every few seconds).
        view = await asyncio.to_thread(
            activity.get_conversation_view,
            before=before,
            limit=limit,
            thread_id=thread_id,
            strict_thread=strict_thread,
        )

        # Named threads: the activity log may miss turns (retention window,
        # pre-thread_id-tagging history, or a response whose activity entry
        # was lost). Fall back to the thread's own conversation file when it
        # holds more messages than the activity-based view.
        if thread_id not in ("default", "inbox") and before is None:
            try:
                safe_thread_id = validate_thread_id(thread_id)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid thread_id: {thread_id}") from None
            file_view = _thread_file_conversation_view(anima_dir, safe_thread_id)
            if file_view and _count_view_messages(file_view) > _count_view_messages(view):
                return file_view
        return view

    @router.get("/animas/{name}/sessions/{session_id}")
    async def get_session_detail(
        name: str,
        session_id: str,
        request: Request,
    ):
        """Get archived session detail."""
        animas_dir = request.app.state.animas_dir
        anima_dir = animas_dir / name
        if not anima_dir.exists():
            raise HTTPException(status_code=404, detail=f"Anima not found: {name}")

        stm = ShortTermMemory(anima_dir)
        archive_dir = stm._archive_dir
        json_path = archive_dir / f"{session_id}.json"
        md_path = archive_dir / f"{session_id}.md"

        if not json_path.exists():
            raise HTTPException(status_code=404, detail="Session not found")

        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError):
            raise HTTPException(status_code=500, detail="Session data corrupted") from None

        markdown = ""
        if md_path.exists():
            markdown = md_path.read_text(encoding="utf-8")

        return {
            "anima": name,
            "session_id": session_id,
            "data": data,
            "markdown": markdown,
        }

    @router.get("/animas/{name}/transcripts/{date}")
    async def get_transcript(name: str, date: str, request: Request):
        """Get full conversation transcript for a specific date."""
        animas_dir = request.app.state.animas_dir
        anima_dir = animas_dir / name
        if not anima_dir.exists():
            raise HTTPException(status_code=404, detail=f"Anima not found: {name}")

        from core.config.models import load_model_config

        model_config = load_model_config(anima_dir)

        conv = ConversationMemory(anima_dir, model_config)
        messages = conv.load_transcript(date)
        return {
            "anima": name,
            "date": date,
            "has_summary": False,
            "compressed_summary": "",
            "compressed_turn_count": 0,
            "turns": messages,
        }

    return router
