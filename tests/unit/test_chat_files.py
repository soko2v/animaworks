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


def test_save_files_controls_path_and_preserves_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_save_files_does_not_overwrite_same_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import core.paths as paths_module

    monkeypatch.setattr(paths_module, "get_data_dir", lambda: tmp_path)
    (tmp_path / "animas" / "sofia").mkdir(parents=True)
    first = save_files("sofia", [_attachment("same.csv", "text/csv", b"first")])
    second = save_files("sofia", [_attachment("same.csv", "text/csv", b"second")])

    assert first != second
    assert (tmp_path / "animas" / "sofia" / first[0]).read_bytes() == b"first"
    assert (tmp_path / "animas" / "sofia" / second[0]).read_bytes() == b"second"
