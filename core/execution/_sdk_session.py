from __future__ import annotations

# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
#
# This file is part of AnimaWorks core/server, licensed under Apache-2.0.
# See LICENSE for the full license text.


"""Mode S session persistence, SDK input helpers, and cleanup utilities.

Leaf module in the dependency graph — no internal framework imports
(except ``core.schemas``).
"""

import asyncio
import json
import logging
import os
import shutil
import sys
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.execution.session_types import resolve_runtime_session_type
from core.schemas import ImageData

logger = logging.getLogger("animaworks.execution.agent_sdk")


# ── SDK constants ────────────────────────────────────────────

# Safety margin for Agent SDK JSON-RPC buffer.  The default (1 MB) is too
# small when system_prompt + conversation history grow large; 4 MB gives
# comfortable headroom while still catching genuinely broken messages.
_SDK_MAX_BUFFER_SIZE = 4 * 1024 * 1024  # 4 MB

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
#
# Fix 4a (2026-09-19): the original 15 s guard was too aggressive.  On a
# loaded host (40+ runners) the first stream event of a resumed
# large-context session routinely takes longer than 15 s, and the timeout
# then discarded a perfectly valid session — losing the entire in-session
# conversation memory.  Default raised to 60 s and made configurable via
# the ANIMAWORKS_SDK_RESUME_TIMEOUT_SEC environment variable.
_RESUME_TIMEOUT_ENV = "ANIMAWORKS_SDK_RESUME_TIMEOUT_SEC"
_RESUME_TIMEOUT_DEFAULT_SEC = 60.0


def _resume_timeout_from_env() -> float:
    """Resolve the SDK resume first-event timeout (seconds) from the env."""
    raw = os.environ.get(_RESUME_TIMEOUT_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
        logger.warning(
            "Invalid %s=%r; using default %.0fs",
            _RESUME_TIMEOUT_ENV,
            raw,
            _RESUME_TIMEOUT_DEFAULT_SEC,
        )
    return _RESUME_TIMEOUT_DEFAULT_SEC


RESUME_TIMEOUT_SEC = _resume_timeout_from_env()

# Fix 4c (2026-09-19): number of resume attempts before the session id is
# discarded.  A first-event timeout does not invalidate the session file in
# ~/.claude, so one retry absorbs transient host-load spikes instead of
# irreversibly dropping the conversation.
RESUME_MAX_ATTEMPTS = 2

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

_RESUMABLE_SESSION_TYPES: frozenset[str] = frozenset({SESSION_TYPE_CHAT})
"""Only these session types persist and resume SDK sessions.
All other types start fresh each time (no resume, no save)."""


def _resolve_session_type(trigger: str) -> str:
    """Resolve SDK session type from execution trigger.

    Each trigger category gets its own session namespace to prevent
    cross-contamination of Claude CLI conversation histories.
    """
    return resolve_runtime_session_type(trigger)


def _session_file(session_type: str, thread_id: str = "default") -> str:
    """Return the session file name for the given session type."""
    if thread_id != "default":
        return f"current_session_{session_type}_{thread_id}.json"
    return f"current_session_{session_type}.json"


def _load_session_id(
    anima_dir: Path,
    session_type: str = "chat",
    thread_id: str = "default",
) -> str | None:
    """Load persisted session ID for SDK session resume.

    Sessions are immortal — no TTL.  The timestamp is retained for
    debug logging only and never used for expiry decisions.
    """
    path = anima_dir / "state" / _session_file(session_type, thread_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        session_id = data.get("session_id")
        if not session_id:
            return None

        ts_str = data.get("timestamp")
        if ts_str:
            saved = datetime.fromisoformat(ts_str)
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

        return session_id
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        logger.warning("Failed to load session ID from %s: %s", path, exc)
        return None


def _save_session_id(anima_dir: Path, session_id: str, session_type: str = "chat", thread_id: str = "default") -> None:
    """Persist session ID for future SDK session resume."""
    path = anima_dir / "state" / _session_file(session_type, thread_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "timestamp": datetime.now(UTC).isoformat(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _clear_session_id(anima_dir: Path, session_type: str = "chat", thread_id: str = "default") -> None:
    """Clear persisted session ID (e.g., after resume failure)."""
    path = anima_dir / "state" / _session_file(session_type, thread_id)
    if path.exists():
        logger.debug(
            "Clearing session ID (%s/%s, thread=%s)",
            session_type,
            anima_dir.name,
            thread_id,
        )
        path.unlink(missing_ok=True)


def clear_session_id_for_type(anima_dir: Path, session_type: str, thread_id: str = "default") -> None:
    """Clear a resolved SDK session ID namespace."""
    _clear_session_id(anima_dir, session_type, thread_id)


# ── SDK query input construction ─────────────────────────────


async def _image_prompt_messages(
    prompt: str,
    images: list[ImageData],
) -> AsyncGenerator[dict[str, Any], None]:
    """Yield a single SDK user message with image content blocks.

    The Agent SDK ``query()`` accepts ``str | AsyncIterable[dict]``.
    When images are present we build an Anthropic-format multimodal
    content block list and wrap it in the SDK's message envelope.
    """
    content_blocks: list[dict[str, Any]] = []
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

    Text-only prompts are passed as plain strings.  When images are
    present, an async generator of Anthropic-format message dicts is
    returned.  Each call produces a fresh generator (they are single-use).
    """
    if images:
        return _image_prompt_messages(prompt, images)
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

        from core.execution._sdk_options import _resolve_sdk_cli_path

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
