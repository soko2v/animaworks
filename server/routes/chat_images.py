from __future__ import annotations

# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
import base64
import binascii
from typing import Any

from core.i18n import t
from core.time_utils import now_local
from server.routes.chat_models import (
    MAX_IMAGE_PAYLOAD_SIZE,
    MAX_IMAGE_SIZE,
    MIME_TO_EXT,
    SUPPORTED_IMAGE_TYPES,
    ImageAttachment,
    require_plain_anima_name,
)


def _matches_image_signature(data: bytes, media_type: str) -> bool:
    """Return whether decoded bytes match the declared supported image type."""
    if media_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if media_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if media_type == "image/webp":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    return False


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
        try:
            decoded = base64.b64decode(img.data, validate=True)
        except (binascii.Error, ValueError):
            return t("chat.invalid_image_data")
        if len(decoded) > MAX_IMAGE_SIZE:
            return t("chat.image_file_too_large", size_mb=len(decoded) // 1024 // 1024)
        if not _matches_image_signature(decoded, img.media_type):
            return t("chat.invalid_image_data")
    return None


def save_images(anima_name: str, images: list[ImageAttachment]) -> list[str]:
    """Save base64 images to ~/.animaworks/animas/{name}/attachments/.

    Returns:
        List of relative paths (e.g. ``attachments/20260217_120000_0.jpeg``).
    """
    if not images:
        return []
    require_plain_anima_name(anima_name)
    from core.paths import get_data_dir

    attachments_dir = get_data_dir() / "animas" / anima_name / "attachments"
    attachments_dir.mkdir(parents=True, exist_ok=True)

    paths: list[str] = []
    ts = now_local().strftime("%Y%m%d_%H%M%S")
    for i, img in enumerate(images):
        ext = MIME_TO_EXT.get(img.media_type, "png")
        filename = f"{ts}_{i}.{ext}"
        filepath = attachments_dir / filename
        filepath.write_bytes(base64.b64decode(img.data))
        paths.append(f"attachments/{filename}")
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
