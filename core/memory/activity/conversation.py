from __future__ import annotations

# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
#
# This file is part of AnimaWorks core/server, licensed under Apache-2.0.
# See LICENSE for the full license text.

"""Conversation view mixin for ActivityLogger.

Internal module — import from :mod:`core.memory.activity.logger` instead.
"""

import json
import logging
from datetime import timedelta
from typing import Any

from core.i18n import t
from core.memory.activity.format import EVENT_SETS
from core.memory.activity.models import (
    ActivityEntry,
    time_diff,
)
from core.time_utils import now_local

logger = logging.getLogger("animaworks.activity")


_TOOL_RESULT_TRUNCATE = 1500
_TOOL_INPUT_TRUNCATE = 500


def _source_key_for_entry(entry: ActivityEntry) -> str:
    """Return a compact source key for chat rendering."""
    if entry.type.startswith("heartbeat_"):
        return "heartbeat"
    if entry.type == "cron_executed":
        return "cron"
    if entry.type.startswith("task_exec_"):
        return "task"
    if entry.channel == "inbox":
        return "inbox"
    if entry.channel == "chat":
        return "chat"
    if entry.type == "message_sent":
        return "dm"
    return entry.channel or "chat"


def _is_inbox_raw_entry(raw: dict[str, Any]) -> bool:
    meta = raw.get("meta", {})
    trigger = meta.get("trigger", "") if isinstance(meta, dict) else ""
    session_type = meta.get("session_type", "") if isinstance(meta, dict) else ""
    return (
        raw.get("channel") == "inbox"
        or session_type == "inbox"
        or trigger == "inbox"
        or (isinstance(trigger, str) and trigger.startswith("inbox:"))
    )


_MAX_HISTORY_ATTACHMENTS = 20
_HISTORY_ATTACHMENT_KEYS = ("path", "name", "media_type", "size", "pages")


def _history_attachments(meta: Any) -> list[dict[str, Any]]:
    """Return sanitised chat attachments recorded on a ``message_received``.

    Only ``attachments/<file>`` paths are passed through, with a fixed set
    of display keys, so the history API never exposes other fields.
    """
    raw = meta.get("attachments") if isinstance(meta, dict) else None
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for item in raw[:_MAX_HISTORY_ATTACHMENTS]:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not isinstance(path, str) or not path.startswith("attachments/") or "/" in path[len("attachments/") :]:
            continue
        result.append({key: item[key] for key in _HISTORY_ATTACHMENT_KEYS if key in item})
    return result


def _truncate_tool_field(value: Any, limit: int) -> Any:
    """Truncate tool input/result for the conversation view API."""
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        return value[:limit] + f"\n…({len(value) - limit} chars truncated)"
    if isinstance(value, (dict, list)):
        s = json.dumps(value, ensure_ascii=False)
        if len(s) <= limit:
            return value
        return s[:limit] + f"\n…({len(s) - limit} chars truncated)"
    return value


class ConversationMixin:
    """Mixin providing conversation view methods for ActivityLogger."""

    _CONVERSATION_TYPES = EVENT_SETS["chat"]

    def get_conversation_view(
        self,
        *,
        before: str | None = None,
        limit: int = 50,
        session_gap_minutes: int = 10,
        thread_id: str | None = None,
        strict_thread: bool = False,
    ) -> dict[str, Any]:
        """Build a conversation view from activity log entries.

        Pairs tool events, groups into sessions (by gap/trigger change),
        returns ``{"sessions": [...], "has_more": bool, "next_before": str}``.
        """
        entries = self._load_conversation_entries(
            before=before,
            limit=limit,
            thread_id=thread_id,
            strict_thread=strict_thread,
        )
        messages = self._entries_to_messages(entries)

        if len(messages) <= limit and entries:
            entries = self._load_conversation_entries(
                before=before,
                limit=limit,
                thread_id=thread_id,
                strict_thread=strict_thread,
                _target_multiplier=15,
            )
            messages = self._entries_to_messages(entries)

        has_more = len(messages) > limit
        if has_more:
            messages = messages[-limit:]

        next_before: str | None = None
        if has_more and messages:
            next_before = messages[0]["ts"]

        sessions = self._group_into_sessions(messages, session_gap_minutes)

        return {
            "sessions": sessions,
            "has_more": has_more,
            "next_before": next_before,
        }

    def _load_conversation_entries(
        self,
        *,
        before: str | None = None,
        limit: int = 50,
        thread_id: str | None = None,
        strict_thread: bool = False,
        _target_multiplier: int = 3,
    ) -> list[ActivityEntry]:
        """Load conversation-relevant entries, scanning enough days for *limit* messages."""
        target_raw = limit * _target_multiplier + 50
        entries: list[ActivityEntry] = []
        today = now_local().date()
        max_scan_days = 365
        # Thread filters may match few entries and never reach target_raw;
        # without a file cap the scan reads (and JSON-parses) every log file
        # ever written, blocking for seconds per request. Named threads have
        # a conversation-file fallback for older history.
        max_files_scanned = 30
        files_scanned = 0

        for day_offset in range(max_scan_days):
            target = today - timedelta(days=day_offset)
            path = self._log_dir / f"{target.isoformat()}.jsonl"  # type: ignore[attr-defined]
            if not path.exists():
                if day_offset > 30 and not entries:
                    break
                continue

            day_entries: list[ActivityEntry] = []
            try:
                for line_num, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(),
                    start=1,
                ):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if raw.get("type") not in self._CONVERSATION_TYPES:
                        continue
                    is_inbox_entry = _is_inbox_raw_entry(raw)
                    # call_human notify/reply stay on the default thread even when
                    # the activity row was written during inbox/heartbeat handling.
                    if (
                        thread_id != "inbox"
                        and is_inbox_entry
                        and raw.get("type") not in ("human_notify", "human_reply")
                    ):
                        continue
                    ts = raw.get("ts", "")
                    if before and ts >= before:
                        continue
                    if thread_id is not None:
                        meta = raw.get("meta", {})
                        has_thread_id = isinstance(meta, dict) and "thread_id" in meta
                        if strict_thread:
                            if not has_thread_id:
                                continue
                            if meta.get("thread_id") != thread_id:
                                continue
                        else:
                            default_tid = "inbox" if is_inbox_entry and thread_id == "inbox" else "default"
                            entry_tid = meta.get("thread_id", default_tid) if isinstance(meta, dict) else default_tid
                            if entry_tid != thread_id:
                                continue
                    if "from" in raw:
                        raw["from_person"] = raw.pop("from")
                    if "to" in raw:
                        raw["to_person"] = raw.pop("to")
                    try:
                        entry = ActivityEntry(
                            **{k: v for k, v in raw.items() if k in ActivityEntry.__dataclass_fields__}
                        )
                    except (TypeError, ValueError, KeyError):
                        logger.debug("Skipping malformed entry at line %d in %s", line_num, path)
                        continue
                    entry._line_number = line_num
                    day_entries.append(entry)
            except Exception:
                logger.exception("Failed to read activity log %s", path)

            entries = day_entries + entries
            files_scanned += 1
            if len(entries) >= target_raw or files_scanned >= max_files_scanned:
                break

        return entries

    def _entries_to_messages(
        self,
        entries: list[ActivityEntry],
    ) -> list[dict[str, Any]]:
        """Convert raw activity entries to conversation messages (pair + nest tools)."""
        from core.memory.activity.format import pair_tool_events

        result_by_use_id: dict[int, ActivityEntry] = {
            id(ex.tool_use): ex.result for ex in pair_tool_events(entries) if ex.result is not None
        }

        messages: list[dict[str, Any]] = []
        pending_tool_calls: list[dict[str, Any]] = []

        for e in entries:
            if e.type == "message_received":
                self._flush_tool_calls(messages, pending_tool_calls)
                human_msg: dict[str, Any] = {
                    "ts": e.ts,
                    "role": "human",
                    "content": e.content,
                    "from_person": e.from_person,
                    "to_person": e.to_person,
                    "source_key": _source_key_for_entry(e),
                    "tool_calls": [],
                }
                attachments = _history_attachments(e.meta)
                if attachments:
                    human_msg["attachments"] = attachments
                messages.append(human_msg)

            elif e.type == "message_sent":
                self._flush_tool_calls(messages, pending_tool_calls)
                messages.append(
                    {
                        "ts": e.ts,
                        "role": "assistant",
                        "content": e.content,
                        "from_person": "",
                        "to_person": e.to_person,
                        "source_key": _source_key_for_entry(e),
                        "tool_calls": [],
                    }
                )

            elif e.type == "response_sent":
                msg = {
                    "ts": e.ts,
                    "role": "assistant",
                    "content": e.content,
                    "from_person": "",
                    "to_person": e.to_person,
                    "source_key": _source_key_for_entry(e),
                    "tool_calls": [],
                }
                if e.meta.get("thinking_text"):
                    msg["thinking_text"] = e.meta["thinking_text"]
                images = e.meta.get("images") or e.meta.get("artifacts") or []
                if isinstance(images, list) and images:
                    msg["images"] = images
                messages.append(msg)
                if pending_tool_calls:
                    msg["tool_calls"].extend(pending_tool_calls)
                    pending_tool_calls.clear()

            elif e.type == "tool_use":
                # human_notify card is the canonical display for call_human.
                if e.tool == "call_human":
                    continue
                tid = e.meta.get("tool_use_id", "")
                result_entry = result_by_use_id.get(id(e))
                raw_input = e.meta.get("args", e.content)
                raw_result = result_entry.content if result_entry else ""
                tc: dict[str, Any] = {
                    "tool_use_id": tid,
                    "tool_name": e.tool,
                    "input": _truncate_tool_field(raw_input, _TOOL_INPUT_TRUNCATE),
                    "result": _truncate_tool_field(raw_result, _TOOL_RESULT_TRUNCATE),
                    "is_error": (result_entry.meta.get("is_error", False) if result_entry else False),
                }
                if e.meta.get("blocked"):
                    tc["is_error"] = True
                    tc["result"] = t("activity.blocked", reason=e.meta.get("reason", ""))
                pending_tool_calls.append(tc)

            elif e.type == "tool_result":
                pass

            elif e.type == "human_notify":
                self._flush_tool_calls(messages, pending_tool_calls)
                messages.append(
                    {
                        "ts": e.ts,
                        "role": "system",
                        "type": "human_notify",
                        "content": e.content,
                        "subject": e.meta.get("subject", ""),
                        "priority": e.meta.get("priority", "normal"),
                        "callback_id": e.meta.get("callback_id", ""),
                        "from_person": "",
                        "source_key": "call_human",
                        "tool_calls": [],
                    }
                )

            elif e.type == "human_reply":
                self._flush_tool_calls(messages, pending_tool_calls)
                messages.append(
                    {
                        "ts": e.ts,
                        "role": "human",
                        "type": "human_reply",
                        "content": e.content,
                        "from_person": e.from_person,
                        "via": e.via,
                        "source_key": "call_human_reply",
                        "tool_calls": [],
                    }
                )

            elif e.type == "heartbeat_start":
                self._flush_tool_calls(messages, pending_tool_calls)
                messages.append(
                    {
                        "ts": e.ts,
                        "role": "system",
                        "content": e.summary or t("activity.heartbeat_start"),
                        "from_person": "",
                        "source_key": _source_key_for_entry(e),
                        "tool_calls": [],
                        "_trigger": "heartbeat",
                    }
                )

            elif e.type == "heartbeat_end":
                self._flush_tool_calls(messages, pending_tool_calls)
                messages.append(
                    {
                        "ts": e.ts,
                        "role": "system",
                        "content": e.summary or e.content or t("activity.heartbeat_end"),
                        "from_person": "",
                        "source_key": _source_key_for_entry(e),
                        "tool_calls": [],
                        "_trigger": "heartbeat",
                    }
                )

            elif e.type == "cron_executed":
                self._flush_tool_calls(messages, pending_tool_calls)
                task_name = e.meta.get("task_name", "")
                content = e.summary or e.content or task_name or t("activity.cron_task_exec")
                messages.append(
                    {
                        "ts": e.ts,
                        "role": "system",
                        "content": content,
                        "from_person": "",
                        "source_key": _source_key_for_entry(e),
                        "tool_calls": [],
                        "_trigger": "cron",
                    }
                )

            elif e.type == "task_exec_start":
                self._flush_tool_calls(messages, pending_tool_calls)
                messages.append(
                    {
                        "ts": e.ts,
                        "role": "system",
                        "content": e.summary or t("activity.task_exec_start_label"),
                        "from_person": "",
                        "source_key": _source_key_for_entry(e),
                        "tool_calls": [],
                        "_trigger": "task",
                    }
                )

            elif e.type == "task_exec_end":
                self._flush_tool_calls(messages, pending_tool_calls)
                messages.append(
                    {
                        "ts": e.ts,
                        "role": "system",
                        "content": e.summary or e.content or t("activity.task_exec_end_label"),
                        "from_person": "",
                        "source_key": _source_key_for_entry(e),
                        "tool_calls": [],
                        "_trigger": "task",
                    }
                )

            elif e.type == "error":
                self._flush_tool_calls(messages, pending_tool_calls)
                messages.append(
                    {
                        "ts": e.ts,
                        "role": "system",
                        "content": t("activity.error_prefix") + (e.content or e.summary or ""),
                        "from_person": "",
                        "source_key": _source_key_for_entry(e),
                        "tool_calls": [],
                    }
                )

        self._flush_tool_calls(messages, pending_tool_calls)

        return messages

    @staticmethod
    def _flush_tool_calls(
        messages: list[dict[str, Any]],
        pending: list[dict[str, Any]],
    ) -> None:
        """Attach pending tool calls to the last assistant message."""
        if not pending:
            return
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                msg["tool_calls"].extend(pending)
                pending.clear()
                return
        pending.clear()

    @staticmethod
    def _group_into_sessions(
        messages: list[dict[str, Any]],
        gap_minutes: int,
    ) -> list[dict[str, Any]]:
        """Group messages into sessions by time gaps and trigger changes."""
        if not messages:
            return []

        gap_seconds = gap_minutes * 60
        sessions: list[dict[str, Any]] = []
        current_msgs: list[dict[str, Any]] = []
        current_trigger = "chat"

        for msg in messages:
            msg_trigger = msg.pop("_trigger", None)
            effective_trigger = msg_trigger or "chat"

            if current_msgs:
                prev_ts = current_msgs[-1]["ts"]
                trigger_changed = effective_trigger != current_trigger
                time_gap = time_diff(prev_ts, msg["ts"]) >= gap_seconds

                if trigger_changed or time_gap:
                    sessions.append(
                        {
                            "session_start": current_msgs[0]["ts"],
                            "session_end": current_msgs[-1]["ts"],
                            "trigger": current_trigger,
                            "messages": current_msgs,
                        }
                    )
                    current_msgs = []

            current_trigger = effective_trigger
            current_msgs.append(msg)

        if current_msgs:
            sessions.append(
                {
                    "session_start": current_msgs[0]["ts"],
                    "session_end": current_msgs[-1]["ts"],
                    "trigger": current_trigger,
                    "messages": current_msgs,
                }
            )

        return sessions
