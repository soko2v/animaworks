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
import zlib
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
_MAX_XLSX_SHEETS = 1024  # distinct worksheet parts read per workbook
_MAX_SHEET_NAME_CHARS = 255  # Excel itself allows 31; anything longer is hostile
_ZIP_FLAG_ENCRYPTED = 0x1  # general purpose bit 0: entry is password protected
_ZIP_ALLOWED_COMPRESSION = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})  # all Office writes
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
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError, ValueError) as exc:
        # UnicodeDecodeError: a central-directory name flagged UTF-8 that is
        # not valid UTF-8; ValueError: malformed zip64 / extra-field records.
        raise DocumentValidationError("invalid", "corrupt ZIP container") from exc
    infos = archive.infolist()
    if len(infos) > _MAX_ZIP_ENTRIES:
        raise DocumentValidationError("invalid", "too many ZIP entries")
    total = 0
    for info in infos:
        name = info.filename
        if name.startswith(("/", "\\")) or ".." in name.split("/") or ".." in name.split("\\"):
            raise DocumentValidationError("invalid", "unsafe ZIP entry name")
        # Per-entry encryption / exotic compression would only surface as a
        # RuntimeError / NotImplementedError at extraction time, after the
        # file had been accepted; refuse them while validating instead.
        if info.flag_bits & _ZIP_FLAG_ENCRYPTED:
            raise DocumentValidationError("invalid", f"encrypted ZIP entry {name}")
        if info.compress_type not in _ZIP_ALLOWED_COMPRESSION:
            raise DocumentValidationError("invalid", f"unsupported compression for {name}")
        total += max(info.file_size, 0)
        if total > _MAX_ZIP_UNCOMPRESSED:
            raise DocumentValidationError("invalid", "ZIP expands beyond limit")
    return archive


def _read_part(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        with archive.open(name) as fh:
            chunk = fh.read(_MAX_PART_BYTES + 1)
    except (
        KeyError,
        zipfile.BadZipFile,
        OSError,
        RuntimeError,
        NotImplementedError,
        zlib.error,
        EOFError,
        UnicodeDecodeError,
        ValueError,
    ) as exc:
        # RuntimeError: encrypted entry; NotImplementedError: compression
        # method; zlib.error / EOFError: corrupt deflate stream;
        # UnicodeDecodeError / ValueError: malformed local header.
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
#
# A minimal MS-CFB reader: header -> DIFAT -> FAT -> directory, plus the mini
# FAT / mini stream for streams below the cutoff.  Every count the file
# declares is checked against the physical file size, every chain is
# loop-guarded and every sector id is bounds-checked before it is dereferenced,
# so a hostile container cannot make the reader allocate more than a small
# multiple of the upload size.  Any inconsistency raises ``invalid``.

_CFB_MAX_REGULAR_SECTOR = 0xFFFFFFFA  # ids >= this are markers (DIFSECT/FATSECT/ENDOFCHAIN/FREESECT)
_CFB_DIFSECT = 0xFFFFFFFC
_CFB_FATSECT = 0xFFFFFFFD
_CFB_MAX_DIR_SECTORS = 4096
_CFB_MAX_DIFAT_SECTORS = 1024
_CFB_DIR_ENTRY = 128
_CFB_HEADER_DIFAT_SLOTS = 109
_CFB_MINI_SECTOR = 64
_CFB_MAX_STREAM_SIZE_V3 = 0x80000000  # MS-CFB 2.6.1: version 3 stream sizes are 32-bit
_CFB_TYPE_STORAGE, _CFB_TYPE_STREAM, _CFB_TYPE_ROOT = 1, 2, 5
_OLE_MACRO_NAMES = frozenset({"macros", "vba", "_vba_project_cur", "_vba_project"})

# Excel binary workbook (BIFF5/BIFF8) records that reveal macro sheets even
# when no VBA storage exists (Excel 4.0 / XLM macros live in the Workbook stream).
_BIFF_BOF = 0x0809
_BIFF_BOUNDSHEET = 0x0085
_BIFF_FILEPASS = 0x002F  # workbook is encrypted from this record on: macro checks would be blind
_BIFF_BOF_MACRO_TYPES = frozenset({0x0006, 0x0040})  # VB module, Excel 4.0 macro sheet
_BIFF_BOUNDSHEET_MACRO_TYPES = frozenset({0x01, 0x06})  # macro sheet, VB module
# Word binary File Information Block: Word 6/95 files carry WordBasic macros
# outside any VBA storage, so only Word 97+ (nFib >= 0x00C1) is accepted.
_WORD_FIB_IDENT = 0xA5EC
_WORD_FIB_MIN_NFIB = 0x00C1


def _u16(buf: bytes, offset: int) -> int:
    return int.from_bytes(buf[offset : offset + 2], "little")


def _u32(buf: bytes, offset: int) -> int:
    return int.from_bytes(buf[offset : offset + 4], "little")


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


class _CfbEntry:
    __slots__ = ("name", "object_type", "size", "start")

    def __init__(self, name: str, object_type: int, start: int, size: int) -> None:
        self.name = name
        self.object_type = object_type
        self.start = start
        self.size = size


class _CompoundFile:
    """Read-only, bounds-checked view of a Compound File Binary payload."""

    def __init__(self, data: bytes) -> None:
        if len(data) < 512 or not data.startswith(_OLE_MAGIC):
            raise DocumentValidationError("invalid", "not an OLE2 compound file")
        major, byte_order = _u16(data, 26), _u16(data, 28)
        shift, mini_shift = _u16(data, 30), _u16(data, 32)
        if byte_order != 0xFFFE or (major, shift) not in {(3, 9), (4, 12)} or mini_shift != 6:
            raise DocumentValidationError("invalid", "unsupported CFB header")
        self.data = data
        self.major = major
        self.sector_size = 1 << shift
        # Sectors physically present after the header sector; every sector id
        # used anywhere in the file must be below this.
        self.sector_count = len(data) // self.sector_size - 1
        if self.sector_count < 1:
            raise DocumentValidationError("invalid", "CFB has no sectors")
        self.mini_cutoff = _u32(data, 56)
        self.fat = self._load_fat()
        self.entries = self._load_directory()
        self._mini_fat: list[int] | None = None
        self._mini_stream: bytes | None = None

    # ── sectors / chains ──

    def _sector(self, index: int) -> bytes:
        if index >= self.sector_count:  # also rejects marker values
            raise DocumentValidationError("invalid", "CFB sector beyond end of file")
        start = (index + 1) * self.sector_size
        return self.data[start : start + self.sector_size]

    @staticmethod
    def _next(fat: list[int], index: int) -> int:
        if index >= len(fat):
            raise DocumentValidationError("invalid", "CFB FAT truncated")
        return fat[index]

    def _read_chain(self, fat: list[int], start: int, size: int, sector_fn, sector_size: int) -> bytes:
        """Return the first *size* bytes of the chain beginning at *start*."""
        if size <= 0:
            return b""
        needed = _ceil_div(size, sector_size)
        out = bytearray()
        seen: set[int] = set()
        cursor = start
        while True:
            if cursor >= _CFB_MAX_REGULAR_SECTOR:
                raise DocumentValidationError("invalid", "CFB chain shorter than stream size")
            if cursor in seen or len(seen) >= needed:
                raise DocumentValidationError("invalid", "CFB chain loop")
            seen.add(cursor)
            out += sector_fn(cursor)
            if len(out) >= size:
                return bytes(out[:size])
            cursor = self._next(fat, cursor)

    # ── FAT / DIFAT ──

    def _load_fat(self) -> list[int]:
        data, sector_size = self.data, self.sector_size
        per_sector = sector_size // 4
        num_fat, first_difat, num_difat = _u32(data, 44), _u32(data, 68), _u32(data, 72)
        # One FAT sector describes `per_sector` sectors; allow one spare sector
        # for writers that pre-allocate, nothing beyond what the file could hold.
        max_fat = _ceil_div(self.sector_count, per_sector) + 1
        if num_fat < 1 or num_fat > max_fat:
            raise DocumentValidationError("invalid", "CFB FAT count inconsistent with file size")
        needed_difat = _ceil_div(max(num_fat - _CFB_HEADER_DIFAT_SLOTS, 0), per_sector - 1)
        max_difat = min(needed_difat + 1, _CFB_MAX_DIFAT_SECTORS, self.sector_count)
        if num_difat < needed_difat or num_difat > max_difat:
            raise DocumentValidationError("invalid", "CFB DIFAT count inconsistent with FAT count")

        fat_sectors = [
            s for s in (_u32(data, 76 + 4 * i) for i in range(_CFB_HEADER_DIFAT_SLOTS)) if s < _CFB_MAX_REGULAR_SECTOR
        ]
        difat_sectors: list[int] = []
        difat_seen: set[int] = set()
        cursor = first_difat
        while cursor < _CFB_MAX_REGULAR_SECTOR:
            if cursor in difat_seen or len(difat_sectors) >= num_difat:
                raise DocumentValidationError("invalid", "CFB DIFAT chain longer than declared")
            difat_seen.add(cursor)
            difat_sectors.append(cursor)
            sec = self._sector(cursor)
            fat_sectors.extend(
                e for e in (_u32(sec, 4 * i) for i in range(per_sector - 1)) if e < _CFB_MAX_REGULAR_SECTOR
            )
            cursor = _u32(sec, sector_size - 4)
        if len(difat_sectors) != num_difat:
            raise DocumentValidationError("invalid", "CFB DIFAT chain shorter than declared")
        if len(fat_sectors) < num_fat:
            raise DocumentValidationError("invalid", "CFB DIFAT lists fewer FAT sectors than declared")
        fat_sectors = fat_sectors[:num_fat]
        fat_set = set(fat_sectors)
        if len(fat_set) != num_fat or fat_set & difat_seen:
            raise DocumentValidationError("invalid", "CFB FAT/DIFAT sector ids repeat")

        fat: list[int] = []
        for s in fat_sectors:
            sec = self._sector(s)
            fat.extend(_u32(sec, 4 * i) for i in range(per_sector))
        # Sector roles: FAT sectors are marked FATSECT and DIFAT sectors DIFSECT.
        for s in fat_sectors:
            if self._next(fat, s) != _CFB_FATSECT:
                raise DocumentValidationError("invalid", "CFB FAT sector not marked FATSECT")
        for s in difat_sectors:
            if self._next(fat, s) != _CFB_DIFSECT:
                raise DocumentValidationError("invalid", "CFB DIFAT sector not marked DIFSECT")
        return fat

    # ── directory ──

    def _load_directory(self) -> list[_CfbEntry]:
        # MS-CFB 2.2: "Number of Directory Sectors" MUST be zero for version 3
        # and, for version 4, MUST match the directory chain actually present.
        declared_dir = _u32(self.data, 40)
        if self.major == 3 and declared_dir != 0:
            raise DocumentValidationError("invalid", "CFB v3 directory sector count must be zero")
        if self.major == 4 and (declared_dir < 1 or declared_dir > min(_CFB_MAX_DIR_SECTORS, self.sector_count)):
            raise DocumentValidationError("invalid", "CFB directory sector count inconsistent with file size")
        entries: list[_CfbEntry] = []
        seen: set[int] = set()
        cursor = _u32(self.data, 48)
        while cursor < _CFB_MAX_REGULAR_SECTOR:
            if cursor in seen or len(seen) >= _CFB_MAX_DIR_SECTORS:
                raise DocumentValidationError("invalid", "CFB directory chain")
            seen.add(cursor)
            sec = self._sector(cursor)
            for off in range(0, self.sector_size, _CFB_DIR_ENTRY):
                entry = sec[off : off + _CFB_DIR_ENTRY]
                etype = entry[66]
                if etype == 0:
                    continue
                if etype not in (_CFB_TYPE_STORAGE, _CFB_TYPE_STREAM, _CFB_TYPE_ROOT):
                    raise DocumentValidationError("invalid", "CFB directory entry type")
                name_len = _u16(entry, 64)
                if name_len < 2 or name_len > 64 or name_len % 2:
                    raise DocumentValidationError("invalid", "CFB directory entry name")
                name = entry[: name_len - 2].decode("utf-16-le", errors="replace")
                size = self._entry_size(entry)
                if etype != _CFB_TYPE_STORAGE and size > len(self.data):
                    raise DocumentValidationError("invalid", "CFB stream larger than file")
                entries.append(_CfbEntry(name, etype, _u32(entry, 116), size))
            cursor = self._next(self.fat, cursor)
        if self.major == 4 and declared_dir != len(seen):
            raise DocumentValidationError("invalid", "CFB directory sector count does not match chain")
        if not entries or entries[0].object_type != _CFB_TYPE_ROOT:
            raise DocumentValidationError("invalid", "CFB root entry missing")
        return entries

    def _entry_size(self, entry: bytes) -> int:
        """Return the declared stream size of a directory *entry* (64-bit for v4, 32-bit for v3)."""
        low, high = _u32(entry, 120), _u32(entry, 124)
        if self.major == 4:
            return low | (high << 32)
        # MS-CFB 2.6.1: version 3 sizes must not exceed 2 GiB and older writers
        # left the high dword uninitialised, so only the low dword is meaningful.
        if low > _CFB_MAX_STREAM_SIZE_V3:
            raise DocumentValidationError("invalid", "CFB v3 stream size exceeds 2 GiB")
        return low

    # ── streams ──

    def find_stream(self, *names: str) -> _CfbEntry | None:
        wanted = {n.lower() for n in names}
        for entry in self.entries:
            if entry.object_type == _CFB_TYPE_STREAM and entry.name.lower() in wanted:
                return entry
        return None

    def _load_mini_stream(self) -> bytes:
        if self._mini_stream is None:
            root = self.entries[0]
            size = min(root.size, len(self.data))
            self._mini_stream = self._read_chain(self.fat, root.start, size, self._sector, self.sector_size)
        return self._mini_stream

    def _load_mini_fat(self) -> list[int]:
        if self._mini_fat is None:
            first, count = _u32(self.data, 60), _u32(self.data, 64)
            if count > self.sector_count:
                raise DocumentValidationError("invalid", "CFB mini FAT count inconsistent with file size")
            raw = self._read_chain(self.fat, first, count * self.sector_size, self._sector, self.sector_size)
            self._mini_fat = [_u32(raw, 4 * i) for i in range(len(raw) // 4)]
        return self._mini_fat

    def read_stream(self, entry: _CfbEntry, limit: int) -> bytes:
        """Return the first ``min(entry.size, limit)`` bytes of *entry*."""
        size = min(entry.size, limit)
        if entry.size >= self.mini_cutoff:
            return self._read_chain(self.fat, entry.start, size, self._sector, self.sector_size)
        mini_stream = self._load_mini_stream()

        def mini_sector(index: int) -> bytes:
            start = index * _CFB_MINI_SECTOR
            if start + _CFB_MINI_SECTOR > len(mini_stream):
                raise DocumentValidationError("invalid", "CFB mini sector beyond mini stream")
            return mini_stream[start : start + _CFB_MINI_SECTOR]

        return self._read_chain(self._load_mini_fat(), entry.start, size, mini_sector, _CFB_MINI_SECTOR)


def _scan_biff_for_macros(stream: bytes) -> None:
    """Walk the BIFF record stream and reject macro sheets / VB modules.

    Encrypted workbooks (``FILEPASS``) are refused outright: every record after
    it is ciphertext, so BOUNDSHEET / substream BOF types could not be checked.
    """
    if len(stream) < 4 or _u16(stream, 0) != _BIFF_BOF:
        raise DocumentValidationError("invalid", "Workbook stream does not start with BOF")
    offset = 0
    while offset + 4 <= len(stream):
        rid, rlen = _u16(stream, offset), _u16(stream, offset + 2)
        body = offset + 4
        if body + rlen > len(stream):
            raise DocumentValidationError("invalid", "truncated BIFF record")
        if rid == _BIFF_FILEPASS:
            raise DocumentValidationError("unsupported", "password-protected Excel workbooks are not accepted")
        if rid == _BIFF_BOF and rlen >= 4 and _u16(stream, body + 2) in _BIFF_BOF_MACRO_TYPES:
            raise DocumentValidationError("macro", "Excel macro sheet / VB module substream")
        if rid == _BIFF_BOUNDSHEET and rlen >= 6 and stream[body + 5] in _BIFF_BOUNDSHEET_MACRO_TYPES:
            raise DocumentValidationError("macro", "Excel macro sheet in BOUNDSHEET")
        offset = body + rlen


def _validate_xls(cfb: _CompoundFile) -> None:
    entry = cfb.find_stream("Workbook") or cfb.find_stream("Book")
    if entry is None:
        raise DocumentValidationError("invalid", "no Workbook stream")
    _scan_biff_for_macros(cfb.read_stream(entry, len(cfb.data)))


def _validate_doc(cfb: _CompoundFile) -> None:
    entry = cfb.find_stream("WordDocument")
    if entry is None:
        raise DocumentValidationError("invalid", "no WordDocument stream")
    fib = cfb.read_stream(entry, 32)
    if len(fib) < 4 or _u16(fib, 0) != _WORD_FIB_IDENT:
        raise DocumentValidationError("invalid", "WordDocument stream has no FIB")
    if _u16(fib, 2) < _WORD_FIB_MIN_NFIB:
        raise DocumentValidationError("unsupported", "Word 6/95 binary documents are not accepted")


def _validate_ole(data: bytes, suffix: str) -> None:
    cfb = _CompoundFile(data)
    for entry in cfb.entries:
        lowered = entry.name.lower()
        if lowered in _OLE_MACRO_NAMES or lowered.startswith("_vba_project"):
            raise DocumentValidationError("macro", f"VBA storage present: {entry.name}")
    if suffix == ".xls":
        _validate_xls(cfb)
    else:
        _validate_doc(cfb)


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
        _validate_ole(data, suffix)
        return
    raise DocumentValidationError("unsupported", f"no validator for {suffix!r}")


# ── Text extraction (single pass over bounded tokens) ──────
#
# Every alternative below starts with a literal "<" and its attribute / text
# portion is a "[^<>]*" or "[^<]*" class, so an attempt that starts at one "<"
# can never scan past the next "<": unclosed tags, long attribute lists and
# repeated prefixes all cost time proportional to the distance to the next
# tag, keeping the whole scan linear in the part size.
#
# The attribute scanner is anchored with "\b" so that a run of word characters
# is attempted once from its first character only; without the anchor every
# position inside the run restarts "\w+" and the scan is quadratic (a 32 MiB
# attribute list would take hours).

_DOCX_TOKEN_RE = re.compile(r"<w:t(?:\s[^<>]*)?>([^<]*)</w:t>|<w:tab/>|<w:br/>|<w:cr/>|</w:p>|<w:p/>")
_SST_TOKEN_RE = re.compile(r"<si>|</si>|<t(?:\s[^<>]*)?>([^<]*)</t>")
_SHEET_TOKEN_RE = re.compile(r"<c\b([^<>]*)>|</c>|</row>|<v>([^<]*)</v>|<t(?:\s[^<>]*)?>([^<]*)</t>")
_ATTR_RE = re.compile(r'\b(\w+)="([^"]*)"')
_WB_SHEET_RE = re.compile(r"<sheet\b([^<>]*)>")
_RELS_RE = re.compile(r"<Relationship\b([^<>]*)>")

_MAX_SHARED_STRINGS = _MAX_TEXT_CHARS  # more strings than output chars can never all be rendered


class _TextBudget:
    """Accumulate output lines while enforcing ``_MAX_TEXT_CHARS``.

    ``reserve`` charges characters *before* they are buffered for the line
    under construction so an unclosed paragraph/row cannot grow without bound
    and a rejected value is never retained; ``fits`` is the non-charging check
    for a value that is held transiently; ``add`` commits a line.
    """

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.size = 0
        self.pending = 0
        self.truncated = False

    def fits(self, chars: int) -> bool:
        if self.size + self.pending + chars > _MAX_TEXT_CHARS:
            self.truncated = True
            return False
        return True

    def reserve(self, chars: int) -> bool:
        if not self.fits(chars):
            return False
        self.pending += chars
        return True

    def add(self, line: str) -> bool:
        self.pending = 0
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
            text = unescape(text)
            if not budget.reserve(len(text)):
                break
            parts.append(text)
            continue
        token = m.group(0)
        if token == "<w:tab/>":
            separator = "\t"
        elif token in ("<w:br/>", "<w:cr/>"):
            separator = "\n"
        else:  # </w:p> or <w:p/>
            if not budget.add("".join(parts)):
                return budget.render()
            parts = []
            continue
        if not budget.reserve(1):
            break
        parts.append(separator)
    if parts:
        budget.add("".join(parts))
    return budget.render()


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> tuple[list[str], bool]:
    """Return ``(strings, truncated)``; parsing stops once the table exceeds the output budget."""
    if "xl/sharedStrings.xml" not in archive.namelist():
        return [], False
    xml = _read_part(archive, "xl/sharedStrings.xml").decode("utf-8", errors="replace")
    strings: list[str] = []
    current: list[str] | None = None
    stored = 0
    for m in _SST_TOKEN_RE.finditer(xml):
        token = m.group(0)
        if token == "<si>":
            current = []
        elif token == "</si>":
            strings.append("".join(current or []))
            current = None
            if len(strings) >= _MAX_SHARED_STRINGS:
                return strings, True
        elif current is not None:
            text = unescape(m.group(1) or "")
            stored += len(text)
            if stored > _MAX_TEXT_CHARS:
                return strings, True
            current.append(text)
    return strings, False


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
    # Each worksheet part is read at most once (a workbook may repeat an r:id
    # any number of times), the sheet count is capped and sheet names are
    # bounded before they are retained, so extraction work stays proportional
    # to the archive's distinct parts rather than to the workbook XML.
    sheets: list[tuple[str, str]] = []
    seen_parts: set[str] = set()
    if "xl/workbook.xml" in names:
        wb_xml = _read_part(archive, "xl/workbook.xml").decode("utf-8", errors="replace")
        for m in _WB_SHEET_RE.finditer(wb_xml):
            if len(sheets) >= _MAX_XLSX_SHEETS:
                break
            attrs = dict(_ATTR_RE.findall(m.group(1)))
            rid = attrs.get("r:id") or attrs.get("id", "")
            part = rels.get(rid)
            if not part or part not in names or part in seen_parts:
                continue
            seen_parts.add(part)
            name = unescape(attrs.get("name", part)[:_MAX_SHEET_NAME_CHARS])[:_MAX_SHEET_NAME_CHARS]
            sheets.append((name, part))
    if not sheets:
        fallback = sorted(n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        sheets = [(Path(n).stem, n) for n in fallback[:_MAX_XLSX_SHEETS]]
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
    shared, shared_truncated = _xlsx_shared_strings(archive)
    budget = _TextBudget()
    budget.truncated = shared_truncated
    for sheet_name, part in _xlsx_sheet_order(archive):
        header = f"## Sheet: {sheet_name}"
        if not budget.reserve(len(header)):
            return budget.render()
        xml = _read_part(archive, part).decode("utf-8", errors="replace")
        if not budget.add(header):
            return budget.render()
        row: list[str] = []
        kind = ""
        raw: str | None = None
        inline: list[str] = []
        in_cell = False
        for m in _SHEET_TOKEN_RE.finditer(xml):
            token = m.group(0)
            attrs = m.group(1)
            if attrs is not None:  # <c ...> or <c .../>
                if attrs.rstrip().endswith("/"):
                    if not budget.reserve(1):
                        break
                    row.append("")
                    in_cell = False
                    continue
                kind = dict(_ATTR_RE.findall(attrs)).get("t", "")
                raw, inline, in_cell = None, [], True
            elif token == "</c>":
                if in_cell:
                    value = _xlsx_cell_value(kind, raw, inline, shared)
                    # Inline-string text was charged as it was buffered; every
                    # other kind is charged exactly once here, plus the separator.
                    if not budget.reserve(1 if kind == "inlineStr" else len(value) + 1):
                        break
                    row.append(value.replace("\t", " ").replace("\n", " "))
                    in_cell = False
            elif token == "</row>":
                if not budget.add("\t".join(row).rstrip("\t")):
                    return budget.render()
                row = []
            elif m.group(2) is not None:  # <v>
                if in_cell:
                    candidate = unescape(m.group(2))
                    if not budget.fits(len(candidate)):  # held until </c>, charged there
                        break
                    raw = candidate
            elif in_cell:  # <t> inside <is>
                text = unescape(m.group(3) or "")
                if not budget.reserve(len(text)):
                    break
                inline.append(text)
        if row:
            budget.add("\t".join(row).rstrip("\t"))
        if budget.truncated:
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
    except (DocumentValidationError, zipfile.BadZipFile, OSError):
        logger.warning("document text extraction skipped for %s: container rejected", suffix)
        return None


def text_sidecar_path(document: Path) -> Path:
    """Return the sidecar path holding extracted text for *document*."""
    return document.with_name(document.name + TEXT_SIDECAR_SUFFIX)
