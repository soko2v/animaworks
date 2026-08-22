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
