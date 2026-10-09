"""Tests for agent_sdk.py resume timeout guard and session type constants.

Covers:
  - RESUME_TIMEOUT_SEC constant is defined
  - asyncio.wait_for is used when resuming (session_id_to_resume is set)
  - TimeoutError causes _clear_session_id to be called and fallback to fresh session
  - _RESUMABLE_SESSION_TYPES only contains chat
  - _resolve_session_type() maps triggers correctly
  - _clear_session_id() file deletion logic
  - _load_session_id() / _save_session_id() persistence (immortal sessions — no TTL)
"""
# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import contextmanager
from datetime import UTC
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from core.schemas import ModelConfig

# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def model_config() -> ModelConfig:
    return ModelConfig(
        model="claude-sonnet-4-6",
        api_key="sk-test",
        context_threshold=0.50,
    )


@pytest.fixture
def anima_dir(tmp_path: Path) -> Path:
    d = tmp_path / "animas" / "test"
    d.mkdir(parents=True)
    (d / "state").mkdir(parents=True)
    return d


# ── Session resume timeout ────────────────────────────────────


class TestSessionImmortal:
    """_load_session_id() returns session ID regardless of age (no TTL)."""

    def test_recent_session_returns_id(self, anima_dir: Path) -> None:
        from core.execution.engines.claude._sdk_session import _load_session_id, _save_session_id

        _save_session_id(anima_dir, "sess-recent", "chat")
        assert _load_session_id(anima_dir, "chat") == "sess-recent"

    def test_old_session_still_returns_id(self, anima_dir: Path) -> None:
        """Sessions are immortal — old sessions are always returned."""
        from core.execution.engines.claude._sdk_session import _load_session_id

        path = anima_dir / "state" / "current_session_chat.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timedelta

        old_ts = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        path.write_text(
            json.dumps({"session_id": "sess-old", "timestamp": old_ts}),
            encoding="utf-8",
        )
        assert _load_session_id(anima_dir, "chat") == "sess-old"

    def test_very_old_session_still_returns_id(self, anima_dir: Path) -> None:
        """Even week-old sessions are returned (TTL was removed)."""
        from core.execution.engines.claude._sdk_session import _load_session_id

        path = anima_dir / "state" / "current_session_chat.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timedelta

        old_ts = (datetime.now(UTC) - timedelta(days=7)).isoformat()
        path.write_text(
            json.dumps({"session_id": "sess-week-old", "timestamp": old_ts}),
            encoding="utf-8",
        )
        assert _load_session_id(anima_dir, "chat") == "sess-week-old"

    def test_naive_timestamp_handled_gracefully(self, anima_dir: Path) -> None:
        """Legacy files without timezone info should still return session ID."""
        from core.execution.engines.claude._sdk_session import _load_session_id

        path = anima_dir / "state" / "current_session_chat.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timedelta

        old_ts = datetime.now(UTC) - timedelta(hours=2)
        path.write_text(
            json.dumps(
                {
                    "session_id": "sess-naive",
                    "timestamp": old_ts.replace(tzinfo=None).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        assert _load_session_id(anima_dir, "chat") == "sess-naive"

    def test_missing_timestamp_still_returns_id(self, anima_dir: Path) -> None:
        """Files without timestamp field (edge case) resume unconditionally."""
        from core.execution.engines.claude._sdk_session import _load_session_id

        path = anima_dir / "state" / "current_session_chat.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"session_id": "sess-no-ts"}),
            encoding="utf-8",
        )
        assert _load_session_id(anima_dir, "chat") == "sess-no-ts"

    def test_heartbeat_old_session_returns_id(self, anima_dir: Path) -> None:
        """Heartbeat sessions are also immortal."""
        from core.execution.engines.claude._sdk_session import _load_session_id

        path = anima_dir / "state" / "current_session_heartbeat.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timedelta

        old_ts = (datetime.now(UTC) - timedelta(hours=12)).isoformat()
        path.write_text(
            json.dumps({"session_id": "hb-old", "timestamp": old_ts}),
            encoding="utf-8",
        )
        assert _load_session_id(anima_dir, "heartbeat") == "hb-old"


# ── Session persistence helpers ───────────────────────────────


class TestClearSessionId:
    """_clear_session_id() removes the session file."""

    def test_removes_existing_file(self, anima_dir: Path) -> None:
        from core.execution.engines.claude._sdk_session import _clear_session_id, _save_session_id

        _save_session_id(anima_dir, "sess-001", "chat")
        path = anima_dir / "state" / "current_session_chat.json"
        assert path.exists()

        _clear_session_id(anima_dir, "chat")
        assert not path.exists()

    def test_noop_when_no_file(self, anima_dir: Path) -> None:
        from core.execution.engines.claude._sdk_session import _clear_session_id

        # Should not raise
        _clear_session_id(anima_dir, "chat")
        _clear_session_id(anima_dir, "heartbeat")

    def test_clears_heartbeat_session(self, anima_dir: Path) -> None:
        from core.execution.engines.claude._sdk_session import _clear_session_id, _save_session_id

        _save_session_id(anima_dir, "sess-hb", "heartbeat")
        path = anima_dir / "state" / "current_session_heartbeat.json"
        assert path.exists()

        _clear_session_id(anima_dir, "heartbeat")
        assert not path.exists()


class TestSessionTypeConstants:
    """_RESUMABLE_SESSION_TYPES and _resolve_session_type() correctness."""

    def test_only_chat_is_resumable(self) -> None:
        from core.execution.engines.claude._sdk_session import (
            _RESUMABLE_SESSION_TYPES,
            SESSION_TYPE_CHAT,
            SESSION_TYPE_CRON,
            SESSION_TYPE_HEARTBEAT,
            SESSION_TYPE_INBOX,
            SESSION_TYPE_TASK,
        )

        assert SESSION_TYPE_CHAT in _RESUMABLE_SESSION_TYPES
        assert SESSION_TYPE_HEARTBEAT not in _RESUMABLE_SESSION_TYPES
        assert SESSION_TYPE_CRON not in _RESUMABLE_SESSION_TYPES
        assert SESSION_TYPE_TASK not in _RESUMABLE_SESSION_TYPES
        assert SESSION_TYPE_INBOX not in _RESUMABLE_SESSION_TYPES

    def test_resolve_session_type(self) -> None:
        from core.execution.engines.claude._sdk_session import (
            SESSION_TYPE_CHAT,
            SESSION_TYPE_CRON,
            SESSION_TYPE_HEARTBEAT,
            SESSION_TYPE_INBOX,
            SESSION_TYPE_TASK,
            _resolve_session_type,
        )

        assert _resolve_session_type("heartbeat") == SESSION_TYPE_HEARTBEAT
        assert _resolve_session_type("cron:daily") == SESSION_TYPE_CRON
        assert _resolve_session_type("task:abc123") == SESSION_TYPE_TASK
        assert _resolve_session_type("inbox:alice") == SESSION_TYPE_INBOX
        assert _resolve_session_type("chat") == SESSION_TYPE_CHAT
        assert _resolve_session_type("") == SESSION_TYPE_CHAT
        assert _resolve_session_type("unknown") == SESSION_TYPE_TASK

    def test_clear_session_id_for_chat(self, anima_dir: Path) -> None:
        from core.execution.engines.claude._sdk_session import (
            _clear_session_id,
            _save_session_id,
        )

        _save_session_id(anima_dir, "sess-chat", "chat")
        chat_path = anima_dir / "state" / "current_session_chat.json"
        assert chat_path.exists()

        _clear_session_id(anima_dir, "chat")
        assert not chat_path.exists()

    def test_clear_session_id_for_type_clears_non_chat_thread(self, anima_dir: Path) -> None:
        from core.execution.engines.claude._sdk_session import (
            _load_session_id,
            _save_session_id,
            clear_session_id_for_type,
        )

        _save_session_id(anima_dir, "sess-inbox", "inbox", thread_id="inbox")
        assert _load_session_id(anima_dir, "inbox", thread_id="inbox") == "sess-inbox"

        clear_session_id_for_type(anima_dir, "inbox", thread_id="inbox")
        assert _load_session_id(anima_dir, "inbox", thread_id="inbox") is None


class TestResumeTimeoutConstant:
    """RESUME_TIMEOUT_SEC is defined with a reasonable value."""

    def test_resume_timeout_defined(self) -> None:
        from core.execution.engines.claude.agent_sdk import RESUME_TIMEOUT_SEC

        assert RESUME_TIMEOUT_SEC == 15.0

    def test_resume_timeout_is_positive(self) -> None:
        from core.execution.engines.claude.agent_sdk import RESUME_TIMEOUT_SEC

        assert RESUME_TIMEOUT_SEC > 0


class TestNonChatSessionCleanup:
    @pytest.mark.asyncio
    async def test_blocking_inbox_clears_stale_session_and_does_not_save(
        self, model_config: ModelConfig, anima_dir: Path
    ) -> None:
        from core.execution.engines.claude._sdk_session import _load_session_id, _save_session_id
        from core.execution.engines.claude.agent_sdk import AgentSDKExecutor
        from core.prompt.context import ContextTracker
        from tests.helpers.mocks import patch_agent_sdk

        _save_session_id(anima_dir, "stale-inbox-session", "inbox", thread_id="inbox")
        _save_session_id(anima_dir, "chat-session", "chat")

        with patch_agent_sdk(response_text="inbox done"):
            executor = AgentSDKExecutor(model_config=model_config, anima_dir=anima_dir)
            tracker = ContextTracker(model="claude-sonnet-4-6")
            result = await executor.execute(
                prompt="check inbox",
                system_prompt="sys",
                tracker=tracker,
                trigger="inbox:sakura",
                thread_id="inbox",
            )

        assert result.text == "inbox done"
        assert _load_session_id(anima_dir, "inbox", thread_id="inbox") is None
        assert _load_session_id(anima_dir, "chat") == "chat-session"

    @pytest.mark.asyncio
    async def test_streaming_inbox_clears_stale_session_and_does_not_save(
        self, model_config: ModelConfig, anima_dir: Path
    ) -> None:
        from core.execution.engines.claude._sdk_session import _load_session_id, _save_session_id
        from core.execution.engines.claude.agent_sdk import AgentSDKExecutor
        from core.prompt.context import ContextTracker
        from tests.helpers.mocks import MockAssistantMessage, MockResultMessage, MockStreamEvent, MockTextBlock

        _save_session_id(anima_dir, "stale-inbox-session", "inbox", thread_id="inbox")
        _save_session_id(anima_dir, "chat-session", "chat")

        messages = [
            MockStreamEvent(
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "inbox stream"},
                    "index": 0,
                }
            ),
            MockAssistantMessage([MockTextBlock("inbox stream")]),
            MockResultMessage(session_id="new-inbox-session"),
        ]

        with _patch_sdk_for_streaming(messages):
            executor = AgentSDKExecutor(model_config=model_config, anima_dir=anima_dir)
            tracker = ContextTracker(model="claude-sonnet-4-6")
            events = []
            async for event in executor.execute_streaming(
                system_prompt="sys",
                prompt="check inbox",
                tracker=tracker,
                trigger="inbox:sakura",
                thread_id="inbox",
            ):
                events.append(event)

        assert any(e["type"] == "done" for e in events)
        assert _load_session_id(anima_dir, "inbox", thread_id="inbox") is None
        assert _load_session_id(anima_dir, "chat") == "chat-session"


# ── Resume timeout guard in execute_streaming ─────────────────


@contextmanager
def _patch_sdk_for_streaming(messages: list[Any]):
    """Patch claude_agent_sdk for streaming tests with custom message sequence."""
    from tests.helpers.mocks import (
        MockAssistantMessage,
        MockClaudeSDKClient,
        MockResultMessage,
        MockStreamEvent,
        MockSystemMessage,
        MockTextBlock,
        MockToolResultBlock,
        MockUserMessage,
    )

    def _client_factory(**kwargs: Any) -> MockClaudeSDKClient:
        return MockClaudeSDKClient(messages=messages, **kwargs)

    mock_module = MagicMock()
    mock_module.ClaudeSDKClient = _client_factory
    mock_module.AssistantMessage = MockAssistantMessage
    mock_module.ResultMessage = MockResultMessage
    mock_module.TextBlock = MockTextBlock
    mock_module.ToolUseBlock = MagicMock
    mock_module.ToolResultBlock = MockToolResultBlock
    mock_module.UserMessage = MockUserMessage
    mock_module.SystemMessage = MockSystemMessage
    mock_module.ClaudeAgentOptions = MagicMock
    mock_module.HookMatcher = MagicMock
    mock_module.ClaudeSDKError = Exception
    mock_module.ProcessError = Exception

    mock_types = MagicMock()
    mock_types.StreamEvent = MockStreamEvent
    mock_types.HookContext = MagicMock
    mock_types.HookInput = MagicMock
    mock_types.PreToolUseHookSpecificOutput = MagicMock
    mock_types.SyncHookJSONOutput = MagicMock
    mock_module.types = mock_types

    saved: dict[str, Any] = {}
    for key in ["claude_agent_sdk", "claude_agent_sdk.types"]:
        saved[key] = sys.modules.get(key)
        sys.modules[key] = mock_types if key == "claude_agent_sdk.types" else mock_module

    try:
        yield mock_module
    finally:
        for key, val in saved.items():
            if val is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = val


class TestResumeTimeoutGuard:
    """asyncio.wait_for is applied to first-event receive during session resume."""

    @pytest.mark.asyncio
    async def test_wait_for_called_with_resume_timeout_when_resuming(
        self, model_config: ModelConfig, anima_dir: Path
    ) -> None:
        """When a session_id is present, asyncio.wait_for wraps first-event receive."""
        from core.execution.engines.claude.agent_sdk import _save_session_id
        from tests.helpers.mocks import MockAssistantMessage, MockResultMessage, MockStreamEvent, MockTextBlock

        # Persist a session ID so execute_streaming takes the resume path
        _save_session_id(anima_dir, "stale-session-001", "chat")

        messages = [
            MockStreamEvent(
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "hello"},
                    "index": 0,
                }
            ),
            MockAssistantMessage([MockTextBlock("hello")]),
            MockResultMessage(usage={"input_tokens": 100, "output_tokens": 50}),
        ]

        wait_for_calls: list[dict] = []
        original_wait_for = asyncio.wait_for

        async def _spy_wait_for(coro, timeout=None, **kwargs):
            wait_for_calls.append({"timeout": timeout})
            return await original_wait_for(coro, timeout=timeout, **kwargs)

        with _patch_sdk_for_streaming(messages):
            from core.execution.engines.claude.agent_sdk import AgentSDKExecutor
            from core.prompt.context import ContextTracker

            executor = AgentSDKExecutor(model_config=model_config, anima_dir=anima_dir)
            tracker = ContextTracker(model="claude-sonnet-4-6")

            with patch("asyncio.wait_for", side_effect=_spy_wait_for):
                events = []
                async for event in executor.execute_streaming(
                    system_prompt="sys",
                    prompt="test",
                    tracker=tracker,
                ):
                    events.append(event)

        # asyncio.wait_for should have been called with RESUME_TIMEOUT_SEC
        from core.execution.engines.claude.agent_sdk import RESUME_TIMEOUT_SEC

        timeout_values = [c["timeout"] for c in wait_for_calls]
        assert RESUME_TIMEOUT_SEC in timeout_values, (
            f"Expected wait_for to be called with {RESUME_TIMEOUT_SEC}s timeout, but got timeouts: {timeout_values}"
        )

    @pytest.mark.asyncio
    async def test_no_wait_for_when_no_session_to_resume(self, model_config: ModelConfig, anima_dir: Path) -> None:
        """When no session ID exists, asyncio.wait_for is NOT called for resume guard."""
        from tests.helpers.mocks import MockAssistantMessage, MockResultMessage, MockStreamEvent, MockTextBlock

        # No session file → fresh session path (no resume)
        messages = [
            MockStreamEvent(
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "fresh"},
                    "index": 0,
                }
            ),
            MockAssistantMessage([MockTextBlock("fresh")]),
            MockResultMessage(usage={"input_tokens": 50, "output_tokens": 20}),
        ]

        wait_for_calls: list[dict] = []
        original_wait_for = asyncio.wait_for

        async def _spy_wait_for(coro, timeout=None, **kwargs):
            wait_for_calls.append({"timeout": timeout})
            return await original_wait_for(coro, timeout=timeout, **kwargs)

        with _patch_sdk_for_streaming(messages):
            from core.execution.engines.claude.agent_sdk import AgentSDKExecutor
            from core.prompt.context import ContextTracker

            executor = AgentSDKExecutor(model_config=model_config, anima_dir=anima_dir)
            tracker = ContextTracker(model="claude-sonnet-4-6")

            with patch("asyncio.wait_for", side_effect=_spy_wait_for):
                events = []
                async for event in executor.execute_streaming(
                    system_prompt="sys",
                    prompt="test",
                    tracker=tracker,
                ):
                    events.append(event)

        # No wait_for should have been called for resume timeout
        from core.execution.engines.claude.agent_sdk import RESUME_TIMEOUT_SEC

        resume_timeout_calls = [c for c in wait_for_calls if c["timeout"] == RESUME_TIMEOUT_SEC]
        assert resume_timeout_calls == [], (
            f"Expected no wait_for resume guard on fresh session, but got: {resume_timeout_calls}"
        )

    @pytest.mark.asyncio
    async def test_clear_session_id_called_on_resume_timeout(self, model_config: ModelConfig, anima_dir: Path) -> None:
        """When resume times out, _clear_session_id is called and falls back to
        fresh session."""
        from core.execution.engines.claude.agent_sdk import _save_session_id
        from tests.helpers.mocks import MockAssistantMessage, MockResultMessage, MockStreamEvent, MockTextBlock

        _save_session_id(anima_dir, "stale-session-for-timeout", "chat")

        # After timeout, fresh session produces events normally
        fresh_messages = [
            MockStreamEvent(
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "recovered"},
                    "index": 0,
                }
            ),
            MockAssistantMessage([MockTextBlock("recovered")]),
            MockResultMessage(usage={"input_tokens": 100, "output_tokens": 50}),
        ]

        clear_calls: list[str] = []
        original_clear = None

        def _spy_clear(anima_dir_arg, session_type, **kwargs):
            clear_calls.append(session_type)
            if original_clear:
                original_clear(anima_dir_arg, session_type, **kwargs)

        call_count = [0]

        async def _timeout_on_first_then_succeed(coro, timeout=None, **kwargs):
            """Raise TimeoutError on the first call (resume), succeed thereafter."""
            from core.execution.engines.claude.agent_sdk import RESUME_TIMEOUT_SEC

            if timeout == RESUME_TIMEOUT_SEC and call_count[0] == 0:
                call_count[0] += 1
                raise TimeoutError("resume timed out")
            return await coro

        with _patch_sdk_for_streaming(fresh_messages):
            from core.execution.engines.claude._sdk_session import _clear_session_id
            from core.execution.engines.claude.agent_sdk import AgentSDKExecutor
            from core.prompt.context import ContextTracker

            original_clear = _clear_session_id

            executor = AgentSDKExecutor(model_config=model_config, anima_dir=anima_dir)
            tracker = ContextTracker(model="claude-sonnet-4-6")

            with (
                patch("asyncio.wait_for", side_effect=_timeout_on_first_then_succeed),
                patch("core.execution.engines.claude._sdk_session._clear_session_id", side_effect=_spy_clear),
            ):
                events = []
                async for event in executor.execute_streaming(
                    system_prompt="sys",
                    prompt="test",
                    tracker=tracker,
                ):
                    events.append(event)

        # _clear_session_id should have been called for the chat session type
        assert len(clear_calls) >= 1, (
            f"Expected _clear_session_id to be called on resume timeout, but got: {clear_calls}"
        )
        assert "chat" in clear_calls, f"Expected 'chat' session to be cleared, got: {clear_calls}"


# ── _with_resume_fallback_history: shortterm injection ────────


class TestWithResumeFallbackHistoryShortterm:
    """_with_resume_fallback_history injects shortterm accumulated_response for Mode S."""

    def test_returns_prompt_unmodified_for_non_chat_session(self, anima_dir: Path) -> None:
        from core.execution.engines.claude.agent_sdk import _with_resume_fallback_history
        from core.schemas import ModelConfig

        mc = ModelConfig(model="claude-sonnet-4-6", api_key="sk-test")
        result = _with_resume_fallback_history(anima_dir, mc, "hello", "heartbeat", "default")
        assert result == "hello"

    def test_returns_prompt_when_both_conv_and_shortterm_empty(self, anima_dir: Path) -> None:
        from core.execution.engines.claude.agent_sdk import _with_resume_fallback_history
        from core.schemas import ModelConfig

        mc = ModelConfig(model="claude-sonnet-4-6", api_key="sk-test")
        result = _with_resume_fallback_history(anima_dir, mc, "hello", "chat", "default")
        assert result == "hello"

    def test_injects_shortterm_accumulated_response_when_conv_empty(self, anima_dir: Path) -> None:
        """When ConversationMemory is empty (Mode S), shortterm accumulated_response is injected."""
        import json

        from core.execution.engines.claude.agent_sdk import _with_resume_fallback_history
        from core.schemas import ModelConfig

        # Write a shortterm session_state.json with session_id='' (idle_compaction style).
        # thread_id="default" → ShortTermMemory uses shortterm/chat/ (no sub-dir).
        shortterm_dir = anima_dir / "shortterm" / "chat"
        shortterm_dir.mkdir(parents=True, exist_ok=True)
        session_state = {
            "session_id": "",
            "timestamp": "2026-09-28T22:00:00",
            "trigger": "idle_compaction",
            "original_prompt": "URLを教えて",
            "accumulated_response": "URL教えて→http://160.16.231.57/discord/ idとパスワード形式でもいいよ",
            "tool_uses": [],
            "context_usage_ratio": 0.6,
            "turn_count": 2,
            "notes": "",
        }
        (shortterm_dir / "session_state.json").write_text(
            json.dumps(session_state, ensure_ascii=False), encoding="utf-8"
        )

        mc = ModelConfig(model="claude-sonnet-4-6", api_key="sk-test")
        result = _with_resume_fallback_history(anima_dir, mc, "パスワードを確認して", "chat", "default")

        assert "URL教えて" in result
        assert "http://160.16.231.57/discord/" in result
        assert "パスワードを確認して" in result
        assert "前のセッションを再開できなかったため" in result
        assert "最新メッセージ" in result

    def test_shortterm_not_injected_when_session_id_nonempty(self, anima_dir: Path) -> None:
        """shortterm with non-empty session_id is not injected (it's from an active session)."""
        import json

        from core.execution.engines.claude.agent_sdk import _with_resume_fallback_history
        from core.schemas import ModelConfig

        # thread_id="default" → ShortTermMemory uses shortterm/chat/ (no sub-dir)
        shortterm_dir = anima_dir / "shortterm" / "chat"
        shortterm_dir.mkdir(parents=True, exist_ok=True)
        session_state = {
            "session_id": "active-session-abc123",
            "timestamp": "2026-09-28T22:00:00",
            "trigger": "chat",
            "original_prompt": "test",
            "accumulated_response": "this should not be injected",
            "tool_uses": [],
            "context_usage_ratio": 0.3,
            "turn_count": 1,
            "notes": "",
        }
        (shortterm_dir / "session_state.json").write_text(
            json.dumps(session_state, ensure_ascii=False), encoding="utf-8"
        )

        mc = ModelConfig(model="claude-sonnet-4-6", api_key="sk-test")
        result = _with_resume_fallback_history(anima_dir, mc, "new message", "chat", "default")

        # Session with non-empty session_id is not injected
        assert result == "new message"

    def test_shortterm_not_injected_when_accumulated_response_empty(self, anima_dir: Path) -> None:
        """shortterm with empty accumulated_response yields no injection."""
        import json

        from core.execution.engines.claude.agent_sdk import _with_resume_fallback_history
        from core.schemas import ModelConfig

        # thread_id="default" → ShortTermMemory uses shortterm/chat/ (no sub-dir)
        shortterm_dir = anima_dir / "shortterm" / "chat"
        shortterm_dir.mkdir(parents=True, exist_ok=True)
        session_state = {
            "session_id": "",
            "timestamp": "2026-09-28T22:00:00",
            "trigger": "idle_compaction",
            "original_prompt": "",
            "accumulated_response": "",
            "tool_uses": [],
            "context_usage_ratio": 0.0,
            "turn_count": 0,
            "notes": "",
        }
        (shortterm_dir / "session_state.json").write_text(
            json.dumps(session_state, ensure_ascii=False), encoding="utf-8"
        )

        mc = ModelConfig(model="claude-sonnet-4-6", api_key="sk-test")
        result = _with_resume_fallback_history(anima_dir, mc, "hello", "chat", "default")
        assert result == "hello"

    def test_shortterm_injected_for_nondefault_thread_id(self, anima_dir: Path) -> None:
        """shortterm is read from the correct thread-specific subdirectory."""
        import json

        from core.execution.engines.claude.agent_sdk import _with_resume_fallback_history
        from core.schemas import ModelConfig

        thread_id = "meeting-f78d72c334fb"
        shortterm_dir = anima_dir / "shortterm" / "chat" / thread_id
        shortterm_dir.mkdir(parents=True, exist_ok=True)
        session_state = {
            "session_id": "",
            "timestamp": "2026-09-28T22:00:00",
            "trigger": "idle_compaction",
            "original_prompt": "discord URLを教えて",
            "accumulated_response": "こちらがURLです: http://example.com/discord",
            "tool_uses": [],
            "context_usage_ratio": 0.5,
            "turn_count": 3,
            "notes": "",
        }
        (shortterm_dir / "session_state.json").write_text(
            json.dumps(session_state, ensure_ascii=False), encoding="utf-8"
        )

        mc = ModelConfig(model="claude-sonnet-4-6", api_key="sk-test")
        result = _with_resume_fallback_history(anima_dir, mc, "続き", "chat", thread_id)

        assert "http://example.com/discord" in result
        assert "続き" in result
        assert "前のセッションを再開できなかったため" in result
