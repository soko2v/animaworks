from __future__ import annotations

# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
#
# This file is part of AnimaWorks core/server, licensed under Apache-2.0.
# See LICENSE for the full license text.

"""PDF attachments shared with an Anima through the web chat.

Server side
    Validation helpers, safe storage names and sidecar extraction.  Each
    stored ``attachments/<file>.pdf`` gets ``<file>.pdf.txt`` (text from
    poppler's ``pdftotext``) and ``<file>.pdf.meta.json`` (original name,
    size, page count, extraction status).

Anima side
    :func:`build_pdf_prompt_context` tells every execution engine where the
    PDF lives and what it says, so Codex/GPT, LiteLLM and CLI engines can all
    read it.  :func:`chat_sdk_documents` publishes eligible PDFs to the Claude
    Agent SDK engine, which attaches them as native ``document`` blocks.

PDF bytes never travel over IPC: only ``attachments/...`` paths do.
"""

import base64
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
from collections.abc import Iterable, Iterator, Sequence
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.i18n import t

logger = logging.getLogger(__name__)

# ── Limits ────────────────────────────────────────────────────

PDF_MEDIA_TYPE = "application/pdf"
PDF_SUFFIX = ".pdf"
# Media types browsers report for PDF files.  An empty type or
# octet-stream is accepted when the name ends with ``.pdf``; the magic
# number is what actually decides.
ACCEPTED_PDF_MEDIA_TYPES = frozenset({"application/pdf", "application/x-pdf"})
GENERIC_MEDIA_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream"})

MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PDF_FILES = 5
MAX_PDF_TOTAL_BYTES = 40 * 1024 * 1024

# Native ``document`` blocks for the Claude Agent SDK.  Kept well under the
# Anthropic 32 MB request / 100 page limits because the block stays in the
# resumed session history for later turns.
SDK_DOCUMENT_MAX_BYTES = 5 * 1024 * 1024
SDK_DOCUMENT_MAX_PAGES = 20
SDK_DOCUMENT_TOTAL_MAX_BYTES = 8 * 1024 * 1024
SDK_DOCUMENT_TOTAL_MAX_PAGES = 30
# Native documents stay in the resumed SDK session, so later turns re-send
# them.  Keep everything sent natively within one conversation window under
# this budget (base64 ~16 MB of the 32 MB Anthropic request limit).
SDK_SESSION_DOCUMENT_BUDGET_BYTES = 12 * 1024 * 1024
SDK_SESSION_DOCUMENT_BUDGET_PAGES = 90  # Anthropic: at most 100 PDF pages per request
NATIVE_USAGE_FILE = "chat_pdf_documents.json"  # <anima>/state/: native PDFs per SDK session

# Extracted text inlined into the prompt for engines without native PDF input.
PROMPT_TEXT_BUDGET_CHARS = 30_000
_MIN_PER_FILE_TEXT_CHARS = 4_000
SIDECAR_TEXT_MAX_CHARS = 1_000_000
EARLIER_PDF_LIMIT = 5

TEXT_SIDECAR_SUFFIX = ".txt"
META_SIDECAR_SUFFIX = ".meta.json"

_PDF_MAGIC = b"%PDF-"
_PDF_MAGIC_WINDOW = 1024
_MAX_DISPLAY_NAME_CHARS = 120
_MAX_STORAGE_STEM_CHARS = 60
_TOOL_TIMEOUT_SECONDS = 60.0
_INFO_TIMEOUT_SECONDS = 20.0
# pdfinfo prints the Info dictionary, which may come from a compressed object
# stream; real-world output is a few KB.
_INFO_OUTPUT_CAP_BYTES = 256 * 1024
_STDERR_CAP_BYTES = 64 * 1024
_MAX_META_BYTES = 64 * 1024
# poppler can be made to allocate a lot while decompressing crafted streams;
# kill it beyond this resident size and never run more than two at a time.
_POPPLER_MAX_RSS_BYTES = 1024 * 1024 * 1024
_POPPLER_SLOTS = threading.BoundedSemaphore(2)
_POPPLER_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")

_UNSAFE_NAME_CHARS_RE = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')
# Bidi / zero-width controls make "evil‮fdp.exe" look like a PDF.
_INVISIBLE_CHARS_RE = re.compile(r"[​-‏‪-‮⁠-⁩﻿]")
_STORAGE_STEM_RE = re.compile(r"[^\w.\-]+", re.UNICODE)
_MULTI_DOT_RE = re.compile(r"\.{2,}")
_STORED_NAME_PREFIX_RE = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{8}_")
_PAGES_RE = re.compile(r"^Pages:\s+(\d+)\s*$", re.MULTILINE)
_ENCRYPTED_RE = re.compile(r"^Encrypted:\s+yes\b", re.MULTILINE)
_LONG_SPACE_RE = re.compile(r"[ \t]{4,}")
_MANY_BLANK_LINES_RE = re.compile(r"\n{4,}")


class PdfAttachmentError(ValueError):
    """A PDF the chat cannot accept.  ``str(exc)`` is user-facing."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


# ── Names ─────────────────────────────────────────────────────


def looks_like_pdf(data: bytes) -> bool:
    """Return True when *data* carries the ``%PDF-`` header (first 1 KiB)."""
    return _PDF_MAGIC in data[:_PDF_MAGIC_WINDOW]


def is_pdf_upload(name: str, media_type: str) -> bool:
    """Return True when the declared name/type describe a PDF upload."""
    media = (media_type or "").strip().lower()
    if media in ACCEPTED_PDF_MEDIA_TYPES:
        return True
    return media in GENERIC_MEDIA_TYPES and (name or "").strip().lower().endswith(PDF_SUFFIX)


def display_name_for_upload(name: str) -> str:
    """Return a safe display name ending in ``.pdf`` for an uploaded file.

    Strips directories, control/bidi characters and characters that are
    unsafe on common filesystems, and caps the length.
    """
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = unicodedata.normalize("NFC", base)
    base = _INVISIBLE_CHARS_RE.sub("", base)
    base = _UNSAFE_NAME_CHARS_RE.sub("_", base).strip()
    stem = base[: -len(PDF_SUFFIX)] if base.lower().endswith(PDF_SUFFIX) else base
    stem = stem.strip().strip(".").strip()
    if not stem:
        stem = "document"
    stem = stem[: _MAX_DISPLAY_NAME_CHARS - len(PDF_SUFFIX)]
    return f"{stem}{PDF_SUFFIX}"


def storage_filename(display_name: str, *, timestamp: str, token: str | None = None) -> str:
    """Return ``<timestamp>_<token>_<stem>.pdf`` safe for the attachments dir."""
    stem = display_name[: -len(PDF_SUFFIX)] if display_name.lower().endswith(PDF_SUFFIX) else display_name
    stem = _STORAGE_STEM_RE.sub("_", stem)
    # No ".." anywhere: the attachment route rejects such names.
    stem = _MULTI_DOT_RE.sub(".", stem).strip("._-") or "document"
    stem = stem[:_MAX_STORAGE_STEM_CHARS].rstrip("._-") or "document"
    token = token or secrets.token_hex(4)
    return f"{timestamp}_{token}_{stem}{PDF_SUFFIX}"


def _fallback_display_name(stored_name: str) -> str:
    """Derive a display name from a stored filename when meta is missing."""
    return _STORED_NAME_PREFIX_RE.sub("", stored_name) or stored_name


def text_sidecar_path(pdf_path: Path) -> Path:
    return pdf_path.with_name(pdf_path.name + TEXT_SIDECAR_SUFFIX)


def meta_sidecar_path(pdf_path: Path) -> Path:
    return pdf_path.with_name(pdf_path.name + META_SIDECAR_SUFFIX)


# ── poppler helpers ───────────────────────────────────────────


def find_poppler_tool(name: str) -> str | None:
    """Locate a poppler CLI (``pdfinfo``/``pdftotext``).

    launchd services often run with a minimal PATH, so the usual Homebrew
    and system locations are checked as well.
    """
    found = shutil.which(name)
    if found:
        return found
    for directory in _POPPLER_DIRS:
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


@dataclass(frozen=True)
class PdfInspection:
    """Result of ``pdfinfo`` on a stored PDF."""

    ok: bool
    pages: int | None = None
    encrypted: bool = False
    password_required: bool = False


def inspect_pdf(path: Path) -> PdfInspection:
    """Read page count and encryption state of the PDF at *path* (``pdfinfo``).

    Never raises: a missing tool, timeout or parse failure yields
    ``ok=False``.  ``password_required`` is set when poppler cannot open the
    file without a user password.
    """
    return _inspect_target(str(path))


def _inspect_target(target: str, pass_fds: tuple[int, ...] = ()) -> PdfInspection:
    tool = find_poppler_tool("pdfinfo")
    if tool is None:
        return PdfInspection(ok=False)
    try:
        result = _run_capped(
            [tool, "-enc", "UTF-8", target],
            stdout_cap=_INFO_OUTPUT_CAP_BYTES,
            stderr_cap=_STDERR_CAP_BYTES,
            timeout=_INFO_TIMEOUT_SECONDS,
            pass_fds=pass_fds,
            max_rss_bytes=_POPPLER_MAX_RSS_BYTES,
        )
    except OSError as exc:
        logger.warning("pdfinfo failed: %s", exc)
        return PdfInspection(ok=False)
    if result.returncode != 0:
        # Killed (timeout / runaway metadata output) or a parse error.
        err = result.stderr.decode("utf-8", "replace")
        return PdfInspection(ok=False, password_required="password" in err.lower())
    out = result.stdout.decode("utf-8", "replace")
    pages_match = _PAGES_RE.search(out)
    return PdfInspection(
        ok=True,
        pages=int(pages_match.group(1)) if pages_match else None,
        encrypted=bool(_ENCRYPTED_RE.search(out)),
    )


@dataclass(frozen=True)
class CappedRun:
    """Outcome of :func:`_run_capped`."""

    stdout: bytes
    stderr: bytes
    truncated: bool  # stdout exceeded its cap (process killed)
    returncode: int | None  # None when the process had to be killed


def _kill_process_tree(proc: subprocess.Popen[bytes]) -> None:
    """Kill *proc* and, on POSIX, its whole process group."""
    with contextlib.suppress(OSError):
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()


def _run_capped(
    argv: list[str],
    *,
    stdout_cap: int,
    stderr_cap: int,
    timeout: float,
    pass_fds: tuple[int, ...] = (),
    max_rss_bytes: int | None = None,
) -> CappedRun:
    """Run *argv* keeping at most *stdout_cap* / *stderr_cap* bytes of output.

    One wall-clock deadline covers the process and the draining of its
    pipes; the process group is killed when stdout exceeds its cap, when
    the child's resident memory exceeds *max_rss_bytes* (decompression
    bombs inside poppler), at the deadline, and also right after the tool
    exits (so descendants that inherited the pipes go away).  On POSIX the
    pipes are drained with ``select`` in the calling thread and closed on
    return, so nothing outlives the call even if a descendant escaped the
    process group.  Elsewhere reader threads are used (best effort).
    """
    deadline = time.monotonic() + timeout
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        start_new_session=(os.name == "posix"),
        pass_fds=pass_fds,
    )
    try:
        if os.name == "posix":
            result = _drain_with_select(proc, deadline, stdout_cap, stderr_cap, max_rss_bytes)
        else:
            result = _drain_with_threads(proc, deadline, stdout_cap, stderr_cap)
    finally:
        _kill_process_tree(proc)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
    return result


def _rss_exceeded(proc: subprocess.Popen[bytes], limit: int | None) -> bool:
    if not limit:
        return False
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a project dependency
        return False
    try:
        return psutil.Process(proc.pid).memory_info().rss > limit
    except psutil.Error:
        return False


def _drain_with_select(
    proc: subprocess.Popen[bytes],
    deadline: float,
    stdout_cap: int,
    stderr_cap: int,
    max_rss_bytes: int | None,
) -> CappedRun:
    """Drain both pipes with :mod:`selectors` (kqueue/epoll: no FD_SETSIZE limit)."""
    import selectors

    assert proc.stdout is not None and proc.stderr is not None
    out_fd, err_fd = proc.stdout.fileno(), proc.stderr.fileno()
    buffers = {out_fd: bytearray(), err_fd: bytearray()}
    caps = {out_fd: stdout_cap, err_fd: stderr_cap}
    overflow = killed = group_killed = False
    selector = selectors.DefaultSelector()
    try:
        for fd in (out_fd, err_fd):
            selector.register(fd, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0 or _rss_exceeded(proc, max_rss_bytes):
                killed = True
                break
            if not group_killed and proc.poll() is not None:
                # Tool finished: descendants in its group must not keep the pipes open.
                _kill_process_tree(proc)
                group_killed = True
            for key, _events in selector.select(timeout=min(remaining, 0.2)):
                fd = key.fd
                chunk = os.read(fd, 65536)
                if not chunk:
                    selector.unregister(fd)
                    continue
                buf = buffers[fd]
                room = caps[fd] - len(buf)
                if room > 0:
                    buf += chunk[:room]
                if fd == out_fd and len(chunk) > room:
                    overflow = killed = True
                    break
            if killed:
                break
    finally:
        selector.close()
        _kill_process_tree(proc)
        for stream in (proc.stdout, proc.stderr):
            with contextlib.suppress(OSError):
                stream.close()
    if not killed:
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        killed = proc.returncode is None
    return CappedRun(
        stdout=bytes(buffers[out_fd]),
        stderr=bytes(buffers[err_fd]),
        truncated=overflow,
        returncode=None if killed else proc.returncode,
    )


def _drain_with_threads(
    proc: subprocess.Popen[bytes],
    deadline: float,
    stdout_cap: int,
    stderr_cap: int,
) -> CappedRun:
    """Fallback for platforms where ``select`` cannot wait on pipes (Windows)."""
    overflow = threading.Event()
    out_buf = bytearray()
    err_buf = bytearray()

    def pump(stream: Any, buf: bytearray, cap: int, kills: bool) -> None:
        try:
            while True:
                chunk = stream.read1(65536)
                if not chunk:
                    return
                room = cap - len(buf)
                if room > 0:
                    buf += chunk[:room]
                if kills and len(chunk) > room:
                    overflow.set()
                    _kill_process_tree(proc)
                    return
        except (OSError, ValueError):
            return

    readers = [
        threading.Thread(target=pump, args=(proc.stdout, out_buf, stdout_cap, True), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, err_buf, stderr_cap, False), daemon=True),
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        timed_out = True
    _kill_process_tree(proc)
    for reader in readers:
        reader.join(timeout=max(0.5, deadline - time.monotonic()))
    stalled = any(reader.is_alive() for reader in readers)
    killed = timed_out or overflow.is_set() or stalled
    return CappedRun(
        stdout=bytes(out_buf),
        stderr=bytes(err_buf),
        truncated=overflow.is_set(),
        returncode=None if killed else proc.returncode,
    )


def _normalize_extracted_text(raw: str) -> str:
    """Tidy ``pdftotext -layout`` output and mark page boundaries."""
    pages = raw.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    rendered: list[str] = []
    for number, page in enumerate(pages, 1):
        lines = [_LONG_SPACE_RE.sub("   ", line).rstrip() for line in page.splitlines()]
        body = "\n".join(lines).strip("\n")
        if len(pages) > 1:
            rendered.append(f"=== Page {number} ===\n{body}")
        else:
            rendered.append(body)
    text = "\n\n".join(rendered)
    return _MANY_BLANK_LINES_RE.sub("\n\n\n", text).strip()


def extract_pdf_text(path: Path, *, max_chars: int = SIDECAR_TEXT_MAX_CHARS) -> tuple[str, str]:
    """Extract the text of the PDF at *path* with ``pdftotext``.

    Returns ``(text, status)`` where status is one of ``ok``, ``truncated``,
    ``empty`` (no text layer, e.g. a scanned PDF), ``failed`` or
    ``unavailable`` (poppler not installed).  Never raises.
    """
    return _extract_target(str(path), max_chars=max_chars)


def _extract_target(target: str, pass_fds: tuple[int, ...] = (), *, max_chars: int) -> tuple[str, str]:
    tool = find_poppler_tool("pdftotext")
    if tool is None:
        return "", "unavailable"
    try:
        result = _run_capped(
            [tool, "-enc", "UTF-8", "-layout", "-q", target, "-"],
            stdout_cap=max_chars * 4,
            stderr_cap=_STDERR_CAP_BYTES,
            timeout=_TOOL_TIMEOUT_SECONDS,
            pass_fds=pass_fds,
            max_rss_bytes=_POPPLER_MAX_RSS_BYTES,
        )
    except OSError as exc:
        logger.warning("pdftotext failed: %s", exc)
        return "", "failed"
    truncated = result.truncated
    if not truncated and result.returncode != 0:
        return "", "failed"
    raw = result.stdout.decode("utf-8", "replace")
    if not raw.replace("\f", "").strip():
        # No text layer at all (e.g. a scanned PDF); page markers alone are not text.
        return "", ("truncated" if truncated else "empty")
    text = _normalize_extracted_text(raw)
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    if not text.strip():
        return "", ("truncated" if truncated else "empty")
    return text, ("truncated" if truncated else "ok")


@contextlib.contextmanager
def _pdf_input(data: bytes) -> Iterator[tuple[str, tuple[int, ...]]]:
    """Yield ``(target, pass_fds)`` letting poppler read *data*.

    POSIX: an anonymous temporary file (unlinked right away) handed over as
    ``/dev/fd/N``, so there is no pathname anything could swap while the
    tools run.  Elsewhere: a file inside a private temporary directory.
    """
    tmpdir: str | None = tempfile.mkdtemp(prefix="aw-chat-pdf-")
    path = os.path.join(tmpdir, "input.pdf")
    fd: int | None = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        if os.name == "posix" and os.path.isdir("/dev/fd"):
            os.unlink(path)
            os.rmdir(tmpdir)
            tmpdir = None
            os.lseek(fd, 0, os.SEEK_SET)
            yield f"/dev/fd/{fd}", (fd,)
        else:
            os.close(fd)
            fd = None
            yield path, ()
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)


def analyse_pdf_bytes(
    data: bytes,
    *,
    max_chars: int = SIDECAR_TEXT_MAX_CHARS,
) -> tuple[PdfInspection, str, str]:
    """``pdfinfo`` + ``pdftotext`` on validated upload bytes.

    Works on a private copy of the bytes, never on a path inside an Anima's
    directory, so nothing can be swapped in while poppler reads.  Returns
    ``(inspection, text, text_status)``; the text is skipped for a PDF that
    needs a password.
    """
    with _POPPLER_SLOTS, _pdf_input(data) as (target, pass_fds):
        inspection = _inspect_target(target, pass_fds)
        if inspection.password_required:
            return inspection, "", "failed"
        if pass_fds:
            os.lseek(pass_fds[0], 0, os.SEEK_SET)
        text, status = _extract_target(target, pass_fds, max_chars=max_chars)
    return inspection, text, status


def build_pdf_meta(display_name: str, size: int, inspection: PdfInspection, text: str, status: str) -> dict[str, Any]:
    """The ``.meta.json`` sidecar of a stored chat PDF."""
    return {
        "version": 1,
        "name": display_name,
        "media_type": PDF_MEDIA_TYPE,
        "size": size,
        "pages": inspection.pages,
        "encrypted": inspection.encrypted,
        "text_status": status,
        "text_chars": len(text),
    }


def _check_component(name: str) -> None:
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError(f"invalid attachment file name: {name!r}")


class AttachmentStore:
    """Create and remove files in ``<anima>/attachments`` without following symlinks.

    POSIX: every operation is relative to a descriptor of the confined
    directory (opened with ``O_NOFOLLOW`` and checked against the confined
    path), so replacing the directory with a symlink later cannot redirect
    a write.  New files are created exclusively (``O_EXCL``, never through a
    symlink) under a temporary name, then renamed into place; existing
    files are never overwritten.  Platforms without ``dir_fd`` use the same
    steps on paths after the confinement check.

    Raises:
        OSError: when the directory is missing, a symlink or escapes the Anima.
    """

    def __init__(self, anima_dir: Path, *, require_race_free: bool = False) -> None:
        """Open the store.

        Args:
            require_race_free: refuse (``OSError``) on platforms without
                ``dir_fd`` support, where a directory swapped for a
                symlink/junction between the check and a write could redirect
                it.  Used for PDFs; chat images keep working everywhere.
        """
        self.fd: int | None = None
        supports_dir_fd = os.open in os.supports_dir_fd
        if require_race_free and not supports_dir_fd:
            raise OSError("race-free attachment storage is not available on this platform")
        (anima_dir / "attachments").mkdir(parents=True, exist_ok=True)
        root = confined_attachments_root(anima_dir)
        if root is None:
            raise OSError(f"attachments directory of {anima_dir.name!r} is not confined")
        self.root = root
        if supports_dir_fd:
            try:
                base_fd = os.open(anima_dir, os.O_RDONLY | _O_DIRECTORY)
                try:
                    self.fd = os.open("attachments", os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=base_fd)
                finally:
                    os.close(base_fd)
                opened, checked = os.fstat(self.fd), os.stat(root)
                if (opened.st_dev, opened.st_ino) != (checked.st_dev, checked.st_ino):
                    raise OSError(f"attachments directory of {anima_dir.name!r} changed while opening")
            except BaseException:
                self.close()
                raise

    def __enter__(self) -> AttachmentStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self.fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = None

    def exists(self, name: str) -> bool:
        _check_component(name)
        try:
            if self.fd is not None:
                os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            else:
                os.lstat(self.root / name)
        except FileNotFoundError:
            return False
        return True

    def write_new(self, name: str, data: bytes) -> None:
        """Create *name* holding *data*; refuses to overwrite anything."""
        _check_component(name)
        tmp = f".{name}.{secrets.token_hex(4)}.part"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_BINARY
        fd = (
            os.open(tmp, flags, 0o644, dir_fd=self.fd)
            if self.fd is not None
            else os.open(self.root / tmp, flags, 0o644)
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            if self.exists(name):
                raise FileExistsError(name)
            if self.fd is not None:
                os.rename(tmp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            else:
                os.rename(self.root / tmp, self.root / name)
        except BaseException:
            self.unlink(tmp)
            raise

    def unlink(self, name: str) -> None:
        """Best-effort removal of *name* (the entry itself, never a link target)."""
        _check_component(name)
        with contextlib.suppress(OSError):
            if self.fd is not None:
                os.unlink(name, dir_fd=self.fd)
            else:
                (self.root / name).unlink()


def store_chat_pdf(
    store: AttachmentStore,
    *,
    data: bytes,
    display_name: str,
    timestamp: str,
    inspection: PdfInspection,
    text: str,
    status: str,
) -> list[str]:
    """Write a validated PDF and its sidecars; returns the names written (PDF first).

    All-or-nothing: on failure the files written by this call are removed.
    """
    name = storage_filename(display_name, timestamp=timestamp)
    while store.exists(name):
        name = storage_filename(display_name, timestamp=timestamp)
    meta = build_pdf_meta(display_name, len(data), inspection, text, status)
    written: list[str] = []
    try:
        store.write_new(name, data)
        written.append(name)
        if text:
            store.write_new(name + TEXT_SIDECAR_SUFFIX, text.encode("utf-8"))
            written.append(name + TEXT_SIDECAR_SUFFIX)
        store.write_new(name + META_SIDECAR_SUFFIX, json.dumps(meta, ensure_ascii=False, indent=1).encode("utf-8"))
        written.append(name + META_SIDECAR_SUFFIX)
    except BaseException:
        for written_name in written:
            store.unlink(written_name)
        raise
    return written


def remove_chat_pdf(store: AttachmentStore, name: str) -> None:
    """Remove a stored chat PDF and its sidecars."""
    for candidate in (name, name + TEXT_SIDECAR_SUFFIX, name + META_SIDECAR_SUFFIX):
        store.unlink(candidate)


# ── Anima side: loading ───────────────────────────────────────


@dataclass(frozen=True)
class ChatPdfAttachment:
    """A stored chat PDF as seen from the receiving Anima."""

    rel_path: str
    path: Path
    name: str
    size: int
    pages: int | None
    encrypted: bool
    text_status: str
    text_chars: int
    text_path: Path | None


def _is_link(path: Path) -> bool:
    """Symlink, or a Windows junction (another reparse point that redirects)."""
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def confined_attachments_root(anima_dir: Path) -> Path | None:
    """The real ``attachments/`` directory of *anima_dir*, or None.

    The directory must exist, must not be a symlink and must resolve
    directly inside the (resolved) Anima directory, so one Anima's
    attachments can never alias another's.
    """
    candidate = anima_dir / "attachments"
    try:
        if _is_link(candidate) or not candidate.is_dir():
            return None
        root = candidate.resolve()
        anima_real = anima_dir.resolve()
    except (OSError, RuntimeError):
        return None
    return root if root.parent == anima_real else None


def _confined_file(root: Path, name: str) -> Path | None:
    """A regular, non-symlink file called *name* directly inside *root*."""
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        return None
    path = root / name
    try:
        if _is_link(path) or not path.is_file():
            return None
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return None
    return resolved if resolved.parent == root else None


def confined_attachment_file(anima_dir: Path, name: str) -> Path | None:
    """Resolve ``attachments/<name>`` of *anima_dir* (no symlinks), or None."""
    root = confined_attachments_root(anima_dir)
    return _confined_file(root, name) if root is not None else None


def _resolve_attachment(anima_dir: Path, rel_path: str) -> Path | None:
    """Resolve ``attachments/<name>`` inside *anima_dir* or return None."""
    if not isinstance(rel_path, str) or not rel_path.startswith("attachments/"):
        return None
    return confined_attachment_file(anima_dir, rel_path[len("attachments/") :])


def _sidecar(pdf_path: Path, suffix: str) -> Path | None:
    """Confined sidecar of a (confined) stored PDF, or None."""
    return _confined_file(pdf_path.parent, pdf_path.name + suffix)


_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)


def pdf_storage_supported() -> bool:
    """True where PDFs can be analysed, stored and read without races.

    Needs ``dir_fd`` support (directory-relative opens) and pipe polling
    (bounded subprocess draining): POSIX.  Elsewhere chat PDFs are refused.
    """
    return os.name == "posix" and os.open in os.supports_dir_fd


def open_confined(base_dir: Path, subdir: str, name: str) -> int | None:
    """Open ``base_dir/subdir/name`` read-only, following no symlink in *subdir* or *name*.

    Uses directory-relative opens (``dir_fd``) so swapping *subdir* or the
    file for a symlink between any earlier check and this open cannot
    redirect the read.  Only regular files are accepted (a FIFO cannot block
    the caller thanks to ``O_NONBLOCK``).  Returns an fd the caller must
    close, or None.
    """
    for component in (subdir, name):
        if not component or "/" in component or "\\" in component or component in {".", ".."}:
            return None
    if os.open not in os.supports_dir_fd:
        # e.g. Windows: only a path check is possible, which a junction could
        # race.  Chat PDFs (and their sidecars) fail closed; other files
        # (images) keep the previous best-effort behaviour.
        if ".pdf" in name.lower():
            return None
        path = confined_attachment_file(base_dir, name) if subdir == "attachments" else None
        if path is None:
            return None
        try:
            fd = os.open(path, os.O_RDONLY | _O_BINARY)
        except OSError:
            return None
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
        return fd
    base_fd = root_fd = None
    fd: int | None = None
    try:
        base_fd = os.open(base_dir, os.O_RDONLY | _O_DIRECTORY)
        root_fd = os.open(subdir, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=base_fd)
        fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK, dir_fd=root_fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            fd = None
    except OSError:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        fd = None
    finally:
        for opened in (root_fd, base_fd):
            if opened is not None:
                with contextlib.suppress(OSError):
                    os.close(opened)
    return fd


def _read_confined(pdf_path: Path, name: str, limit: int) -> bytes | None:
    """Read up to *limit* bytes of ``attachments/<name>`` next to a stored PDF.

    *pdf_path* is a confined, resolved ``<anima>/attachments/<file>.pdf``.
    """
    fd = open_confined(pdf_path.parent.parent, "attachments", name)
    if fd is None:
        return None
    with os.fdopen(fd, "rb") as handle:
        return handle.read(limit)


def _read_meta(pdf_path: Path) -> dict[str, Any]:
    try:
        raw = _read_confined(pdf_path, pdf_path.name + META_SIDECAR_SUFFIX, _MAX_META_BYTES)
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _positive_int(value: Any) -> int | None:
    """Return *value* as a positive int, or None (bools are rejected)."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def pdf_display_name(pdf_path: Path) -> str:
    """Original (sanitised) name of a stored chat PDF, for downloads."""
    name = _read_meta(pdf_path).get("name")
    if not isinstance(name, str) or not name:
        name = _fallback_display_name(pdf_path.name)
    return display_name_for_upload(name)


def load_chat_pdf_attachment(anima_dir: Path, rel_path: str) -> ChatPdfAttachment | None:
    """Load one stored PDF attachment, or None when it is not a valid one."""
    if not isinstance(rel_path, str) or not rel_path.lower().endswith(PDF_SUFFIX):
        return None
    path = _resolve_attachment(anima_dir, rel_path)
    if path is None:
        return None
    meta = _read_meta(path)
    text_path = _sidecar(path, TEXT_SIDECAR_SUFFIX)
    has_text = text_path is not None
    size = _positive_int(meta.get("size"))
    if size is None:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
    name = meta.get("name") if isinstance(meta.get("name"), str) else ""
    status = meta.get("text_status")
    return ChatPdfAttachment(
        rel_path=rel_path,
        path=path,
        name=display_name_for_upload(name or _fallback_display_name(path.name)),
        size=size,
        pages=_positive_int(meta.get("pages")),
        encrypted=meta.get("encrypted") is True,
        text_status=status if isinstance(status, str) and status else ("ok" if has_text else "unavailable"),
        text_chars=_positive_int(meta.get("text_chars")) or 0,
        text_path=text_path if has_text else None,
    )


def load_chat_pdf_attachments(anima_dir: Path, attachment_paths: Iterable[str] | None) -> list[ChatPdfAttachment]:
    """Load every PDF among *attachment_paths* (non-PDF paths are skipped)."""
    result: list[ChatPdfAttachment] = []
    seen: set[str] = set()
    for rel_path in attachment_paths or ():
        if rel_path in seen:
            continue
        seen.add(rel_path)
        attachment = load_chat_pdf_attachment(anima_dir, rel_path)
        if attachment is not None:
            result.append(attachment)
    return result


def earlier_thread_pdfs(
    anima_dir: Path,
    turns: Sequence[Any],
    *,
    exclude: Iterable[str] = (),
    limit: int = EARLIER_PDF_LIMIT,
) -> list[ChatPdfAttachment]:
    """Return PDFs attached to earlier turns still in the conversation window.

    Newest first, at most *limit*.  Lets engines that rebuild history each
    turn (and fresh SDK sessions) find a PDF shared a few messages ago.
    """
    excluded = set(exclude)
    found: list[ChatPdfAttachment] = []
    for turn in reversed(list(turns)):
        attachments = getattr(turn, "attachments", None) or []
        for rel_path in reversed(list(attachments)):
            if rel_path in excluded:
                continue
            excluded.add(rel_path)
            attachment = load_chat_pdf_attachment(anima_dir, rel_path)
            if attachment is not None:
                found.append(attachment)
                if len(found) >= limit:
                    return found
    return found


_IMAGE_MEDIA_TYPES = {
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def describe_attachment_paths(anima_dir: Path, attachment_paths: Iterable[str] | None) -> list[dict[str, Any]]:
    """Describe chat attachments for history display (activity log meta).

    PDFs carry their original name and size; images carry their media type.
    Unknown or unsafe paths are dropped.
    """
    entries: list[dict[str, Any]] = []
    for rel_path in attachment_paths or ():
        if not isinstance(rel_path, str):
            continue
        suffix = Path(rel_path).suffix.lower()
        if suffix == PDF_SUFFIX:
            attachment = load_chat_pdf_attachment(anima_dir, rel_path)
            if attachment is None:
                continue
            entry: dict[str, Any] = {
                "path": rel_path,
                "name": attachment.name,
                "media_type": PDF_MEDIA_TYPE,
                "size": attachment.size,
            }
            if attachment.pages:
                entry["pages"] = attachment.pages
            entries.append(entry)
        elif suffix in _IMAGE_MEDIA_TYPES and _resolve_attachment(anima_dir, rel_path) is not None:
            entries.append({"path": rel_path, "media_type": _IMAGE_MEDIA_TYPES[suffix]})
    return entries


# ── Anima side: prompt context ────────────────────────────────


def native_document_eligible(attachment: ChatPdfAttachment) -> bool:
    """Small enough (pages and bytes) to be a native Claude ``document`` block."""
    return (
        not attachment.encrypted
        and attachment.pages is not None
        and 0 < attachment.size <= SDK_DOCUMENT_MAX_BYTES
        and attachment.pages <= SDK_DOCUMENT_MAX_PAGES
    )


def select_sdk_documents(
    attachments: Sequence[ChatPdfAttachment],
    *,
    already_sent_bytes: int = 0,
    already_sent_pages: int = 0,
) -> list[ChatPdfAttachment]:
    """Pick PDFs to send as native Claude ``document`` blocks this turn.

    Per turn at most :data:`SDK_DOCUMENT_TOTAL_MAX_BYTES` /
    :data:`SDK_DOCUMENT_TOTAL_MAX_PAGES`; together with what was already
    sent natively into the same SDK session (*already_sent_bytes* /
    *already_sent_pages*, see :func:`native_usage`) at most
    :data:`SDK_SESSION_DOCUMENT_BUDGET_BYTES` /
    :data:`SDK_SESSION_DOCUMENT_BUDGET_PAGES`.  The rest is delivered as
    extracted text + path like for every other engine.
    """
    chosen: list[ChatPdfAttachment] = []
    total_bytes = 0
    total_pages = 0
    for attachment in attachments:
        if not native_document_eligible(attachment):
            continue
        assert attachment.pages is not None
        if total_bytes + attachment.size > SDK_DOCUMENT_TOTAL_MAX_BYTES:
            continue
        if total_pages + attachment.pages > SDK_DOCUMENT_TOTAL_MAX_PAGES:
            continue
        if already_sent_bytes + total_bytes + attachment.size > SDK_SESSION_DOCUMENT_BUDGET_BYTES:
            continue
        if already_sent_pages + total_pages + attachment.pages > SDK_SESSION_DOCUMENT_BUDGET_PAGES:
            continue
        chosen.append(attachment)
        total_bytes += attachment.size
        total_pages += attachment.pages
    return chosen


UNKNOWN_SESSION = "?"


def _usage_path(anima_dir: Path) -> Path:
    return anima_dir / "state" / NATIVE_USAGE_FILE


def _load_usage(anima_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads(_usage_path(anima_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def native_usage(anima_dir: Path, thread_id: str, session_id: str | None) -> tuple[int, int]:
    """Bytes/pages of PDFs already sent natively into the current SDK session.

    *session_id* is the thread's current Claude SDK chat session (None: a
    fresh session will start, nothing is carried over; ``UNKNOWN_SESSION``:
    could not be determined, keep what was recorded).  A recorded entry for
    a different session no longer applies; an entry recorded while the
    session id was not yet known is attributed to the current session
    (conservative).
    """
    if session_id is None:
        return 0, 0
    entry = _load_usage(anima_dir).get(thread_id)
    if not isinstance(entry, dict):
        return 0, 0
    recorded = entry.get("session_id")
    if session_id != UNKNOWN_SESSION and recorded is not None and recorded != session_id:
        return 0, 0
    return _positive_int(entry.get("bytes")) or 0, _positive_int(entry.get("pages")) or 0


def record_native_usage(
    anima_dir: Path,
    thread_id: str,
    session_id: str | None,
    added_bytes: int,
    added_pages: int,
) -> None:
    """Add PDFs sent natively this turn to the thread's session ledger."""
    used_bytes, used_pages = native_usage(anima_dir, thread_id, session_id)
    data = _load_usage(anima_dir)
    data[thread_id] = {
        "session_id": None if session_id == UNKNOWN_SESSION else session_id,
        "bytes": used_bytes + added_bytes,
        "pages": used_pages + added_pages,
    }
    path = _usage_path(anima_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def format_size(size: int) -> str:
    """Human readable size (``820 B`` / ``12 KB`` / ``3.4 MB``)."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _read_text_excerpt(path: Path, limit: int) -> str:
    """First *limit*+1 characters of a text sidecar (UTF-8, confined read)."""
    try:
        raw = _read_confined(path, path.name, (limit + 1) * 4)
    except OSError:
        return ""
    return (raw or b"").decode("utf-8", "replace")[: limit + 1]


def build_pdf_prompt_context(
    current: Sequence[ChatPdfAttachment],
    *,
    sdk_documents: Sequence[ChatPdfAttachment] = (),
    earlier: Sequence[ChatPdfAttachment] = (),
) -> str:
    """Build the text block appended to the user's message.

    For every PDF of this message: original name, pages, size, absolute
    path and extracted-text path.  PDFs that are not delivered as native
    documents also get an excerpt of their extracted text (shared budget of
    :data:`PROMPT_TEXT_BUDGET_CHARS`).  PDFs from earlier turns are listed
    by path only.  The content is framed as data, not instructions.
    """
    if not current and not earlier:
        return ""
    document_paths = {attachment.path for attachment in sdk_documents}
    lines: list[str] = []
    if current:
        lines.append(t("chat.attachments_header", count=len(current)))
        inline = [a for a in current if a.path not in document_paths and a.text_path is not None]
        per_file = max(_MIN_PER_FILE_TEXT_CHARS, PROMPT_TEXT_BUDGET_CHARS // max(1, len(inline)))
        nonce = secrets.token_hex(3)
        for index, attachment in enumerate(current, 1):
            pages = (
                t("chat.attachments_pages", pages=attachment.pages)
                if attachment.pages
                else t("chat.attachments_pages_unknown")
            )
            lines.append("")
            lines.append(
                t(
                    "chat.attachments_file_line",
                    index=index,
                    name=attachment.name,
                    pages=pages,
                    size=format_size(attachment.size),
                )
            )
            lines.append(t("chat.attachments_saved_path", path=str(attachment.path)))
            if attachment.text_path is not None:
                lines.append(t("chat.attachments_text_path", path=str(attachment.text_path)))
            if attachment.encrypted:
                lines.append(t("chat.attachments_encrypted"))
            if attachment.path in document_paths:
                lines.append(t("chat.attachments_document_attached"))
                continue
            if attachment.text_path is not None:
                excerpt = _read_text_excerpt(attachment.text_path, per_file)
                shown = excerpt[:per_file]
                lines.append(f"----- BEGIN PDF TEXT {nonce} -----")
                lines.append(shown)
                lines.append(f"----- END PDF TEXT {nonce} -----")
                if len(excerpt) > per_file or attachment.text_status == "truncated":
                    lines.append(t("chat.attachments_text_truncated", path=str(attachment.text_path)))
            elif attachment.text_status == "empty":
                lines.append(t("chat.attachments_text_empty", path=str(attachment.path)))
            else:
                lines.append(t("chat.attachments_text_unavailable", path=str(attachment.path)))
    if earlier:
        if lines:
            lines.append("")
        lines.append(t("chat.attachments_earlier_header"))
        for attachment in earlier:
            target = attachment.text_path or attachment.path
            lines.append(t("chat.attachments_earlier_line", name=attachment.name, path=str(target)))
    return "\n".join(lines)


def append_pdf_prompt_context(content: str, context: str) -> str:
    """Append *context* to the user's message (or use it alone)."""
    if not context:
        return content
    if not content.strip():
        return context
    return f"{content}\n\n{context}"


# ── Claude Agent SDK document blocks ──────────────────────────


@dataclass(frozen=True)
class SdkDocument:
    """A PDF published for the Claude Agent SDK query of this chat turn."""

    path: Path
    title: str


_CHAT_SDK_DOCUMENTS: ContextVar[tuple[SdkDocument, ...]] = ContextVar(
    "animaworks_chat_sdk_documents",
    default=(),
)


def set_chat_sdk_documents(documents: Sequence[ChatPdfAttachment]) -> Token[tuple[SdkDocument, ...]]:
    """Publish *documents* for the Claude SDK query of the current chat turn."""
    return _CHAT_SDK_DOCUMENTS.set(tuple(SdkDocument(path=a.path, title=a.name) for a in documents))


def reset_chat_sdk_documents(token: Token[tuple[SdkDocument, ...]]) -> None:
    """Undo :func:`set_chat_sdk_documents` (safe from another context)."""
    try:
        _CHAT_SDK_DOCUMENTS.reset(token)
    except ValueError:
        # An async generator finalised from a different context.
        _CHAT_SDK_DOCUMENTS.set(())


@contextlib.contextmanager
def chat_sdk_documents(documents: Sequence[ChatPdfAttachment]) -> Iterator[None]:
    """Publish *documents* to :func:`chat_sdk_document_blocks` for this turn."""
    token = set_chat_sdk_documents(documents)
    try:
        yield
    finally:
        reset_chat_sdk_documents(token)


def chat_sdk_document_blocks() -> list[dict[str, Any]]:
    """Return Anthropic ``document`` blocks for the PDFs of the current turn.

    Re-validates size and magic at read time; unreadable files are skipped.
    """
    blocks: list[dict[str, Any]] = []
    for document in _CHAT_SDK_DOCUMENTS.get():
        try:
            data = _read_confined(document.path, document.path.name, SDK_DOCUMENT_MAX_BYTES + 1)
        except OSError as exc:
            logger.warning("Skipping chat PDF %s: %s", document.path.name, exc)
            continue
        if data is None:
            logger.warning("Skipping chat PDF %s: not a confined regular file", document.path.name)
            continue
        if len(data) > SDK_DOCUMENT_MAX_BYTES or not looks_like_pdf(data):
            logger.warning("Skipping chat PDF %s: not an eligible PDF", document.path.name)
            continue
        blocks.append(
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": PDF_MEDIA_TYPE,
                    "data": base64.b64encode(data).decode("ascii"),
                },
                "title": document.title[:_MAX_DISPLAY_NAME_CHARS],
            }
        )
    return blocks
