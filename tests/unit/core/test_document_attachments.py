from __future__ import annotations

import io
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
        zf.writestr(
            "xl/sharedStrings.xml",
            "<sst>" + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>",
        )
        zf.writestr("xl/worksheets/sheet1.xml", f"<worksheet><sheetData>{''.join(row_xml)}</sheetData></worksheet>")
    return buf.getvalue()


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


def test_ole_formats_require_magic_and_reject_vba() -> None:
    validate_document_bytes(OLE_MAGIC + b"\x00" * 64, ".doc")
    validate_document_bytes(OLE_MAGIC + b"\x00" * 64, ".xls")
    with pytest.raises(DocumentValidationError) as bad:
        validate_document_bytes(b"PK\x03\x04", ".doc")
    assert bad.value.code == "invalid"
    with pytest.raises(DocumentValidationError) as macro:
        validate_document_bytes(OLE_MAGIC + b"\x00" * 8 + "Macros".encode("utf-16-le") + b"\x00" * 8, ".xls")
    assert macro.value.code == "macro"


def test_unknown_suffix_is_unsupported() -> None:
    with pytest.raises(DocumentValidationError) as exc:
        validate_document_bytes(b"MZ", ".exe")
    assert exc.value.code == "unsupported"


# ── extraction ────────────────────────────────────────────


def test_docx_text_extraction_keeps_paragraphs_and_entities() -> None:
    text = extract_document_text(make_docx(["第1章 概要", "Tom &amp; Jerry &lt;b&gt;"]), ".docx")
    assert text == "第1章 概要\nTom & Jerry <b>"


def test_docx_extraction_does_not_expand_entities() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES.format(extra=""))
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY lol "lol"><!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            "<w:document><w:body><w:p><w:r><w:t>&xxe;&lol;</w:t></w:r></w:p></w:body></w:document>",
        )
    text = extract_document_text(buf.getvalue(), ".docx")
    assert text == "&xxe;&lol;"


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
