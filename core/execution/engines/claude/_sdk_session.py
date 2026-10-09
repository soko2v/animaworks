from __future__ import annotations

# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
#
# This file is part of AnimaWorks core/server, licensed under Apache-2.0.
# See LICENSE for the full license text.


"""Mode S session persistence, SDK input helpers, and cleanup utilities.

Leaf module in the dependency graph — depends only on the shared session
store and ``core.schemas``.
"""

import asyncio
import json
import logging
import shutil
import sys
import threading
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.execution.session_store import SessionStore
from core.execution.session_types import RESUMABLE_SESSION_TYPES
from core.schemas import ImageData

logger = logging.getLogger("animaworks.execution.agent_sdk")


# ── SDK constants ────────────────────────────────────────────

# Safety margin for Agent SDK JSON-RPC buffer.  The default (1 MB) is too
# small when system_prompt + conversation history grow large; 4 MB gives
# comfortable headroom while still catching genuinely broken messages.
_SDK_MAX_BUFFER_SIZE = 64 * 1024 * 1024  # 64 MB (PDF page reads can exceed 4 MB)

# When the system prompt is too large for a CLI argument, it is written to a
# temp file and passed via the CLI's --system-prompt-file flag.
#
# Platform limits:
#   - Linux: MAX_ARG_STRLEN = 128 KiB per argument (kernel compile-time constant).
#     Exceeding it causes `execve` to fail with E2BIG ([Errno 7]).
#   - Windows: Always use a file.  The system prompt contains Japanese (and
#     other non-ASCII) characters that get corrupted when passed through
#     cmd.exe → Node.js argument parsing.  The original 28 KB threshold
#     was based on the CreateProcess() 32,767-character limit, but the
#     encoding corruption issue makes inline prompts unreliable at *any*
#     size on Windows.
_PROMPT_FILE_THRESHOLD = 0 if sys.platform == "win32" else 100_000

# SDK Issue #387: invalid session ID causes SDK to hang for ~60s before
# raising an error.  We wrap the first-event receive in asyncio.wait_for
# so that a stale/invalid resume fails fast and falls back to a fresh session.
RESUME_TIMEOUT_SEC = 15.0

# /compact processes the entire session transcript — this takes significantly
# longer than a simple resume handshake.
COMPACT_TIMEOUT_SEC: float = 300.0

# When estimated context usage leaves fewer than max_tokens * this factor
# free, the PreToolUse hook triggers session termination for auto-compact.
_CONTEXT_AUTOCOMPACT_SAFETY = 2

# Timeout for client.interrupt() during graceful session interruption.
# The SDK default is 60s; we use a shorter timeout to avoid blocking the
# user's next message.  On timeout, the StreamEvent fallback captures the
# session_id so the session can still be resumed.
INTERRUPT_TIMEOUT_SEC: float = 5.0


# ── Debug helpers ────────────────────────────────────────────


def _is_debug_superuser(anima_dir: Path) -> bool:
    """Check if an anima has debug_superuser flag in status.json."""
    status_path = anima_dir / "status.json"
    if not status_path.is_file():
        return False
    try:
        data = json.loads(status_path.read_text(encoding="utf-8"))
        return bool(data.get("debug_superuser"))
    except (json.JSONDecodeError, OSError):
        return False


# ── Prompt file cleanup ──────────────────────────────────────


def _cleanup_prompt_files(files: list[Path]) -> None:
    """Remove temp prompt files created for --system-prompt-file."""
    for f in files:
        try:
            f.unlink(missing_ok=True)
        except OSError:
            logger.debug("Failed to remove temp prompt file: %s", f)


# ── Session ID persistence ───────────────────────────────────

# ── Session type constants ────────────────────────────────────

SESSION_TYPE_CHAT = "chat"
SESSION_TYPE_HEARTBEAT = "heartbeat"
SESSION_TYPE_CRON = "cron"
SESSION_TYPE_TASK = "task"
SESSION_TYPE_INBOX = "inbox"

_RESUMABLE_SESSION_TYPES = RESUMABLE_SESSION_TYPES
"""Only these session types persist and resume SDK sessions.
All other types start fresh each time (no resume, no save)."""


@dataclass(frozen=True)
class SessionContextState:
    """Persisted metadata for a resumable SDK session."""

    session_id: str
    timestamp: str
    created_at: str
    updated_at: str
    baseline_tokens: int = 0
    last_tokens: int = 0
    last_ratio: float = 0.0
    model: str = ""
    swept_at: str = ""


_session_state_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _session_state_path(anima_dir: Path, session_type: str, thread_id: str) -> Path:
    return SessionStore.path_for("agent_sdk", anima_dir, session_type, thread_id)


def _state_from_dict(data: dict[str, Any]) -> SessionContextState | None:
    """Convert old and current state-file shapes into typed state."""
    session_id = data.get("session_id")
    if not isinstance(session_id, str):
        return None
    if not session_id and not any(
        key in data for key in ("created_at", "updated_at", "baseline_tokens", "last_tokens", "last_ratio")
    ):
        return None
    timestamp = data.get("timestamp")
    if not isinstance(timestamp, str) or not timestamp:
        timestamp = _now_iso()
    created_at = data.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        created_at = timestamp
    updated_at = data.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at:
        updated_at = timestamp
    try:
        baseline_tokens = max(0, int(data.get("baseline_tokens", 0) or 0))
        last_tokens = max(0, int(data.get("last_tokens", 0) or 0))
        last_ratio = float(data.get("last_ratio", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None
    model = data.get("model", "")
    swept_at = data.get("swept_at", "")
    return SessionContextState(
        session_id=session_id,
        timestamp=timestamp,
        created_at=created_at,
        updated_at=updated_at,
        baseline_tokens=baseline_tokens,
        last_tokens=last_tokens,
        last_ratio=last_ratio,
        model=model if isinstance(model, str) else "",
        swept_at=swept_at if isinstance(swept_at, str) else "",
    )


def load_session_state(
    anima_dir: Path,
    session_type: str = SESSION_TYPE_CHAT,
    thread_id: str = "default",
) -> SessionContextState | None:
    """Load persisted SDK session metadata, including legacy files."""
    path = _session_state_path(anima_dir, session_type, thread_id)
    try:
        data = SessionStore(path).read_json()
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return _state_from_dict(data)


def _write_session_state(path: Path, data: dict[str, Any]) -> None:
    """Atomically replace a session state file in its own directory."""
    SessionStore(path).write_json(data)


def _state_to_dict(state: SessionContextState) -> dict[str, Any]:
    return {
        "session_id": state.session_id,
        "timestamp": state.timestamp,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "baseline_tokens": state.baseline_tokens,
        "last_tokens": state.last_tokens,
        "last_ratio": state.last_ratio,
        "model": state.model,
        "swept_at": state.swept_at,
    }


def record_session_measurement(
    anima_dir: Path,
    session_type: str = SESSION_TYPE_CHAT,
    thread_id: str = "default",
    *,
    tokens: int,
    ratio: float,
    model: str = "",
    session_id: str | None = None,
    baseline_tokens: int = 0,
) -> SessionContextState:
    """Persist a context measurement without reseeding its baseline."""
    path = _session_state_path(anima_dir, session_type, thread_id)
    now = _now_iso()
    with _session_state_lock:
        existing = load_session_state(anima_dir, session_type, thread_id)
        if existing is None:
            sid = session_id or ""
            state = SessionContextState(
                session_id=sid,
                timestamp=now,
                created_at=now,
                updated_at=now,
                baseline_tokens=max(0, baseline_tokens or tokens),
                last_tokens=max(0, tokens),
                last_ratio=ratio,
                model=model,
            )
        else:
            state = SessionContextState(
                session_id=session_id or existing.session_id,
                timestamp=now,
                created_at=existing.created_at,
                updated_at=now,
                baseline_tokens=existing.baseline_tokens or max(0, baseline_tokens or tokens),
                last_tokens=max(0, tokens),
                last_ratio=ratio,
                model=model or existing.model,
                swept_at=existing.swept_at,
            )
        _write_session_state(path, _state_to_dict(state))
    return state


from core.execution.session_context import _resolve_session_type  # noqa: F401 (re-exported to agent_sdk)


def _session_file(session_type: str, thread_id: str = "default") -> str:
    """Return the session file name for the given session type."""
    return SessionStore.sdk_state_filename(session_type, thread_id)


def _load_session_id(
    anima_dir: Path,
    session_type: str = SESSION_TYPE_CHAT,
    thread_id: str = "default",
) -> str | None:
    """Load persisted session ID through the unified state reader."""
    state = load_session_state(anima_dir, session_type, thread_id)
    if state is None:
        return None
    try:
        saved = datetime.fromisoformat(state.timestamp)
        if saved.tzinfo is None:
            saved = saved.replace(tzinfo=UTC)
        elapsed_min = (datetime.now(UTC) - saved).total_seconds() / 60
        logger.debug(
            "Session loaded (%s/%s, thread=%s): age %.1f min",
            session_type,
            anima_dir.name,
            thread_id,
            elapsed_min,
        )
    except ValueError:
        pass
    return state.session_id or None


def _save_session_id(
    anima_dir: Path,
    session_id: str,
    session_type: str = SESSION_TYPE_CHAT,
    thread_id: str = "default",
) -> None:
    """Persist a session ID while retaining all measurement metadata."""
    path = _session_state_path(anima_dir, session_type, thread_id)
    now = _now_iso()
    with _session_state_lock:
        existing = load_session_state(anima_dir, session_type, thread_id)
        if existing is None:
            state = SessionContextState(
                session_id=session_id,
                timestamp=now,
                created_at=now,
                updated_at=now,
            )
        else:
            state = SessionContextState(
                session_id=session_id,
                timestamp=now,
                created_at=existing.created_at,
                updated_at=existing.updated_at,
                baseline_tokens=existing.baseline_tokens,
                last_tokens=existing.last_tokens,
                last_ratio=existing.last_ratio,
                model=existing.model,
                swept_at=existing.swept_at,
            )
        _write_session_state(path, _state_to_dict(state))


def mark_session_swept(
    anima_dir: Path,
    session_type: str = SESSION_TYPE_CHAT,
    thread_id: str = "default",
) -> None:
    """Record that the idle sweep already handled this measurement.

    Only Mode S compaction deletes the state file. Every other mode leaves
    it in place, so without this marker a stale session would be compacted
    again on every sweep tick. Marking ``swept_at`` with the current
    ``updated_at`` makes the sweep wait for fresh activity.
    """
    path = _session_state_path(anima_dir, session_type, thread_id)
    with _session_state_lock:
        existing = load_session_state(anima_dir, session_type, thread_id)
        if existing is None:
            return
        state = SessionContextState(
            session_id=existing.session_id,
            timestamp=existing.timestamp,
            created_at=existing.created_at,
            updated_at=existing.updated_at,
            baseline_tokens=existing.baseline_tokens,
            last_tokens=existing.last_tokens,
            last_ratio=existing.last_ratio,
            model=existing.model,
            swept_at=existing.updated_at,
        )
        _write_session_state(path, _state_to_dict(state))


def _clear_session_id(anima_dir: Path, session_type: str = "chat", thread_id: str = "default") -> None:
    """Clear persisted session ID (e.g., after resume failure)."""
    path = _session_state_path(anima_dir, session_type, thread_id)
    with _session_state_lock:
        if path.exists():
            logger.debug(
                "Clearing session ID (%s/%s, thread=%s)",
                session_type,
                anima_dir.name,
                thread_id,
            )
            SessionStore(path).clear()


def clear_session_id_for_type(anima_dir: Path, session_type: str, thread_id: str = "default") -> None:
    """Clear a resolved SDK session ID namespace."""
    _clear_session_id(anima_dir, session_type, thread_id)


# ── SDK query input construction ─────────────────────────────


async def _image_prompt_messages(
    prompt: str,
    images: list[ImageData],
    documents: list[dict[str, Any]] | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Yield a single SDK user message with document/image content blocks.

    The Agent SDK ``query()`` accepts ``str | AsyncIterable[dict]``.
    When images or PDF documents are present we build an Anthropic-format
    multimodal content block list (documents first, then images, then the
    text) and wrap it in the SDK's message envelope.
    """
    content_blocks: list[dict[str, Any]] = list(documents or [])
    for img in images:
        content_blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img["media_type"],
                    "data": img["data"],
                },
            }
        )
    content_blocks.append({"type": "text", "text": prompt})
    yield {
        "type": "user",
        "message": {"role": "user", "content": content_blocks},
        "parent_tool_use_id": None,
    }


def _build_sdk_query_input(
    prompt: str,
    images: list[ImageData] | None,
) -> str | AsyncGenerator[dict[str, Any], None]:
    """Return the appropriate input for ``ClaudeSDKClient.query()``.

    Text-only prompts are passed as plain strings.  When images or chat PDF
    documents (published for this turn via
    :func:`core.chat_attachments.chat_sdk_documents`) are present, an async
    generator of Anthropic-format message dicts is returned.  Each call
    produces a fresh generator (they are single-use).
    """
    from core.chat_attachments import chat_sdk_document_blocks

    documents = chat_sdk_document_blocks()
    if images or documents:
        return _image_prompt_messages(prompt, images or [], documents)
    return prompt


# ── Tool output cleanup ──────────────────────────────────────


def _cleanup_tool_outputs(anima_dir: Path) -> None:
    """Remove temporary tool output files created during the session."""
    tool_output_dir = anima_dir / "shortterm" / "tool_outputs"
    if tool_output_dir.exists():
        shutil.rmtree(tool_output_dir, ignore_errors=True)
        logger.debug("Cleaned up tool output directory: %s", tool_output_dir)


# ── Idle compaction ───────────────────────────────────────────


async def compact_sdk_session(
    anima_dir: Path,
    session_type: str = "chat",
    thread_id: str = "default",
) -> bool:
    """Send /compact to an idle SDK session to trigger compaction.

    Resumes the session and sends ``/compact`` as the query.  The SDK
    compresses the transcript and the same session_id is preserved for
    future resume.  On failure the session file is **preserved** so
    that the next regular chat can still resume from the old state.

    Returns:
        True if compaction succeeded, False otherwise.
    """
    session_id = _load_session_id(anima_dir, session_type, thread_id)
    if not session_id:
        logger.info("No session to compact for %s/%s/%s", session_type, anima_dir.name, thread_id)
        return False

    logger.info(
        "Starting idle compaction (session=%s, type=%s, thread=%s)",
        session_id,
        session_type,
        thread_id,
    )

    try:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        from core.execution.engines.claude._sdk_options import _resolve_sdk_cli_path

        _cli = _resolve_sdk_cli_path()
        options = ClaudeAgentOptions(
            system_prompt=f"{anima_dir.name} session compaction",
            **{"max_turns": None},
            resume=session_id,
            **({"cli_path": _cli} if _cli else {}),
        )

        found_session_id = False
        async with asyncio.timeout(COMPACT_TIMEOUT_SEC):
            async with ClaudeSDKClient(options=options) as client:
                await client.query("/compact")
                async for message in client.receive_messages():
                    if hasattr(message, "session_id") and message.session_id:
                        _save_session_id(anima_dir, message.session_id, session_type, thread_id)
                        logger.info(
                            "Idle compaction completed (session=%s, type=%s, thread=%s)",
                            message.session_id,
                            session_type,
                            thread_id,
                        )
                        found_session_id = True

        if not found_session_id:
            logger.warning(
                "Idle compaction did not receive session_id for %s/%s/%s",
                anima_dir.name,
                session_type,
                thread_id,
            )
        return found_session_id
    except ImportError:
        logger.info("Agent SDK not available; skipping /compact")
        return False
    except TimeoutError:
        logger.warning(
            "Idle compaction timed out for %s/%s/%s; session preserved for next resume",
            anima_dir.name,
            session_type,
            thread_id,
        )
        return False
    except Exception:
        logger.error(
            "Idle compaction failed for %s/%s/%s; session preserved for next resume",
            anima_dir.name,
            session_type,
            thread_id,
            exc_info=True,
        )
        return False
