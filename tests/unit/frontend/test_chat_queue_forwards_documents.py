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
