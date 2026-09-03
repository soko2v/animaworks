from __future__ import annotations

# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
"""Validation and text extraction for user-uploaded document attachments.

Attachments come from untrusted browsers.  Everything here is fail-closed:
the extension allowlist, the declared MIME allowlist per extension, magic
bytes, container structure, macro/VBA detection and archive limits are all
enforced on the decoded bytes, never on client-declared metadata.

Office Open XML files (``.docx``/``.xlsx``) are additionally turned into a
plain-text sidecar so an Anima whose tools cannot parse Office formats can
still read the content.  Extraction is a single left-to-right pass over
bounded tokens (no XML parser, no ``.*?`` spanning tags), so it is linear in
the input size and immune to entity expansion.  Extracted text is untrusted
data and must never be treated as instructions.
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


# ── Validation: text / OOXML ───────────────────────────────


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
            if lowered.endswith("vbaproject.bin") or (lowered.endswith(".bin") and "vba" in lowered):
                raise DocumentValidationError("macro", "VBA project present")
        content_types = _read_part(archive, "[Content_Types].xml").lower()
        if b"macroenabled" in content_types or b"vbaproject" in content_types:
            raise DocumentValidationError("macro", "macro-enabled content type")


# ── Validation: OLE2 / Compound File Binary (.doc/.xls) ────

_CFB_MAX_REGULAR_SECTOR = 0xFFFFFFFA  # sector ids >= this are markers (DIFSECT/FATSECT/ENDOFCHAIN/FREESECT)
_CFB_MAX_DIR_SECTORS = 4096
_CFB_MAX_DIFAT_SECTORS = 1024
_CFB_DIR_ENTRY = 128
_CFB_TYPE_STORAGE, _CFB_TYPE_STREAM, _CFB_TYPE_ROOT = 1, 2, 5
_OLE_MACRO_NAMES = frozenset({"macros", "vba", "_vba_project_cur", "_vba_project"})


def _u16(buf: bytes, offset: int) -> int:
    return int.from_bytes(buf[offset : offset + 2], "little")


def _u32(buf: bytes, offset: int) -> int:
    return int.from_bytes(buf[offset : offset + 4], "little")


def _cfb_sector(data: bytes, index: int, sector_size: int) -> bytes:
    if index >= _CFB_MAX_REGULAR_SECTOR:
        raise DocumentValidationError("invalid", "CFB sector id is a marker")
    start = (index + 1) * sector_size
    end = start + sector_size
    if end > len(data):
        raise DocumentValidationError("invalid", "CFB sector beyond end of file")
    return data[start:end]


def _cfb_directory_entries(data: bytes) -> list[tuple[str, int]]:
    """Return ``[(name, object_type), ...]`` for every used directory entry.

    Walks the header -> DIFAT -> FAT -> directory chain of a Compound File
    (MS-CFB).  Any structural inconsistency raises ``invalid`` so a payload
    that merely starts with the OLE magic is not accepted.
    """
    if len(data) < 512 or not data.startswith(_OLE_MAGIC):
        raise DocumentValidationError("invalid", "not an OLE2 compound file")
    major, byte_order, shift = _u16(data, 26), _u16(data, 28), _u16(data, 30)
    if byte_order != 0xFFFE or (major, shift) not in {(3, 9), (4, 12)}:
        raise DocumentValidationError("invalid", "unsupported CFB header")
    sector_size = 1 << shift
    num_fat, first_dir = _u32(data, 44), _u32(data, 48)
    first_difat, num_difat = _u32(data, 68), _u32(data, 72)

    fat_sectors = [s for s in (_u32(data, 76 + 4 * i) for i in range(109)) if s < _CFB_MAX_REGULAR_SECTOR]
    seen: set[int] = set()
    cursor = first_difat
    while cursor < _CFB_MAX_REGULAR_SECTOR:
        if cursor in seen or len(seen) >= min(num_difat, _CFB_MAX_DIFAT_SECTORS):
            raise DocumentValidationError("invalid", "CFB DIFAT chain")
        seen.add(cursor)
        sec = _cfb_sector(data, cursor, sector_size)
        fat_sectors.extend(
            e for e in (_u32(sec, 4 * i) for i in range(sector_size // 4 - 1)) if e < _CFB_MAX_REGULAR_SECTOR
        )
        cursor = _u32(sec, sector_size - 4)
    fat_sectors = fat_sectors[:num_fat]
    if not fat_sectors:
        raise DocumentValidationError("invalid", "CFB has no FAT")
    fat: list[int] = []
    for s in fat_sectors:
        sec = _cfb_sector(data, s, sector_size)
        fat.extend(_u32(sec, 4 * i) for i in range(sector_size // 4))

    entries: list[tuple[str, int]] = []
    seen = set()
    cursor = first_dir
    while cursor < _CFB_MAX_REGULAR_SECTOR:
        if cursor in seen or len(seen) >= _CFB_MAX_DIR_SECTORS:
            raise DocumentValidationError("invalid", "CFB directory chain")
        seen.add(cursor)
        sec = _cfb_sector(data, cursor, sector_size)
        for off in range(0, sector_size, _CFB_DIR_ENTRY):
            entry = sec[off : off + _CFB_DIR_ENTRY]
            etype = entry[66]
            if etype == 0:
                continue
            if etype not in (_CFB_TYPE_STORAGE, _CFB_TYPE_STREAM, _CFB_TYPE_ROOT):
                raise DocumentValidationError("invalid", "CFB directory entry type")
            name_len = _u16(entry, 64)
            if name_len < 2 or name_len > 64 or name_len % 2:
                raise DocumentValidationError("invalid", "CFB directory entry name")
            entries.append((entry[: name_len - 2].decode("utf-16-le", errors="replace"), etype))
        if cursor >= len(fat):
            raise DocumentValidationError("invalid", "CFB FAT truncated")
        cursor = fat[cursor]
    if not entries or entries[0][1] != _CFB_TYPE_ROOT:
        raise DocumentValidationError("invalid", "CFB root entry missing")
    return entries


def _validate_ole(data: bytes) -> None:
    for name, _etype in _cfb_directory_entries(data):
        lowered = name.lower()
        if lowered in _OLE_MACRO_NAMES or lowered.startswith("_vba_project"):
            raise DocumentValidationError("macro", f"VBA storage present: {name}")


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


# ── Text extraction (single pass over bounded tokens) ──────
#
# Every alternative below starts with a literal "<" and is bounded by "[^<>]*"
# / "[^>]*" character classes, so the scanner never re-reads input for an
# unclosed tag: cost is proportional to the XML size regardless of nesting.

_DOCX_TOKEN_RE = re.compile(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>|<w:tab/>|<w:br/>|<w:cr/>|</w:p>|<w:p/>")
_SST_TOKEN_RE = re.compile(r"<si>|</si>|<t(?:\s[^>]*)?>([^<]*)</t>")
_SHEET_TOKEN_RE = re.compile(
    r"<c\b([^>]*?)/>|<c\b([^>]*)>|</c>|</row>|<v>([^<]*)</v>|<t(?:\s[^>]*)?>([^<]*)</t>",
)
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
_WB_SHEET_RE = re.compile(r"<sheet\b([^>]*)/?>")
_RELS_RE = re.compile(r"<Relationship\b([^>]*)/?>")


class _TextBudget:
    """Accumulate output lines while enforcing ``_MAX_TEXT_CHARS``."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.size = 0
        self.truncated = False

    def add(self, line: str) -> bool:
        self.lines.append(line)
        self.size += len(line) + 1
        if self.size > _MAX_TEXT_CHARS:
            self.truncated = True
            return False
        return True

    def render(self) -> str:
        text = "\n".join(self.lines)
        if self.truncated:
            return text[:_MAX_TEXT_CHARS] + _TRUNCATION_MARK
        return text.strip("\n")


def _extract_docx_text(archive: zipfile.ZipFile) -> str:
    xml = _read_part(archive, "word/document.xml").decode("utf-8", errors="replace")
    budget = _TextBudget()
    parts: list[str] = []
    for m in _DOCX_TOKEN_RE.finditer(xml):
        text = m.group(1)
        if text is not None:
            parts.append(unescape(text))
            continue
        token = m.group(0)
        if token == "<w:tab/>":
            parts.append("\t")
        elif token in ("<w:br/>", "<w:cr/>"):
            parts.append("\n")
        else:  # </w:p> or <w:p/>
            if not budget.add("".join(parts)):
                return budget.render()
            parts = []
    if parts:
        budget.add("".join(parts))
    return budget.render()


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    xml = _read_part(archive, "xl/sharedStrings.xml").decode("utf-8", errors="replace")
    strings: list[str] = []
    current: list[str] | None = None
    for m in _SST_TOKEN_RE.finditer(xml):
        token = m.group(0)
        if token == "<si>":
            current = []
        elif token == "</si>":
            strings.append("".join(current or []))
            current = None
        elif current is not None:
            current.append(unescape(m.group(1) or ""))
    return strings


def _xlsx_sheet_order(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    """Return ``[(sheet_name, part_path), ...]`` in workbook order."""
    names = set(archive.namelist())
    rels: dict[str, str] = {}
    if "xl/_rels/workbook.xml.rels" in names:
        rels_xml = _read_part(archive, "xl/_rels/workbook.xml.rels").decode("utf-8", errors="replace")
        for m in _RELS_RE.finditer(rels_xml):
            attrs = dict(_ATTR_RE.findall(m.group(1)))
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
            attrs = dict(_ATTR_RE.findall(m.group(1)))
            rid = attrs.get("r:id") or attrs.get("id", "")
            part = rels.get(rid)
            if part and part in names:
                sheets.append((unescape(attrs.get("name", part)), part))
    if not sheets:
        fallback = sorted(n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        sheets = [(Path(n).stem, n) for n in fallback]
    return sheets


def _xlsx_cell_value(kind: str, raw: str | None, inline: list[str], shared: list[str]) -> str:
    if kind == "inlineStr":
        return "".join(inline)
    if raw is None:
        return ""
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
    budget = _TextBudget()
    for sheet_name, part in _xlsx_sheet_order(archive):
        xml = _read_part(archive, part).decode("utf-8", errors="replace")
        if not budget.add(f"## Sheet: {sheet_name}"):
            return budget.render()
        row: list[str] = []
        kind = ""
        raw: str | None = None
        inline: list[str] = []
        in_cell = False
        for m in _SHEET_TOKEN_RE.finditer(xml):
            token = m.group(0)
            if m.group(1) is not None:  # <c .../> empty cell
                row.append("")
            elif m.group(2) is not None:  # <c ...>
                kind = dict(_ATTR_RE.findall(m.group(2))).get("t", "")
                raw, inline, in_cell = None, [], True
            elif token == "</c>":
                if in_cell:
                    value = _xlsx_cell_value(kind, raw, inline, shared)
                    row.append(value.replace("\t", " ").replace("\n", " "))
                in_cell = False
            elif token == "</row>":
                if not budget.add("\t".join(row).rstrip("\t")):
                    return budget.render()
                row = []
            elif m.group(3) is not None:  # <v>
                if in_cell:
                    raw = unescape(m.group(3))
            elif in_cell:  # <t> inside <is>
                inline.append(unescape(m.group(4) or ""))
        if row and not budget.add("\t".join(row).rstrip("\t")):
            return budget.render()
        budget.add("")
    return budget.render()


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
