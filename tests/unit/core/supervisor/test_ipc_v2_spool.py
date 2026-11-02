"""Oversized IPC v2 ``run`` params (large chat images) travel via a spool file."""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from core.runtime.ipc_v2 import (
    IPC_V2_MAX_FRAME_BYTES,
    IPCV2ConnectionState,
    IPCV2Envelope,
    IPCV2Identity,
    IPCV2ProtocolError,
)
from core.runtime.ipc_v2_spool import (
    IPC_V2_SPOOL_KEY,
    encode_params,
    load_spooled_run_params,
    spool_dir_for_socket,
    spool_run_params_if_large,
    sweep_orphan_spool_files,
)


def _large_chat_params(image_bytes: int = 4_400_000) -> dict[str, Any]:
    """Chat run params shaped like the 2026-11-02 failure (≈5.8 MB frame)."""
    image = base64.b64encode(os.urandom(image_bytes)).decode("ascii")
    return {
        "kind": "message",
        "payload": {
            "message": "この画像を見て",
            "from_person": "okoso",
            "stream": True,
            "images": [{"data": image, "media_type": "image/png"}],
            "attachment_paths": ["attachments/20261102_150537_0.png"],
            "thread_id": "default",
        },
        "environment": {"urls": {"ANIMAWORKS_EMBED_URL": "http://127.0.0.1:1/embed"}},
    }


@pytest.fixture
def short_dir() -> Path:
    # AF_UNIX paths are limited to ~104 bytes on macOS; pytest tmp_path is too long.
    path = Path(tempfile.mkdtemp(prefix="awsp", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_small_params_stay_inline(tmp_path: Path) -> None:
    params = {"kind": "message", "payload": {"message": "hi"}}
    wire, spool_path = spool_run_params_if_large(params, spool_dir=tmp_path / "spool", job_id="chat-1")
    assert wire is params
    assert spool_path is None
    assert not (tmp_path / "spool").exists()


def test_large_params_round_trip_through_private_spool_file(tmp_path: Path) -> None:
    params = _large_chat_params()
    assert len(encode_params(params)) > IPC_V2_MAX_FRAME_BYTES
    spool_dir = tmp_path / "spool"

    wire, spool_path = spool_run_params_if_large(params, spool_dir=spool_dir, job_id="chat-stream-abc")

    assert spool_path == spool_dir / "chat-stream-abc.json"
    assert set(wire) == {IPC_V2_SPOOL_KEY}
    assert len(encode_params(wire)) < 1024
    assert stat.S_IMODE(spool_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(spool_dir.stat().st_mode) == 0o700
    assert list(spool_dir.iterdir()) == [spool_path]  # no temp file left behind
    assert load_spooled_run_params(wire, spool_dir=spool_dir, job_id="chat-stream-abc") == params


def test_inline_params_pass_through_loader(tmp_path: Path) -> None:
    params = {"kind": "message", "payload": {"message": "hi"}}
    assert load_spooled_run_params(params, spool_dir=tmp_path, job_id="chat-1") is params


@pytest.mark.parametrize(
    "mutate",
    [
        lambda ref, path: ref.__setitem__("name", "../../etc/passwd"),
        lambda ref, path: ref.__setitem__("bytes", True),
        lambda ref, path: ref.__setitem__("sha256", "0" * 64),
        lambda ref, path: path.write_bytes(path.read_bytes()[:-1] + b" "),
        lambda ref, path: path.unlink(),
    ],
    ids=["foreign-name", "bool-bytes", "bad-sha", "tampered", "missing"],
)
def test_loader_rejects_mismatched_references(tmp_path: Path, mutate: Any) -> None:
    wire, spool_path = spool_run_params_if_large(_large_chat_params(), spool_dir=tmp_path, job_id="chat-x")
    assert spool_path is not None
    ref = dict(wire[IPC_V2_SPOOL_KEY])
    mutate(ref, spool_path)
    with pytest.raises(IPCV2ProtocolError):
        load_spooled_run_params({IPC_V2_SPOOL_KEY: ref}, spool_dir=tmp_path, job_id="chat-x")


def test_loader_resolves_path_from_own_job_id_only(tmp_path: Path) -> None:
    wire, _ = spool_run_params_if_large(_large_chat_params(), spool_dir=tmp_path, job_id="chat-a")
    with pytest.raises(IPCV2ProtocolError):
        load_spooled_run_params(wire, spool_dir=tmp_path, job_id="chat-b")
    with pytest.raises(IPCV2ProtocolError):
        load_spooled_run_params(wire, spool_dir=tmp_path, job_id="../chat-a")


def test_spool_dir_is_per_endpoint_and_private(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    alice = spool_dir_for_socket(run_dir / "sockets" / "alice.task-v2.sock")
    bob = spool_dir_for_socket(run_dir / "sockets" / "bob.task-v2.sock")
    assert alice == run_dir / "ipc-v2-spool" / "alice.task-v2"
    assert alice != bob
    spool_run_params_if_large(_large_chat_params(), spool_dir=alice, job_id="chat-1")
    assert stat.S_IMODE(alice.stat().st_mode) == 0o700
    assert stat.S_IMODE(alice.parent.stat().st_mode) == 0o700


def test_sweep_removes_every_file_not_owned_by_a_live_job(tmp_path: Path) -> None:
    live = tmp_path / "chat-live.json"
    live_tmp = tmp_path / ".chat-live.json.123.tmp"
    orphan = tmp_path / "chat-dead.json"
    orphan_tmp = tmp_path / ".chat-dead.json.123.tmp"
    other = tmp_path / "keep.txt"
    for path in (live, live_tmp, orphan, orphan_tmp, other):
        path.write_text("{}")
    assert sweep_orphan_spool_files(tmp_path, {"chat-live"}) == 2
    assert live.exists() and live_tmp.exists() and other.exists()
    assert not orphan.exists() and not orphan_tmp.exists()
    assert sweep_orphan_spool_files(tmp_path / "absent", set()) == 0


@pytest.mark.asyncio
async def test_real_ipc_v2_delivers_six_mb_image_run_params(
    short_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: ``IPC v2 frame is 5787742 bytes; maximum is 4194304``."""
    monkeypatch.setenv("ANIMAWORKS_PROCESS_ROLE", "anima")
    from core.runtime.task_runner import _connect
    from core.runtime.task_runner_supervisor import TaskRunnerJob, TaskRunnerSupervisor

    params = _large_chat_params()
    identity = IPCV2Identity(
        job_id=f"chat-stream-{uuid.uuid4()}",
        root_epoch=str(uuid.uuid4()),
        attempt=1,
        lane="chat",
        display_lane="chat",
    )
    socket_path = short_dir / "sockets" / "a.sock"
    socket_path.parent.mkdir()
    spool_dir = spool_dir_for_socket(socket_path)
    wire, spool_path = spool_run_params_if_large(params, spool_dir=spool_dir, job_id=identity.job_id)

    loop = asyncio.get_running_loop()
    supervisor = TaskRunnerSupervisor("alice", short_dir / "animas" / "alice", short_dir / "shared")
    job = TaskRunnerJob(
        identity=identity,
        request_id="run-contract",
        params=params,
        result=loop.create_future(),
        peer_state=IPCV2ConnectionState(identity),
        wire_params=wire,
        params_spool_path=spool_path,
    )
    supervisor.jobs[identity.job_id] = job
    server = await asyncio.start_unix_server(
        supervisor._handle_connection,
        path=str(socket_path),
        limit=IPC_V2_MAX_FRAME_BYTES + 1,
    )
    try:
        connection, run_envelope = await asyncio.wait_for(
            _connect(socket_path, IPCV2ConnectionState(identity)), timeout=10.0
        )
        assert len(run_envelope.to_bytes()) < 4096
        loaded = load_spooled_run_params(
            run_envelope.body["params"],
            spool_dir=spool_dir_for_socket(socket_path),
            job_id=identity.job_id,
        )
        assert loaded == params
        assert not job.result.done()  # no PROTOCOL_ERROR was recorded for the job
        await connection.close()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_without_spool_the_same_params_overflow_the_frame() -> None:
    identity = IPCV2Identity(
        job_id="chat-overflow",
        root_epoch=str(uuid.uuid4()),
        attempt=1,
        lane="chat",
        display_lane="chat",
    )
    envelope = IPCV2Envelope(
        kind="request",
        identity=identity,
        seq=1,
        body={"request_id": "r", "method": "run", "params": _large_chat_params()},
    )
    with pytest.raises(Exception, match="maximum is 4194304"):
        envelope.to_bytes()


@pytest.mark.asyncio
async def test_spawn_spools_large_params_and_removes_file_when_job_ends(
    short_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.runtime.task_runner_supervisor import TaskRunnerSupervisor

    supervisor = TaskRunnerSupervisor("alice", short_dir / "animas" / "alice", short_dir / "shared")
    seen: dict[str, Any] = {}

    async def _fail_spawn(*args: Any, **kwargs: Any) -> Any:
        job = next(iter(supervisor.jobs.values()))
        seen["wire"] = job.wire_params
        seen["path"] = job.params_spool_path
        seen["exists"] = job.params_spool_path is not None and job.params_spool_path.exists()
        raise OSError("spawn blocked in test")

    params = _large_chat_params()
    spool_dir = spool_dir_for_socket(supervisor.socket_path)
    spool_dir.mkdir(parents=True)
    orphan = spool_dir / "chat-stream-left-by-crashed-main.json"
    orphan.write_text("{}")
    with patch("asyncio.create_subprocess_exec", side_effect=_fail_spawn), pytest.raises(OSError):
        await supervisor._spawn_and_await(
            lane="chat",
            job_prefix="chat-stream",
            params_builder=lambda url_env: params,
            log_context="test",
            attempt=1,
            display_lane="chat",
            on_spawned=None,
            url_env={"ANIMAWORKS_EMBED_URL": "http://127.0.0.1:1/embed"},
        )

    assert seen["exists"] is True
    assert set(seen["wire"]) == {IPC_V2_SPOOL_KEY}
    assert seen["path"].parent == spool_dir_for_socket(supervisor.socket_path)
    assert not seen["path"].exists()
    assert not orphan.exists()
    assert not supervisor.jobs


@pytest.mark.asyncio
async def test_cancel_during_spool_write_still_removes_the_file(short_dir: Path) -> None:
    import threading

    from core.runtime import task_runner_supervisor as trs

    supervisor = trs.TaskRunnerSupervisor("alice", short_dir / "animas" / "alice", short_dir / "shared")
    entered = threading.Event()
    release = threading.Event()
    real = trs.spool_run_params_if_large

    def _slow_spool(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        release.wait(5)
        return real(*args, **kwargs)

    params = _large_chat_params()
    with (
        patch.object(trs, "spool_run_params_if_large", _slow_spool),
        patch("asyncio.create_subprocess_exec", side_effect=AssertionError("must not spawn")),
    ):
        task = asyncio.create_task(
            supervisor._spawn_and_await(
                lane="chat",
                job_prefix="chat-stream",
                params_builder=lambda url_env: params,
                log_context="test",
                attempt=1,
                display_lane="chat",
                on_spawned=None,
                url_env={"ANIMAWORKS_EMBED_URL": "http://127.0.0.1:1/embed"},
            )
        )
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.05)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    spool_dir = spool_dir_for_socket(supervisor.socket_path)
    assert spool_dir.is_dir()
    assert list(spool_dir.iterdir()) == []
    assert not supervisor.jobs


@pytest.mark.asyncio
async def test_run_task_rehydrates_spooled_params_before_execution(
    short_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.runtime import task_runner

    embed_url = "http://127.0.0.1:1/embed"
    monkeypatch.setenv("ANIMAWORKS_EMBED_URL", embed_url)
    params = _large_chat_params()
    identity = IPCV2Identity(
        job_id="chat-stream-rehydrate",
        root_epoch=str(uuid.uuid4()),
        attempt=1,
        lane="chat",
        display_lane="chat",
    )
    socket_path = short_dir / "sockets" / "a.sock"
    wire, _ = spool_run_params_if_large(params, spool_dir=spool_dir_for_socket(socket_path), job_id=identity.job_id)
    run_envelope = IPCV2Envelope(
        kind="request",
        identity=identity,
        seq=1,
        body={"request_id": "run-1", "method": "run", "params": wire},
    )

    class _Conn:
        def __init__(self) -> None:
            self.responses: list[dict[str, Any]] = []

        async def send_response(self, request_id: str, **kwargs: Any) -> int:
            self.responses.append({"request_id": request_id, **kwargs})
            return 1

        async def close(self) -> None:
            return None

    conn = _Conn()
    captured: dict[str, Any] = {}

    async def _fake_connect(*args: Any, **kwargs: Any) -> tuple[_Conn, IPCV2Envelope]:
        return conn, run_envelope

    async def _fake_prepare(args: Any, ident: Any, prepared_params: dict[str, Any], **kwargs: Any) -> Any:
        captured["params"] = prepared_params
        raise ValueError("stop after capture")

    monkeypatch.setattr(task_runner, "_connect", _fake_connect)
    monkeypatch.setattr(task_runner, "_prepare_execution", _fake_prepare)
    monkeypatch.setattr(task_runner, "configure_state_writer", lambda writer: None)

    args = type("Args", (), {"anima": "alice", "lane": "chat", "job": identity.job_id})()
    code = await task_runner.run_task(args, socket_path, identity)

    assert code == 2
    assert captured["params"] == params
    assert conn.responses[-1]["error"]["message"] == "stop after capture"


@pytest.mark.asyncio
async def test_run_task_reports_protocol_error_for_bad_spool(
    short_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.runtime import task_runner

    identity = IPCV2Identity(
        job_id="chat-stream-bad",
        root_epoch=str(uuid.uuid4()),
        attempt=1,
        lane="chat",
        display_lane="chat",
    )
    socket_path = short_dir / "sockets" / "a.sock"
    run_envelope = IPCV2Envelope(
        kind="request",
        identity=identity,
        seq=1,
        body={
            "request_id": "run-1",
            "method": "run",
            "params": {IPC_V2_SPOOL_KEY: {"name": "chat-stream-bad.json", "bytes": 10, "sha256": "x"}},
        },
    )

    class _Conn:
        responses: list[dict[str, Any]] = []

        async def send_response(self, request_id: str, **kwargs: Any) -> int:
            self.responses.append(kwargs)
            return 1

        async def close(self) -> None:
            return None

    async def _fake_connect(*args: Any, **kwargs: Any) -> tuple[_Conn, IPCV2Envelope]:
        return _Conn(), run_envelope

    monkeypatch.setattr(task_runner, "_connect", _fake_connect)
    args = type("Args", (), {"anima": "alice", "lane": "chat", "job": identity.job_id})()

    assert await task_runner.run_task(args, socket_path, identity) == 2
    assert _Conn.responses[-1]["error"]["code"] == "PROTOCOL_ERROR"
    assert "unreadable" in _Conn.responses[-1]["error"]["message"]
