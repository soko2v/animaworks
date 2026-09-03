from __future__ import annotations

# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from core.schemas import ImageData

MAX_CHAT_MESSAGE_SIZE = 10 * 1024 * 1024  # 10MB
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5MB decoded per image
MAX_IMAGE_PAYLOAD_SIZE = 20 * 1024 * 1024  # 20MB total base64
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB decoded per file
MAX_FILE_PAYLOAD_SIZE = 20 * 1024 * 1024  # 20MB total base64
MAX_FILE_COUNT = 10  # document attachments per message (enforced server-side)

SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MIME_TO_EXT = {
    "image/jpeg": "jpeg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


def require_plain_anima_name(anima_name: str) -> str:
    """Return *anima_name* if it is a single plain path segment, else raise.

    Every attachment writer composes ``animas/<name>/attachments`` from this
    value, so it must never be empty, ``.``/``..`` or contain a separator.
    """
    if not anima_name or anima_name in {".", ".."} or Path(anima_name).name != anima_name:
        raise ValueError(f"invalid anima name: {anima_name!r}")
    return anima_name


class ImageAttachment(BaseModel):
    """A single base64-encoded image attachment."""

    data: str  # Base64 encoded string (no data: prefix)
    media_type: str  # "image/jpeg", "image/png", "image/gif", "image/webp"


class FileAttachment(BaseModel):
    """A single base64-encoded document attachment."""

    data: str
    media_type: str
    name: str


def _to_image_data(attachments: list[ImageAttachment]) -> list[ImageData]:
    """Convert API-layer ImageAttachment list to core-layer ImageData list."""
    return [{"data": img.data, "media_type": img.media_type} for img in attachments]


class ChatRequest(BaseModel):
    message: str
    from_person: str = "human"
    intent: str = ""
    images: list[ImageAttachment] = []
    files: list[FileAttachment] = []
    resume: str | None = None
    last_event_id: str | None = None
    thread_id: str = "default"


class ChatResponse(BaseModel):
    response: str
    anima: str
    images: list[dict[str, Any]] = []
