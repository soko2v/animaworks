# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
#
# This file is part of AnimaWorks core/server, licensed under Apache-2.0.
# See LICENSE for the full license text.

"""Per-Anima × per-thread_id idle compaction timer management.

Manages compaction timers using asyncio.Handle (via loop.call_later).
When a session goes idle for ``idle_minutes``, mode-specific compaction
runs (SDK compact, conversation compress, shortterm save, etc.).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.execution.engine_session import clear_engine_session
from core.execution.session_store import SessionEngine

if TYPE_CHECKING:
    from core.anima.digital_anima import DigitalAnima
    from core.memory.activity.models import ActivityEntry

logger = logging.getLogger("animaworks.session_compactor")

# ── Activity-log extraction constants ─────────────────────
_MAX_CONVERSATION_ROUNDS = 3
_MAX_TOOL_ENTRIES = 10
_TOOL_INPUT_TRUNCATE = 500
_TOOL_RESULT_TRUNCATE = 500
_SCAN_DAYS = 2
_TOOL_EVENT_TYPES = frozenset({"tool_use", "tool_result"})
# Activity ``ctx`` labels a chat-session tool may carry ("" = mode without ctx).
_CHAT_TOOL_CONTEXTS = frozenset({"chat", ""})

# LRU limit for _timers (same as conversation_locks).
_MAX_TIMERS = 20
_SWEEP_INTERVAL_SEC = 60.0


def _already_swept(swept_at: str, updated: datetime) -> bool:
    """True when the sweep already handled this exact measurement."""
    if not swept_at:
        return False
    try:
        marked = datetime.fromisoformat(swept_at)
    except ValueError:
        return False
    if marked.tzinfo is None:
        marked = marked.replace(tzinfo=UTC)
    return marked >= updated


# ── SessionCompactor ──────────────────────────────────────────


class SessionCompactor:
    """Anima別 × thread_id別のアイドルコンパクションタイマー管理。"""

    def __init__(self, idle_minutes: float = 10.0) -> None:
        """Initialize compactor with idle threshold in minutes.

        Args:
            idle_minutes: Minutes of inactivity before compaction runs.
        """
        self._idle_minutes = idle_minutes
        # key: (anima_name, thread_id) → asyncio.Handle
        self._timers: dict[tuple[str, str], asyncio.Handle] = {}
        self._sweep_task: asyncio.Task[None] | None = None
        self._sweep_anima: DigitalAnima | None = None

    def schedule(
        self,
        anima_name: str,
        thread_id: str,
        callback: Callable[[], None],
    ) -> None:
        """Schedule a compaction timer (cancel existing if any, then reschedule).

        Args:
            anima_name: Anima name.
            thread_id: Thread/conversation ID.
            callback: Sync callable invoked when timer fires (e.g. schedules
                asyncio.create_task(run_idle_compaction(...))).
        """
        key = (anima_name, thread_id)
        if key in self._timers:
            self._timers[key].cancel()
            del self._timers[key]
            logger.info("SessionCompactor rescheduled: %s (%.1f min)", key, self._idle_minutes)
        else:
            logger.info("SessionCompactor scheduled: %s (%.1f min)", key, self._idle_minutes)

        while len(self._timers) >= _MAX_TIMERS:
            oldest = next(iter(self._timers))
            h = self._timers.pop(oldest)
            h.cancel()
            logger.debug("SessionCompactor LRU evict: %s", oldest)

        loop = asyncio.get_running_loop()
        # Detach from the scheduling cycle's context: this timer fires minutes
        # after the cycle ends, so inheriting its cycle_id would misattribute
        # the idle-compaction logs to an unrelated, long-finished cycle. A fresh
        # empty Context makes the fired work log as cycle-less (cycle_id "-").
        handle = loop.call_later(
            self._idle_minutes * 60,
            self._fire,
            key,
            callback,
            context=contextvars.Context(),
        )
        self._timers[key] = handle

    def _fire(self, key: tuple[str, str], callback: Callable[[], None]) -> None:
        """Invoked when timer fires; removes handle and invokes callback."""
        self._timers.pop(key, None)
        logger.info("SessionCompactor timer fired: %s", key)
        try:
            callback()
        except Exception:
            logger.exception("SessionCompactor callback failed for %s", key)

    def cancel(self, anima_name: str, thread_id: str) -> None:
        """Cancel the timer for the given anima and thread."""
        key = (anima_name, thread_id)
        if key in self._timers:
            self._timers[key].cancel()
            del self._timers[key]
            logger.info("SessionCompactor cancelled: %s", key)

    def start(self, anima: DigitalAnima) -> None:
        """Start the persistent sweep that does not depend on call_later."""
        if self._sweep_task is not None and not self._sweep_task.done():
            return
        self._sweep_anima = anima
        self._sweep_task = asyncio.create_task(
            self._sweep_loop(),
            name=f"session-compaction-sweep-{anima.name}",
        )
        logger.info("SessionCompactor sweep started: %s", anima.name)

    async def _sweep_once(self, anima: DigitalAnima | None = None) -> None:
        """Compact every stale persisted chat session found on disk."""
        target = anima or self._sweep_anima
        if target is None:
            return
        from core.execution.engines.claude._sdk_session import load_session_state, mark_session_swept

        state_dir = target.anima_dir / "state"
        prefix = "current_session_chat"
        try:
            paths = tuple(state_dir.glob(f"{prefix}*.json"))
        except OSError:
            logger.exception("SessionCompactor sweep could not list %s", state_dir)
            return
        now = datetime.now(UTC)
        for path in paths:
            stem = path.stem
            if stem == prefix:
                thread_id = "default"
            elif stem.startswith(f"{prefix}_"):
                thread_id = stem[len(prefix) + 1 :]
            else:
                continue
            try:
                state = load_session_state(target.anima_dir, "chat", thread_id)
                if state is None or not state.session_id:
                    continue
                updated = datetime.fromisoformat(state.updated_at)
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=UTC)
                if (now - updated).total_seconds() < self._idle_minutes * 60:
                    continue
                if _already_swept(state.swept_at, updated):
                    continue
                logger.info(
                    "SessionCompactor sweep firing: anima=%s thread=%s idle=%.1f min",
                    target.name,
                    thread_id,
                    (now - updated).total_seconds() / 60,
                )
                await run_idle_compaction(target, thread_id)
                # Only Mode S deletes the state file. For every other mode the
                # file survives with the same ``updated_at``, so without this
                # marker the sweep would recompact it on every tick.
                mark_session_swept(target.anima_dir, "chat", thread_id)
            except Exception:
                logger.exception(
                    "SessionCompactor sweep failed for %s/%s; continuing",
                    target.name,
                    thread_id,
                )

    async def _sweep_loop(self) -> None:
        """Run periodic disk-backed session checks until cancelled."""
        while True:
            try:
                await asyncio.sleep(_SWEEP_INTERVAL_SEC)
                await self._sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("SessionCompactor sweep loop failed; continuing")

    def cancel_all_for_anima(self, anima_name: str) -> None:
        """Cancel all timers for an anima (e.g. on anima stop)."""
        to_remove = [k for k in self._timers if k[0] == anima_name]
        for key in to_remove:
            self._timers[key].cancel()
            del self._timers[key]

    def shutdown(self) -> None:
        """Cancel all timers and the disk-backed sweep."""
        for handle in self._timers.values():
            handle.cancel()
        self._timers.clear()
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            self._sweep_task = None
        self._sweep_anima = None


# ── Activity-log based context extraction ─────────────────────


def _extract_recent_chat_context(
    anima_dir: Path,
    thread_id: str = "default",
) -> dict[str, Any]:
    """Extract recent chat context from the activity_log for idle compaction."""
    from core.memory.activity.format import (
        EVENT_SETS,
        EntryRole,
        clip,
        entry_role,
        iter_entries,
    )
    from core.time_utils import now_local

    now = now_local()

    raw_entries: list[ActivityEntry] = []
    in_turn = False
    for entry in iter_entries(
        anima_dir,
        days=_SCAN_DAYS,
        types=list(EVENT_SETS["compaction"]),
    ):
        meta = entry.meta or {}
        trigger = meta.get("trigger", "")
        if (
            entry.channel == "inbox"
            or meta.get("session_type") == "inbox"
            or trigger == "inbox"
            or (isinstance(trigger, str) and trigger.startswith("inbox:"))
        ):
            continue
        if entry.type in _TOOL_EVENT_TYPES:
            # Tool events carry no thread_id, so cron/heartbeat/task tools would
            # otherwise land in the default chat handover. Keep only chat-context
            # tools emitted while a human turn on this thread was open.
            if not in_turn or entry.ctx not in _CHAT_TOOL_CONTEXTS:
                continue
            if meta.get("thread_id", thread_id) != thread_id:
                continue
            raw_entries.append(entry)
            continue
        if meta.get("thread_id", "default") != thread_id:
            continue
        if entry.type == "message_received":
            if meta.get("from_type", "") != "human":
                continue
            in_turn = True
        elif entry.type == "response_sent":
            in_turn = False
        raw_entries.append(entry)

    if not raw_entries:
        return {}

    turns: list[dict[str, Any]] = []
    user_count = 0
    assistant_count = 0
    tool_count = 0

    for entry in reversed(raw_entries):
        etype = entry.type
        role = entry_role(entry)

        if role == EntryRole.USER and user_count < _MAX_CONVERSATION_ROUNDS:
            turns.append({"role": "user", "content": entry.content})
            user_count += 1
        elif role == EntryRole.ASSISTANT and assistant_count < _MAX_CONVERSATION_ROUNDS:
            content = entry.content or entry.summary
            turns.append({"role": "assistant", "content": content})
            assistant_count += 1
        elif etype in ("tool_use", "tool_result") and tool_count < _MAX_TOOL_ENTRIES:
            meta = entry.meta or {}
            turns.append({"role": etype, "entry": entry, "meta": meta})
            tool_count += 1

        if (
            user_count >= _MAX_CONVERSATION_ROUNDS
            and assistant_count >= _MAX_CONVERSATION_ROUNDS
            and tool_count >= _MAX_TOOL_ENTRIES
        ):
            break

    turns.reverse()

    conversation_parts: list[str] = []
    for t in turns:
        if t["role"] in ("user", "assistant"):
            conversation_parts.append(f"{t['role']}: {t['content']}")

    tool_uses: list[dict[str, Any]] = []
    pending_use: dict[str, Any] | None = None
    for t in turns:
        if t["role"] == "tool_use":
            if pending_use:
                tool_uses.append(pending_use)
            entry = t["entry"]
            meta = t["meta"]
            tool_name = entry.tool or clip(entry.content, 100)
            args = meta.get("args", {})
            pending_use = {
                "name": tool_name,
                "input": str(args)[:_TOOL_INPUT_TRUNCATE] if args else clip(entry.content, _TOOL_INPUT_TRUNCATE),
                "tool_use_id": meta.get("tool_use_id", ""),
            }
        elif t["role"] == "tool_result":
            entry = t["entry"]
            meta = t["meta"]
            result_id = meta.get("tool_use_id", "")
            result_text = clip(entry.content, _TOOL_RESULT_TRUNCATE)
            if pending_use and result_id and result_id == pending_use.get("tool_use_id", ""):
                pending_use["result"] = result_text
                del pending_use["tool_use_id"]
                tool_uses.append(pending_use)
                pending_use = None
            else:
                if pending_use:
                    del pending_use["tool_use_id"]
                    tool_uses.append(pending_use)
                    pending_use = None
                tool_uses.append({"name": "tool_result", "input": "", "result": result_text})
    if pending_use:
        pending_use.pop("tool_use_id", None)
        tool_uses.append(pending_use)

    first_user = ""
    for t in turns:
        if t["role"] == "user":
            first_user = t["content"]
            break

    return {
        "accumulated_response": "\n".join(conversation_parts)[:8000],
        "tool_uses": tool_uses[-_MAX_TOOL_ENTRIES:],
        "original_prompt": first_user[:2000],
        "timestamp": now.isoformat(),
        "trigger": "idle_compaction",
        "notes": "Auto-extracted from activity_log (session discarded)",
    }


# ── Mode-specific compaction ──────────────────────────────────


async def _compact_mode_s_shared(
    anima_dir: Path,
    anima_name: str,
    thread_id: str,
    *,
    trigger: str = "idle_compaction",
    notes: str = "",
) -> bool:
    """Extract context, save shortterm, and discard a Mode S session."""
    from core.execution.engines.claude._sdk_session import SESSION_TYPE_CHAT, _clear_session_id
    from core.memory.conversation.shortterm import SessionState, ShortTermMemory, build_thread_context_note

    logger.debug("_compact_mode_s: entry (anima=%s, thread=%s)", anima_name, thread_id)
    ctx = _extract_recent_chat_context(anima_dir, thread_id=thread_id)
    if ctx.get("accumulated_response") or ctx.get("tool_uses"):
        shortterm = ShortTermMemory(anima_dir, session_type="chat", thread_id=thread_id)
        # The activity-log extract covers only the last few rounds; append the
        # thread's earlier topics so the memo is not just the latest work.
        memo_notes = "\n\n".join(
            part for part in (notes or ctx.get("notes", ""), build_thread_context_note(anima_dir, thread_id)) if part
        )
        shortterm.save(
            SessionState(
                accumulated_response=ctx.get("accumulated_response", ""),
                tool_uses=ctx.get("tool_uses", []),
                original_prompt=ctx.get("original_prompt", ""),
                timestamp=ctx.get("timestamp", ""),
                trigger=trigger,
                notes=memo_notes,
            )
        )
        logger.info("_compact_mode_s: shortterm saved from activity_log")
    _clear_session_id(anima_dir, SESSION_TYPE_CHAT, thread_id)
    logger.info("_compact_mode_s: session_id cleared (anima=%s, thread=%s)", anima_name, thread_id)
    return True


async def _compact_mode_s(anima: DigitalAnima, thread_id: str) -> bool:
    """Mode S: activity_log extraction → shortterm save → session_id clear."""
    return await _compact_mode_s_shared(anima.anima_dir, anima.name, thread_id)


async def _compact_conversation(
    anima: DigitalAnima,
    thread_id: str,
    *,
    clear_engine: SessionEngine | None,
) -> dict[str, Any]:
    """Compress conversation history, save shortterm context, and optionally discard its engine session."""
    from core.memory.conversation.memory import ConversationMemory
    from core.memory.conversation.shortterm import SessionState, ShortTermMemory
    from core.time_utils import now_local

    logger.debug("_compact_conversation: entry (anima=%s, thread=%s)", anima.name, thread_id)
    conv = ConversationMemory(anima.anima_dir, anima.agent.model_config, thread_id=thread_id)
    compression = await conv.compress_if_needed_detailed()

    state = conv.load()
    summary_parts: list[str] = []
    if state.compressed_summary:
        summary_parts.append(state.compressed_summary)
    if state.turns:
        for turn in state.turns[-3:]:
            summary_parts.append(f"{turn.role}: {turn.content[:200]}")

    shortterm_saved = bool(summary_parts)
    if shortterm_saved:
        shortterm = ShortTermMemory(anima.anima_dir, session_type="chat", thread_id=thread_id)
        shortterm.save(
            SessionState(
                accumulated_response="\n".join(summary_parts)[:4000],
                timestamp=now_local().isoformat(),
                trigger="idle_compaction",
                notes="Auto-saved during idle compaction",
            )
        )

    if clear_engine is not None:
        clear_engine_session(anima.anima_dir, clear_engine, "chat", thread_id)
    finalized = await conv.finalize_if_session_ended()
    logger.debug("_compact_conversation: exit (compressed=%s)", compression.performed)
    return {
        "compression_status": compression.status,
        "compression_performed": compression.performed,
        "compression_fallback": compression.fallback_used,
        "raw_turns_before": compression.raw_turns_before,
        "raw_turns_after": compression.raw_turns_after,
        "compressed_turns": compression.compressed_turns,
        "compression_error": compression.error,
        "shortterm_saved": shortterm_saved,
        "engine_session_cleared": clear_engine is not None,
        "engine": clear_engine,
        "finalized": finalized,
    }


# ── Public API ────────────────────────────────────────────────


async def run_idle_compaction(anima: DigitalAnima, thread_id: str) -> bool:
    """Run mode-specific idle compaction for the given anima and thread.

    Acquires the thread lock with a 30-second timeout. If the lock cannot
    be acquired (or compaction otherwise fails), compaction is skipped and
    ``False`` is returned. On success logs an "idle_compaction" activity
    event and returns ``True``.
    """
    mode = anima.agent.execution_mode
    logger.info(
        "run_idle_compaction: start (anima=%s, mode=%s, thread=%s)",
        anima.name,
        mode,
        thread_id,
    )
    lock = anima._get_thread_lock(thread_id)

    try:
        await asyncio.wait_for(lock.acquire(), timeout=30)
    except TimeoutError:
        logger.warning(
            "Idle compaction skipped for %s/%s: lock timeout",
            anima.name,
            thread_id,
        )
        return False

    try:
        try:
            from core.memory.maintenance.background_review import request_background_review

            request_background_review(anima.anima_dir, "auto_compact")
        except Exception:
            logger.warning("Could not queue idle-compaction review for %s", anima.name)
        compaction_meta: dict[str, Any] = {
            "mode": mode,
            "thread_id": thread_id,
            "session_type": "chat",
        }

        def _record_result(result: Any) -> None:
            if isinstance(result, dict):
                compaction_meta.update(result)
            else:
                compaction_meta["compaction_result"] = bool(result)

        if mode == "s":
            ok = await _compact_mode_s(anima, thread_id)
            compaction_meta["mode_s_ok"] = ok
            if not ok:
                logger.info(
                    "Mode S compaction returned False for %s/%s; falling back to Mode A",
                    anima.name,
                    thread_id,
                )
                _record_result(await _compact_conversation(anima, thread_id, clear_engine=None))
        else:
            executor = getattr(getattr(anima, "agent", None), "_executor", None)
            clear_engine = getattr(executor, "session_engine", None)
            if clear_engine not in {"agent_sdk", "codex", "cursor", "grok"}:
                clear_engine = None
            _record_result(await _compact_conversation(anima, thread_id, clear_engine=clear_engine))
    except Exception:
        logger.exception("Idle compaction failed for %s/%s", anima.name, thread_id)
        return False
    finally:
        lock.release()

    logger.info(
        "run_idle_compaction: success (anima=%s, mode=%s, thread=%s)",
        anima.name,
        mode,
        thread_id,
    )
    try:
        from core.memory.activity.logger import ActivityLogger

        activity = ActivityLogger(anima.anima_dir)
        activity.log(
            "idle_compaction",
            summary=f"Idle compaction completed (mode={mode}, thread={thread_id})",
            meta=compaction_meta,
        )
    except Exception:
        logger.warning("Failed to log idle_compaction activity", exc_info=True)
    return True
