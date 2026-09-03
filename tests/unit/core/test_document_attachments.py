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


def make_cfb(entries: list[tuple[str, int]] | None = None) -> bytes:
    """Minimal valid MS-CFB v3 file: sector 0 = FAT, sector 1 = directory (root + up to 3 entries)."""
    sector = 512
    header = bytearray(sector)
    header[0:8] = OLE_MAGIC
    struct.pack_into("<H", header, 24, 0x003E)  # minor version
    struct.pack_into("<H", header, 26, 3)  # major version
    struct.pack_into("<H", header, 28, 0xFFFE)  # byte order
    struct.pack_into("<H", header, 30, 9)  # sector shift (512)
    struct.pack_into("<H", header, 32, 6)  # mini sector shift
    struct.pack_into("<I", header, 44, 1)  # number of FAT sectors
    struct.pack_into("<I", header, 48, 1)  # first directory sector
    struct.pack_into("<I", header, 56, 4096)  # mini stream cutoff
    struct.pack_into("<I", header, 60, 0xFFFFFFFE)  # first mini FAT sector
    struct.pack_into("<I", header, 68, 0xFFFFFFFE)  # first DIFAT sector
    struct.pack_into("<I", header, 72, 0)  # number of DIFAT sectors
    for i in range(109):
        struct.pack_into("<I", header, 76 + 4 * i, 0xFFFFFFFF)
    struct.pack_into("<I", header, 76, 0)  # DIFAT[0] -> FAT sector 0
    fat = bytearray(b"\xff" * sector)
    struct.pack_into("<I", fat, 0, 0xFFFFFFFD)  # sector 0 is a FAT sector
    struct.pack_into("<I", fat, 4, 0xFFFFFFFE)  # sector 1 (directory) ends the chain
    directory = bytearray(sector)
    for i, (name, etype) in enumerate([("Root Entry", 5)] + list(entries or [])):
        off = i * 128
        encoded = name.encode("utf-16-le") + b"\x00\x00"
        directory[off : off + len(encoded)] = encoded
        struct.pack_into("<H", directory, off + 64, len(encoded))
        directory[off + 66] = etype
        directory[off + 67] = 1
        for sibling in (68, 72, 76):
            struct.pack_into("<I", directory, off + sibling, 0xFFFFFFFF)
    return bytes(header + fat + directory)


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


def test_cfb_documents_without_vba_are_valid() -> None:
    validate_document_bytes(make_cfb([("WordDocument", 2), ("1Table", 2)]), ".doc")
    validate_document_bytes(make_cfb([("Workbook", 2), ("\x05SummaryInformation", 2)]), ".xls")


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
        validate_document_bytes(make_cfb(entries), ".doc")
    assert exc.value.code == "macro"


def test_ole_magic_alone_is_not_enough() -> None:
    for payload in (OLE_MAGIC + b"\x00" * 64, OLE_MAGIC + b"\x00" * 2048, b"PK\x03\x04"):
        with pytest.raises(DocumentValidationError) as exc:
            validate_document_bytes(payload, ".doc")
        assert exc.value.code == "invalid"


def test_cfb_structural_corruption_is_rejected() -> None:
    base = make_cfb([("WordDocument", 2)])
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
        (".xlsx", make_xlsx_raw("<worksheet><sheetData>" + '<row r="1"><c t="s"><v>0' * 60_000 + "</sheetData>")),
        (".xlsx", make_xlsx_raw("<worksheet/>", shared_strings="<sst>" + "<si><t>x" * 120_000 + "</sst>")),
    ],
    ids=["docx-unclosed-w:t", "docx-unclosed-w:p", "xlsx-unclosed-row", "xlsx-unclosed-si"],
)
def test_extraction_is_linear_on_unclosed_tags(suffix: str, payload: bytes) -> None:
    """Adversarial unclosed tags must not trigger quadratic scanning."""
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


@pytest.mark.parametrize("suffix", [".pdf", ".doc", ".xls", ".txt", ".csv", ".md"])
def test_no_sidecar_text_for_non_ooxml(suffix: str) -> None:
    assert extract_document_text(b"anything", suffix) is None


def test_text_sidecar_path() -> None:
    assert text_sidecar_path(Path("/x/attachments/r.docx")) == Path("/x/attachments/r.docx.txt")
