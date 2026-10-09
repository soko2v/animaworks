# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
#
# This file is part of AnimaWorks core/server, licensed under Apache-2.0.
# See LICENSE for the full license text.

"""Prompt building functions for conversation memory."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from core.i18n import t
from core.memory.conversation.models import (
    _MAX_DISPLAY_TURNS,
    _MAX_HUMAN_CHARS_IN_HISTORY,
    _MAX_RENDERED_TOOL_RECORDS,
    _MAX_RESPONSE_CHARS_IN_HISTORY,
    ConversationState,
    ConversationTurn,
    _sanitize_tool_id,
)
from core.paths import load_prompt

if TYPE_CHECKING:
    from core.schemas import ModelConfig

# ── Fresh-session handover limits ─────────────────────────────
# Turns rendered in full at the end of a handover; older uncompressed turns
# are condensed into a digest so that none of them silently disappears.
_HANDOVER_DETAIL_TURNS = 6
# Per-line clip lengths (human, assistant) tried in order until the whole
# digest fits its budget; an assistant clip of 0 keeps only the human lines.
_HANDOVER_DIGEST_CLIPS: tuple[tuple[int, int], ...] = (
    (200, 120),
    (140, 80),
    (90, 50),
    (60, 30),
    (40, 0),
)
# The recent turns always get at least this much room.
_HANDOVER_MIN_DETAIL_CHARS = 1500
# Thread-context section of a short-term handover memo.
_HANDOVER_NOTE_SUMMARY_CHARS = 1500
_HANDOVER_NOTE_DIGEST_CHARS = 2000
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def build_chat_prompt(
    state: ConversationState,
    content: str,
    from_person: str = "human",
    max_history_chars: int | None = None,
    model_config: ModelConfig | None = None,
) -> str:
    """Build the user prompt with conversation history injected.

    Args:
        state: The conversation state to render.
        content: The current user message.
        from_person: Sender identifier.
        max_history_chars: If set, truncate the rendered history text
            to at most this many characters (tail-preserving).
        model_config: Optional model config (unused, for API compatibility).
    """
    history_block = _format_history(state, max_chars=max_history_chars)

    if history_block:
        return load_prompt(
            "chat_message_with_history",
            conversation_history=history_block,
            from_person=from_person,
            content=content,
        )
    else:
        return load_prompt(
            "chat_message",
            from_person=from_person,
            content=content,
        )


def build_structured_messages(
    state: ConversationState,
    content: str,
    fmt: str = "openai",
    model_config: ModelConfig | None = None,
) -> list[dict[str, Any]]:
    """Build structured message history for Mode A/Fallback.

    Preserves tool_use/tool_result structure to prevent the LLM
    from learning to describe tool calls in text instead of actually
    calling them.

    Args:
        state: The conversation state to render.
        content: The current user message content.
        fmt: Message format — ``"openai"`` for LiteLLM/OpenAI-style
            or ``"anthropic"`` for native Anthropic API format.
        model_config: Optional model config (unused, for API compatibility).

    Returns:
        List of message dicts ready to pass to the LLM API.
    """
    messages: list[dict[str, Any]] = []

    # Compressed summary as context
    if state.compressed_summary:
        messages.append(
            {
                "role": "user",
                "content": (
                    t("conversation.summary_label", count=state.compressed_turn_count)
                    + f"\n\n{state.compressed_summary}"
                ),
            }
        )
        messages.append(
            {
                "role": "assistant",
                "content": t("conversation.summary_ack"),
            }
        )

    # Track total tool records rendered
    rendered_tool_count = 0
    display_turns = state.turns[-_MAX_DISPLAY_TURNS:]

    # Track pending tool_result blocks for Anthropic format merging
    pending_tool_results: list[dict[str, Any]] = []

    for turn in display_turns:
        if turn.role == "human":
            display = turn.content
            if len(display) > _MAX_HUMAN_CHARS_IN_HISTORY:
                display = display[:_MAX_HUMAN_CHARS_IN_HISTORY] + "..."
            if fmt == "anthropic" and pending_tool_results:
                # Merge tool_result blocks with this user message
                merged_content = pending_tool_results + [{"type": "text", "text": display}]
                messages.append({"role": "user", "content": merged_content})
                pending_tool_results = []
            else:
                messages.append({"role": "user", "content": display})

        elif turn.role == "assistant":
            display = turn.content
            if len(display) > _MAX_RESPONSE_CHARS_IN_HISTORY:
                display = display[:_MAX_RESPONSE_CHARS_IN_HISTORY] + "..."

            has_tools = turn.tool_records and rendered_tool_count < _MAX_RENDERED_TOOL_RECORDS

            if has_tools and fmt == "openai":
                # OpenAI/LiteLLM format
                tool_calls: list[dict[str, Any]] = []
                for tr in turn.tool_records:
                    if rendered_tool_count >= _MAX_RENDERED_TOOL_RECORDS:
                        break
                    tool_calls.append(
                        {
                            "id": _sanitize_tool_id(tr.tool_id or f"hist_{rendered_tool_count}"),
                            "type": "function",
                            "function": {
                                "name": tr.tool_name,
                                "arguments": json.dumps(
                                    {"_summary": tr.input_summary},
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    )
                    rendered_tool_count += 1
                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": tool_calls,
                    }
                )
                # Tool results — match by index
                for i, tc in enumerate(tool_calls):
                    result_text = turn.tool_records[i].result_summary if i < len(turn.tool_records) else ""
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": result_text or "(completed)",
                        }
                    )

            elif has_tools and fmt == "anthropic":
                # Anthropic format
                content_blocks: list[dict[str, Any]] = []
                if display:
                    content_blocks.append({"type": "text", "text": display})
                for tr in turn.tool_records:
                    if rendered_tool_count >= _MAX_RENDERED_TOOL_RECORDS:
                        break
                    tid = _sanitize_tool_id(tr.tool_id or f"hist_{rendered_tool_count}")
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "id": tid,
                            "name": tr.tool_name,
                            "input": {"_summary": tr.input_summary},
                        }
                    )
                    pending_tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tid,
                            "content": tr.result_summary or "(completed)",
                        }
                    )
                    rendered_tool_count += 1
                messages.append({"role": "assistant", "content": content_blocks})

            else:
                # No tool records or budget exceeded — plain text
                messages.append({"role": "assistant", "content": display})

    # Current user message (merge with any pending tool_results)
    if pending_tool_results:
        merged_content = pending_tool_results + [{"type": "text", "text": content}]
        messages.append({"role": "user", "content": merged_content})
    elif fmt == "anthropic" and messages and messages[-1]["role"] == "user":
        # Merge to avoid consecutive user roles (Anthropic API requirement)
        existing = messages[-1]["content"]
        if isinstance(existing, list):
            existing.append({"type": "text", "text": content})
        else:
            messages[-1]["content"] = [
                {"type": "text", "text": existing},
                {"type": "text", "text": content},
            ]
    else:
        messages.append({"role": "user", "content": content})

    return messages


def _format_history(
    state: ConversationState,
    max_chars: int | None = None,
) -> str:
    """Format conversation history for prompt injection.

    Args:
        state: The conversation state to render.
        max_chars: If set, truncate the rendered text to at most this
            many characters, keeping the tail (most recent turns) and
            prepending an ellipsis marker.
    """
    parts: list[str] = []

    if state.compressed_summary:
        parts.append(
            t("conversation.history_summary_header", count=state.compressed_turn_count)
            + f"\n\n{state.compressed_summary}"
        )

    if state.turns:
        display_turns = state.turns[-_MAX_DISPLAY_TURNS:]
        turn_lines: list[str] = []
        for turn in display_turns:
            # Extract time portion for compact display
            ts = turn.timestamp[11:16] if len(turn.timestamp) >= 16 else turn.timestamp
            role_label = t("conversation.role_you") if turn.role == "assistant" else turn.role
            display = turn.content
            if turn.role == "assistant" and len(display) > _MAX_RESPONSE_CHARS_IN_HISTORY:
                display = display[:_MAX_RESPONSE_CHARS_IN_HISTORY] + "..."
            elif turn.role != "assistant" and len(display) > _MAX_HUMAN_CHARS_IN_HISTORY:
                display = display[:_MAX_HUMAN_CHARS_IN_HISTORY] + "..."
            if turn.role == "assistant" and turn.tool_records:
                tool_names = ", ".join(tr.tool_name for tr in turn.tool_records)
                display += "\n" + t("conversation.tools_executed", tool_names=tool_names)
            turn_lines.append(f"**[{ts}] {role_label}:**\n{display}")

        if parts:
            parts.append(t("conversation.recent_conversation_header") + "\n\n" + "\n\n".join(turn_lines))
        else:
            parts.append("\n\n".join(turn_lines))

    result = "\n\n---\n\n".join(parts) if parts else ""

    if max_chars and len(result) > max_chars:
        result = result[-max_chars:]
        result = t("conversation.ellipsis_omitted") + "\n" + result

    return result


def _clip_tail(text: str, limit: int) -> str:
    """Return *text* cut to its last *limit* chars, marking the cut."""
    if len(text) <= limit:
        return text
    return t("conversation.ellipsis_omitted") + "\n" + text[-limit:]


def _digest_line(turn: ConversationTurn, clip: int) -> str:
    """Render *turn* as a single line clipped to *clip* chars."""
    text = " ".join(_HTML_COMMENT_RE.sub(" ", turn.content or "").split())
    if len(text) > clip:
        text = text[:clip] + "..."
    ts = turn.timestamp or ""
    when = f"{ts[5:10]} {ts[11:16]}" if len(ts) >= 16 else ts
    role_label = t("conversation.role_you") if turn.role == "assistant" else turn.role
    return f"- [{when}] {role_label}: {text}"


def _format_turn_digest(turns: list[ConversationTurn], max_chars: int) -> str:
    """Condense *turns* into one clipped line each, oldest first.

    The clip shrinks until every turn fits in *max_chars*; assistant lines go
    before any human line does.  Only a backlog that still does not fit with
    the shortest clip loses its oldest lines.
    """
    if not turns or max_chars <= 0:
        return ""
    lines: list[str] = []
    for human_clip, assistant_clip in _HANDOVER_DIGEST_CLIPS:
        lines = [
            _digest_line(turn, assistant_clip if turn.role == "assistant" else human_clip)
            for turn in turns
            if turn.role != "assistant" or assistant_clip > 0
        ]
        if len("\n".join(lines)) <= max_chars:
            return "\n".join(lines)
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        if used + len(line) + 1 > max_chars:
            break
        kept.append(line)
        used += len(line) + 1
    kept.reverse()
    return t("conversation.ellipsis_omitted") + "\n" + "\n".join(kept)


def format_handover_history(
    state: ConversationState,
    max_chars: int,
    *,
    summary_chars: int,
    digest_chars: int,
    detail_turns: int = _HANDOVER_DETAIL_TURNS,
) -> str:
    """Render a thread's history for a fresh session that cannot resume.

    ``_format_history`` keeps only the tail when it truncates, so on a long
    thread the compressed summary (the thread's main topics and open
    requests) and every turn before the last ``_MAX_DISPLAY_TURNS`` are the
    first things lost, and the fresh session only sees the latest work.
    This renderer keeps all three parts instead:

    1. the compressed summary, tail-kept within *summary_chars* (newer topics
       and open action items are appended at its end),
    2. a one-line digest of every uncompressed turn before the detailed
       window, fitted into *digest_chars*,
    3. the last *detail_turns* turns in full, tail-kept in the rest of
       *max_chars* (never less than ``_HANDOVER_MIN_DETAIL_CHARS``).

    Args:
        state: Conversation state to render (current message already removed).
        max_chars: Overall target length.
        summary_chars: Budget for the compressed summary body.
        digest_chars: Budget for the older-turn digest.
        detail_turns: Number of most recent turns rendered in full; ``0``
            puts every turn in the digest.

    When *detail_turns* > 0 and the plain ``_format_history`` rendering already
    holds every turn within *max_chars*, that rendering is returned unchanged.

    Returns:
        The rendered history, or ``""`` when the state is empty.
    """
    if detail_turns > 0 and len(state.turns) <= _MAX_DISPLAY_TURNS:
        full = _format_history(state)
        if len(full) <= max_chars:
            return full
    separator = "\n\n---\n\n"
    parts: list[str] = []
    summary = (state.compressed_summary or "").strip()
    if summary and summary_chars > 0:
        parts.append(
            t("conversation.history_summary_header", count=state.compressed_turn_count)
            + "\n\n"
            + _clip_tail(summary, summary_chars)
        )
    turns = list(state.turns)
    recent = turns[-detail_turns:] if detail_turns > 0 else []
    digest = _format_turn_digest(turns[: len(turns) - len(recent)], digest_chars)
    if digest:
        parts.append(t("conversation.handover_digest_header") + "\n\n" + digest)
    if recent:
        header = t("conversation.recent_conversation_header") + "\n\n" if parts else ""
        used = sum(len(part) + len(separator) for part in parts) + len(header)
        detail = _format_history(
            ConversationState(turns=recent),
            max_chars=max(max_chars - used, _HANDOVER_MIN_DETAIL_CHARS),
        )
        if detail:
            parts.append(header + detail)
    return separator.join(parts)


def format_thread_context_note(state: ConversationState) -> str:
    """Render the thread's topics and requests for a short-term handover memo.

    A handover memo otherwise only carries the turn that ended the session,
    so the next session would take the latest work for the whole thread.

    Returns:
        A headed section, or ``""`` when the thread has no history.
    """
    body = format_handover_history(
        state,
        _HANDOVER_NOTE_SUMMARY_CHARS + _HANDOVER_NOTE_DIGEST_CHARS,
        summary_chars=_HANDOVER_NOTE_SUMMARY_CHARS,
        digest_chars=_HANDOVER_NOTE_DIGEST_CHARS,
        detail_turns=0,
    )
    if not body:
        return ""
    return t("shortterm.thread_context_header") + "\n" + t("shortterm.thread_context_note") + "\n\n" + body
