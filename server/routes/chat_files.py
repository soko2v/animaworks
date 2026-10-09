from __future__ import annotations

# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
"""PDF file attachments for the chat routes: validation and storage.

``prepare_files`` validates and decodes in memory (no side effects), so the
routes can reject bad input before deciding whether the message will be
processed at all.  ``save_files`` then stores the PDFs under the receiving
Anima's ``attachments/`` directory together with the text/meta sidecars
built by :mod:`core.chat_attachments`.
"""

import asyncio
import base64
import binascii
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from core.chat_attachments import (
    MAX_PDF_BYTES,
    MAX_PDF_FILES,
    MAX_PDF_TOTAL_BYTES,
    AttachmentStore,
    PdfAttachmentError,
    analyse_pdf_bytes,
    display_name_for_upload,
    is_pdf_upload,
    looks_like_pdf,
    pdf_storage_supported,
    remove_chat_pdf,
    store_chat_pdf,
)
from core.i18n import t
from core.time_utils import now_local
from server.routes.chat_models import FileAttachment

logger = logging.getLogger("animaworks.routes.chat")

_MB = 1024 * 1024
# PDF analysis/storage runs on its own small pool so slow or crafted uploads
# never occupy the default executor (history, downloads, ...), and at most
# this many uploads are admitted at once; more get an immediate "busy".
_ANALYSIS_WORKERS = 2
_MAX_ADMITTED_UPLOADS = 4
_ANALYSIS_EXECUTOR = ThreadPoolExecutor(max_workers=_ANALYSIS_WORKERS, thread_name_prefix="chat-pdf")
_UPLOAD_SLOTS = threading.BoundedSemaphore(_MAX_ADMITTED_UPLOADS)
# Base64 padding can add up to two bytes to the length-based estimate.
_ESTIMATE_SLACK = 3


@dataclass(frozen=True)
class PreparedFile:
    """A validated, decoded PDF waiting to be stored."""

    data: bytes
    name: str


def _mb(size: int) -> str:
    text = f"{size / _MB:.1f}"
    return text[:-2] if text.endswith(".0") else text


def _too_large(name: str, size: int) -> PdfAttachmentError:
    return PdfAttachmentError(
        t("chat.file_too_large", name=name, size_mb=_mb(size), max_mb=_mb(MAX_PDF_BYTES)),
        413,
    )


def _total_too_large(size: int) -> PdfAttachmentError:
    return PdfAttachmentError(
        t("chat.file_total_too_large", size_mb=_mb(size), max_mb=_mb(MAX_PDF_TOTAL_BYTES)),
        413,
    )


def prepare_files(files: list[FileAttachment]) -> list[PreparedFile]:
    """Validate and decode PDF attachments without touching the disk.

    Raises:
        PdfAttachmentError: with a user-facing message and an HTTP status
            (413 too many/too large, 415 not a PDF, 400 broken data).
    """
    if not files:
        return []
    if len(files) > MAX_PDF_FILES:
        raise PdfAttachmentError(t("chat.file_too_many", max=MAX_PDF_FILES, count=len(files)), 413)

    # Reject oversized payloads before decoding anything.
    estimated_total = sum(len(item.data) for item in files) * 3 // 4
    if estimated_total > MAX_PDF_TOTAL_BYTES + _ESTIMATE_SLACK * len(files):
        raise _total_too_large(estimated_total)

    prepared: list[PreparedFile] = []
    total = 0
    for item in files:
        name = display_name_for_upload(item.name)
        if not is_pdf_upload(item.name, item.media_type):
            raise PdfAttachmentError(t("chat.file_unsupported", name=name), 415)
        estimated = len(item.data) * 3 // 4
        if estimated > MAX_PDF_BYTES + _ESTIMATE_SLACK:
            raise _too_large(name, estimated)
        try:
            data = base64.b64decode(item.data, validate=True)
        except (binascii.Error, ValueError):
            raise PdfAttachmentError(t("chat.file_invalid_data", name=name), 400) from None
        if not data:
            raise PdfAttachmentError(t("chat.file_invalid_data", name=name), 400)
        if len(data) > MAX_PDF_BYTES:
            raise _too_large(name, len(data))
        total += len(data)
        if total > MAX_PDF_TOTAL_BYTES:
            raise _total_too_large(total)
        if not looks_like_pdf(data):
            raise PdfAttachmentError(t("chat.file_not_pdf", name=name), 415)
        prepared.append(PreparedFile(data=data, name=name))
    return prepared


def _anima_dir(anima_name: str) -> Path:
    from core.anima.factory import validate_anima_name
    from core.paths import get_data_dir

    if validate_anima_name(anima_name) is not None:
        raise ValueError(f"invalid anima name: {anima_name!r}")
    return get_data_dir() / "animas" / anima_name


def remove_saved_files(anima_name: str, rel_paths: list[str]) -> None:
    """Roll back PDFs stored by :func:`save_files` (e.g. a later step failed)."""
    if not rel_paths:
        return
    try:
        store = AttachmentStore(_anima_dir(anima_name), require_race_free=True)
    except OSError:
        logger.exception("Cannot open attachments of anima=%s for rollback", anima_name)
        return
    with store:
        for rel_path in rel_paths:
            remove_chat_pdf(store, rel_path.removeprefix("attachments/"))


def save_files(anima_name: str, prepared: list[PreparedFile]) -> list[str]:
    """Store validated PDFs and their sidecars for *anima_name*.

    Runs ``pdfinfo``/``pdftotext`` on a private copy of the validated bytes
    (never on a path an Anima could swap), then writes through
    :class:`~core.chat_attachments.AttachmentStore`.  Call it through
    ``asyncio.to_thread``.  All-or-nothing: on any failure the files written
    by this call are removed again.

    Returns:
        Relative paths such as ``attachments/20261009_110000_1a2b3c4d_report.pdf``.

    Raises:
        PdfAttachmentError: for a password-protected PDF (400) or a storage
            failure (500).
    """
    if not prepared:
        return []
    if not pdf_storage_supported():
        raise PdfAttachmentError(t("chat.file_platform_unsupported"), 501)
    analysed = []
    for item in prepared:
        inspection, text, status = analyse_pdf_bytes(item.data)
        if inspection.password_required:
            raise PdfAttachmentError(t("chat.file_password_protected", name=item.name), 400)
        analysed.append((item, inspection, text, status))

    current = prepared[0].name
    try:
        store = AttachmentStore(_anima_dir(anima_name), require_race_free=True)
    except OSError as exc:
        logger.error("Refusing to store chat PDF for anima=%s: %s", anima_name, exc)
        raise PdfAttachmentError(t("chat.file_save_failed", name=current), 500) from exc

    timestamp = now_local().strftime("%Y%m%d_%H%M%S")
    written: list[str] = []
    stored: list[str] = []
    with store:
        try:
            for item, inspection, text, status in analysed:
                current = item.name
                names = store_chat_pdf(
                    store,
                    data=item.data,
                    display_name=item.name,
                    timestamp=timestamp,
                    inspection=inspection,
                    text=text,
                    status=status,
                )
                written.extend(names)
                stored.append(names[0])
        except OSError as exc:
            logger.exception("Failed to store chat PDF for anima=%s", anima_name)
            for name in written:
                store.unlink(name)
            raise PdfAttachmentError(t("chat.file_save_failed", name=current), 500) from exc
    logger.info("chat_files_saved anima=%s count=%d", anima_name, len(stored))
    return [f"attachments/{name}" for name in stored]


async def store_files(anima_name: str, prepared: list[PreparedFile]) -> list[str]:
    """Admission-controlled :func:`save_files` on the dedicated PDF executor.

    Raises:
        PdfAttachmentError: 503 when too many uploads are already in progress,
            plus everything :func:`save_files` raises.
    """
    if not prepared:
        return []
    slots = _UPLOAD_SLOTS
    if not slots.acquire(blocking=False):
        raise PdfAttachmentError(t("chat.file_busy"), 503)
    try:
        future = _ANALYSIS_EXECUTOR.submit(save_files, anima_name, prepared)
    except RuntimeError:
        slots.release()
        raise
    # The slot is given back exactly once, when the future is settled: the job
    # finished, failed, or was cancelled while still queued (a cancelled
    # request cancels a queued job; a running job always runs to its end).
    future.add_done_callback(lambda _done: slots.release())
    return await asyncio.wrap_future(future)
