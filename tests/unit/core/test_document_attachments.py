from __future__ import annotations

import io
import struct
import time
import zipfile
from pathlib import Path

import pytest

from core.document_attachments import (
    DOCUMENT_SUFFIXES,
    DocumentValidationError,
    extract_document_text,
    is_allowed_media_type,
    normalize_suffix,
    text_sidecar_path,
    validate_document_bytes,
)

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/>'
    "{extra}</Types>"
)


def make_docx(paragraphs: list[str], *, macro: bool = False, macro_content_type: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        extra = ""
        if macro_content_type:
            extra = (
                '<Override PartName="/word/document.xml" '
                'ContentType="application/vnd.ms-word.document.macroEnabled.main+xml"/>'
            )
        zf.writestr("[Content_Types].xml", CONTENT_TYPES.format(extra=extra))
        body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>' for p in paragraphs)
        zf.writestr(
            "word/document.xml",
            f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{body}</w:body></w:document>",
        )
        if macro:
            zf.writestr("word/vbaProject.bin", b"\x00\x01")
    return buf.getvalue()


def make_docx_raw(document_xml: str) -> bytes:
    """A .docx whose word/document.xml is *document_xml* verbatim (may be malformed)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES.format(extra=""))
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def make_xlsx(rows: list[list[object]], *, sheet_name: str = "Data") -> bytes:
    shared: list[str] = []

    def cell_xml(ref: str, value: object) -> str:
        if isinstance(value, bool):
            return f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>'
        if isinstance(value, int | float):
            return f'<c r="{ref}"><v>{value}</v></c>'
        if isinstance(value, str) and value.startswith("inline:"):
            return f'<c r="{ref}" t="inlineStr"><is><t>{value[7:]}</t></is></c>'
        shared.append(str(value))
        return f'<c r="{ref}" t="s"><v>{len(shared) - 1}</v></c>'

    row_xml = []
    for r, row in enumerate(rows, start=1):
        cells = "".join(cell_xml(f"{chr(65 + c)}{r}", value) for c, value in enumerate(row))
        row_xml.append(f'<row r="{r}">{cells}</row>')
    return make_xlsx_raw(
        f"<worksheet><sheetData>{''.join(row_xml)}</sheetData></worksheet>",
        shared_strings="<sst>" + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>",
        sheet_name=sheet_name,
    )


def make_xlsx_raw(sheet_xml: str, *, shared_strings: str = "<sst/>", sheet_name: str = "Data") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES.format(extra=""))
        zf.writestr(
            "xl/workbook.xml",
            '<workbook xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{sheet_name}" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships><Relationship Id="rId1" Type="x" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        zf.writestr("xl/sharedStrings.xml", shared_strings)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buf.getvalue()


FREESECT, ENDOFCHAIN, FATSECT, DIFSECT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFC


def make_cfb(
    entries: list[tuple[str, int]] | None = None,
    *,
    streams: dict[str, bytes] | None = None,
    sector_shift: int = 9,
) -> bytes:
    """Spec-conformant MS-CFB (v3 for shift 9, v4 for shift 12) with optional stream content.

    Layout: sector 0 = FAT, 1 = directory, 2 = mini FAT, 3.. = stream chains
    (mini stream container first).  ``entries`` are content-less directory
    entries ``(name, object_type)``; ``streams`` maps names to bytes and are
    placed in the mini stream when shorter than the 4096-byte cutoff.
    """
    sector = 1 << sector_shift
    per_sector = sector // 4
    fat: list[int] = [FATSECT, ENDOFCHAIN, ENDOFCHAIN]  # FAT, directory, mini FAT
    payloads: dict[int, bytes] = {}
    mini_fat: list[int] = []
    mini_container = bytearray()

    def alloc(payload: bytes) -> int:
        count = max(1, -(-len(payload) // sector))
        first = len(fat)
        for i in range(count):
            fat.append(first + i + 1 if i < count - 1 else ENDOFCHAIN)
            payloads[first + i] = payload[i * sector : (i + 1) * sector].ljust(sector, b"\0")
        return first

    directory: list[tuple[str, int, int, int]] = [(name, etype, ENDOFCHAIN, 0) for name, etype in entries or []]
    for name, payload in (streams or {}).items():
        if not payload:
            directory.append((name, 2, ENDOFCHAIN, 0))
        elif len(payload) < 4096:
            count = -(-len(payload) // 64)
            first = len(mini_fat)
            for i in range(count):
                mini_fat.append(first + i + 1 if i < count - 1 else ENDOFCHAIN)
                mini_container += payload[i * 64 : (i + 1) * 64].ljust(64, b"\0")
            directory.append((name, 2, first, len(payload)))
        else:
            directory.append((name, 2, alloc(payload), len(payload)))
    root_start = alloc(bytes(mini_container)) if mini_container else ENDOFCHAIN
    assert len(fat) <= per_sector and len(mini_fat) <= per_sector and len(directory) < sector // 128

    header = bytearray(sector)
    header[0:8] = OLE_MAGIC
    struct.pack_into("<H", header, 24, 0x003E)  # minor version
    struct.pack_into("<H", header, 26, 4 if sector_shift == 12 else 3)  # major version
    struct.pack_into("<H", header, 28, 0xFFFE)  # byte order
    struct.pack_into("<H", header, 30, sector_shift)
    struct.pack_into("<H", header, 32, 6)  # mini sector shift
    struct.pack_into("<I", header, 40, 1 if sector_shift == 12 else 0)  # directory sectors (v4 only)
    struct.pack_into("<I", header, 44, 1)  # number of FAT sectors
    struct.pack_into("<I", header, 48, 1)  # first directory sector
    struct.pack_into("<I", header, 56, 4096)  # mini stream cutoff
    struct.pack_into("<I", header, 60, 2 if mini_fat else ENDOFCHAIN)  # first mini FAT sector
    struct.pack_into("<I", header, 64, 1 if mini_fat else 0)  # number of mini FAT sectors
    struct.pack_into("<I", header, 68, ENDOFCHAIN)  # first DIFAT sector
    struct.pack_into("<I", header, 72, 0)  # number of DIFAT sectors
    for i in range(109):
        struct.pack_into("<I", header, 76 + 4 * i, FREESECT)
    struct.pack_into("<I", header, 76, 0)  # DIFAT[0] -> FAT sector 0

    def table(values: list[int]) -> bytes:
        return b"".join(struct.pack("<I", v) for v in values).ljust(sector, b"\xff")

    dir_sector = bytearray(sector)
    rows = [("Root Entry", 5, root_start, len(mini_container))] + directory
    for i, (name, etype, start, size) in enumerate(rows):
        off = i * 128
        encoded = name.encode("utf-16-le") + b"\x00\x00"
        dir_sector[off : off + len(encoded)] = encoded
        struct.pack_into("<H", dir_sector, off + 64, len(encoded))
        dir_sector[off + 66] = etype
        dir_sector[off + 67] = 1
        for sibling in (68, 72, 76):
            struct.pack_into("<I", dir_sector, off + sibling, FREESECT)
        struct.pack_into("<I", dir_sector, off + 116, start)
        struct.pack_into("<I", dir_sector, off + 120, size)
    body = header + table(fat) + dir_sector + table(mini_fat)
    for index in range(3, len(fat)):
        body += payloads[index]
    return bytes(body)


def biff(rid: int, body: bytes) -> bytes:
    return struct.pack("<HH", rid, len(body)) + body


def biff_bof(dt: int) -> bytes:
    return biff(0x0809, struct.pack("<HHHHII", 0x0600, dt, 0x0DBB, 0x07CC, 0, 0x0006))


def make_workbook(sheet_types: list[int], *, bof_types: list[int] | None = None) -> bytes:
    """BIFF8 Workbook stream: globals BOF, one BOUNDSHEET per sheet, EOF, then one substream per sheet."""
    out = biff_bof(0x0005)
    for i, dt in enumerate(sheet_types):
        name = f"Sheet{i + 1}".encode()
        out += biff(0x0085, struct.pack("<IBB", 0, 0, dt) + bytes([len(name), 0]) + name)
    out += biff(0x000A, b"")
    for dt in bof_types if bof_types is not None else [0x0010] * len(sheet_types):
        out += biff_bof(dt) + biff(0x000A, b"")
    return out


def make_fib(nfib: int = 0x00C1) -> bytes:
    """Word binary File Information Block header (wIdent + nFib)."""
    return struct.pack("<HH", 0xA5EC, nfib) + b"\x00" * 28


# ── allowlists ────────────────────────────────────────────


def test_allowlist_covers_requested_document_types() -> None:
    assert {".docx", ".doc", ".pdf", ".xlsx", ".xls", ".csv", ".txt", ".md"} <= DOCUMENT_SUFFIXES
    assert ".docm" not in DOCUMENT_SUFFIXES
    assert ".xlsm" not in DOCUMENT_SUFFIXES
    assert ".exe" not in DOCUMENT_SUFFIXES


@pytest.mark.parametrize(
    ("name", "media_type", "expected"),
    [
        ("a.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", True),
        ("a.DOCX", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", True),
        ("a.xls", "application/vnd.ms-excel", True),
        ("a.csv", "application/vnd.ms-excel", True),
        ("a.md", "text/plain", True),
        ("a.docx", "application/msword", False),
        ("a.pdf", "text/plain", False),
        ("a.exe", "application/octet-stream", False),
        ("noext", "text/plain", False),
    ],
)
def test_is_allowed_media_type(name: str, media_type: str, expected: bool) -> None:
    assert is_allowed_media_type(normalize_suffix(name), media_type) is expected


# ── validation ────────────────────────────────────────────


def test_pdf_requires_magic() -> None:
    validate_document_bytes(b"%PDF-1.7\n", ".pdf")
    with pytest.raises(DocumentValidationError) as exc:
        validate_document_bytes(b"MZ\x90", ".pdf")
    assert exc.value.code == "invalid"


@pytest.mark.parametrize("suffix", [".txt", ".md", ".csv"])
def test_text_formats_require_utf8_without_nul(suffix: str) -> None:
    validate_document_bytes("# 見出し\n本文".encode(), suffix)
    with pytest.raises(DocumentValidationError) as nul:
        validate_document_bytes(b"ab\x00cd", suffix)
    assert nul.value.code == "invalid"
    with pytest.raises(DocumentValidationError) as enc:
        validate_document_bytes(b"\x81\x81", suffix)
    assert enc.value.code == "encoding"


def test_docx_valid_and_renamed_zip_rejected() -> None:
    validate_document_bytes(make_docx(["hello"]), ".docx")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "x")
    with pytest.raises(DocumentValidationError) as exc:
        validate_document_bytes(buf.getvalue(), ".docx")
    assert exc.value.code == "invalid"
    with pytest.raises(DocumentValidationError):
        validate_document_bytes(b"not a zip at all", ".docx")


def test_docx_with_vba_project_is_rejected() -> None:
    with pytest.raises(DocumentValidationError) as exc:
        validate_document_bytes(make_docx(["x"], macro=True), ".docx")
    assert exc.value.code == "macro"


def test_docx_with_macro_content_type_is_rejected() -> None:
    with pytest.raises(DocumentValidationError) as exc:
        validate_document_bytes(make_docx(["x"], macro_content_type=True), ".docx")
    assert exc.value.code == "macro"


def test_xlsx_valid_and_docx_bytes_rejected_as_xlsx() -> None:
    validate_document_bytes(make_xlsx([["a", 1]]), ".xlsx")
    with pytest.raises(DocumentValidationError):
        validate_document_bytes(make_docx(["x"]), ".xlsx")


def test_zip_slip_entry_names_are_rejected() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES.format(extra=""))
        zf.writestr("word/document.xml", "<w:document/>")
        zf.writestr("../evil.txt", "x")
    with pytest.raises(DocumentValidationError) as exc:
        validate_document_bytes(buf.getvalue(), ".docx")
    assert exc.value.code == "invalid"


def test_zip_bomb_declared_size_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.document_attachments as mod

    monkeypatch.setattr(mod, "_MAX_ZIP_UNCOMPRESSED", 64)
    with pytest.raises(DocumentValidationError) as exc:
        validate_document_bytes(make_docx(["x" * 1000]), ".docx")
    assert exc.value.code == "invalid"


# ── OLE2 / CFB ────────────────────────────────────────────


@pytest.mark.parametrize("sector_shift", [9, 12], ids=["v3-512", "v4-4096"])
@pytest.mark.parametrize("sheets", [1, 400], ids=["mini-stream", "regular-sectors"])
def test_xls_without_macros_is_valid(sector_shift: int, sheets: int) -> None:
    workbook = make_workbook([0x00] * sheets)
    assert (len(workbook) < 4096) == (sheets == 1), "both mini-stream and regular placement must be exercised"
    payload = make_cfb(streams={"Workbook": workbook, "\x05SummaryInformation": b"\x00" * 8}, sector_shift=sector_shift)
    validate_document_bytes(payload, ".xls")


def test_xls_excel95_book_stream_is_accepted() -> None:
    validate_document_bytes(make_cfb(streams={"Book": make_workbook([0x00, 0x02])}), ".xls")


@pytest.mark.parametrize(
    "workbook",
    [
        make_workbook([0x00, 0x01]),
        make_workbook([0x06]),
        make_workbook([0x00], bof_types=[0x0040]),
        make_workbook([0x00], bof_types=[0x0006]),
    ],
    ids=["boundsheet-xlm-macro-sheet", "boundsheet-vb-module", "bof-macro-sheet", "bof-vb-module"],
)
def test_xls_macro_sheets_are_rejected_without_any_vba_storage(workbook: bytes) -> None:
    with pytest.raises(DocumentValidationError) as exc:
        validate_document_bytes(make_cfb(streams={"Workbook": workbook}), ".xls")
    assert exc.value.code == "macro"


def test_xls_requires_well_formed_workbook_stream() -> None:
    cases = {
        "no-workbook": {"Other": make_workbook([0x00])},
        "no-bof": {"Workbook": b"\x00" * 64},
        "truncated-record": {"Workbook": make_workbook([0x00])[:-10]},
    }
    for label, streams in cases.items():
        with pytest.raises(DocumentValidationError) as exc:
            validate_document_bytes(make_cfb(streams=streams), ".xls")
        assert exc.value.code == "invalid", label


@pytest.mark.parametrize("stream_name", ["Workbook", "Book"])
def test_xls_password_protected_workbook_is_rejected(stream_name: str) -> None:
    # FILEPASS follows the globals BOF; every later record (BOUNDSHEET, sheet
    # BOFs) is ciphertext, so a macro sheet behind it could never be detected.
    filepass = biff(0x002F, struct.pack("<HHH", 0x0001, 0x0001, 0x0000) + b"\x00" * 48)
    workbook = biff_bof(0x0005) + filepass + make_workbook([0x00, 0x01])[len(biff_bof(0x0005)) :]
    with pytest.raises(DocumentValidationError) as exc:
        validate_document_bytes(make_cfb(streams={stream_name: workbook}), ".xls")
    assert exc.value.code == "unsupported"


def test_doc_requires_word97_fib() -> None:
    validate_document_bytes(make_cfb(streams={"WordDocument": make_fib(0x00C1), "1Table": b"\x00" * 16}), ".doc")
    validate_document_bytes(make_cfb(streams={"WordDocument": make_fib(0x0112)}, sector_shift=12), ".doc")
    with pytest.raises(DocumentValidationError) as legacy:
        validate_document_bytes(make_cfb(streams={"WordDocument": make_fib(0x0068)}), ".doc")
    assert legacy.value.code == "unsupported"
    for streams in ({"1Table": b"\x00" * 16}, {"WordDocument": b"\x00" * 32}):
        with pytest.raises(DocumentValidationError) as exc:
            validate_document_bytes(make_cfb(streams=streams), ".doc")
        assert exc.value.code == "invalid"


@pytest.mark.parametrize(
    "entries",
    [
        [("Macros", 1), ("VBA", 1)],
        [("_VBA_PROJECT_CUR", 1)],
        [("WordDocument", 2), ("macros", 1)],
    ],
)
def test_cfb_vba_storages_are_rejected(entries: list[tuple[str, int]]) -> None:
    with pytest.raises(DocumentValidationError) as exc:
        validate_document_bytes(make_cfb(entries, streams={"WordDocument": make_fib()}), ".doc")
    assert exc.value.code == "macro"


def test_ole_magic_alone_is_not_enough() -> None:
    for payload in (OLE_MAGIC + b"\x00" * 64, OLE_MAGIC + b"\x00" * 2048, b"PK\x03\x04"):
        with pytest.raises(DocumentValidationError) as exc:
            validate_document_bytes(payload, ".doc")
        assert exc.value.code == "invalid"


def test_cfb_structural_corruption_is_rejected() -> None:
    base = make_cfb(streams={"WordDocument": make_fib()})
    validate_document_bytes(base, ".doc")
    # Truncated after the header: directory sector beyond EOF.
    with pytest.raises(DocumentValidationError):
        validate_document_bytes(base[:512], ".doc")
    # Directory chain loops on itself.
    looped = bytearray(base)
    struct.pack_into("<I", looped, 512 + 4, 1)
    with pytest.raises(DocumentValidationError) as loop:
        validate_document_bytes(bytes(looped), ".doc")
    assert loop.value.code == "invalid"
    # Bogus directory entry type.
    bad_type = bytearray(base)
    bad_type[1024 + 128 + 66] = 9
    with pytest.raises(DocumentValidationError) as etype:
        validate_document_bytes(bytes(bad_type), ".doc")
    assert etype.value.code == "invalid"
    # Root entry missing (first entry is a plain stream).
    no_root = bytearray(base)
    no_root[1024 + 66] = 2
    with pytest.raises(DocumentValidationError):
        validate_document_bytes(bytes(no_root), ".doc")
    # Stream chain ends before the declared size.
    short_chain = bytearray(base)
    struct.pack_into("<I", short_chain, 1024 + 120, 1 << 20)  # root (mini stream) claims 1 MiB
    with pytest.raises(DocumentValidationError) as chain:
        validate_document_bytes(bytes(short_chain), ".doc")
    assert chain.value.code == "invalid"


def test_cfb_declared_counts_are_bounded_by_file_size() -> None:
    base = make_cfb(streams={"WordDocument": make_fib()})
    validate_document_bytes(base, ".doc")

    def mutated(*writes: tuple[int, int]) -> bytes:
        buf = bytearray(base)
        for offset, value in writes:
            struct.pack_into("<I", buf, offset, value)
        return bytes(buf)

    cases = {
        # A million FAT sectors declared through 1024 DIFAT sectors that all point at sector 0.
        "difat-fat-explosion": mutated((44, 1_000_000), (68, 0), (72, 1024)),
        "fat-count-beyond-file": mutated((44, 3)),
        "difat-count-without-chain": mutated((72, 1)),
        "duplicate-fat-sector": mutated((44, 2), (80, 0)),
        "fat-sector-not-marked-fatsect": mutated((512, ENDOFCHAIN)),
        "mini-fat-count-beyond-file": mutated((64, 4096)),
    }
    started = time.perf_counter()
    for label, payload in cases.items():
        with pytest.raises(DocumentValidationError) as exc:
            validate_document_bytes(payload, ".doc")
        assert exc.value.code == "invalid", label
    assert time.perf_counter() - started < 1.0, "malformed counts must be rejected before any large allocation"


def test_cfb_stream_sizes_use_version_specific_width() -> None:
    v4 = make_cfb(streams={"WordDocument": make_fib()}, sector_shift=12)
    validate_document_bytes(v4, ".doc")
    # Version 4 sizes are 64-bit: a non-zero high dword declares > 4 GiB.
    huge = bytearray(v4)
    struct.pack_into("<I", huge, 8192 + 128 + 124, 1)  # directory = sector 1
    with pytest.raises(DocumentValidationError) as exc:
        validate_document_bytes(bytes(huge), ".doc")
    assert exc.value.code == "invalid"
    # Version 3 sizes are 32-bit; MS-CFB 2.6.1 notes that older writers left
    # the high dword uninitialised, so it is ignored rather than trusted ...
    stale_high = bytearray(make_cfb(streams={"WordDocument": make_fib()}))
    struct.pack_into("<I", stale_high, 1024 + 128 + 124, 0xDEADBEEF)
    validate_document_bytes(bytes(stale_high), ".doc")
    # ... while the low dword must still be a legal (<= 2 GiB) v3 size.
    too_big = bytearray(make_cfb(streams={"WordDocument": make_fib()}))
    struct.pack_into("<I", too_big, 1024 + 128 + 120, 0x80000001)
    with pytest.raises(DocumentValidationError) as big:
        validate_document_bytes(bytes(too_big), ".doc")
    assert big.value.code == "invalid"


def test_unknown_suffix_is_unsupported() -> None:
    with pytest.raises(DocumentValidationError) as exc:
        validate_document_bytes(b"MZ", ".exe")
    assert exc.value.code == "unsupported"


# ── extraction ────────────────────────────────────────────


def test_docx_text_extraction_keeps_paragraphs_and_entities() -> None:
    text = extract_document_text(make_docx(["第1章 概要", "Tom &amp; Jerry &lt;b&gt;"]), ".docx")
    assert text == "第1章 概要\nTom & Jerry <b>"


def test_docx_extraction_handles_tabs_breaks_and_trailing_runs() -> None:
    xml = (
        "<w:document><w:body>"
        "<w:p><w:r><w:t>a</w:t><w:tab/><w:t>b</w:t><w:br/><w:t>c</w:t></w:r></w:p>"
        "<w:p/>"
        "<w:p><w:r><w:t>tail</w:t></w:r><w:r><w:t>unterminated run is ignored"
        "</w:body></w:document>"
    )
    assert extract_document_text(make_docx_raw(xml), ".docx") == "a\tb\nc\n\ntail"


def test_docx_extraction_does_not_expand_entities() -> None:
    xml = (
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY lol "lol"><!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        "<w:document><w:body><w:p><w:r><w:t>&xxe;&lol;</w:t></w:r></w:p></w:body></w:document>"
    )
    assert extract_document_text(make_docx_raw(xml), ".docx") == "&xxe;&lol;"


@pytest.mark.parametrize(
    ("suffix", "payload"),
    [
        (".docx", make_docx_raw("<w:document><w:body>" + "<w:p><w:r><w:t>x" * 60_000 + "</w:body></w:document>")),
        (".docx", make_docx_raw("<w:document>" + "<w:p>" * 120_000 + "</w:document>")),
        (".docx", make_docx_raw("<w:document><w:body><w:p>" + "<w:t " * 200_000 + "</w:p></w:body></w:document>")),
        (".docx", make_docx_raw("<w:document><w:body><w:p>" + '<w:t xml:space="preserve" ' * 100_000 + "</w:body>")),
        (".xlsx", make_xlsx_raw("<worksheet><sheetData>" + '<row r="1"><c t="s"><v>0' * 60_000 + "</sheetData>")),
        (".xlsx", make_xlsx_raw("<worksheet><sheetData><row>" + "<c " * 200_000 + "</row></sheetData></worksheet>")),
        (".xlsx", make_xlsx_raw("<worksheet><sheetData><row>" + '<c r="A1" t="s" ' * 100_000 + "</sheetData>")),
        (".xlsx", make_xlsx_raw("<worksheet/>", shared_strings="<sst>" + "<si><t>x" * 120_000 + "</sst>")),
        (".xlsx", make_xlsx_raw("<worksheet/>", shared_strings="<sst>" + "<si><t " * 100_000 + "</sst>")),
    ],
    ids=[
        "docx-unclosed-w:t",
        "docx-unclosed-w:p",
        "docx-unclosed-w:t-prefix",
        "docx-unclosed-w:t-attrs",
        "xlsx-unclosed-row",
        "xlsx-unclosed-c-prefix",
        "xlsx-unclosed-c-attrs",
        "xlsx-unclosed-si",
        "xlsx-unclosed-t-prefix",
    ],
)
def test_extraction_is_linear_on_unclosed_tags(suffix: str, payload: bytes) -> None:
    """Adversarial unclosed tags / repeated prefixes must not trigger quadratic scanning."""
    validate_document_bytes(payload, suffix)
    started = time.perf_counter()
    text = extract_document_text(payload, suffix)
    elapsed = time.perf_counter() - started
    assert text is not None
    assert elapsed < 5.0, f"extraction took {elapsed:.2f}s"


def test_xlsx_text_extraction_rows_and_types() -> None:
    text = extract_document_text(
        make_xlsx([["name", "qty", "ok"], ["apple", 3, True], ["inline:pear", 2.5, False]]), ".xlsx"
    )
    assert text is not None
    lines = text.splitlines()
    assert lines[0] == "## Sheet: Data"
    assert lines[1] == "name\tqty\tok"
    assert lines[2] == "apple\t3\tTRUE"
    assert lines[3] == "pear\t2.5\tFALSE"


def test_xlsx_empty_cells_and_unterminated_row() -> None:
    sheet = '<worksheet><sheetData><row r="1"><c r="A1"/><c r="B1"><v>7</v></c></row><row r="2"><c><v>9</v></c>'
    text = extract_document_text(make_xlsx_raw(sheet + "</sheetData></worksheet>"), ".xlsx")
    assert text == "## Sheet: Data\n\t7\n9"


def test_extraction_truncates_large_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.document_attachments as mod

    monkeypatch.setattr(mod, "_MAX_TEXT_CHARS", 50)
    text = extract_document_text(make_docx(["a" * 40, "b" * 40, "c" * 40]), ".docx")
    assert text is not None
    assert "truncated" in text
    assert len(text) < 200


def test_unclosed_paragraph_is_bounded_by_output_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.document_attachments as mod

    monkeypatch.setattr(mod, "_MAX_TEXT_CHARS", 100)
    xml = "<w:document><w:body><w:p>" + "<w:r><w:t>abcdefghij</w:t></w:r>" * 5_000 + "</w:body></w:document>"
    text = extract_document_text(make_docx_raw(xml), ".docx")
    assert text is not None
    assert "truncated" in text
    assert len(text) <= 100 + len(mod._TRUNCATION_MARK)


def test_shared_strings_and_unterminated_row_are_bounded_by_output_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.document_attachments as mod

    monkeypatch.setattr(mod, "_MAX_TEXT_CHARS", 100)
    monkeypatch.setattr(mod, "_MAX_SHARED_STRINGS", 100)
    sst = "<sst>" + "<si><t>abc</t></si>" * 5_000 + "</sst>"
    sheet = '<worksheet><sheetData><row r="1"><c t="s"><v>4999</v></c><c t="s"><v>0</v></c></row>' + "<c/>" * 5_000
    text = extract_document_text(make_xlsx_raw(sheet + "</sheetData></worksheet>", shared_strings=sst), ".xlsx")
    assert text is not None
    assert "truncated" in text
    assert "\tabc" in text, "strings inside the retained table resolve; those beyond it render empty"
    assert len(text) <= 100 + len(mod._TRUNCATION_MARK)


def test_budget_rejects_oversized_values_before_buffering(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.document_attachments as mod

    monkeypatch.setattr(mod, "_MAX_TEXT_CHARS", 100)
    committed: list[int] = []
    original_add = mod._TextBudget.add

    def spying_add(self: mod._TextBudget, line: str) -> bool:
        committed.append(len(line))
        return original_add(self, line)

    monkeypatch.setattr(mod._TextBudget, "add", spying_add)
    big = "x" * 10_000

    docx = make_docx_raw(
        f"<w:document><w:body><w:p><w:r><w:t>ok</w:t></w:r><w:r><w:t>{big}</w:t></w:r></w:p></w:body></w:document>"
    )
    text = extract_document_text(docx, ".docx")
    assert text is not None and "truncated" in text
    assert max(committed) <= 100, "a single over-budget run must not be buffered or flushed"

    committed.clear()
    sheet = (
        '<worksheet><sheetData><row r="1"><c><v>1</v></c>'
        f'<c t="inlineStr"><is><t>{big}</t></is></c><c><v>{big}</v></c></row></sheetData></worksheet>'
    )
    text = extract_document_text(make_xlsx_raw(sheet), ".xlsx")
    assert text is not None and "truncated" in text
    assert max(committed) <= 100

    with mod._open_ooxml(make_xlsx_raw("<worksheet/>", shared_strings=f"<sst><si><t>{big}</t></si></sst>")) as archive:
        strings, truncated = mod._xlsx_shared_strings(archive)
    assert truncated and strings == [], "an over-budget shared string is dropped, not retained"


def test_xlsx_values_within_budget_are_charged_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.document_attachments as mod

    monkeypatch.setattr(mod, "_MAX_TEXT_CHARS", 80)
    sst = "<sst><si><t>abcdefghij</t></si></sst>"
    sheet = (
        '<worksheet><sheetData><row r="1"><c><v>123456789</v></c><c t="s"><v>0</v></c><c t="b"><v>1</v></c>'
        '<c t="inlineStr"><is><t>klmnopqrst</t></is></c></row>'
        '<row r="2"><c><v>987654321</v></c><c t="s"><v>0</v></c></row></sheetData></worksheet>'
    )
    text = extract_document_text(make_xlsx_raw(sheet, shared_strings=sst), ".xlsx")
    assert text is not None
    # 74 characters of output sit between 50% and 100% of the cap: double
    # charging numeric / shared / boolean cells would truncate this sheet.
    assert "truncated" not in text
    assert "123456789\tabcdefghij\tTRUE\tklmnopqrst" in text
    assert "987654321\tabcdefghij" in text


@pytest.mark.parametrize("suffix", [".pdf", ".doc", ".xls", ".txt", ".csv", ".md"])
def test_no_sidecar_text_for_non_ooxml(suffix: str) -> None:
    assert extract_document_text(b"anything", suffix) is None


def test_text_sidecar_path() -> None:
    assert text_sidecar_path(Path("/x/attachments/r.docx")) == Path("/x/attachments/r.docx.txt")
