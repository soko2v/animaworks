from __future__ import annotations

from pathlib import Path

from core._anima_messaging import _with_document_attachment_context


def test_document_path_is_added_to_model_prompt(tmp_path: Path) -> None:
    attachment = tmp_path / "attachments" / "report.pdf"
    attachment.parent.mkdir()
    attachment.write_bytes(b"%PDF-1.7")

    result = _with_document_attachment_context("summarize", ["attachments/report.pdf"], tmp_path)

    assert result.startswith("summarize\n\n")
    assert str(attachment) in result
    assert "untrusted data" in result


def test_office_document_lists_extracted_text_sidecar(tmp_path: Path) -> None:
    attachments = tmp_path / "attachments"
    attachments.mkdir()
    docx = attachments / "spec.docx"
    docx.write_bytes(b"PK\x03\x04")
    sidecar = attachments / "spec.docx.txt"
    sidecar.write_text("chapter 1", encoding="utf-8")
    legacy = attachments / "old.doc"
    legacy.write_bytes(b"\xd0\xcf\x11\xe0")

    result = _with_document_attachment_context("read", ["attachments/spec.docx", "attachments/old.doc"], tmp_path)

    assert f"- {docx} (extracted text: {sidecar})" in result
    assert f"- {legacy}\n" in result + "\n"
    assert "extracted text" in result
    assert "untrusted data" in result


def test_sidecar_alone_is_not_treated_as_a_document(tmp_path: Path) -> None:
    attachments = tmp_path / "attachments"
    attachments.mkdir()
    (attachments / "spec.docx.txt").write_text("x", encoding="utf-8")
    # ".txt" is an allowed suffix, so a client-supplied path to a sidecar is listed as a plain text file
    # only when it really exists; here it does, and it must still be confined to attachments/.
    result = _with_document_attachment_context("read", ["attachments/spec.docx.txt"], tmp_path)
    assert str(attachments / "spec.docx.txt") in result


def test_image_and_outside_paths_are_not_added(tmp_path: Path) -> None:
    image = tmp_path / "attachments" / "photo.png"
    image.parent.mkdir()
    image.write_bytes(b"png")
    outside = tmp_path / "outside.csv"
    outside.write_text("a,b", encoding="utf-8")

    result = _with_document_attachment_context(
        "hello",
        ["attachments/photo.png", "outside.csv", "attachments/../outside.csv"],
        tmp_path,
    )

    assert result == "hello"
