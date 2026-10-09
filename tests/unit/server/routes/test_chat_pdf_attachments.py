"""Chat PDF attachments: validation, storage, chat routes, serving and history."""
# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient

from core import chat_attachments as ca
from core.chat_attachments import PdfAttachmentError
from server.routes import chat_files
from server.routes.chat_files import prepare_files, save_files
from server.routes.chat_models import ChatRequest, FileAttachment
from server.stream_registry import StreamRegistry
from tests.helpers.pdf_fixtures import make_pdf

PDF = make_pdf(["Hello PDF ALPHA-42"])
PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16).decode()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _file(name: str = "report.pdf", data: bytes = PDF, media_type: str = "application/pdf") -> FileAttachment:
    return FileAttachment(data=_b64(data), media_type=media_type, name=name)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ANIMAWORKS_DATA_DIR", str(tmp_path))
    (tmp_path / "animas" / "alice").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def fake_poppler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic pdfinfo/pdftotext stand-ins (no poppler needed)."""
    monkeypatch.setattr(
        chat_files,
        "analyse_pdf_bytes",
        lambda data, **kw: (ca.PdfInspection(ok=True, pages=1), "Hello PDF ALPHA-42", "ok"),
    )


def _attachment_files(data_dir: Path) -> list[str]:
    folder = data_dir / "animas" / "alice" / "attachments"
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


# ── model ───────────────────────────────────────────────


def test_chat_request_files_default_empty() -> None:
    assert ChatRequest(message="hi").files == []
    req = ChatRequest(message="", files=[{"data": "QQ==", "name": "a.pdf"}])
    assert req.files[0].media_type == ""


# ── prepare_files ───────────────────────────────────────


class TestPrepareFiles:
    def test_ok(self) -> None:
        prepared = prepare_files([_file("見積書.PDF")])
        assert prepared[0].data == PDF and prepared[0].name == "見積書.pdf"

    def test_empty_type_with_pdf_name_ok(self) -> None:
        assert prepare_files([_file("scan.pdf", media_type="")])[0].name == "scan.pdf"

    def test_none(self) -> None:
        assert prepare_files([]) == []

    def test_too_many(self) -> None:
        with pytest.raises(PdfAttachmentError) as exc:
            prepare_files([_file(f"{i}.pdf") for i in range(ca.MAX_PDF_FILES + 1)])
        assert exc.value.status_code == 413

    def test_unsupported_type(self) -> None:
        with pytest.raises(PdfAttachmentError) as exc:
            prepare_files([_file("evil.exe", media_type="application/x-msdownload")])
        assert exc.value.status_code == 415
        assert "evil.exe.pdf" in str(exc.value)

    def test_not_a_pdf(self) -> None:
        with pytest.raises(PdfAttachmentError) as exc:
            prepare_files([_file("fake.pdf", data=b"MZ\x90\x00 definitely not")])
        assert exc.value.status_code == 415

    def test_invalid_base64(self) -> None:
        with pytest.raises(PdfAttachmentError) as exc:
            prepare_files([FileAttachment(data="!!!not base64!!!", media_type="application/pdf", name="a.pdf")])
        assert exc.value.status_code == 400

    def test_empty_payload(self) -> None:
        with pytest.raises(PdfAttachmentError) as exc:
            prepare_files([FileAttachment(data="", media_type="application/pdf", name="a.pdf")])
        assert exc.value.status_code == 400

    def test_file_too_large_rejected_before_decoding(self) -> None:
        huge = "A" * ((ca.MAX_PDF_BYTES // 3 + 10) * 4)
        with pytest.raises(PdfAttachmentError) as exc:
            prepare_files([FileAttachment(data=huge, media_type="application/pdf", name="big.pdf")])
        assert exc.value.status_code == 413 and "big.pdf" in str(exc.value)

    def test_total_too_large(self) -> None:
        part = b"%PDF-1.4\n" + b"0" * (ca.MAX_PDF_TOTAL_BYTES // 3)
        with pytest.raises(PdfAttachmentError) as exc:
            prepare_files([_file(f"{i}.pdf", data=part) for i in range(4)])
        assert exc.value.status_code == 413


# ── save_files ──────────────────────────────────────────


class TestSaveFiles:
    def test_writes_pdf_and_sidecars(self, data_dir: Path, fake_poppler: None) -> None:
        paths = save_files("alice", prepare_files([_file("Q3 報告.pdf")]))
        assert len(paths) == 1 and paths[0].startswith("attachments/") and paths[0].endswith("_Q3_報告.pdf")
        stored = data_dir / "animas" / "alice" / paths[0]
        assert stored.read_bytes() == PDF
        meta = json.loads(ca.meta_sidecar_path(stored).read_text(encoding="utf-8"))
        assert meta["name"] == "Q3 報告.pdf" and meta["pages"] == 1 and meta["size"] == len(PDF)
        assert ca.text_sidecar_path(stored).read_text(encoding="utf-8") == "Hello PDF ALPHA-42"
        assert not [n for n in _attachment_files(data_dir) if n.endswith(".part")]

    def test_password_protected_leaves_nothing(self, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            chat_files,
            "analyse_pdf_bytes",
            lambda data, **kw: (ca.PdfInspection(ok=False, password_required=True), "", "failed"),
        )
        with pytest.raises(PdfAttachmentError) as exc:
            save_files("alice", prepare_files([_file("locked.pdf")]))
        assert exc.value.status_code == 400 and "locked.pdf" in str(exc.value)
        assert _attachment_files(data_dir) == []

    def test_password_on_second_file_stores_nothing(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch, fake_poppler: None
    ) -> None:
        calls = {"n": 0}

        def analyse(data: bytes, **kw: object) -> tuple[ca.PdfInspection, str, str]:
            calls["n"] += 1
            locked = calls["n"] == 2
            return ca.PdfInspection(ok=not locked, pages=1, password_required=locked), "", "empty"

        monkeypatch.setattr(chat_files, "analyse_pdf_bytes", analyse)
        with pytest.raises(PdfAttachmentError):
            save_files("alice", prepare_files([_file("one.pdf"), _file("two.pdf")]))
        assert _attachment_files(data_dir) == []

    def test_storage_failure_mid_batch_rolls_back(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch, fake_poppler: None
    ) -> None:
        real = ca.AttachmentStore.write_new
        calls = {"n": 0}

        def flaky(self: ca.AttachmentStore, name: str, data: bytes) -> None:
            calls["n"] += 1
            if calls["n"] == 4:  # second PDF itself (after a.pdf + 2 sidecars)
                raise OSError("disk full")
            real(self, name, data)

        monkeypatch.setattr(ca.AttachmentStore, "write_new", flaky)
        with pytest.raises(PdfAttachmentError) as exc:
            save_files("alice", prepare_files([_file("a.pdf"), _file("b.pdf")]))
        assert exc.value.status_code == 500 and "b.pdf" in str(exc.value)
        assert _attachment_files(data_dir) == []

    def test_rejects_invalid_anima_name(self, data_dir: Path) -> None:
        with pytest.raises(ValueError):
            save_files("../escape", prepare_files([_file()]))

    def test_refuses_symlinked_attachments_dir(self, data_dir: Path, fake_poppler: None) -> None:
        other = data_dir / "animas" / "bob" / "attachments"
        other.mkdir(parents=True)
        (data_dir / "animas" / "alice" / "attachments").symlink_to(other)
        with pytest.raises(PdfAttachmentError) as exc:
            save_files("alice", prepare_files([_file()]))
        assert exc.value.status_code == 500
        assert list(other.iterdir()) == []

    def test_dotted_name_is_servable(self, data_dir: Path, fake_poppler: None) -> None:
        paths = save_files("alice", prepare_files([_file("report..draft.pdf")]))
        assert ".." not in paths[0]
        assert ca.pdf_display_name(data_dir / "animas" / "alice" / paths[0]) == "report..draft.pdf"

    def test_remove_saved_files(self, data_dir: Path, fake_poppler: None) -> None:
        paths = save_files("alice", prepare_files([_file("a.pdf"), _file("b.pdf")]))
        chat_files.remove_saved_files("alice", paths)
        assert _attachment_files(data_dir) == []

    def test_nothing_to_save(self, data_dir: Path) -> None:
        assert save_files("alice", []) == []


# ── routes ──────────────────────────────────────────────


def _make_app(*, bootstrapping: bool = False, processes: tuple[str, ...] = ("alice",)):
    from fastapi import FastAPI

    from core.supervisor.ipc import IPCResponse
    from server.routes.chat import create_chat_router

    captured: dict = {"calls": 0}
    sup = MagicMock()
    sup.processes = set(processes)
    sup.is_bootstrapping = MagicMock(return_value=bootstrapping)

    async def send_request(anima_name, method, params, timeout=60.0):
        captured["calls"] += 1
        captured["params"] = params
        return {"response": "ok", "replied_to": []}

    async def send_request_stream(anima_name, method, params, timeout=120.0):
        captured["calls"] += 1
        captured["params"] = params
        yield IPCResponse(
            id="t",
            stream=True,
            done=True,
            result={"response": "ok", "replied_to": [], "cycle_result": {"summary": "ok"}},
        )

    sup.send_request = send_request
    sup.send_request_stream = send_request_stream
    app = FastAPI()
    app.state.supervisor = sup
    app.state.stream_registry = StreamRegistry()
    app.state.ws_manager = MagicMock()
    app.state.ws_manager.broadcast = AsyncMock()
    app.include_router(create_chat_router(), prefix="/api")
    return app, captured


async def _post(app, path: str, body: dict):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(path, json=body)


def _body(**extra) -> dict:
    return {
        "message": "読んで",
        "files": [{"data": _b64(PDF), "media_type": "application/pdf", "name": "r.pdf"}],
        **extra,
    }


class TestStreamRoute:
    async def test_saves_pdf_and_forwards_path(self, data_dir: Path, fake_poppler: None) -> None:
        app, captured = _make_app()
        resp = await _post(
            app, "/api/animas/alice/chat/stream", _body(images=[{"data": PNG_B64, "media_type": "image/png"}])
        )
        assert resp.status_code == 200 and "event: done" in resp.text
        paths = captured["params"]["attachment_paths"]
        assert len(paths) == 2
        assert paths[0].endswith(".png") and paths[1].endswith("_r.pdf")
        assert "files" not in captured["params"]
        assert all(len(json.dumps(v)) < 4096 for k, v in captured["params"].items() if k != "images")
        assert (data_dir / "animas" / "alice" / paths[1]).read_bytes() == PDF

    async def test_invalid_pdf_rejected_without_side_effects(self, data_dir: Path) -> None:
        app, captured = _make_app()
        body = {
            "message": "x",
            "files": [{"data": _b64(b"not a pdf"), "media_type": "application/pdf", "name": "x.pdf"}],
        }
        resp = await _post(app, "/api/animas/alice/chat/stream", body)
        assert resp.status_code == 415
        assert "x.pdf" in resp.json()["detail"]
        assert captured["calls"] == 0 and _attachment_files(data_dir) == []

    async def test_password_protected_returns_400(self, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            chat_files,
            "analyse_pdf_bytes",
            lambda data, **kw: (ca.PdfInspection(ok=False, password_required=True), "", "failed"),
        )
        app, captured = _make_app()
        resp = await _post(app, "/api/animas/alice/chat/stream", _body())
        assert resp.status_code == 400 and "r.pdf" in resp.json()["detail"]
        assert captured["calls"] == 0 and _attachment_files(data_dir) == []

    async def test_bootstrapping_stores_nothing(self, data_dir: Path, fake_poppler: None) -> None:
        app, captured = _make_app(bootstrapping=True)
        resp = await _post(
            app, "/api/animas/alice/chat/stream", _body(images=[{"data": PNG_B64, "media_type": "image/png"}])
        )
        assert resp.status_code == 200 and "bootstrap" in resp.text
        assert _attachment_files(data_dir) == []

    async def test_resume_stores_nothing(self, data_dir: Path, fake_poppler: None) -> None:
        app, _captured = _make_app()
        resp = await _post(app, "/api/animas/alice/chat/stream", _body(resume="missing-stream"))
        assert resp.status_code in (200, 404)
        assert _attachment_files(data_dir) == []

    async def test_unknown_anima(self, data_dir: Path) -> None:
        app, _captured = _make_app()
        resp = await _post(app, "/api/animas/bob/chat/stream", _body())
        assert resp.status_code == 404
        assert not (data_dir / "animas" / "bob").exists()

    async def test_text_only_unchanged(self, data_dir: Path) -> None:
        app, captured = _make_app()
        resp = await _post(app, "/api/animas/alice/chat/stream", {"message": "hi"})
        assert resp.status_code == 200
        assert captured["params"]["attachment_paths"] == []

    async def test_broken_image_with_pdf_stores_nothing(self, data_dir: Path, fake_poppler: None) -> None:
        app, captured = _make_app()
        resp = await _post(
            app, "/api/animas/alice/chat/stream", _body(images=[{"data": "A", "media_type": "image/png"}])
        )
        assert resp.status_code == 400
        assert captured["calls"] == 0 and _attachment_files(data_dir) == []

    async def test_image_store_failure_rolls_back_pdfs(
        self, data_dir: Path, fake_poppler: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import server.routes.chat as chat_route

        def broken_save_images(name, images):
            raise OSError("disk full")

        monkeypatch.setattr(chat_route, "save_images", broken_save_images)
        app, captured = _make_app()
        resp = await _post(
            app, "/api/animas/alice/chat/stream", _body(images=[{"data": PNG_B64, "media_type": "image/png"}])
        )
        assert resp.status_code == 500
        assert captured["calls"] == 0 and _attachment_files(data_dir) == []


class TestBlockingRoute:
    async def test_saves_pdf(self, data_dir: Path, fake_poppler: None) -> None:
        app, captured = _make_app()
        resp = await _post(app, "/api/animas/alice/chat", _body())
        assert resp.status_code == 200
        assert captured["params"]["attachment_paths"][0].endswith("_r.pdf")

    async def test_unknown_anima_with_pdf(self, data_dir: Path) -> None:
        app, captured = _make_app()
        resp = await _post(app, "/api/animas/bob/chat", _body())
        assert resp.status_code == 404 and captured["calls"] == 0
        assert not (data_dir / "animas" / "bob").exists()

    async def test_invalid_pdf_json_error(self, data_dir: Path) -> None:
        app, captured = _make_app()
        body = {"message": "x", "files": [{"data": _b64(b"nope"), "media_type": "application/pdf", "name": "x.pdf"}]}
        resp = await _post(app, "/api/animas/alice/chat", body)
        assert resp.status_code == 415 and "x.pdf" in resp.json()["error"]
        assert captured["calls"] == 0

    async def test_broken_image_with_pdf_stores_nothing(self, data_dir: Path, fake_poppler: None) -> None:
        app, captured = _make_app()
        resp = await _post(app, "/api/animas/alice/chat", _body(images=[{"data": "A", "media_type": "image/png"}]))
        assert resp.status_code == 400 and resp.json()["error"]
        assert captured["calls"] == 0 and _attachment_files(data_dir) == []


# ── attachment serving ─────────────────────────────────


async def test_pdf_attachment_served_inline_with_original_name(tmp_path: Path) -> None:
    from fastapi import FastAPI

    from server.routes.assets import create_assets_router

    attachments = tmp_path / "alice" / "attachments"
    attachments.mkdir(parents=True)
    stored = attachments / "20261009_110000_0123abcd_見積書.pdf"
    stored.write_bytes(PDF)
    ca.meta_sidecar_path(stored).write_text(json.dumps({"name": "見積書 (最終).pdf"}), encoding="utf-8")
    (attachments / "20261009_110000_0.png").write_bytes(b"\x89PNG")

    app = FastAPI()
    app.state.animas_dir = tmp_path
    app.include_router(create_assets_router(), prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/animas/alice/attachments/{quote(stored.name)}")
        png = await client.get("/api/animas/alice/attachments/20261009_110000_0.png")
        bad = await client.get("/api/animas/alice/attachments/..%2Fsecret.pdf")
    assert resp.status_code == 200 and resp.content == PDF
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["x-content-type-options"] == "nosniff"
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith("inline;") and quote("見積書 (最終).pdf") in disposition
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert "content-disposition" not in png.headers
    assert bad.status_code in (400, 404)


async def test_attachment_serving_refuses_symlinks_and_dot_names(tmp_path: Path) -> None:
    from fastapi import FastAPI

    from server.routes.assets import create_assets_router

    secret = tmp_path / "secret.pdf"
    secret.write_bytes(PDF)
    (tmp_path / "bob" / "attachments").mkdir(parents=True)
    (tmp_path / "bob" / "attachments" / "b.pdf").write_bytes(PDF)
    (tmp_path / "alice" / "attachments").mkdir(parents=True)
    (tmp_path / "alice" / "attachments" / "link.pdf").symlink_to(secret)
    (tmp_path / "carol").mkdir()
    (tmp_path / "carol" / "attachments").symlink_to(tmp_path / "bob" / "attachments")
    (tmp_path / "attachments").mkdir()
    (tmp_path / "attachments" / "top.pdf").write_bytes(PDF)

    app = FastAPI()
    app.state.animas_dir = tmp_path
    app.include_router(create_assets_router(), prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        link = await client.get("/api/animas/alice/attachments/link.pdf")
        alias = await client.get("/api/animas/carol/attachments/b.pdf")
        own = await client.get("/api/animas/bob/attachments/b.pdf")
        dotdot = await client.get("/api/animas/../attachments/top.pdf")
        dot_name = await client.get("/api/animas/%2E%2E/attachments/top.pdf")
    assert link.status_code == 404
    assert alias.status_code == 404
    assert own.status_code == 200 and own.headers["content-type"] == "application/pdf"
    assert dotdot.status_code == 404
    assert dot_name.status_code == 404


# ── history ─────────────────────────────────────────────


def test_activity_history_carries_sanitised_attachments() -> None:
    from core.memory.activity.conversation import _history_attachments

    meta = {
        "attachments": [
            {
                "path": "attachments/a.pdf",
                "name": "a.pdf",
                "media_type": "application/pdf",
                "size": 3,
                "pages": 1,
                "x": 1,
            },
            {"path": "attachments/../../etc/passwd", "name": "p"},
            {"path": "attachments/sub/b.pdf"},
            {"path": "/abs/c.pdf"},
            "not-a-dict",
            {"path": "attachments/d.jpeg", "media_type": "image/jpeg"},
        ]
    }
    assert _history_attachments(meta) == [
        {"path": "attachments/a.pdf", "name": "a.pdf", "media_type": "application/pdf", "size": 3, "pages": 1},
        {"path": "attachments/d.jpeg", "media_type": "image/jpeg"},
    ]
    assert _history_attachments({}) == [] and _history_attachments(None) == []
    many = {"attachments": [{"path": f"attachments/{i}.pdf"} for i in range(50)]}
    assert len(_history_attachments(many)) == 20


def test_conversation_view_includes_human_attachments(tmp_path: Path) -> None:
    from core.memory.activity.logger import ActivityLogger

    anima_dir = tmp_path / "alice"
    anima_dir.mkdir()
    activity = ActivityLogger(anima_dir)
    attachments = [{"path": "attachments/x.pdf", "name": "x.pdf", "media_type": "application/pdf", "size": 10}]
    activity.log(
        "message_received",
        content="",
        summary="[x.pdf]",
        from_person="human",
        channel="chat",
        meta={"from_type": "human", "thread_id": "default", "attachments": attachments},
    )
    activity.log(
        "message_received", content="plain", from_person="human", channel="chat", meta={"thread_id": "default"}
    )
    view = activity.get_conversation_view(limit=10, thread_id="default")
    humans = [m for s in view["sessions"] for m in s["messages"] if m["role"] == "human"]
    assert humans[0]["attachments"] == attachments
    assert "attachments" not in humans[1]


async def test_thread_file_history_includes_attachments(tmp_path: Path) -> None:
    from fastapi import FastAPI

    from server.routes.sessions import create_sessions_router

    anima_dir = tmp_path / "alice"
    (anima_dir / "attachments").mkdir(parents=True)
    stored = anima_dir / "attachments" / "20261009_110000_0123abcd_doc.pdf"
    stored.write_bytes(PDF)
    ca.meta_sidecar_path(stored).write_text(json.dumps({"name": "doc.pdf", "pages": 1}), encoding="utf-8")
    conv_dir = anima_dir / "state" / "conversations"
    conv_dir.mkdir(parents=True)
    turns = [
        {
            "role": "human",
            "content": "see",
            "timestamp": "2026-10-09T11:00:00",
            "attachments": [f"attachments/{stored.name}"],
        },
        {"role": "assistant", "content": "ok", "timestamp": "2026-10-09T11:00:05"},
    ]
    (conv_dir / "t1.json").write_text(json.dumps({"turns": turns}), encoding="utf-8")

    app = FastAPI()
    app.state.animas_dir = tmp_path
    app.include_router(create_sessions_router(), prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/animas/alice/conversation/history?thread_id=t1")
    assert resp.status_code == 200
    human = [m for s in resp.json()["sessions"] for m in s["messages"] if m["role"] == "human"][0]
    assert human["attachments"] == [
        {
            "path": f"attachments/{stored.name}",
            "name": "doc.pdf",
            "media_type": "application/pdf",
            "size": len(PDF),
            "pages": 1,
        }
    ]


async def test_attachment_serving_tmp_fallback_and_special_files(tmp_path: Path) -> None:
    import os

    from fastapi import FastAPI

    from server.routes.assets import create_assets_router

    animas = tmp_path / "animas"
    (animas / "alice" / "attachments").mkdir(parents=True)
    os.mkfifo(animas / "alice" / "attachments" / "pipe.pdf")
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(PDF)
    shared = tmp_path / "tmp" / "attachments"
    shared.mkdir(parents=True)
    (shared / "leak.pdf").symlink_to(secret)
    (shared / "shared.png").write_bytes(b"\x89PNG-shared")

    app = FastAPI()
    app.state.animas_dir = animas
    app.include_router(create_assets_router(), prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        leak = await client.get("/api/animas/alice/attachments/leak.pdf")
        pipe = await client.get("/api/animas/alice/attachments/pipe.pdf")
        shared_png = await client.get("/api/animas/alice/attachments/shared.png")
        head = await client.head("/api/animas/alice/attachments/shared.png")
    assert leak.status_code == 404
    assert pipe.status_code == 404
    assert shared_png.status_code == 200 and shared_png.content == b"\x89PNG-shared"
    assert head.status_code == 200 and head.headers["content-length"] == str(len(b"\x89PNG-shared"))
    assert head.content == b""


def test_inline_disposition_header() -> None:
    from server.routes.assets import _inline_disposition

    assert _inline_disposition("report.pdf") == "inline; filename=\"report.pdf\"; filename*=UTF-8''report.pdf"
    header = _inline_disposition('見積書 "最終".pdf')
    assert header.startswith("inline; filename=\"document.pdf\"; filename*=UTF-8''")
    assert '"' not in header.split("filename*=", 1)[1]


def test_save_images_all_or_nothing(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.routes.chat_images import save_images
    from server.routes.chat_models import ImageAttachment

    real = ca.AttachmentStore.write_new
    calls = {"n": 0}

    def flaky(self: ca.AttachmentStore, name: str, data: bytes) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        real(self, name, data)

    monkeypatch.setattr(ca.AttachmentStore, "write_new", flaky)
    images = [ImageAttachment(data=PNG_B64, media_type="image/png") for _ in range(3)]
    with pytest.raises(OSError):
        save_images("alice", images)
    assert _attachment_files(data_dir) == []


def test_save_images_unique_names_in_same_second(data_dir: Path) -> None:
    from server.routes.chat_images import save_images
    from server.routes.chat_models import ImageAttachment

    first = save_images("alice", [ImageAttachment(data=PNG_B64, media_type="image/png")])
    second = save_images("alice", [ImageAttachment(data=PNG_B64, media_type="image/png")])
    assert first != second
    assert len(_attachment_files(data_dir)) == 2


def test_attachment_route_closes_descriptors(tmp_path: Path) -> None:
    import asyncio
    import os

    from fastapi import FastAPI

    from server.routes.assets import create_assets_router

    (tmp_path / "alice" / "attachments").mkdir(parents=True)
    (tmp_path / "alice" / "attachments" / "a.pdf").write_bytes(PDF)
    app = FastAPI()
    app.state.animas_dir = tmp_path
    app.include_router(create_assets_router(), prefix="/api")

    async def hit() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for _ in range(3):
                assert (await client.get("/api/animas/alice/attachments/a.pdf")).status_code == 200
                assert (await client.head("/api/animas/alice/attachments/a.pdf")).status_code == 200

    asyncio.run(hit())  # warm-up (imports, thread pool)
    before = len(os.listdir("/dev/fd"))
    asyncio.run(hit())
    assert len(os.listdir("/dev/fd")) <= before


def _assets_app(animas_dir: Path):
    from fastapi import FastAPI

    from server.routes.assets import create_assets_router

    app = FastAPI()
    app.state.animas_dir = animas_dir
    app.include_router(create_assets_router(), prefix="/api")
    return app


async def test_request_cancelled_while_opening_does_not_leak_the_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker thread still finishes the open; its descriptor must be closed, not dropped."""
    import asyncio
    import os
    import threading

    from server.routes import assets

    (tmp_path / "alice" / "attachments").mkdir(parents=True)
    (tmp_path / "alice" / "attachments" / "doc.pdf").write_bytes(PDF)
    real_open = assets._open_attachment
    entered, release = threading.Event(), threading.Event()
    opened: list[int] = []

    def slow_open(anima_dir: Path, tmp_dir: Path, name: str):  # type: ignore[no-untyped-def]
        entered.set()
        release.wait(10)
        result = real_open(anima_dir, tmp_dir, name)
        if result is not None:
            opened.append(result[1])
        return result

    monkeypatch.setattr(assets, "_open_attachment", slow_open)
    async with AsyncClient(transport=ASGITransport(app=_assets_app(tmp_path)), base_url="http://test") as client:
        request = asyncio.create_task(client.get("/api/animas/alice/attachments/doc.pdf"))
        assert await asyncio.to_thread(entered.wait, 10)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
    release.set()
    for _ in range(200):
        if opened:
            break
        await asyncio.sleep(0.01)
    assert opened, "the worker finished its open"
    await asyncio.sleep(0.05)  # the completion callback runs on the event loop
    try:
        with pytest.raises(OSError):
            os.fstat(opened[0])
    finally:
        try:  # do not leak it into other tests if the assertion failed
            os.close(opened[0])
        except OSError:
            pass


async def test_attachment_streams_large_file_in_chunks(tmp_path: Path) -> None:
    big = b"%PDF-1.4\n" + bytes(range(256)) * 1200  # ~300 KB, several chunks
    (tmp_path / "alice" / "attachments").mkdir(parents=True)
    (tmp_path / "alice" / "attachments" / "big.pdf").write_bytes(big)
    async with AsyncClient(transport=ASGITransport(app=_assets_app(tmp_path)), base_url="http://test") as client:
        resp = await client.get("/api/animas/alice/attachments/big.pdf")
        head = await client.head("/api/animas/alice/attachments/big.pdf")
    assert resp.status_code == 200 and resp.content == big
    assert resp.headers["content-length"] == str(len(big))
    assert head.status_code == 200 and head.content == b"" and head.headers["content-length"] == str(len(big))


async def test_descriptor_closed_on_disconnect_and_unsent_response(tmp_path: Path) -> None:
    import gc
    import os

    from server.routes.assets import _DescriptorResponse

    data = b"%PDF-1.4\n" + b"x" * 300_000
    path = tmp_path / "f.pdf"
    path.write_bytes(data)
    fds_before = len(os.listdir("/dev/fd"))

    # client goes away after the first chunk
    resp = _DescriptorResponse(
        os.open(path, os.O_RDONLY), size=len(data), head_only=False, headers={}, media_type="application/pdf"
    )
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)
        if message["type"] == "http.response.body":
            raise OSError("client disconnected")

    async def never_disconnects() -> dict:
        import anyio

        await anyio.sleep_forever()
        return {}

    await resp({"type": "http"}, never_disconnects, send)  # disconnect ends the response quietly
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]

    # response created but never sent (e.g. dropped by middleware)
    unsent = _DescriptorResponse(
        os.open(path, os.O_RDONLY), size=len(data), head_only=False, headers={}, media_type="application/pdf"
    )
    del unsent
    gc.collect()
    assert len(os.listdir("/dev/fd")) <= fds_before


async def test_disconnect_stops_streaming_early(tmp_path: Path) -> None:
    """ASGI 2.3 servers drop send() silently after a disconnect: the response
    must notice http.disconnect itself and stop reading."""
    import os

    from server.routes.assets import _DescriptorResponse

    data = b"%PDF-1.4\n" + b"y" * (4 * 1024 * 1024)
    path = tmp_path / "big.pdf"
    path.write_bytes(data)
    fds_before = len(os.listdir("/dev/fd"))
    resp = _DescriptorResponse(
        os.open(path, os.O_RDONLY), size=len(data), head_only=False, headers={}, media_type="application/pdf"
    )
    bodies: list[int] = []
    disconnected = {"now": False}

    async def send(message: dict) -> None:  # never raises, like ASGI 2.3
        if message["type"] == "http.response.body":
            bodies.append(len(message["body"]))
            if len(bodies) == 2:
                disconnected["now"] = True

    async def receive() -> dict:
        import anyio

        while not disconnected["now"]:
            await anyio.sleep(0.001)
        return {"type": "http.disconnect"}

    await resp({"type": "http"}, receive, send)
    assert sum(bodies) < len(data) // 4
    assert len(os.listdir("/dev/fd")) <= fds_before


class TestUploadAdmission:
    async def test_cancelled_queued_upload_gives_its_slot_back(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A request cancelled while its job waits in the executor queue must
        not keep its admission slot (the job never runs, so nothing else
        would release it)."""
        import asyncio
        import threading
        from concurrent.futures import ThreadPoolExecutor

        gate = threading.Event()
        calls: list[str] = []

        def slow_save(anima_name: str, prepared: list) -> list[str]:
            calls.append(anima_name)
            gate.wait(10)
            return [f"attachments/{anima_name}.pdf"]

        executor = ThreadPoolExecutor(max_workers=1)
        monkeypatch.setattr(chat_files, "_ANALYSIS_EXECUTOR", executor)
        monkeypatch.setattr(chat_files, "_UPLOAD_SLOTS", threading.BoundedSemaphore(2))
        monkeypatch.setattr(chat_files, "save_files", slow_save)
        prepared = prepare_files([_file()])
        try:
            running = asyncio.create_task(chat_files.store_files("first", prepared))
            await asyncio.sleep(0.05)
            queued = asyncio.create_task(chat_files.store_files("second", prepared))
            await asyncio.sleep(0.05)
            queued.cancel()  # e.g. the client went away
            with pytest.raises(asyncio.CancelledError):
                await queued
            gate.set()
            assert await running == ["attachments/first.pdf"]
            await asyncio.sleep(0.05)
            assert calls == ["first"]  # the cancelled job never ran
            slots = chat_files._UPLOAD_SLOTS
            assert slots.acquire(blocking=False) and slots.acquire(blocking=False)  # both slots free again
        finally:
            gate.set()
            executor.shutdown(wait=True)

    async def test_busy_when_all_slots_taken(
        self, data_dir: Path, fake_poppler: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import threading

        monkeypatch.setattr(chat_files, "_UPLOAD_SLOTS", threading.BoundedSemaphore(1))
        assert chat_files._UPLOAD_SLOTS.acquire(blocking=False)  # someone else is uploading
        with pytest.raises(PdfAttachmentError) as exc:
            await chat_files.store_files("alice", prepare_files([_file()]))
        assert exc.value.status_code == 503
        chat_files._UPLOAD_SLOTS.release()
        assert _attachment_files(data_dir) == []

    async def test_runs_on_dedicated_executor_and_releases_slot(
        self, data_dir: Path, fake_poppler: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import threading

        seen: list[str] = []
        real = chat_files.save_files

        def spy(anima_name: str, prepared: list) -> list[str]:
            seen.append(threading.current_thread().name)
            return real(anima_name, prepared)

        monkeypatch.setattr(chat_files, "save_files", spy)
        monkeypatch.setattr(chat_files, "_UPLOAD_SLOTS", threading.BoundedSemaphore(1))
        paths = await chat_files.store_files("alice", prepare_files([_file()]))
        assert paths and seen and seen[0].startswith("chat-pdf")
        assert chat_files._UPLOAD_SLOTS.acquire(blocking=False)  # slot was given back

    def test_unsupported_platform_refuses_before_analysis(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called: list[bytes] = []
        monkeypatch.setattr(chat_files, "pdf_storage_supported", lambda: False)
        monkeypatch.setattr(chat_files, "analyse_pdf_bytes", lambda data, **kw: called.append(data))
        with pytest.raises(PdfAttachmentError) as exc:
            save_files("alice", prepare_files([_file()]))
        assert exc.value.status_code == 501 and called == []


async def test_download_through_http_middlewares(tmp_path: Path) -> None:
    """The server stacks several ``@app.middleware("http")`` layers
    (BaseHTTPMiddleware): the disconnect watcher must cooperate with them."""
    big = b"%PDF-1.4\n" + bytes(range(256)) * 1200
    (tmp_path / "alice" / "attachments").mkdir(parents=True)
    (tmp_path / "alice" / "attachments" / "big.pdf").write_bytes(big)
    app = _assets_app(tmp_path)
    for _ in range(4):

        @app.middleware("http")
        async def passthrough(request, call_next):  # type: ignore[no-untyped-def]
            return await call_next(request)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for _ in range(3):
            resp = await client.get("/api/animas/alice/attachments/big.pdf")
            head = await client.head("/api/animas/alice/attachments/big.pdf")
            assert resp.status_code == 200 and resp.content == big
            assert head.status_code == 200 and head.content == b""
            assert head.headers["content-length"] == str(len(big))
