from __future__ import annotations

import base64
import binascii
import logging
import re
import uuid
from pathlib import Path

from core.document_attachments import (
    DOCUMENT_SUFFIXES,
    DocumentValidationError,
    extract_document_text,
    is_allowed_media_type,
    normalize_suffix,
    text_sidecar_path,
    validate_document_bytes,
)
from core.i18n import t
from core.time_utils import now_local
from server.routes.chat_models import (
    MAX_FILE_COUNT,
    MAX_FILE_PAYLOAD_SIZE,
    MAX_FILE_SIZE,
    FileAttachment,
    require_plain_anima_name,
)

logger = logging.getLogger(__name__)

_ERROR_KEYS = {
    "unsupported": "chat.unsupported_file_format",
    "invalid": "chat.invalid_file_data",
    "macro": "chat.file_macro_rejected",
    "encoding": "chat.csv_encoding_invalid",
}


def _validate_files(files: list[FileAttachment]) -> str | None:
    """Validate document attachments without trusting client MIME or names.

    Every check runs on the decoded bytes: extension allowlist, declared MIME
    allowlist per extension, per-file and total size, file count, magic bytes,
    macro/VBA rejection and UTF-8 for text formats.
    """
    if not files:
        return None
    if len(files) > MAX_FILE_COUNT:
        return t("chat.file_count_exceeded", max_count=MAX_FILE_COUNT)
    if sum(len(item.data) for item in files) > MAX_FILE_PAYLOAD_SIZE:
        return t("chat.file_payload_too_large")
    for item in files:
        suffix = normalize_suffix(item.name)
        if suffix not in DOCUMENT_SUFFIXES or not is_allowed_media_type(suffix, item.media_type):
            return t("chat.unsupported_file_format")
        try:
            decoded = base64.b64decode(item.data, validate=True)
        except (binascii.Error, ValueError):
            return t("chat.invalid_file_data")
        if len(decoded) > MAX_FILE_SIZE:
            return t("chat.file_too_large")
        try:
            validate_document_bytes(decoded, suffix)
        except DocumentValidationError as exc:
            logger.info("attachment rejected name=%r suffix=%s code=%s detail=%s", item.name, suffix, exc.code, exc)
            return t(_ERROR_KEYS.get(exc.code, "chat.invalid_file_data"))
    return None


def _safe_stem(original_name: str) -> str:
    """Return an ASCII-only stem derived from *original_name* (never a path)."""
    raw_stem = Path(original_name).stem
    return re.sub(r"[^A-Za-z0-9._-]+", "_", raw_stem).strip("._")[:80] or "file"


def save_files(anima_name: str, files: list[FileAttachment]) -> list[str]:
    """Save validated document attachments with controlled names and suffixes.

    ``.docx``/``.xlsx`` additionally get a ``<name>.txt`` sidecar with the
    extracted text so the receiving Anima can read Office documents with
    plain file tools.  Only the document path is returned; the sidecar is
    discovered by :func:`core.document_attachments.text_sidecar_path`.
    """
    if not files:
        return []
    require_plain_anima_name(anima_name)
    from core.paths import get_data_dir

    attachments_dir = get_data_dir() / "animas" / anima_name / "attachments"
    attachments_dir.mkdir(parents=True, exist_ok=True)
    timestamp = now_local().strftime("%Y%m%d_%H%M%S")
    paths: list[str] = []
    for index, item in enumerate(files):
        suffix = normalize_suffix(item.name)
        if suffix not in DOCUMENT_SUFFIXES:
            raise ValueError(f"unsupported attachment suffix: {suffix!r}")
        unique = uuid.uuid4().hex[:12]
        filename = f"{timestamp}_{unique}_{index}_{_safe_stem(item.name)}{suffix}"
        destination = attachments_dir / filename
        decoded = base64.b64decode(item.data, validate=True)
        # Extract before persisting so a failure cannot leave a document on
        # disk that the caller never learns about.
        text = extract_document_text(decoded, suffix)
        destination.write_bytes(decoded)
        if text is not None:
            text_sidecar_path(destination).write_text(text, encoding="utf-8")
        paths.append(f"attachments/{filename}")
    return paths
