# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for core/chat_attachments.py (chat PDF attachments)."""

from __future__ import annotations

import asyncio
import base64
import contextvars
import json
import os
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import chat_attachments as ca
from tests.helpers.pdf_fixtures import make_pdf

HAS_POPPLER = ca.find_poppler_tool("pdftotext") is not None and ca.find_poppler_tool("pdfinfo") is not None
needs_poppler = pytest.mark.skipif(not HAS_POPPLER, reason="poppler (pdftotext/pdfinfo) not installed")
posix_only = pytest.mark.skipif(os.name == "nt", reason="uses /bin/sh scripts")


def _store_pdf(anima_dir: Path, name: str, data: bytes, *, display: str | None = None, meta: dict | None = None) -> str:
    attachments = anima_dir / "attachments"
    attachments.mkdir(parents=True, exist_ok=True)
    path = attachments / name
    path.write_bytes(data)
    if meta is not None:
        payload = {"name": display or name, "size": len(data), **meta}
        ca.meta_sidecar_path(path).write_text(json.dumps(payload), encoding="utf-8")
    return f"attachments/{name}"


# ── names / detection ──────────────────────────────────────


class TestDetection:
    def test_magic_at_start(self) -> None:
        assert ca.looks_like_pdf(b"%PDF-1.7\n...")

    def test_magic_within_first_kib(self) -> None:
        assert ca.looks_like_pdf(b"\x00" * 500 + b"%PDF-1.4")

    def test_magic_missing_or_too_late(self) -> None:
        assert not ca.looks_like_pdf(b"PK\x03\x04 not a pdf")
        assert not ca.looks_like_pdf(b"\x00" * 2000 + b"%PDF-1.4")

    @pytest.mark.parametrize(
        ("name", "media_type", "expected"),
        [
            ("a.pdf", "application/pdf", True),
            ("a.bin", "application/pdf", True),
            ("a.pdf", "application/x-pdf", True),
            ("a.PDF", "", True),
            ("a.pdf", "application/octet-stream", True),
            ("a.txt", "", False),
            ("a.pdf", "image/png", False),
            ("a.pdf", "text/html", False),
        ],
    )
    def test_is_pdf_upload(self, name: str, media_type: str, expected: bool) -> None:
        assert ca.is_pdf_upload(name, media_type) is expected


class TestDisplayName:
    def test_keeps_japanese_and_normalises_suffix(self) -> None:
        assert ca.display_name_for_upload("見積書（最終）.PDF") == "見積書（最終）.pdf"

    def test_strips_directories(self) -> None:
        assert ca.display_name_for_upload("../../etc/passwd") == "passwd.pdf"
        assert ca.display_name_for_upload("C:\\Users\\me\\report.pdf") == "report.pdf"

    def test_removes_bidi_override_spoofing(self) -> None:
        name = ca.display_name_for_upload("invoice\u202eexe.pdf")
        assert "\u202e" not in name
        assert name.endswith(".pdf")

    def test_replaces_unsafe_and_control_characters(self) -> None:
        name = ca.display_name_for_upload('a<b>c:"d|e?\x00f.pdf')
        assert not re.search(r'[<>:"|?\x00]', name)
        assert name.endswith(".pdf")

    def test_empty_falls_back(self) -> None:
        assert ca.display_name_for_upload("") == "document.pdf"
        assert ca.display_name_for_upload("...") == "document.pdf"
        assert ca.display_name_for_upload(".pdf") == "document.pdf"

    def test_length_capped(self) -> None:
        name = ca.display_name_for_upload("x" * 500 + ".pdf")
        assert len(name) <= 120 and name.endswith(".pdf")


class TestStorageFilename:
    def test_format(self) -> None:
        name = ca.storage_filename("社内 連絡(テスト).pdf", timestamp="20261009_110000", token="0123abcd")
        assert name == "20261009_110000_0123abcd_社内_連絡_テスト.pdf"

    def test_random_token_and_long_stem(self) -> None:
        name = ca.storage_filename("y" * 300 + ".pdf", timestamp="20261009_110000")
        assert re.fullmatch(r"20261009_110000_[0-9a-f]{8}_y{60}\.pdf", name)

    def test_stem_of_only_symbols(self) -> None:
        assert ca.storage_filename("$$$.pdf", timestamp="t", token="00000000") == "t_00000000_document.pdf"

    def test_no_double_dots(self) -> None:
        # the attachment route rejects any name containing ".."
        name = ca.storage_filename("report..draft...v2.pdf", timestamp="t", token="00000000")
        assert ".." not in name and name == "t_00000000_report.draft.v2.pdf"


# ── poppler helpers ───────────────────────────────────────


class TestNormalizeText:
    def test_page_markers_and_whitespace(self) -> None:
        raw = "Title          Value\n\n\n\n\nnext\f page two  \f"
        text = ca._normalize_extracted_text(raw)
        assert text.startswith("=== Page 1 ===\nTitle   Value")
        assert "\n\n\n\n" not in text
        assert text.endswith("=== Page 2 ===\n page two")

    def test_single_page_has_no_marker(self) -> None:
        assert ca._normalize_extracted_text("only page\n\f") == "only page"


@needs_poppler
class TestPopplerIntegration:
    def test_inspect_and_extract(self, tmp_path: Path) -> None:
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(make_pdf(["Secret code ALPHA-42", "Second page BLUE"]))
        info = ca.inspect_pdf(pdf)
        assert info.ok and info.pages == 2 and not info.encrypted and not info.password_required
        text, status = ca.extract_pdf_text(pdf)
        assert status == "ok"
        assert "ALPHA-42" in text and "=== Page 2 ===" in text and "BLUE" in text

    def test_scanned_like_pdf_is_empty(self, tmp_path: Path) -> None:
        pdf = tmp_path / "scan.pdf"
        pdf.write_bytes(make_pdf(["", ""]))
        assert ca.extract_pdf_text(pdf) == ("", "empty")

    def test_extract_truncates(self, tmp_path: Path) -> None:
        pdf = tmp_path / "long.pdf"
        pdf.write_bytes(make_pdf(["A" * 60, "B" * 60]))
        text, status = ca.extract_pdf_text(pdf, max_chars=20)
        assert status == "truncated" and len(text) <= 20

    def test_broken_pdf(self, tmp_path: Path) -> None:
        pdf = tmp_path / "broken.pdf"
        pdf.write_bytes(b"%PDF-1.4\nthis is not really a pdf")
        assert not ca.inspect_pdf(pdf).ok
        text, status = ca.extract_pdf_text(pdf)
        assert text == "" and status in {"failed", "empty"}


class TestPopplerFallbacks:
    def test_tool_missing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(ca, "find_poppler_tool", lambda name: None)
        assert ca.extract_pdf_text(tmp_path / "x.pdf") == ("", "unavailable")
        assert ca.inspect_pdf(tmp_path / "x.pdf") == ca.PdfInspection(ok=False)

    @staticmethod
    def _fake_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
        tool = tmp_path / "fake-poppler"
        tool.write_text("#!/bin/sh\n" + script + "\n", encoding="utf-8")
        tool.chmod(0o755)
        monkeypatch.setattr(ca, "find_poppler_tool", lambda name: str(tool))

    @posix_only
    def test_password_required_detected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._fake_tool(tmp_path, monkeypatch, 'echo "Command Line Error: Incorrect password" >&2; exit 1')
        info = ca.inspect_pdf(tmp_path / "locked.pdf")
        assert info.password_required and not info.ok

    @posix_only
    def test_encrypted_flag_parsed(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._fake_tool(
            tmp_path, monkeypatch, 'printf "Pages:           7\\nEncrypted:       yes (print:yes copy:no)\\n"'
        )
        info = ca.inspect_pdf(tmp_path / "restricted.pdf")
        assert info == ca.PdfInspection(ok=True, pages=7, encrypted=True)

    @posix_only
    def test_runaway_metadata_output_is_capped(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # A crafted PDF can make pdfinfo print gigabytes of decompressed metadata.
        self._fake_tool(tmp_path, monkeypatch, "exec yes 'Title: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'")
        assert ca.inspect_pdf(tmp_path / "bomb.pdf") == ca.PdfInspection(ok=False)

    @posix_only
    def test_runaway_stderr_does_not_block_or_grow(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._fake_tool(tmp_path, monkeypatch, "yes 'noise' | head -c 2000000 >&2; printf 'Pages: 3\\n'")
        assert ca.inspect_pdf(tmp_path / "noisy.pdf") == ca.PdfInspection(ok=True, pages=3)

    @posix_only
    def test_extract_runaway_output_truncated(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._fake_tool(tmp_path, monkeypatch, "exec yes 'page text'")
        text, status = ca.extract_pdf_text(tmp_path / "bomb.pdf", max_chars=1000)
        assert status == "truncated" and 0 < len(text) <= 1000

    @pytest.mark.skipif(os.name == "nt" or not Path("/usr/bin/yes").exists(), reason="needs /usr/bin/yes")
    def test_run_capped_bounds_memory(self) -> None:
        result = ca._run_capped(["/usr/bin/yes"], stdout_cap=4096, stderr_cap=100, timeout=10)
        assert result.truncated and len(result.stdout) == 4096 and result.returncode is None

    @pytest.mark.skipif(os.name == "nt" or not Path("/bin/sleep").exists(), reason="needs /bin/sleep")
    def test_run_capped_times_out(self) -> None:
        result = ca._run_capped(["/bin/sleep", "5"], stdout_cap=100, stderr_cap=100, timeout=0.3)
        assert result.stdout == b"" and not result.truncated and result.returncode is None

    @posix_only
    def test_run_capped_keeps_stderr(self) -> None:
        result = ca._run_capped(
            ["/bin/sh", "-c", "echo out; echo err >&2; exit 3"], stdout_cap=100, stderr_cap=100, timeout=5
        )
        assert result.stdout == b"out\n" and result.stderr == b"err\n" and result.returncode == 3


# ── sidecars and loading ──────────────────────────────────


@needs_poppler
def test_store_and_load_round_trip(tmp_path: Path) -> None:
    anima_dir = tmp_path / "alice"
    data = make_pdf(["Quarterly REPORT text"])
    inspection, text, status = ca.analyse_pdf_bytes(data)
    assert inspection.ok and inspection.pages == 1 and status == "ok" and "REPORT" in text
    with ca.AttachmentStore(anima_dir) as store:
        names = ca.store_chat_pdf(
            store,
            data=data,
            display_name="Report (Q3).pdf",
            timestamp="20261009_110000",
            inspection=inspection,
            text=text,
            status=status,
        )
    pdf_name = names[0]
    assert names == [pdf_name, pdf_name + ".txt", pdf_name + ".meta.json"]
    rel = f"attachments/{pdf_name}"
    pdf = anima_dir / rel
    meta = json.loads((anima_dir / "attachments" / (pdf_name + ".meta.json")).read_text(encoding="utf-8"))
    assert meta["pages"] == 1 and meta["text_status"] == "ok" and meta["name"] == "Report (Q3).pdf"
    attachment = ca.load_chat_pdf_attachment(anima_dir, rel)
    assert attachment is not None
    assert attachment.name == "Report (Q3).pdf"
    assert attachment.pages == 1 and attachment.size == len(data) == pdf.stat().st_size
    assert attachment.text_path == ca.text_sidecar_path(pdf.resolve())
    assert "REPORT" in attachment.text_path.read_text(encoding="utf-8")
    assert ca.pdf_display_name(pdf) == "Report (Q3).pdf"
    with ca.AttachmentStore(anima_dir) as store:
        ca.remove_chat_pdf(store, pdf_name)
    assert not any((anima_dir / "attachments").iterdir())


class TestLoadAttachment:
    def test_without_meta_uses_stored_name(self, tmp_path: Path) -> None:
        rel = _store_pdf(tmp_path, "20261009_110000_0123abcd_契約書.pdf", make_pdf(["x"]))
        attachment = ca.load_chat_pdf_attachment(tmp_path, rel)
        assert attachment is not None
        assert attachment.name == "契約書.pdf"
        assert attachment.pages is None and attachment.text_path is None
        assert attachment.text_status == "unavailable"

    @pytest.mark.parametrize(
        "rel_path",
        [
            "attachments/../secret.pdf",
            "attachments/sub/doc.pdf",
            "state/doc.pdf",
            "/etc/doc.pdf",
            "attachments/missing.pdf",
            "attachments/image.png",
        ],
    )
    def test_rejects_unsafe_or_missing(self, tmp_path: Path, rel_path: str) -> None:
        (tmp_path / "attachments" / "sub").mkdir(parents=True)
        (tmp_path / "attachments" / "sub" / "doc.pdf").write_bytes(make_pdf(["x"]))
        (tmp_path / "secret.pdf").write_bytes(make_pdf(["x"]))
        (tmp_path / "attachments" / "image.png").write_bytes(b"\x89PNG")
        assert ca.load_chat_pdf_attachment(tmp_path, rel_path) is None

    def test_rejects_symlink_escape(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside.pdf"
        outside.write_bytes(make_pdf(["x"]))
        (tmp_path / "anima" / "attachments").mkdir(parents=True)
        (tmp_path / "anima" / "attachments" / "link.pdf").symlink_to(outside)
        assert ca.load_chat_pdf_attachment(tmp_path / "anima", "attachments/link.pdf") is None

    def test_rejects_symlinked_attachments_dir(self, tmp_path: Path) -> None:
        # alice/attachments -> bob/attachments must not expose Bob's files to Alice
        rel = _store_pdf(tmp_path / "bob", "secret.pdf", make_pdf(["bob only"]), meta={"pages": 1})
        (tmp_path / "alice").mkdir()
        (tmp_path / "alice" / "attachments").symlink_to(tmp_path / "bob" / "attachments")
        assert ca.confined_attachments_root(tmp_path / "alice") is None
        assert ca.load_chat_pdf_attachment(tmp_path / "alice", rel) is None
        assert ca.describe_attachment_paths(tmp_path / "alice", [rel]) == []
        assert ca.confined_attachment_file(tmp_path / "alice", "secret.pdf") is None
        assert ca.confined_attachment_file(tmp_path / "bob", "secret.pdf") is not None

    def test_symlinked_sidecars_are_ignored(self, tmp_path: Path) -> None:
        private = tmp_path / "private.txt"
        private.write_text("TOP SECRET VAULT", encoding="utf-8")
        fake_meta = tmp_path / "meta.json"
        fake_meta.write_text(json.dumps({"name": "spoofed.pdf", "pages": 3}), encoding="utf-8")
        rel = _store_pdf(tmp_path / "anima", "doc.pdf", make_pdf(["x"]))
        stored = tmp_path / "anima" / rel
        ca.text_sidecar_path(stored).symlink_to(private)
        ca.meta_sidecar_path(stored).symlink_to(fake_meta)
        attachment = ca.load_chat_pdf_attachment(tmp_path / "anima", rel)
        assert attachment is not None
        assert attachment.text_path is None and attachment.name == "doc.pdf" and attachment.pages is None
        assert "TOP SECRET" not in ca.build_pdf_prompt_context([attachment])
        assert ca.pdf_display_name(stored.resolve()) == "doc.pdf"

    def test_bad_meta_values_are_ignored(self, tmp_path: Path) -> None:
        rel = _store_pdf(
            tmp_path,
            "a.pdf",
            make_pdf(["x"]),
            display="<b>.pdf",
            meta={"pages": "999", "size": -5, "encrypted": "yes", "text_chars": True},
        )
        attachment = ca.load_chat_pdf_attachment(tmp_path, rel)
        assert attachment is not None
        assert attachment.pages is None and attachment.encrypted is False and attachment.text_chars == 0
        assert attachment.size == (tmp_path / rel).stat().st_size
        assert "<" not in attachment.name

    def test_load_many_dedupes_and_skips_images(self, tmp_path: Path) -> None:
        rel = _store_pdf(tmp_path, "a.pdf", make_pdf(["x"]))
        (tmp_path / "attachments" / "b.png").write_bytes(b"\x89PNG")
        loaded = ca.load_chat_pdf_attachments(tmp_path, [rel, rel, "attachments/b.png"])
        assert [a.rel_path for a in loaded] == [rel]


def test_describe_attachment_paths(tmp_path: Path) -> None:
    rel = _store_pdf(tmp_path, "a.pdf", make_pdf(["x"]), display="見積.pdf", meta={"pages": 3})
    (tmp_path / "attachments" / "20261009_1.jpeg").write_bytes(b"\xff\xd8\xff")
    entries = ca.describe_attachment_paths(
        tmp_path,
        [
            rel,
            "attachments/20261009_1.jpeg",
            "attachments/missing.png",
            "attachments/../x.pdf",
            42,
            "attachments/n.txt",
        ],
    )
    assert entries == [
        {
            "path": rel,
            "name": "見積.pdf",
            "media_type": "application/pdf",
            "size": (tmp_path / rel).stat().st_size,
            "pages": 3,
        },
        {"path": "attachments/20261009_1.jpeg", "media_type": "image/jpeg"},
    ]


def test_earlier_thread_pdfs_newest_first(tmp_path: Path) -> None:
    rels = [_store_pdf(tmp_path, f"{i}.pdf", make_pdf(["x"])) for i in range(4)]
    turns = [
        SimpleNamespace(attachments=[rels[0]]),
        SimpleNamespace(attachments=["attachments/img.png", rels[1]]),
        SimpleNamespace(attachments=[]),
        SimpleNamespace(attachments=[rels[2], rels[3]]),
    ]
    found = ca.earlier_thread_pdfs(tmp_path, turns, exclude=[rels[3]], limit=2)
    assert [a.rel_path for a in found] == [rels[2], rels[1]]


# ── selection / prompt context ────────────────────────────


def _attachments_dir(tmp_path: Path) -> Path:
    """Real ``<anima>/attachments`` directory (resolved, as the loaders return)."""
    folder = tmp_path / "anima" / "attachments"
    folder.mkdir(parents=True, exist_ok=True)
    return folder.resolve()


def _attachment(
    tmp_path: Path,
    name: str,
    *,
    size: int = 1000,
    pages: int | None = 2,
    encrypted: bool = False,
    text: str | None = "hello world",
    status: str = "ok",
) -> ca.ChatPdfAttachment:
    folder = _attachments_dir(tmp_path)
    path = folder / name
    text_path = None
    if text is not None:
        text_path = folder / f"{name}.txt"
        text_path.write_text(text, encoding="utf-8")
    return ca.ChatPdfAttachment(
        rel_path=f"attachments/{name}",
        path=path,
        name=name,
        size=size,
        pages=pages,
        encrypted=encrypted,
        text_status=status,
        text_chars=len(text or ""),
        text_path=text_path,
    )


class TestSelectSdkDocuments:
    def test_limits(self, tmp_path: Path) -> None:
        small = _attachment(tmp_path, "small.pdf")
        big = _attachment(tmp_path, "big.pdf", size=ca.SDK_DOCUMENT_MAX_BYTES + 1)
        long = _attachment(tmp_path, "long.pdf", pages=ca.SDK_DOCUMENT_MAX_PAGES + 1)
        locked = _attachment(tmp_path, "locked.pdf", encrypted=True)
        unknown = _attachment(tmp_path, "unknown.pdf", pages=None)
        assert ca.select_sdk_documents([small, big, long, locked, unknown]) == [small]

    def test_cumulative_caps(self, tmp_path: Path) -> None:
        docs = [_attachment(tmp_path, f"{i}.pdf", pages=12, size=1000) for i in range(3)]
        assert len(ca.select_sdk_documents(docs)) == 2  # 36 pages > 30
        heavy = [_attachment(tmp_path, f"h{i}.pdf", pages=1, size=4 * 1024 * 1024) for i in range(3)]
        assert len(ca.select_sdk_documents(heavy)) == 2  # 12 MiB > 8 MiB


class TestPromptContext:
    def test_empty(self) -> None:
        assert ca.build_pdf_prompt_context([]) == ""

    def test_document_attached_has_no_excerpt(self, tmp_path: Path) -> None:
        att = _attachment(tmp_path, "doc.pdf", text="SECRET BODY")
        context = ca.build_pdf_prompt_context([att], sdk_documents=[att])
        assert "doc.pdf" in context and str(att.path) in context and str(att.text_path) in context
        assert "SECRET BODY" not in context
        assert "BEGIN PDF TEXT" not in context

    def test_excerpt_for_engines_without_documents(self, tmp_path: Path) -> None:
        att = _attachment(tmp_path, "doc.pdf", text="SECRET BODY")
        context = ca.build_pdf_prompt_context([att])
        assert "SECRET BODY" in context
        begin = re.search(r"----- BEGIN PDF TEXT ([0-9a-f]{6}) -----", context)
        assert begin and f"----- END PDF TEXT {begin.group(1)} -----" in context

    def test_truncation_hint(self, tmp_path: Path) -> None:
        att = _attachment(tmp_path, "big.pdf", text="x" * (ca.PROMPT_TEXT_BUDGET_CHARS + 100))
        context = ca.build_pdf_prompt_context([att])
        assert context.count("x") <= ca.PROMPT_TEXT_BUDGET_CHARS + 50
        assert str(att.text_path) in context.split("----- END PDF TEXT")[1]

    def test_budget_shared_between_files(self, tmp_path: Path) -> None:
        first = _attachment(tmp_path, "a.pdf", text="a" * 40_000)
        second = _attachment(tmp_path, "b.pdf", text="b" * 40_000)
        context = ca.build_pdf_prompt_context([first, second])
        assert context.count("a" * 100) > 0 and context.count("b" * 100) > 0
        assert len(context) < 2 * (ca.PROMPT_TEXT_BUDGET_CHARS // 2) + 3000

    def test_scanned_and_unavailable_hints(self, tmp_path: Path) -> None:
        scanned = _attachment(tmp_path, "scan.pdf", text=None, status="empty")
        missing = _attachment(tmp_path, "nopoppler.pdf", text=None, status="unavailable")
        context = ca.build_pdf_prompt_context([scanned, missing])
        assert "pdftoppm" in context
        assert str(missing.path) in context

    def test_earlier_section(self, tmp_path: Path) -> None:
        old = _attachment(tmp_path, "old.pdf")
        context = ca.build_pdf_prompt_context([], earlier=[old])
        assert "old.pdf" in context and str(old.text_path) in context

    def test_append(self) -> None:
        assert ca.append_pdf_prompt_context("hi", "") == "hi"
        assert ca.append_pdf_prompt_context("  ", "CTX") == "CTX"
        assert ca.append_pdf_prompt_context("hi", "CTX") == "hi\n\nCTX"


# ── SDK document blocks (ContextVar) ──────────────────────


class TestSdkDocuments:
    def test_blocks_only_inside_context(self, tmp_path: Path) -> None:
        pdf_bytes = make_pdf(["x"])
        (_attachments_dir(tmp_path) / "doc.pdf").write_bytes(pdf_bytes)
        att = _attachment(tmp_path, "doc.pdf")
        assert ca.chat_sdk_document_blocks() == []
        with ca.chat_sdk_documents([att]):
            blocks = ca.chat_sdk_document_blocks()
        assert ca.chat_sdk_document_blocks() == []
        assert blocks == [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.b64encode(pdf_bytes).decode(),
                },
                "title": "doc.pdf",
            }
        ]

    def test_skips_missing_non_pdf_and_oversized(self, tmp_path: Path) -> None:
        (_attachments_dir(tmp_path) / "fake.pdf").write_bytes(b"not a pdf")
        (_attachments_dir(tmp_path) / "huge.pdf").write_bytes(b"%PDF-" + b"0" * ca.SDK_DOCUMENT_MAX_BYTES)
        docs = [_attachment(tmp_path, n) for n in ("missing.pdf", "fake.pdf", "huge.pdf")]
        with ca.chat_sdk_documents(docs):
            assert ca.chat_sdk_document_blocks() == []

    def test_reset_from_other_context_does_not_raise(self, tmp_path: Path) -> None:
        (_attachments_dir(tmp_path) / "doc.pdf").write_bytes(make_pdf(["x"]))
        att = _attachment(tmp_path, "doc.pdf")
        token = ca.set_chat_sdk_documents([att])
        foreign = contextvars.copy_context()
        assert len(foreign.run(ca.chat_sdk_document_blocks)) == 1
        # A foreign context cannot undo our binding (and must not raise) but
        # is itself cleared, so nothing more runs there with the documents ...
        foreign.run(ca.reset_chat_sdk_documents, token)
        assert foreign.run(ca.chat_sdk_document_blocks) == []
        assert len(ca.chat_sdk_document_blocks()) == 1
        # ... the publishing context does.
        ca.reset_chat_sdk_documents(token)
        assert ca.chat_sdk_document_blocks() == []

    async def test_generator_closed_early_cleans_up(self, tmp_path: Path) -> None:
        (_attachments_dir(tmp_path) / "doc.pdf").write_bytes(make_pdf(["x"]))
        att = _attachment(tmp_path, "doc.pdf")

        async def turn():
            token = ca.set_chat_sdk_documents([att])
            try:
                yield len(ca.chat_sdk_document_blocks())
                yield "never"
            finally:
                ca.reset_chat_sdk_documents(token)

        gen = turn()
        assert await gen.__anext__() == 1
        assert len(ca.chat_sdk_document_blocks()) == 1  # consumer shares the context mid-turn
        await gen.aclose()  # consumer stops early (client disconnect)
        assert ca.chat_sdk_document_blocks() == []

    async def test_task_visibility(self, tmp_path: Path) -> None:
        (_attachments_dir(tmp_path) / "doc.pdf").write_bytes(make_pdf(["x"]))
        att = _attachment(tmp_path, "doc.pdf")
        seen: dict[str, int] = {}

        async def probe(label: str) -> None:
            seen[label] = len(ca.chat_sdk_document_blocks())

        before = asyncio.create_task(probe("created_before_turn"))
        with ca.chat_sdk_documents([att]):
            during = asyncio.create_task(probe("created_during_turn"))  # e.g. an engine worker task
            await asyncio.gather(before, during)
        await asyncio.create_task(probe("created_after_turn"))
        assert seen == {"created_before_turn": 0, "created_during_turn": 1, "created_after_turn": 0}


# ── confined, symlink-free opens ──────────────────────────


@posix_only
class TestOpenConfined:
    def _read(self, fd: int | None) -> bytes | None:
        if fd is None:
            return None
        with os.fdopen(fd, "rb") as handle:
            return handle.read()

    def test_regular_file(self, tmp_path: Path) -> None:
        (_attachments_dir(tmp_path) / "a.pdf").write_bytes(b"%PDF-ok")
        assert self._read(ca.open_confined(tmp_path / "anima", "attachments", "a.pdf")) == b"%PDF-ok"

    def test_refuses_symlinked_file_and_dir(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "a.pdf").write_bytes(b"%PDF-secret")
        folder = _attachments_dir(tmp_path)
        (folder / "link.pdf").symlink_to(outside / "a.pdf")
        assert ca.open_confined(tmp_path / "anima", "attachments", "link.pdf") is None
        (tmp_path / "other").mkdir()
        (tmp_path / "other" / "attachments").symlink_to(outside)
        assert ca.open_confined(tmp_path / "other", "attachments", "a.pdf") is None

    def test_refuses_fifo_without_blocking(self, tmp_path: Path) -> None:
        os.mkfifo(_attachments_dir(tmp_path) / "pipe.pdf")
        assert ca.open_confined(tmp_path / "anima", "attachments", "pipe.pdf") is None

    @pytest.mark.parametrize("name", ["", ".", "..", "a/b.pdf", "..\\x.pdf"])
    def test_refuses_bad_names(self, tmp_path: Path, name: str) -> None:
        _attachments_dir(tmp_path)
        assert ca.open_confined(tmp_path / "anima", "attachments", name) is None

    def test_swap_after_check_is_not_followed(self, tmp_path: Path) -> None:
        # The check passes, then attachments/ is replaced by a symlink to a
        # directory holding a same-named secret: the later read must refuse.
        rel = _store_pdf(tmp_path / "anima", "doc.pdf", make_pdf(["mine"]))
        attachment = ca.load_chat_pdf_attachment(tmp_path / "anima", rel)
        assert attachment is not None
        evil = tmp_path / "evil"
        evil.mkdir()
        (evil / "doc.pdf").write_bytes(b"%PDF-1.4 SECRET")
        (evil / "doc.pdf.txt").write_text("SECRET TEXT", encoding="utf-8")
        real = tmp_path / "anima" / "attachments"
        real.rename(tmp_path / "anima" / "attachments.moved")
        real.symlink_to(evil)
        with ca.chat_sdk_documents([attachment]):
            assert ca.chat_sdk_document_blocks() == []
        assert ca._read_text_excerpt(attachment.path.with_name("doc.pdf.txt"), 100) == ""
        assert ca._read_meta(attachment.path) == {}


# ── writes through AttachmentStore ────────────────────────


@posix_only
class TestAttachmentStore:
    def test_write_new_and_refuse_overwrite(self, tmp_path: Path) -> None:
        with ca.AttachmentStore(tmp_path / "anima") as store:
            store.write_new("a.bin", b"one")
            with pytest.raises(FileExistsError):
                store.write_new("a.bin", b"two")
        folder = tmp_path / "anima" / "attachments"
        assert (folder / "a.bin").read_bytes() == b"one"
        assert sorted(p.name for p in folder.iterdir()) == ["a.bin"]  # no leftover temp files

    def test_never_writes_through_a_symlink(self, tmp_path: Path) -> None:
        victim = tmp_path / "victim.txt"
        victim.write_text("keep me", encoding="utf-8")
        folder = _attachments_dir(tmp_path)
        (folder / "x.bin").symlink_to(victim)
        with ca.AttachmentStore(tmp_path / "anima") as store, pytest.raises(FileExistsError):
            store.write_new("x.bin", b"evil")
        assert victim.read_text(encoding="utf-8") == "keep me"

    def test_refuses_symlinked_directory(self, tmp_path: Path) -> None:
        (tmp_path / "elsewhere").mkdir()
        (tmp_path / "anima").mkdir()
        (tmp_path / "anima" / "attachments").symlink_to(tmp_path / "elsewhere")
        with pytest.raises(OSError):
            ca.AttachmentStore(tmp_path / "anima")

    def test_directory_swapped_after_open_is_not_followed(self, tmp_path: Path) -> None:
        anima = tmp_path / "anima"
        store = ca.AttachmentStore(anima)
        (anima / "attachments").rename(anima / "attachments.real")
        (tmp_path / "evil").mkdir()
        (anima / "attachments").symlink_to(tmp_path / "evil")
        with store:
            store.write_new("doc.bin", b"data")
            assert store.exists("doc.bin")
            store.unlink("doc.bin")
            store.write_new("kept.bin", b"data")
        assert list((tmp_path / "evil").iterdir()) == []
        assert (anima / "attachments.real" / "kept.bin").read_bytes() == b"data"

    def test_store_chat_pdf_rolls_back(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        real = ca.AttachmentStore.write_new
        calls = {"n": 0}

        def flaky(self: ca.AttachmentStore, name: str, data: bytes) -> None:
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError("disk full")
            real(self, name, data)

        monkeypatch.setattr(ca.AttachmentStore, "write_new", flaky)
        with ca.AttachmentStore(tmp_path / "anima") as store, pytest.raises(OSError):
            ca.store_chat_pdf(
                store,
                data=make_pdf(["x"]),
                display_name="a.pdf",
                timestamp="t",
                inspection=ca.PdfInspection(ok=True, pages=1),
                text="x",
                status="ok",
            )
        assert list((tmp_path / "anima" / "attachments").iterdir()) == []


@needs_poppler
class TestAnalyseBytes:
    def test_reads_private_copy_only(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        private = tmp_path / "private-tmp"
        private.mkdir()
        monkeypatch.setattr(ca.tempfile, "tempdir", str(private))
        inspection, text, status = ca.analyse_pdf_bytes(make_pdf(["Private ALPHA-42", "two"]))
        assert inspection.pages == 2 and status == "ok" and "ALPHA-42" in text
        assert list(private.iterdir()) == []  # nothing left behind

    def test_scanned_pdf(self) -> None:
        inspection, text, status = ca.analyse_pdf_bytes(make_pdf(["", ""]))
        assert inspection.pages == 2 and text == "" and status == "empty"

    @posix_only
    def test_password_skips_text(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        TestPopplerFallbacks._fake_tool(tmp_path, monkeypatch, 'echo "Incorrect password" >&2; exit 1')
        inspection, text, status = ca.analyse_pdf_bytes(b"%PDF-1.4 locked")
        assert inspection.password_required and text == "" and status == "failed"


@posix_only
class TestRunCappedDescendants:
    def test_background_child_in_group_is_reaped(self) -> None:
        started = time.monotonic()
        result = ca._run_capped(["/bin/sh", "-c", "sleep 30 & echo hi"], stdout_cap=100, stderr_cap=100, timeout=10)
        assert result.stdout == b"hi\n" and result.returncode == 0
        assert time.monotonic() - started < 5

    def test_escaped_descendant_cannot_block_past_deadline(self) -> None:
        # The child leaves the process group (setsid) before the tool exits,
        # so the group kill cannot reach it and it keeps stdout open.
        escape = "import os, time; os.setsid(); time.sleep(4)"
        started = time.monotonic()
        result = ca._run_capped(
            ["/bin/sh", "-c", f'{sys.executable} -c "{escape}" & sleep 0.5; echo hi'],
            stdout_cap=100,
            stderr_cap=100,
            timeout=1.5,
        )
        assert time.monotonic() - started < 3.5
        assert result.stdout == b"hi\n" and result.returncode is None


# ── r5: process/memory/descriptor hygiene, session budget ─


@posix_only
class TestRunCappedHygiene:
    def test_escaped_holder_leaves_no_threads_or_pipes(self) -> None:
        import threading

        threads_before = threading.active_count()
        fds_before = len(os.listdir("/dev/fd"))
        escape = "import os, time; os.setsid(); time.sleep(3)"
        result = ca._run_capped(
            ["/bin/sh", "-c", f'{sys.executable} -c "{escape}" & sleep 0.5; echo hi'],
            stdout_cap=100,
            stderr_cap=100,
            timeout=1.5,
        )
        assert result.returncode is None
        assert threading.active_count() == threads_before
        assert len(os.listdir("/dev/fd")) <= fds_before

    def test_memory_hungry_tool_is_killed(self) -> None:
        hog = "import time; x = bytearray(400 * 1024 * 1024); time.sleep(10)"
        started = time.monotonic()
        result = ca._run_capped(
            [sys.executable, "-c", hog],
            stdout_cap=100,
            stderr_cap=100,
            timeout=8,
            max_rss_bytes=150 * 1024 * 1024,
        )
        assert result.returncode is None
        assert time.monotonic() - started < 6


class TestAttachmentStoreHygiene:
    @posix_only
    def test_failed_construction_closes_descriptor(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _attachments_dir(tmp_path)
        real_stat = os.stat

        def failing_stat(path, *args, **kwargs):  # type: ignore[no-untyped-def]
            if str(path).endswith("attachments") and not kwargs.get("dir_fd"):
                raise FileNotFoundError(path)
            return real_stat(path, *args, **kwargs)

        fds_before = len(os.listdir("/dev/fd"))
        monkeypatch.setattr(ca.os, "stat", failing_stat)
        for _ in range(5):
            with pytest.raises(OSError):
                ca.AttachmentStore(tmp_path / "anima")
        monkeypatch.setattr(ca.os, "stat", real_stat)
        assert len(os.listdir("/dev/fd")) <= fds_before

    def test_race_free_required_without_dir_fd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ca.os, "supports_dir_fd", set())
        with pytest.raises(OSError):
            ca.AttachmentStore(tmp_path / "anima", require_race_free=True)
        with ca.AttachmentStore(tmp_path / "anima") as store:  # images keep the path fallback
            store.write_new("img.png", b"png")
        assert (tmp_path / "anima" / "attachments" / "img.png").read_bytes() == b"png"


def test_session_budget_limits_native_documents(tmp_path: Path) -> None:
    docs = [_attachment(tmp_path, f"{i}.pdf", size=4 * 1024 * 1024, pages=2) for i in range(2)]
    assert len(ca.select_sdk_documents(docs)) == 2
    budget = ca.SDK_SESSION_DOCUMENT_BUDGET_BYTES
    assert len(ca.select_sdk_documents(docs, already_sent_bytes=budget - 5 * 1024 * 1024)) == 1
    assert ca.select_sdk_documents(docs, already_sent_bytes=budget) == []
    assert ca.native_document_eligible(docs[0])
    assert not ca.native_document_eligible(_attachment(tmp_path, "big.pdf", size=ca.SDK_DOCUMENT_MAX_BYTES + 1))


# ── r6: selectors, platform gate, session ledger ─────────


@posix_only
def test_run_capped_with_descriptors_above_fd_setsize() -> None:
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = 1400
    if hard != resource.RLIM_INFINITY and hard < wanted:
        pytest.skip("cannot raise the descriptor limit")
    resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, wanted), hard))
    filler: list[int] = []
    try:
        base = os.open(os.devnull, os.O_RDONLY)
        filler.append(base)
        while len(filler) < 1100:
            filler.append(os.dup(base))
        result = ca._run_capped(["/bin/sh", "-c", "echo hi"], stdout_cap=100, stderr_cap=100, timeout=10)
        assert result.stdout == b"hi\n" and result.returncode == 0
    finally:
        for fd in filler:
            os.close(fd)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_pdf_reads_fail_closed_without_dir_fd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = _attachments_dir(tmp_path)
    (folder / "a.pdf").write_bytes(make_pdf(["x"]))
    (folder / "a.pdf.txt").write_text("text", encoding="utf-8")
    (folder / "b.png").write_bytes(b"png")
    monkeypatch.setattr(ca.os, "supports_dir_fd", set())
    assert not ca.pdf_storage_supported()
    assert ca.open_confined(tmp_path / "anima", "attachments", "a.pdf") is None
    assert ca.open_confined(tmp_path / "anima", "attachments", "a.pdf.txt") is None
    fd = ca.open_confined(tmp_path / "anima", "attachments", "b.png")
    assert fd is not None
    os.close(fd)


class TestNativeUsageLedger:
    def test_accumulates_within_a_session(self, tmp_path: Path) -> None:
        ca.record_native_usage(tmp_path, "t1", "s1", 100, 2)
        ca.record_native_usage(tmp_path, "t1", "s1", 50, 1)
        assert ca.native_usage(tmp_path, "t1", "s1") == (150, 3)
        assert ca.native_usage(tmp_path, "t2", "s1") == (0, 0)  # per thread

    def test_new_or_cleared_session_starts_from_zero(self, tmp_path: Path) -> None:
        ca.record_native_usage(tmp_path, "t1", "s1", 100, 2)
        assert ca.native_usage(tmp_path, "t1", "s2") == (0, 0)
        assert ca.native_usage(tmp_path, "t1", None) == (0, 0)
        ca.record_native_usage(tmp_path, "t1", "s2", 10, 1)
        assert ca.native_usage(tmp_path, "t1", "s2") == (10, 1)

    def test_fresh_session_is_attributed_once_it_has_an_id(self, tmp_path: Path) -> None:
        ca.record_native_usage(tmp_path, "t1", None, 100, 2)  # sent into a brand-new session
        assert ca.native_usage(tmp_path, "t1", "s9") == (100, 2)

    def test_unknown_session_keeps_counts(self, tmp_path: Path) -> None:
        ca.record_native_usage(tmp_path, "t1", "s1", 100, 2)
        assert ca.native_usage(tmp_path, "t1", ca.UNKNOWN_SESSION) == (100, 2)

    def test_corrupt_ledger_is_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "state").mkdir()
        (tmp_path / "state" / ca.NATIVE_USAGE_FILE).write_text("{not json", encoding="utf-8")
        assert ca.native_usage(tmp_path, "t1", "s1") == (0, 0)


def test_page_budget_across_session(tmp_path: Path) -> None:
    docs = [_attachment(tmp_path, f"p{i}.pdf", size=1000, pages=20) for i in range(2)]
    budget = ca.SDK_SESSION_DOCUMENT_BUDGET_PAGES
    assert len(ca.select_sdk_documents(docs, already_sent_pages=budget - 25)) == 1
    assert ca.select_sdk_documents(docs, already_sent_pages=budget - 10) == []
