from __future__ import annotations

# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
import base64
import binascii
import secrets
from typing import Any

from core.i18n import t
from core.time_utils import now_local
from server.routes.chat_models import (
    MAX_IMAGE_PAYLOAD_SIZE,
    MIME_TO_EXT,
    SUPPORTED_IMAGE_TYPES,
    ImageAttachment,
)


def _validate_images(images: list[ImageAttachment]) -> str | None:
    """Validate image attachments. Returns error message or None."""
    if not images:
        return None
    total_size = sum(len(img.data) for img in images)
    if total_size > MAX_IMAGE_PAYLOAD_SIZE:
        return t("chat.image_too_large", size_mb=total_size // 1024 // 1024)
    for img in images:
        if img.media_type not in SUPPORTED_IMAGE_TYPES:
            return t("chat.unsupported_image_format", media_type=img.media_type)
    return None


def _check_image_data(images: list[ImageAttachment]) -> str | None:
    """Return an error message when an image's base64 cannot be decoded.

    Uses the same lenient decoding as :func:`save_images`, so nothing that
    is stored today is rejected; it only moves the failure before any
    attachment is written (instead of an HTTP 500 half-way through).
    """
    for img in images:
        try:
            base64.b64decode(img.data)
        except (binascii.Error, ValueError):
            return t("chat.image_invalid_data")
    return None


def save_images(anima_name: str, images: list[ImageAttachment]) -> list[str]:
    """Save base64 images to ~/.animaworks/animas/{name}/attachments/.

    Each image gets a unique name and is created exclusively through
    :class:`~core.chat_attachments.AttachmentStore` (never through a symlink,
    never overwriting another upload).  All-or-nothing: when one image fails,
    the images written by this call are removed again.

    Returns:
        List of relative paths (e.g. ``attachments/20260217_120000_1a2b3c4d_0.jpeg``).
    """
    if not images:
        return []
    from core.chat_attachments import AttachmentStore
    from core.paths import get_data_dir

    paths: list[str] = []
    written: list[str] = []
    ts = now_local().strftime("%Y%m%d_%H%M%S")
    with AttachmentStore(get_data_dir() / "animas" / anima_name) as store:
        try:
            for i, img in enumerate(images):
                ext = MIME_TO_EXT.get(img.media_type, "png")
                data = base64.b64decode(img.data)
                filename = f"{ts}_{secrets.token_hex(4)}_{i}.{ext}"
                store.write_new(filename, data)
                written.append(filename)
                paths.append(f"attachments/{filename}")
        except BaseException:
            for name in written:
                store.unlink(name)
            raise
    return paths


def build_content_blocks(
    message: str,
    images: list[ImageAttachment],
) -> str | list[dict[str, Any]]:
    """Convert text + images to LLM content blocks.

    Returns plain string if no images are present.
    """
    if not images:
        return message
    blocks: list[dict[str, Any]] = []
    for img in images:
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img.media_type,
                    "data": img.data,
                },
            }
        )
    if message.strip():
        blocks.append({"type": "text", "text": message})
    return blocks
