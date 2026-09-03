"""API tests for document attachments on POST /api/animas/{name}/chat."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from server.stream_registry import StreamRegistry
from tests.unit.core.test_document_attachments import make_cfb, make_docx, make_fib, make_workbook, make_xlsx

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _b64(content: bytes) -> str:
    return base64.b64encode(content).decode()


def _make_app(supervisor: MagicMock) -> object:
    from fastapi import FastAPI

    from server.routes.chat import create_chat_router

    app = FastAPI()
    app.state.animas = {}
    app.state.ws_manager = MagicMock()
    app.state.ws_manager.broadcast = AsyncMock()
    app.state.stream_registry = StreamRegistry()
    supervisor.is_bootstrapping = MagicMock(return_value=False)
    supervisor.processes = {"alice"}
    app.state.supervisor = supervisor
    app.include_router(create_chat_router(), prefix="/api")
    return app


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import core.paths as paths_module

    monkeypatch.setattr(paths_module, "get_data_dir", lambda: tmp_path)
    (tmp_path / "animas" / "alice").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def supervisor() -> MagicMock:
    sup = MagicMock()
    sup.send_request = AsyncMock(return_value={"response": "ok", "replied_to": []})
    return sup


async def _post(app, payload: dict):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/api/animas/alice/chat", json=payload)


async def test_docx_and_xlsx_are_saved_with_text_sidecars(data_dir: Path, supervisor: MagicMock) -> None:
    app = _make_app(supervisor)
    resp = await _post(
        app,
        {
            "message": "要約して",
            "files": [
                {"name": "議事録.docx", "media_type": DOCX, "data": _b64(make_docx(["決定事項", "次回 9/10"]))},
                {"name": "売上.xlsx", "media_type": XLSX, "data": _b64(make_xlsx([["month", "amt"], ["Aug", 12]]))},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    supervisor.send_request.assert_awaited_once()
    params = supervisor.send_request.await_args.kwargs["params"]
    paths = params["attachment_paths"]
    assert len(paths) == 2
    assert paths[0].startswith("attachments/") and paths[0].endswith(".docx")
    assert paths[1].endswith(".xlsx")
    anima_dir = data_dir / "animas" / "alice"
    docx_path = anima_dir / paths[0]
    assert docx_path.is_file()
    sidecar = docx_path.with_name(docx_path.name + ".txt")
    assert sidecar.read_text(encoding="utf-8") == "決定事項\n次回 9/10"
    xlsx_sidecar = anima_dir / (paths[1] + ".txt")
    assert "month\tamt" in xlsx_sidecar.read_text(encoding="utf-8")
    # The anima-side prompt context points at both the file and its sidecar.
    from core._anima_messaging import _with_document_attachment_context

    context = _with_document_attachment_context("要約して", paths, anima_dir)
    assert str(docx_path) in context
    assert str(sidecar) in context
    assert "untrusted data" in context


async def test_legacy_payload_without_files_still_works(data_dir: Path, supervisor: MagicMock) -> None:
    app = _make_app(supervisor)
    resp = await _post(app, {"message": "Hi"})
    assert resp.status_code == 200
    params = supervisor.send_request.await_args.kwargs["params"]
    assert params["attachment_paths"] == []
    assert params["images"] == []


async def test_filename_traversal_is_neutralised(data_dir: Path, supervisor: MagicMock) -> None:
    app = _make_app(supervisor)
    resp = await _post(
        app,
        {
            "message": "x",
            "files": [{"name": "../../../etc/passwd.txt", "media_type": "text/plain", "data": _b64(b"root:x")}],
        },
    )
    assert resp.status_code == 200, resp.text
    path = supervisor.send_request.await_args.kwargs["params"]["attachment_paths"][0]
    attachments_dir = (data_dir / "animas" / "alice" / "attachments").resolve()
    saved = (data_dir / "animas" / "alice" / path).resolve()
    assert saved.is_relative_to(attachments_dir)
    assert saved.name.endswith("passwd.txt")
    assert not (data_dir / "etc").exists()


@pytest.mark.parametrize(
    ("name", "media_type", "content"),
    [
        ("tool.exe", "application/octet-stream", b"MZ\x90\x00"),
        ("macro.docm", "application/vnd.ms-word.document.macroEnabled.12", b"PK\x03\x04"),
        ("sheet.xlsm", "application/vnd.ms-excel.sheet.macroEnabled.12", b"PK\x03\x04"),
        ("report.docx", "application/pdf", b"%PDF-1.7"),
        ("report.docx", DOCX, b"%PDF-1.7 disguised"),
        ("notes.txt", "text/plain", b"a\x00b"),
    ],
)
async def test_rejected_formats_return_413_and_save_nothing(
    data_dir: Path, supervisor: MagicMock, name: str, media_type: str, content: bytes
) -> None:
    app = _make_app(supervisor)
    resp = await _post(
        app, {"message": "x", "files": [{"name": name, "media_type": media_type, "data": _b64(content)}]}
    )
    assert resp.status_code == 413, resp.text
    assert "error" in resp.json()
    supervisor.send_request.assert_not_awaited()
    assert not (data_dir / "animas" / "alice" / "attachments").exists()


async def test_macro_enabled_docx_is_rejected(data_dir: Path, supervisor: MagicMock) -> None:
    app = _make_app(supervisor)
    resp = await _post(
        app,
        {"message": "x", "files": [{"name": "m.docx", "media_type": DOCX, "data": _b64(make_docx(["x"], macro=True))}]},
    )
    assert resp.status_code == 413
    assert "VBA" in resp.json()["error"]
    supervisor.send_request.assert_not_awaited()


async def test_legacy_office_formats_are_stored_without_sidecar(data_dir: Path, supervisor: MagicMock) -> None:
    app = _make_app(supervisor)
    resp = await _post(
        app,
        {
            "message": "x",
            "files": [
                {
                    "name": "old.doc",
                    "media_type": "application/msword",
                    "data": _b64(make_cfb(streams={"WordDocument": make_fib()})),
                },
                {
                    "name": "old.xls",
                    "media_type": "application/vnd.ms-excel",
                    "data": _b64(make_cfb(streams={"Workbook": make_workbook([0x00])})),
                },
                {"name": "win.csv", "media_type": "application/vnd.ms-excel", "data": _b64(b"a,b\n1,2\n")},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    paths = supervisor.send_request.await_args.kwargs["params"]["attachment_paths"]
    assert [Path(p).suffix for p in paths] == [".doc", ".xls", ".csv"]
    anima_dir = data_dir / "animas" / "alice"
    for p in paths:
        assert (anima_dir / p).is_file()
        assert not (anima_dir / (p + ".txt")).exists()


async def test_file_count_limit_is_enforced_server_side(data_dir: Path, supervisor: MagicMock) -> None:
    app = _make_app(supervisor)
    files = [{"name": f"f{i}.txt", "media_type": "text/plain", "data": _b64(b"ok")} for i in range(11)]
    resp = await _post(app, {"message": "x", "files": files})
    assert resp.status_code == 413
    assert "10" in resp.json()["error"]
    supervisor.send_request.assert_not_awaited()


async def test_stream_endpoint_rejects_invalid_files_before_streaming(data_dir: Path, supervisor: MagicMock) -> None:
    app = _make_app(supervisor)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/animas/alice/chat/stream",
            json={"message": "x", "files": [{"name": "x.exe", "media_type": "text/plain", "data": _b64(b"MZ")}]},
        )
    assert resp.status_code == 413
    assert not (data_dir / "animas" / "alice" / "attachments").exists()
