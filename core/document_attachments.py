from __future__ import annotations

# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
"""Validation and text extraction for user-uploaded document attachments.

Attachments come from untrusted browsers.  Everything here is fail-closed:
the extension allowlist, the declared MIME allowlist per extension, magic
bytes, macro/VBA detection and archive limits are all enforced on the
decoded bytes, never on client-declared metadata.

Office Open XML files (``.docx``/``.xlsx``) are additionally turned into a
plain-text sidecar so an Anima whose tools cannot parse Office formats can
still read the content.  The extraction is regex based on purpose: no XML
parser is involved, so entity expansion / external entity attacks are not
possible.  Extracted text is untrusted data and must never be treated as
instructions.
"""

import io
import logging
import re
import zipfile
from html import unescape
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Allowlists ─────────────────────────────────────────────

DOCUMENT_MEDIA_TYPES: dict[str, frozenset[str]] = {
    ".pdf": frozenset({"application/pdf"}),
    # Windows browsers report CSV as application/vnd.ms-excel.
    ".csv": frozenset({"text/csv", "application/csv", "application/vnd.ms-excel", "text/plain"}),
    ".txt": frozenset({"text/plain"}),
    ".md": frozenset({"text/markdown", "text/x-markdown", "text/plain"}),
    ".docx": frozenset({"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}),
    ".doc": frozenset({"application/msword"}),
    ".xlsx": frozenset({"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}),
    ".xls": frozenset({"application/vnd.ms-excel"}),
}
"""Allowed *declared* media types per extension. Macro-enabled OOXML (.docm/.xlsm) is intentionally absent."""

CANONICAL_MEDIA_TYPE: dict[str, str] = {
    ".pdf": "application/pdf",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
}

DOCUMENT_SUFFIXES: frozenset[str] = frozenset(DOCUMENT_MEDIA_TYPES)
TEXT_SUFFIXES: frozenset[str] = frozenset({".csv", ".txt", ".md"})
OOXML_SUFFIXES: frozenset[str] = frozenset({".docx", ".xlsx"})
OLE_SUFFIXES: frozenset[str] = frozenset({".doc", ".xls"})

TEXT_SIDECAR_SUFFIX = ".txt"
"""``report.docx`` gets an extracted-text sidecar ``report.docx.txt`` next to it."""

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# OLE2 storage/stream names are stored as UTF-16LE; these mark VBA projects.
_OLE_MACRO_MARKERS: tuple[bytes, ...] = (
    "_VBA_PROJECT".encode("utf-16-le"),
    "Macros".encode("utf-16-le"),
    "VBA".encode("utf-16-le"),
)

_MAX_ZIP_ENTRIES = 4096
_MAX_ZIP_UNCOMPRESSED = 64 * 1024 * 1024  # zip-bomb guard (sum of declared sizes)
_MAX_PART_BYTES = 32 * 1024 * 1024  # single XML part read cap
_MAX_TEXT_CHARS = 200_000  # extracted sidecar cap
_TRUNCATION_MARK = "\n\n[... text truncated by AnimaWorks: document exceeds extraction limit ...]\n"


class DocumentValidationError(ValueError):
    """Raised when decoded attachment bytes fail validation.

    ``code`` is one of ``"unsupported"``, ``"invalid"``, ``"macro"``, ``"encoding"``.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


def normalize_suffix(name: str) -> str:
    """Return the lower-cased suffix of *name* (``""`` when absent)."""
    return Path(name).suffix.lower()


def is_allowed_media_type(suffix: str, media_type: str) -> bool:
    """Return whether the client-declared *media_type* is acceptable for *suffix*."""
    allowed = DOCUMENT_MEDIA_TYPES.get(suffix)
    return allowed is not None and media_type.lower() in allowed


# ── Validation ─────────────────────────────────────────────


def _validate_text_bytes(data: bytes) -> None:
    if b"\x00" in data:
        raise DocumentValidationError("invalid", "NUL byte in text file")
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DocumentValidationError("encoding", "text file is not UTF-8") from exc


def _open_ooxml(data: bytes) -> zipfile.ZipFile:
    if not data.startswith(_ZIP_MAGIC):
        raise DocumentValidationError("invalid", "not a ZIP container")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise DocumentValidationError("invalid", "corrupt ZIP container") from exc
    infos = archive.infolist()
    if len(infos) > _MAX_ZIP_ENTRIES:
        raise DocumentValidationError("invalid", "too many ZIP entries")
    total = 0
    for info in infos:
        name = info.filename
        if name.startswith(("/", "\\")) or ".." in name.split("/") or ".." in name.split("\\"):
            raise DocumentValidationError("invalid", "unsafe ZIP entry name")
        total += max(info.file_size, 0)
        if total > _MAX_ZIP_UNCOMPRESSED:
            raise DocumentValidationError("invalid", "ZIP expands beyond limit")
    return archive


def _read_part(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        with archive.open(name) as fh:
            chunk = fh.read(_MAX_PART_BYTES + 1)
    except (KeyError, zipfile.BadZipFile, OSError) as exc:
        raise DocumentValidationError("invalid", f"cannot read {name}") from exc
    if len(chunk) > _MAX_PART_BYTES:
        raise DocumentValidationError("invalid", f"{name} exceeds part limit")
    return chunk


def _validate_ooxml(data: bytes, suffix: str) -> None:
    with _open_ooxml(data) as archive:
        names = set(archive.namelist())
        if "[Content_Types].xml" not in names:
            raise DocumentValidationError("invalid", "missing [Content_Types].xml")
        required = "word/document.xml" if suffix == ".docx" else "xl/workbook.xml"
        if required not in names:
            raise DocumentValidationError("invalid", f"missing {required}")
        for name in names:
            lowered = name.lower()
            if lowered.endswith("vbaproject.bin") or lowered.endswith(".bin") and "vba" in lowered:
                raise DocumentValidationError("macro", "VBA project present")
        content_types = _read_part(archive, "[Content_Types].xml").lower()
        if b"macroenabled" in content_types or b"vbaproject" in content_types:
            raise DocumentValidationError("macro", "macro-enabled content type")


def _validate_ole(data: bytes) -> None:
    if not data.startswith(_OLE_MAGIC):
        raise DocumentValidationError("invalid", "not an OLE2 compound file")
    if any(marker in data for marker in _OLE_MACRO_MARKERS):
        raise DocumentValidationError("macro", "VBA storage present")


def validate_document_bytes(data: bytes, suffix: str) -> None:
    """Validate decoded attachment bytes against *suffix*.

    Raises:
        DocumentValidationError: on any mismatch. Callers map ``code`` to a
            user-facing message.
    """
    if suffix not in DOCUMENT_SUFFIXES:
        raise DocumentValidationError("unsupported", f"suffix {suffix!r} not allowed")
    if suffix == ".pdf":
        if not data.startswith(_PDF_MAGIC):
            raise DocumentValidationError("invalid", "missing %PDF- header")
        return
    if suffix in TEXT_SUFFIXES:
        _validate_text_bytes(data)
        return
    if suffix in OOXML_SUFFIXES:
        _validate_ooxml(data, suffix)
        return
    if suffix in OLE_SUFFIXES:
        _validate_ole(data)
        return
    raise DocumentValidationError("unsupported", f"no validator for {suffix!r}")


# ── Text extraction (regex based, no XML parser) ───────────

_TAG_RE = re.compile(r"<[^>]+>")
_DOCX_PARA_RE = re.compile(r"<w:p[ >].*?</w:p>|<w:p/>", re.DOTALL)
_DOCX_RUN_TEXT_RE = re.compile(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>|<w:tab/>|<w:br/>|<w:cr/>", re.DOTALL)
_XLSX_SI_RE = re.compile(r"<si>(.*?)</si>", re.DOTALL)
_XLSX_T_RE = re.compile(r"<t(?:\s[^>]*)?>(.*?)</t>", re.DOTALL)
_XLSX_ROW_RE = re.compile(r"<row[ >].*?</row>", re.DOTALL)
_XLSX_CELL_RE = re.compile(r"<c\b([^>]*?)(?:/>|>(.*?)</c>)", re.DOTALL)
_XLSX_V_RE = re.compile(r"<v>(.*?)</v>", re.DOTALL)
_XLSX_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
_WB_SHEET_RE = re.compile(r"<sheet\b([^>]*)/?>")
_RELS_RE = re.compile(r"<Relationship\b([^>]*)/?>")


def _clean(fragment: str) -> str:
    return unescape(_TAG_RE.sub("", fragment))


def _extract_docx_text(archive: zipfile.ZipFile) -> str:
    xml = _read_part(archive, "word/document.xml").decode("utf-8", errors="replace")
    lines: list[str] = []
    size = 0
    for para in _DOCX_PARA_RE.finditer(xml):
        parts: list[str] = []
        for m in _DOCX_RUN_TEXT_RE.finditer(para.group(0)):
            token = m.group(0)
            if token.startswith("<w:tab"):
                parts.append("\t")
            elif token.startswith(("<w:br", "<w:cr")):
                parts.append("\n")
            else:
                parts.append(unescape(m.group(1) or ""))
        line = "".join(parts)
        lines.append(line)
        size += len(line) + 1
        if size > _MAX_TEXT_CHARS:
            return "\n".join(lines)[:_MAX_TEXT_CHARS] + _TRUNCATION_MARK
    return "\n".join(lines).strip("\n")


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    xml = _read_part(archive, "xl/sharedStrings.xml").decode("utf-8", errors="replace")
    return ["".join(unescape(t) for t in _XLSX_T_RE.findall(si)) for si in _XLSX_SI_RE.findall(xml)]


def _xlsx_sheet_order(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    """Return ``[(sheet_name, part_path), ...]`` in workbook order."""
    names = set(archive.namelist())
    rels: dict[str, str] = {}
    if "xl/_rels/workbook.xml.rels" in names:
        rels_xml = _read_part(archive, "xl/_rels/workbook.xml.rels").decode("utf-8", errors="replace")
        for m in _RELS_RE.finditer(rels_xml):
            attrs = dict(_XLSX_ATTR_RE.findall(m.group(1)))
            target = attrs.get("Target", "")
            if not target:
                continue
            target = target.lstrip("/")
            if not target.startswith("xl/"):
                target = f"xl/{target}"
            rels[attrs.get("Id", "")] = target
    sheets: list[tuple[str, str]] = []
    if "xl/workbook.xml" in names:
        wb_xml = _read_part(archive, "xl/workbook.xml").decode("utf-8", errors="replace")
        for m in _WB_SHEET_RE.finditer(wb_xml):
            attrs = dict(_XLSX_ATTR_RE.findall(m.group(1)))
            rid = attrs.get("r:id") or attrs.get("id", "")
            part = rels.get(rid)
            if part and part in names:
                sheets.append((unescape(attrs.get("name", part)), part))
    if not sheets:
        fallback = sorted(n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        sheets = [(Path(n).stem, n) for n in fallback]
    return sheets


def _xlsx_cell_value(attrs: str, inner: str | None, shared: list[str]) -> str:
    parsed = dict(_XLSX_ATTR_RE.findall(attrs))
    kind = parsed.get("t", "")
    inner = inner or ""
    if kind == "inlineStr":
        return "".join(unescape(t) for t in _XLSX_T_RE.findall(inner))
    v = _XLSX_V_RE.search(inner)
    if v is None:
        return ""
    raw = unescape(v.group(1))
    if kind == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    if kind == "b":
        return "TRUE" if raw.strip() == "1" else "FALSE"
    return raw


def _extract_xlsx_text(archive: zipfile.ZipFile) -> str:
    shared = _xlsx_shared_strings(archive)
    chunks: list[str] = []
    size = 0
    for sheet_name, part in _xlsx_sheet_order(archive):
        xml = _read_part(archive, part).decode("utf-8", errors="replace")
        chunks.append(f"## Sheet: {sheet_name}")
        for row in _XLSX_ROW_RE.finditer(xml):
            cells = [
                _xlsx_cell_value(m.group(1), m.group(2), shared).replace("\t", " ").replace("\n", " ")
                for m in _XLSX_CELL_RE.finditer(row.group(0))
            ]
            line = "\t".join(cells).rstrip("\t")
            chunks.append(line)
            size += len(line) + 1
            if size > _MAX_TEXT_CHARS:
                return "\n".join(chunks)[:_MAX_TEXT_CHARS] + _TRUNCATION_MARK
        chunks.append("")
    return "\n".join(chunks).strip("\n")


def extract_document_text(data: bytes, suffix: str) -> str | None:
    """Return plain text for ``.docx``/``.xlsx`` bytes, ``None`` for other formats.

    ``data`` must already have passed :func:`validate_document_bytes`.
    Legacy binary Office formats (``.doc``/``.xls``) and PDF are stored as-is
    without a sidecar.
    """
    if suffix not in OOXML_SUFFIXES:
        return None
    try:
        with _open_ooxml(data) as archive:
            return _extract_docx_text(archive) if suffix == ".docx" else _extract_xlsx_text(archive)
    except DocumentValidationError:
        logger.warning("document text extraction skipped for %s: container rejected", suffix)
        return None


def text_sidecar_path(document: Path) -> Path:
    """Return the sidecar path holding extracted text for *document*."""
    return document.with_name(document.name + TEXT_SIDECAR_SUFFIX)
