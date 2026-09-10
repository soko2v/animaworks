# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for multi-thread chat frontend implementation.

Static analysis tests: read JS/CSS files and verify patterns for
thread tabs, thread_id passthrough, and refactored chat history structure.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# ── Paths ────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CHAT_JS = PROJECT_ROOT / "server" / "static" / "pages" / "chat.js"
CHAT_CTX_JS = PROJECT_ROOT / "server" / "static" / "pages" / "chat" / "ctx.js"
CHAT_STREAMING_JS = PROJECT_ROOT / "server" / "static" / "pages" / "chat" / "streaming-controller.js"
CHAT_RENDERER_JS = PROJECT_ROOT / "server" / "static" / "pages" / "chat" / "chat-renderer.js"
WORKSPACE_APP_JS = PROJECT_ROOT / "server" / "static" / "workspace" / "modules" / "app.js"
WORKSPACE_CHAT_THREAD_JS = PROJECT_ROOT / "server" / "static" / "workspace" / "modules" / "chat-thread.js"
WORKSPACE_CHAT_STREAMING_JS = PROJECT_ROOT / "server" / "static" / "workspace" / "modules" / "chat-streaming.js"
SESSION_MANAGER_JS = PROJECT_ROOT / "server" / "static" / "shared" / "chat" / "session-manager.js"
WORKSPACE_STATE_JS = PROJECT_ROOT / "server" / "static" / "workspace" / "modules" / "state.js"
CHAT_CSS = PROJECT_ROOT / "server" / "static" / "styles" / "chat.css"
CHAT_EVENTS_JS = PROJECT_ROOT / "server" / "static" / "pages" / "chat" / "events-controller.js"
CHAT_THREAD_JS = PROJECT_ROOT / "server" / "static" / "pages" / "chat" / "thread-controller.js"
MEETING_CONTROLLER_JS = PROJECT_ROOT / "server" / "static" / "pages" / "chat" / "meeting-controller.js"
WORKSPACE_STYLE = PROJECT_ROOT / "server" / "static" / "workspace" / "style.css"


def _read(path: Path) -> str:
    """Read a file's text content."""
    return path.read_text(encoding="utf-8")


# ── TestThreadRename ────────────────────────────────────────


@pytest.mark.unit
class TestThreadRename:
    """Thread labels are user-editable in both chat surfaces."""

    THREAD_LOGIC_JS = PROJECT_ROOT / "server" / "static" / "shared" / "chat" / "thread-logic.js"
    I18N_DIR = PROJECT_ROOT / "server" / "static" / "i18n"
    RESPONSIVE_CSS = PROJECT_ROOT / "server" / "static" / "styles" / "responsive.css"

    def test_shared_logic_renders_rename_button_and_exports_rename(self) -> None:
        js = _read(self.THREAD_LOGIC_JS)
        assert "export function renameThread(" in js
        assert 'class="thread-tab-rename"' in js

    def test_chat_page_binds_rename_button_and_dblclick(self) -> None:
        js = _read(CHAT_THREAD_JS)
        assert "renameThread as sharedRenameThread" in js
        assert '".thread-tab-rename"' in js
        assert '"dblclick"' in js
        assert "renameDblclickBound" in js, "dblclick must be delegated to the container (tabs re-render on select)"
        assert "chat-thread-dd-rename" in js, "mobile dropdown needs a rename affordance"
        assert 'renameThread(tid, "dropdown")' in js, "dropdown rename must restore focus in the dropdown (tabs are hidden on mobile)"
        assert '.chat-thread-dd-rename[data-thread=' in js
        assert "scheduleSaveChatUiState(ctx)" in js.split("function renameThread(")[1].split("\n  }\n")[0], \
            "rename must persist via chat ui-state"

    def test_workspace_binds_rename_button_and_dblclick(self) -> None:
        js = _read(WORKSPACE_CHAT_THREAD_JS)
        assert "renameThread as sharedRenameThread" in js
        assert "export function renameWsThread(" in js
        assert '".thread-tab-rename"' in js
        assert '"dblclick"' in js
        assert "renameDblclickBound" in js, "dblclick must be delegated to the container (tabs re-render on select)"

    def test_i18n_keys_present_in_all_locales(self) -> None:
        import json
        for lang in ("ja", "en", "ko"):
            data = json.loads((self.I18N_DIR / f"{lang}.json").read_text(encoding="utf-8"))
            for key in ("thread.rename", "thread.rename_short", "thread.rename_prompt", "thread.rename_hint"):
                assert data.get(key), f"{lang}: missing {key}"

    def test_css_has_rename_styles(self) -> None:
        assert ".thread-tab-rename" in _read(CHAT_CSS)
        assert ".chat-thread-dd-rename" in _read(self.RESPONSIVE_CSS)
        assert ".ws-thread-tabs .thread-tab-rename" in _read(WORKSPACE_STYLE)


# ── TestChatJsThreadTabs ────────────────────────────────────


@pytest.mark.unit
class TestChatJsThreadTabs:
    """Verify chat.js HTML structure for thread tabs."""

    def test_chat_js_contains_thread_tabs_div_with_id(self) -> None:
        """Pane host contains thread-tabs div with chatThreadTabs data-chat-id."""
        pane_host = PROJECT_ROOT / "server" / "static" / "pages" / "chat" / "pane-host.js"
        js = _read(pane_host)
        assert 'class="thread-tabs"' in js
        assert 'data-chat-id="chatThreadTabs"' in js

    def test_chat_js_contains_chat_new_thread_btn(self) -> None:
        """Pane host contains chatNewThreadBtn button."""
        pane_host = PROJECT_ROOT / "server" / "static" / "pages" / "chat" / "pane-host.js"
        js = _read(pane_host)
        assert "chatNewThreadBtn" in js

    def test_chat_js_selected_thread_id_initialized_default(self) -> None:
        """selectedThreadId is initialized to 'default' in chat context."""
        js = _read(CHAT_CTX_JS)
        assert 'selectedThreadId: "default"' in js

    def test_chat_js_threads_initialized_empty_object(self) -> None:
        """threads is initialized as empty object {} in chat context."""
        js = _read(CHAT_CTX_JS)
        assert "threads: {}" in js

    def test_new_thread_button_has_one_binding_owner(self) -> None:
        """Dynamic thread rendering owns the + button listener exactly once."""
        events_js = _read(CHAT_EVENTS_JS)
        thread_js = _read(CHAT_THREAD_JS)
        assert 'addListener("chatNewThreadBtn"' not in events_js
        assert thread_js.count('const newBtn = $("chatNewThreadBtn")') == 1


@pytest.mark.unit
class TestMeetingRoomReentry:
    """Verify active meeting rooms can be listed and reopened."""

    def test_meeting_controller_lists_and_reopens_rooms(self) -> None:
        js = _read(MEETING_CONTROLLER_JS)
        assert 'api("/api/rooms")' in js
        assert "meeting-room-reopen" in js
        assert "openRoom(btn.dataset.roomId)" in js

    def test_meeting_creation_has_duplicate_submit_guard(self) -> None:
        js = _read(MEETING_CONTROLLER_JS)
        assert "state.meetingRoomCreating" in js
        assert "finally" in js


# ── TestChatJsThreadIdInSendChat ─────────────────────────────


@pytest.mark.unit
class TestChatJsThreadIdInSendChat:
    """Verify thread_id is included in send and fetch calls."""

    def test_send_chat_includes_thread_id_in_body(self) -> None:
        """Session manager includes thread_id: thread in the body JSON."""
        js = _read(SESSION_MANAGER_JS)
        assert "thread_id: thread" in js
        assert "bodyObj" in js or "body" in js
        assert re.search(r"thread_id\s*:\s*thread", js)

    def test_fetch_conversation_history_includes_thread_id_param(self) -> None:
        """Chat renderer includes thread_id parameter in URL."""
        js = _read(CHAT_RENDERER_JS)
        assert "thread_id" in js
        assert "&thread_id=" in js
        assert "encodeURIComponent" in js and "threadId" in js


# ── TestChatJsChatHistoryRefactored ─────────────────────────


@pytest.mark.unit
class TestChatJsChatHistoryRefactored:
    """Verify _chatHistories and _historyState use per-thread structure."""

    def test_render_chat_accesses_chat_histories_by_name_and_tid(self) -> None:
        """Chat renderer accesses messages via mgr.getMessages(name, tid)."""
        js = _read(CHAT_RENDERER_JS)
        assert "mgr.getMessages(name, tid)" in js

    def test_render_chat_accesses_history_state_by_name_and_tid(self) -> None:
        """History state is accessed via mgr.getHistoryState(name, tid)."""
        js = _read(CHAT_RENDERER_JS)
        assert "mgr.getHistoryState(name, tid)" in js


# ── TestWorkspaceStateFields ────────────────────────────────


@pytest.mark.unit
class TestWorkspaceStateFields:
    """Verify state.js contains thread-related state fields."""

    def test_state_js_contains_active_thread_id(self) -> None:
        """state.js contains activeThreadId field."""
        js = _read(WORKSPACE_STATE_JS)
        assert "activeThreadId" in js

    def test_state_js_contains_threads(self) -> None:
        """state.js contains threads field."""
        js = _read(WORKSPACE_STATE_JS)
        assert "threads:" in js or "threads :" in js

    def test_state_js_contains_chat_messages_by_thread(self) -> None:
        """state.js contains chatMessagesByThread field."""
        js = _read(WORKSPACE_STATE_JS)
        assert "chatMessagesByThread" in js


# ── TestWorkspaceAppJsThreadTabs ─────────────────────────────


@pytest.mark.unit
class TestWorkspaceAppJsThreadTabs:
    """Verify workspace app.js thread tab rendering and thread_id in send."""

    def test_app_js_contains_thread_tab_rendering(self) -> None:
        """chat-thread.js contains thread tab rendering function/logic."""
        js = _read(WORKSPACE_CHAT_THREAD_JS)
        assert "thread-tab" in js
        assert "wsThreadTabs" in js or "threadTabs" in js

    def test_send_conversation_message_includes_thread_id_in_body(self) -> None:
        """session-manager.js includes thread_id: thread in the body."""
        js = _read(SESSION_MANAGER_JS)
        assert "thread_id: thread" in js
        assert re.search(r"thread_id\s*:\s*thread", js)


# ── TestChatCssThreadStyles ──────────────────────────────────


@pytest.mark.unit
class TestChatCssThreadStyles:
    """Verify chat.css contains thread tab CSS classes."""

    def test_chat_css_thread_tabs_class(self) -> None:
        """chat.css contains .thread-tabs class."""
        css = _read(CHAT_CSS)
        assert ".thread-tabs" in css

    def test_chat_css_thread_tab_class(self) -> None:
        """chat.css contains .thread-tab class."""
        css = _read(CHAT_CSS)
        assert ".thread-tab" in css

    def test_chat_css_thread_tab_active_class(self) -> None:
        """chat.css contains .thread-tab.active class."""
        css = _read(CHAT_CSS)
        assert ".thread-tab.active" in css

    def test_chat_css_thread_tab_new_class(self) -> None:
        """chat.css contains .thread-tab-new class."""
        css = _read(CHAT_CSS)
        assert ".thread-tab-new" in css


# ── TestWorkspaceCssThreadStyles ─────────────────────────────


@pytest.mark.unit
class TestWorkspaceCssThreadStyles:
    """Verify workspace style.css contains thread tab styles."""

    def test_workspace_css_thread_tabs_exists(self) -> None:
        """Workspace style.css contains thread-tabs / ws-thread-tabs."""
        css = _read(WORKSPACE_STYLE)
        assert "thread-tabs" in css or "ws-thread-tabs" in css

    def test_workspace_css_thread_tab_class(self) -> None:
        """Workspace has .thread-tab styling."""
        css = _read(WORKSPACE_STYLE)
        assert ".thread-tab" in css

    def test_workspace_css_thread_tab_new_class(self) -> None:
        """Workspace has .thread-tab-new styling."""
        css = _read(WORKSPACE_STYLE)
        assert ".thread-tab-new" in css
