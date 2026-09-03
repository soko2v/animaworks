from __future__ import annotations

import base64
from pathlib import Path

import pytest

from server.routes.chat_files import _validate_files, save_files
from server.routes.chat_models import FileAttachment


def _attachment(name: str, media_type: str, content: bytes) -> FileAttachment:
    return FileAttachment(name=name, media_type=media_type, data=base64.b64encode(content).decode())


@pytest.mark.parametrize(
    ("name", "media_type", "content"),
    [
        ("report.pdf", "application/pdf", b"%PDF-1.7\ncontent"),
        ("table.csv", "text/csv", b"name,value\nAlice,1\n"),
    ],
)
def test_supported_file_is_valid(name: str, media_type: str, content: bytes) -> None:
    assert _validate_files([_attachment(name, media_type, content)]) is None


def test_pdf_signature_mismatch_is_rejected() -> None:
    assert _validate_files([_attachment("report.pdf", "application/pdf", b"not a pdf")]) is not None


def test_extension_mime_mismatch_is_rejected() -> None:
    assert _validate_files([_attachment("report.csv", "application/pdf", b"%PDF-1.7")]) is not None


def test_binary_csv_is_rejected() -> None:
    assert _validate_files([_attachment("table.csv", "text/csv", b"a,b\x00c")]) is not None


def test_non_utf8_csv_is_rejected() -> None:
    assert _validate_files([_attachment("table.csv", "text/csv", b"\x81\x81")]) is not None


def test_invalid_base64_is_rejected() -> None:
    item = FileAttachment(name="table.csv", media_type="text/csv", data="%%%")
    assert _validate_files([item]) is not None


def test_save_files_controls_path_and_preserves_suffix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import core.paths as paths_module

    monkeypatch.setattr(paths_module, "get_data_dir", lambda: tmp_path)
    (tmp_path / "animas" / "sofia").mkdir(parents=True)
    item = _attachment("../../quarter 1.csv", "text/csv", b"a,b\n1,2\n")

    paths = save_files("sofia", [item])

    assert len(paths) == 1
    assert paths[0].startswith("attachments/")
    assert paths[0].endswith("quarter_1.csv")
    destination = tmp_path / "animas" / "sofia" / paths[0]
    assert destination.is_file()
    assert destination.read_bytes() == b"a,b\n1,2\n"
    assert destination.resolve().is_relative_to((tmp_path / "animas" / "sofia" / "attachments").resolve())


DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.mark.parametrize(
    ("name", "media_type"),
    [
        ("notes.txt", "text/plain"),
        ("README.md", "text/markdown"),
        ("README.md", "text/plain"),
        ("win.csv", "application/vnd.ms-excel"),
    ],
)
def test_text_document_types_are_valid(name: str, media_type: str) -> None:
    assert _validate_files([_attachment(name, media_type, b"# title\nbody")]) is None


def test_office_documents_are_validated_by_bytes() -> None:
    from tests.unit.core.test_document_attachments import (
        OLE_MAGIC,
        make_cfb,
        make_docx,
        make_fib,
        make_workbook,
        make_xlsx,
    )

    assert _validate_files([_attachment("a.docx", DOCX, make_docx(["hi"]))]) is None
    assert _validate_files([_attachment("a.xlsx", XLSX, make_xlsx([["a"]]))]) is None
    assert (
        _validate_files([_attachment("a.doc", "application/msword", make_cfb(streams={"WordDocument": make_fib()}))])
        is None
    )
    assert (
        _validate_files(
            [_attachment("a.xls", "application/vnd.ms-excel", make_cfb(streams={"Workbook": make_workbook([0x00])}))]
        )
        is None
    )
    # Renamed / mismatched / macro containers are rejected regardless of the declared type.
    assert _validate_files([_attachment("a.docx", DOCX, b"%PDF-1.7")]) is not None
    assert _validate_files([_attachment("a.xls", "application/vnd.ms-excel", b"PK\x03\x04")]) is not None
    assert _validate_files([_attachment("a.doc", "application/msword", OLE_MAGIC + b"\x00" * 1024)]) is not None
    assert _validate_files([_attachment("a.docx", DOCX, make_docx(["hi"], macro=True))]) is not None
    macro_doc = make_cfb([("Macros", 1), ("VBA", 1)], streams={"WordDocument": make_fib()})
    error = _validate_files([_attachment("a.doc", "application/msword", macro_doc)])
    assert error is not None and "VBA" in error


@pytest.mark.parametrize(
    ("name", "media_type"),
    [
        ("tool.exe", "application/octet-stream"),
        ("macro.docm", "application/vnd.ms-word.document.macroEnabled.12"),
        ("sheet.xlsm", "application/vnd.ms-excel.sheet.macroEnabled.12"),
        ("script.js", "text/javascript"),
        ("noext", "text/plain"),
        ("archive.zip", "application/zip"),
    ],
)
def test_non_allowlisted_extensions_are_rejected(name: str, media_type: str) -> None:
    assert _validate_files([_attachment(name, media_type, b"PK\x03\x04data")]) is not None


def test_file_count_limit_is_enforced() -> None:
    items = [_attachment(f"f{i}.txt", "text/plain", b"ok") for i in range(11)]
    error = _validate_files(items)
    assert error is not None
    assert "10" in error
    assert _validate_files(items[:10]) is None


def test_save_files_writes_text_sidecar_for_ooxml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import core.paths as paths_module
    from tests.unit.core.test_document_attachments import make_docx

    monkeypatch.setattr(paths_module, "get_data_dir", lambda: tmp_path)
    (tmp_path / "animas" / "sofia").mkdir(parents=True)
    paths = save_files("sofia", [_attachment("仕様書 v2.docx", DOCX, make_docx(["第1章", "本文"]))])

    assert len(paths) == 1
    document = tmp_path / "animas" / "sofia" / paths[0]
    assert document.suffix == ".docx"
    sidecar = document.with_name(document.name + ".txt")
    assert sidecar.read_text(encoding="utf-8") == "第1章\n本文"
    assert sidecar.resolve().is_relative_to((tmp_path / "animas" / "sofia" / "attachments").resolve())


def test_save_files_refuses_unknown_suffix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import core.paths as paths_module

    monkeypatch.setattr(paths_module, "get_data_dir", lambda: tmp_path)
    (tmp_path / "animas" / "sofia").mkdir(parents=True)
    with pytest.raises(ValueError):
        save_files("sofia", [_attachment("tool.exe", "text/plain", b"MZ")])


def test_save_files_does_not_overwrite_same_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import core.paths as paths_module

    monkeypatch.setattr(paths_module, "get_data_dir", lambda: tmp_path)
    (tmp_path / "animas" / "sofia").mkdir(parents=True)
    first = save_files("sofia", [_attachment("same.csv", "text/csv", b"first")])
    second = save_files("sofia", [_attachment("same.csv", "text/csv", b"second")])

    assert first != second
    assert (tmp_path / "animas" / "sofia" / first[0]).read_bytes() == b"first"
    assert (tmp_path / "animas" / "sofia" / second[0]).read_bytes() == b"second"


def test_validate_files_rejects_encrypted_ooxml_entry() -> None:
    from tests.unit.core.test_document_attachments import make_docx, set_zip_entry_flag

    encrypted = set_zip_entry_flag(make_docx(["secret"]), "word/document.xml", 0x1)
    assert _validate_files([_attachment("locked.docx", DOCX, encrypted)]) is not None


def test_save_files_extracts_text_before_persisting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit.core.test_document_attachments import make_docx

    monkeypatch.setattr("core.paths.get_data_dir", lambda: tmp_path)

    def exploding(_data: bytes, _suffix: str) -> str | None:
        raise RuntimeError("simulated extraction failure")

    monkeypatch.setattr("server.routes.chat_files.extract_document_text", exploding)
    with pytest.raises(RuntimeError):
        save_files("sofia", [_attachment("spec.docx", DOCX, make_docx(["x"]))])
    attachments = tmp_path / "animas" / "sofia" / "attachments"
    assert not attachments.exists() or list(attachments.iterdir()) == []
