"""Every queued-message drain site must forward document attachments.

Queued entries carry ``files``/``displayFiles`` alongside images.  A drain
site that forwards only images silently drops queued documents, because the
input manager was already cleared when the entry was enqueued.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DRAIN_SITES = {
    PROJECT_ROOT / "server" / "static" / "workspace" / "modules" / "chat-streaming.js": 2,
    PROJECT_ROOT / "server" / "static" / "pages" / "chat" / "streaming-controller.js": 3,
}
_CALL = re.compile(r"next\.text,\s*\{([^}]*)\}")


@pytest.mark.parametrize(("path", "min_sites"), DRAIN_SITES.items(), ids=[p.name for p in DRAIN_SITES])
def test_every_queue_drain_forwards_files_and_display_files(path: Path, min_sites: int) -> None:
    source = path.read_text(encoding="utf-8")
    calls = _CALL.findall(source)
    assert len(calls) >= min_sites, f"expected at least {min_sites} drain sites in {path.name}, found {len(calls)}"
    for args in calls:
        assert "images: next.images" in args, args
        assert "displayImages: next.displayImages" in args, args
        assert "files: next.files" in args, f"{path.name} drain site drops queued documents: {{{args}}}"
        assert "displayFiles: next.displayFiles" in args, (
            f"{path.name} drain site drops queued document previews: {{{args}}}"
        )


def test_workspace_enqueue_captures_files_and_display_files() -> None:
    source = (PROJECT_ROOT / "server" / "static" / "workspace" / "modules" / "chat-streaming.js").read_text(
        encoding="utf-8"
    )
    assert "files: im?.getPendingFiles() || []" in source
    assert "displayFiles: im?.getDisplayFiles() || []" in source


QUEUE_EDIT_SITES = {
    PROJECT_ROOT
    / "server"
    / "static"
    / "workspace"
    / "modules"
    / "chat-streaming.js": "im?.restoreAttachments(removed)",
    PROJECT_ROOT
    / "server"
    / "static"
    / "pages"
    / "chat"
    / "streaming-controller.js": "state.imageInputManager?.restoreAttachments(removed)",
}


@pytest.mark.parametrize(("path", "call"), QUEUE_EDIT_SITES.items(), ids=[p.name for p in QUEUE_EDIT_SITES])
def test_queue_edit_restores_attachments_not_only_text(path: Path, call: str) -> None:
    """Clicking a queued item back into the composer must restore its files/images too."""
    source = path.read_text(encoding="utf-8")
    assert "removed.text" in source
    assert call in source, f"{path.name} queue edit restores only text; queued attachments would be lost"


_WORKSPACE = PROJECT_ROOT / "server" / "static" / "workspace" / "modules" / "chat-streaming.js"
_CHAT_PAGE = PROJECT_ROOT / "server" / "static" / "pages" / "chat" / "streaming-controller.js"


@pytest.mark.parametrize("path", [_WORKSPACE, _CHAT_PAGE], ids=[_WORKSPACE.name, _CHAT_PAGE.name])
def test_every_queue_drain_pins_the_target_anima_and_thread(path: Path) -> None:
    """A drained entry must be sent to the conversation it was queued for.

    The drain fires on a timer; if the user switches conversations meanwhile,
    resolving the *current* anima/thread at send time would deliver (and
    persist) the queued attachment under the wrong Anima.
    """
    source = path.read_text(encoding="utf-8")
    calls = _CALL.findall(source)
    assert calls
    for args in calls:
        assert "targetAnima:" in args, f"{path.name} drain site does not pin its anima: {{{args}}}"
        assert "targetThread:" in args, f"{path.name} drain site does not pin its thread: {{{args}}}"


def test_workspace_send_resolves_the_pinned_target_before_the_current_conversation() -> None:
    source = _WORKSPACE.read_text(encoding="utf-8")
    assert "overrideImages?.targetAnima || curAnima" in source
    assert "overrideImages?.targetThread || curThread" in source


def test_workspace_background_pinned_send_does_not_mutate_the_active_composer() -> None:
    """Draining conversation A after switching to B must preserve B's draft/UI."""
    source = _WORKSPACE.read_text(encoding="utf-8")
    assert "const isTargetActive = () =>" in source
    assert 'if (isTargetActive()) {\n    dom.convInput.value = ""' in source
    assert "if (isTargetActive()) {\n          renderWsThreadTabs();" in source
    assert "wsUpdateSendButton(false); wsSaveDraft(); dom.convInput?.focus();" in source


QUEUE_EDIT_PREFLIGHT = {
    _WORKSPACE: ("im.canRestoreAttachments(queued)", "mgr.removeFromQueue(anima, thread, idx)"),
    _CHAT_PAGE: (
        "state.imageInputManager.canRestoreAttachments(queued)",
        "mgr.removeFromQueue(name, tid, idx)",
    ),
}


@pytest.mark.parametrize(("path", "calls"), QUEUE_EDIT_PREFLIGHT.items(), ids=[p.name for p in QUEUE_EDIT_PREFLIGHT])
def test_queue_edit_preflights_capacity_before_removing_the_entry(path: Path, calls: tuple[str, str]) -> None:
    """The entry must stay queued when its documents do not fit in the composer.

    Removing first and restoring afterwards would drop whatever does not fit
    within the document count limit.
    """
    preflight, remove = calls
    source = path.read_text(encoding="utf-8")
    assert preflight in source, f"{path.name} restores without a capacity preflight"
    assert source.index(preflight) < source.index(remove), f"{path.name} removes the queue entry before the preflight"


def test_chat_page_pinned_sends_keep_their_target_when_a_meeting_is_active() -> None:
    """A queued item drained into sendChat() must not be redirected to a meeting.

    The drain sites pin ``targetAnima``/``targetThread``; if the user entered a
    meeting while the 150 ms drain timer was pending, the meeting branch would
    otherwise take the text/images and drop the documents.
    """
    source = _CHAT_PAGE.read_text(encoding="utf-8")
    assert "if (!overrideImages?.targetAnima && ctx.controllers.meeting?.isActive?.())" in source
    assert "if (ctx.controllers.meeting?.isActive?.()) {\n      sendMeetingChat(message, overrideImages);" not in source


def test_chat_page_requeues_a_pinned_entry_if_the_drain_races_a_new_stream() -> None:
    """The delayed regular-chat drain must not drop the dequeued entry on a race."""
    source = _CHAT_PAGE.read_text(encoding="utf-8")
    busy = source.index("if (mgr.isStreamingFor(name, tid))")
    next_branch = source.index("const currentAnima", busy)
    branch = source[busy:next_branch]
    assert "if (overrideImages?.targetAnima)" in branch
    assert "requeuePinnedEntry(name, tid, message, images, displayImages, files, displayFiles)" in branch
    assert "function requeuePinnedEntry" in source
    assert "return { text: message, images, displayImages, files, displayFiles };" in source


@pytest.mark.parametrize(
    ("path", "recovery"),
    [
        (_CHAT_PAGE, "recoverFailedEntry(name, tid, message, images, displayImages, files, displayFiles)"),
        (_WORKSPACE, "_recoverFailedEntry(anima, thread, text, images, displayImages, files, displayFiles)"),
    ],
    ids=[_CHAT_PAGE.name, _WORKSPACE.name],
)
def test_transport_failure_keeps_the_exact_attachment_entry_for_user_retry(path: Path, recovery: str) -> None:
    """A failed stream must restore or queue the original document/image entry, never discard it."""
    source = path.read_text(encoding="utf-8")
    assert recovery in source
    assert "displayImages" in source
    assert "displayFiles" in source
    assert "canRestoreAttachments" in source
    assert "restoreAttachments(entry)" in source
    assert "requiresExplicitRetry: true" in source


@pytest.mark.parametrize(
    ("path", "start", "end"),
    [
        (_CHAT_PAGE, "function recoverFailedEntry", "async function sendChat"),
        (_WORKSPACE, "function _recoverFailedEntry", "let _convLatestZone"),
    ],
    ids=[_CHAT_PAGE.name, _WORKSPACE.name],
)
def test_transport_failure_does_not_merge_its_retry_with_new_composer_attachments(
    path: Path, start: str, end: str
) -> None:
    """A new attachment added during a failed request must remain a separate composition."""
    source = path.read_text(encoding="utf-8")
    start_index = source.index(start)
    recovery = source[start_index : source.index(end, start_index)]
    assert "const composerHasAttachments =" in recovery
    assert "getImageCount?.()" in recovery
    assert "getFileCount?.()" in recovery
    assert "isProcessing?.()" in recovery
    assert "const composerIsEmpty =" in recovery
    assert "composerIsEmpty && canRestore" in recovery


def test_automatic_drains_stop_before_a_failed_entry_that_requires_an_explicit_retry() -> None:
    """A later stream completion must not silently retry an ambiguous failed request."""
    workspace = _WORKSPACE.read_text(encoding="utf-8")
    chat_page = _CHAT_PAGE.read_text(encoding="utf-8")
    assert "q.length === 0 || q[0].requiresExplicitRetry" in workspace
    assert "!pendingQueue[0].requiresExplicitRetry" in chat_page


def test_terminal_stream_failure_does_not_drain_another_entry_before_recovery() -> None:
    """The failure callback must block onFinally's automatic drain.

    ``streamChat`` now rejects terminal SSE error/bootstrap-busy responses.  Its
    caller's ``onFinally`` runs before the failed attachment is restored, so a
    queue drain there would otherwise send a later entry ahead of a manual
    retry and obscure the original failed submission.
    """
    workspace = _WORKSPACE.read_text(encoding="utf-8")
    chat_page = _CHAT_PAGE.read_text(encoding="utf-8")
    assert "let transportFailure = false;" in workspace
    assert "transportFailure = true;" in workspace
    assert "if (!transportFailure) _drainQueue(anima, thread, isTargetActive());" in workspace
    assert "let transportFailure = false;" in chat_page
    assert "transportFailure = true;" in chat_page
    assert "if (!transportFailure && pendingQueue.length > 0" in chat_page


def test_page_stream_finalizer_does_not_save_a_different_thread_draft() -> None:
    """Finishing a pinned background send must not store the visible draft under its old thread."""
    source = _CHAT_PAGE.read_text(encoding="utf-8")
    assert "const isVisible = () => state.selectedAnima === name && state.selectedThreadId === tid;" in source
    assert "if (inputEl && isVisible())" in source


def test_workspace_pending_indicator_describes_document_only_entries_as_attachments() -> None:
    source = _WORKSPACE.read_text(encoding="utf-8")
    indicator = source[source.index("export function wsShowPendingIndicator") :]
    assert "p.files?.length" in indicator
    assert 't("chat.attachments_only")' in indicator
    assert 't("chat.attachment_count"' in indicator
    assert 't("chat.image_only")' not in indicator
