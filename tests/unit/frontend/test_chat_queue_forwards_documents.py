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
    / "chat-streaming.js": "_getImageManager()?.restoreAttachments(removed)",
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
