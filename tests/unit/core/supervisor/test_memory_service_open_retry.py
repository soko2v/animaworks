"""Self-recovery of the Anima main memory store after a non-busy open failure.

Regression for 2026-11-03 (luca): ENOSPC made a Chroma reopen fail with
``(code: 4874) disk I/O error``; ``_store`` stayed ``None`` and every request
returned 503 until the runner was restarted, even after disk space returned.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import core.runtime.memory_service as memory_service_module
from core.memory.rag.store import Document, SearchResult
from core.memory.rag.store_status import read_store_status, store_status_path
from core.runtime.memory_service import MemoryService, MemoryServiceUnavailable

_PARAMS = {"collection": "luca_episodes", "embedding": [0.1], "top_k": 1}
_PLENTY = 50 * 1024**3


class _Store:
    def __init__(self, errors: list[str] | None = None) -> None:
        self.errors = list(errors or [])
        self.closed = False
        self.query_calls = 0

    def _query_once(self, *_args, **_kwargs) -> list[SearchResult]:
        self.query_calls += 1
        if self.closed:
            raise RuntimeError("store is closed")
        if self.errors:
            raise RuntimeError(self.errors.pop(0))
        return [SearchResult(Document("ok", "ok"), 1.0)]

    def close(self) -> None:
        self.closed = True


class _Clock:
    def __init__(self, start: float = 10_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(memory_service_module, "monotonic", fake)
    return fake


@pytest.fixture(autouse=True)
def _no_repair_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.memory.rag import repair

    monkeypatch.setattr(repair, "record_chroma_error", MagicMock())
    monkeypatch.setattr(MemoryService, "_request_startup_repair", lambda self, error: None)


def _enospc_open_error() -> Exception:
    return RuntimeError("(code: 4874) disk I/O error")


def _service(anima_dir: Path, opener, *, free: list[int] | None = None, **kwargs) -> MemoryService:
    service = MemoryService("luca", anima_dir, opener=opener, **kwargs)
    free_box = free if free is not None else [_PLENTY]
    service._free_bytes = lambda: free_box[0]  # type: ignore[method-assign]
    return service


async def _query(service: MemoryService) -> dict:
    return await service.handle("memory.query", _PARAMS)


@pytest.mark.asyncio
async def test_failed_reopen_recovers_after_min_interval_without_restart(tmp_path: Path, clock: _Clock) -> None:
    anima_dir = tmp_path / "luca"
    broken = _Store(["disk I/O error", "disk I/O error"])
    recovered = _Store()
    opener = MagicMock(side_effect=[broken, _enospc_open_error(), recovered])
    service = _service(anima_dir, opener)
    try:
        # Reproduce the incident: transient op failure -> close -> reopen fails.
        with pytest.raises(MemoryServiceUnavailable, match="4874"):
            await _query(service)
        assert broken.closed
        assert service._store is None
        assert opener.call_count == 2
        marker = read_store_status(anima_dir)
        assert marker is not None and marker["unavailable_since"]
        assert "4874" in marker["last_error"]

        # Within the interval: no new open attempt, explicit unavailable error.
        clock.advance(MemoryService._OPEN_RETRY_MIN_INTERVAL_SECONDS - 1)
        with pytest.raises(MemoryServiceUnavailable, match=r"memory store unavailable: .*4874.*\(since "):
            await _query(service)
        assert opener.call_count == 2

        # After the interval the same process reopens and serves again.
        clock.advance(2)
        result = await _query(service)
        assert result["results"][0]["document"]["id"] == "ok"
        assert opener.call_count == 3
        assert service._store is recovered
        assert service._open_error is None
        assert service._unavailable_since is None
        assert not store_status_path(anima_dir).exists()

        # And keeps serving without further opens.
        await _query(service)
        assert opener.call_count == 3
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_startup_open_failure_is_retried_and_cleared(tmp_path: Path, clock: _Clock) -> None:
    anima_dir = tmp_path / "luca"
    store = _Store()
    opener = MagicMock(side_effect=[OSError(errno.ENOSPC, "No space left on device"), store])
    service = _service(anima_dir, opener)
    try:
        await service.start()
        assert service._store is None
        assert read_store_status(anima_dir) is not None
        with pytest.raises(MemoryServiceUnavailable, match="No space left"):
            await _query(service)
        assert opener.call_count == 1

        clock.advance(MemoryService._OPEN_RETRY_MIN_INTERVAL_SECONDS)
        assert (await _query(service))["results"]
        assert opener.call_count == 2
        assert read_store_status(anima_dir) is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_rapid_calls_do_not_hammer_the_opener(tmp_path: Path, clock: _Clock) -> None:
    opener = MagicMock(side_effect=_enospc_open_error())
    service = _service(tmp_path / "luca", opener)
    try:
        await service.start()
        for _ in range(50):
            with pytest.raises(MemoryServiceUnavailable):
                await _query(service)
            clock.advance(0.5)
        # 25s of hammering: still only the initial open.
        assert opener.call_count == 1

        clock.advance(MemoryService._OPEN_RETRY_MIN_INTERVAL_SECONDS)
        for _ in range(20):
            with pytest.raises(MemoryServiceUnavailable):
                await _query(service)
        # Exactly one retry per elapsed interval, however many callers.
        assert opener.call_count == 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_concurrent_callers_share_one_open_attempt(tmp_path: Path, clock: _Clock) -> None:
    release = threading.Event()
    entered = threading.Event()
    store = _Store()
    calls = {"n": 0}

    def opener() -> _Store:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _enospc_open_error()
        entered.set()
        assert release.wait(5)
        return store

    service = _service(tmp_path / "luca", opener)
    try:
        await service.start()
        clock.advance(MemoryService._OPEN_RETRY_MIN_INTERVAL_SECONDS)

        first = asyncio.create_task(_query(service))
        await asyncio.to_thread(entered.wait, 5)
        others = await asyncio.gather(*(_query(service) for _ in range(8)), return_exceptions=True)
        release.set()
        assert (await first)["results"]

        assert calls["n"] == 2
        assert all(isinstance(result, MemoryServiceUnavailable) for result in others)
        assert service._store is store
    finally:
        release.set()
        await service.close()


@pytest.mark.asyncio
async def test_owner_busy_path_is_not_retried_by_open_retry(tmp_path: Path, clock: _Clock) -> None:
    from core.memory.rag.owner_lock import VectorOwnerLock

    anima_dir = tmp_path / "luca"
    other_owner = VectorOwnerLock(anima_dir, "cli:index")
    other_owner.acquire()
    opener = MagicMock(return_value=_Store())
    service = _service(anima_dir, opener)
    try:
        await service.start()
        clock.advance(MemoryService._OPEN_RETRY_MIN_INTERVAL_SECONDS * 3)
        for _ in range(3):
            with pytest.raises(MemoryServiceUnavailable, match="luca"):
                await _query(service)
        opener.assert_not_called()
        assert service._unavailable_since is None
        assert not store_status_path(anima_dir).exists()

        # Existing owner backoff path still recovers once the owner leaves.
        other_owner.release()
        clock.advance(6)  # existing 5s owner backoff
        assert (await _query(service))["results"]
        assert opener.call_count == 1
    finally:
        other_owner.release()
        await service.close()


@pytest.mark.asyncio
async def test_retry_waits_for_repair_and_fence(tmp_path: Path, clock: _Clock) -> None:
    fenced = {"value": True}
    opener = MagicMock(side_effect=[_enospc_open_error(), _Store()])
    service = _service(tmp_path / "luca", opener, repair_fenced=lambda: fenced["value"])
    try:
        await service.start()
        clock.advance(MemoryService._OPEN_RETRY_MIN_INTERVAL_SECONDS)
        with pytest.raises(MemoryServiceUnavailable):
            await _query(service)
        assert opener.call_count == 1

        fenced["value"] = False
        async with service._repair_lock:
            with pytest.raises(MemoryServiceUnavailable):
                await _query(service)
        assert opener.call_count == 1

        service._repairing = True
        with pytest.raises(MemoryServiceUnavailable, match="repair in progress"):
            await _query(service)
        service._repairing = False
        assert opener.call_count == 1

        assert (await _query(service))["results"]
        assert opener.call_count == 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_retry_is_deferred_while_disk_is_nearly_full(tmp_path: Path, clock: _Clock) -> None:
    anima_dir = tmp_path / "luca"
    free = [1024]
    opener = MagicMock(side_effect=[_enospc_open_error(), _Store()])
    service = _service(anima_dir, opener, free=free)
    try:
        await service.start()
        clock.advance(MemoryService._OPEN_RETRY_MIN_INTERVAL_SECONDS)
        with pytest.raises(MemoryServiceUnavailable, match="insufficient free disk space"):
            await _query(service)
        assert opener.call_count == 1
        marker = read_store_status(anima_dir)
        assert marker is not None
        assert "insufficient free disk space" in marker["last_error"]
        assert marker["free_bytes"] == 1024

        free[0] = _PLENTY
        clock.advance(MemoryService._OPEN_RETRY_MIN_INTERVAL_SECONDS)
        assert (await _query(service))["results"]
        assert opener.call_count == 2
        assert read_store_status(anima_dir) is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_transient_error_on_full_disk_keeps_the_open_handle(tmp_path: Path, clock: _Clock) -> None:
    free = [0]
    store = _Store(["disk I/O error", "disk I/O error"])
    opener = MagicMock(return_value=store)
    service = _service(tmp_path / "luca", opener, free=free)
    try:
        with pytest.raises(MemoryServiceUnavailable, match="disk I/O error"):
            await _query(service)
        # No close/reopen while the volume is full: the handle survives.
        assert opener.call_count == 1
        assert not store.closed
        assert service._store is store
        assert service._open_error is None

        free[0] = _PLENTY
        assert (await _query(service))["results"]
        assert opener.call_count == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_persistent_open_error_warns_rate_limited(
    tmp_path: Path, clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    opener = MagicMock(side_effect=_enospc_open_error())
    service = _service(tmp_path / "luca", opener)
    caplog.set_level(logging.WARNING, logger="core.runtime.memory_service")
    try:
        await service.start()

        def warnings() -> list[str]:
            return [r.getMessage() for r in caplog.records if "unavailable since" in r.getMessage()]

        with pytest.raises(MemoryServiceUnavailable):
            await _query(service)
        assert warnings() == []

        clock.advance(MemoryService._UNAVAILABLE_WARN_INTERVAL_SECONDS + 1)
        with pytest.raises(MemoryServiceUnavailable):
            await _query(service)
        assert len(warnings()) == 1
        assert "luca" in warnings()[0] and "4874" in warnings()[0]

        clock.advance(10)
        with pytest.raises(MemoryServiceUnavailable):
            await _query(service)
        assert len(warnings()) == 1

        clock.advance(MemoryService._UNAVAILABLE_WARN_INTERVAL_SECONDS)
        with pytest.raises(MemoryServiceUnavailable):
            await _query(service)
        assert len(warnings()) == 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_close_during_retry_does_not_leak_the_new_handle(tmp_path: Path, clock: _Clock) -> None:
    release = threading.Event()
    entered = threading.Event()
    store = _Store()
    calls = {"n": 0}

    def opener() -> _Store:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _enospc_open_error()
        entered.set()
        assert release.wait(5)
        return store

    anima_dir = tmp_path / "luca"
    service = _service(anima_dir, opener)
    await service.start()
    clock.advance(MemoryService._OPEN_RETRY_MIN_INTERVAL_SECONDS)
    pending = asyncio.create_task(_query(service))
    await asyncio.to_thread(entered.wait, 5)
    closing = asyncio.create_task(service.close())
    await asyncio.sleep(0)
    release.set()
    await closing
    with pytest.raises(MemoryServiceUnavailable):
        await pending
    assert store.closed
    assert service._store is None
    assert not store_status_path(anima_dir).exists()


@pytest.mark.asyncio
async def test_cli_owned_service_does_not_publish_root_status(tmp_path: Path, clock: _Clock) -> None:
    anima_dir = tmp_path / "luca"
    service = _service(anima_dir, MagicMock(side_effect=_enospc_open_error()), owner_label="cli:index")
    try:
        await service.start()
        assert service._open_error is not None
        assert not store_status_path(anima_dir).exists()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_successful_start_clears_stale_marker_from_previous_runner(tmp_path: Path) -> None:
    anima_dir = tmp_path / "luca"
    path = store_status_path(anima_dir)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"unavailable_since": "2026-11-03T00:28:31+00:00"}), encoding="utf-8")
    service = _service(anima_dir, MagicMock(return_value=_Store()))
    try:
        await service.start()
        assert not path.exists()
    finally:
        await service.close()


def test_store_status_write_failure_is_best_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.memory.rag import store_status

    def full_disk(*_args, **_kwargs) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(store_status, "atomic_write_json", full_disk)
    assert (
        store_status.write_store_unavailable(
            tmp_path / "luca",
            unavailable_since="2026-11-03T00:28:31+00:00",
            last_error="x",
            last_attempt_at=None,
            next_retry_at=None,
            free_bytes=0,
        )
        is False
    )


@pytest.mark.asyncio
async def test_anima_detail_api_and_cli_show_store_unavailable_since(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from cli.commands.anima_mgmt import _print_anima_status
    from core.memory.rag.store_status import clear_store_unavailable, write_store_unavailable
    from server.routes.animas import create_animas_router

    animas_dir = tmp_path / "animas"
    anima_dir = animas_dir / "luca"
    (anima_dir / "state").mkdir(parents=True)
    (anima_dir / "status.json").write_text(json.dumps({"enabled": True}), encoding="utf-8")
    app = FastAPI()
    app.state.animas_dir = animas_dir
    app.state.anima_names = ["luca"]
    app.state.supervisor = MagicMock()
    app.state.supervisor.get_process_status.return_value = {"status": "running", "pid": 35854}
    app.include_router(create_animas_router(), prefix="/api")
    since = "2026-11-03T00:28:31+00:00"
    write_store_unavailable(
        anima_dir,
        unavailable_since=since,
        last_error="OperationalError: (code: 4874) disk I/O error",
        last_attempt_at=since,
        next_retry_at=None,
        free_bytes=0,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        status = (await client.get("/api/animas/luca")).json()["status"]
        assert status["status"] == "running"
        assert status["store_unavailable_since"] == since
        assert "4874" in status["store_last_error"]

        monkeypatch.setattr("core.paths.get_animas_dir", lambda: animas_dir)
        _print_anima_status("luca", status)
        output = capsys.readouterr().out
        assert f"Memory store: unavailable since {since}" in output

        clear_store_unavailable(anima_dir)
        status = (await client.get("/api/animas/luca")).json()["status"]
        assert "store_unavailable_since" not in status
        _print_anima_status("luca", status)
        assert "Memory store" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_slow_open_does_not_queue_more_retries_across_intervals(tmp_path: Path, clock: _Clock) -> None:
    release = threading.Event()
    entered = threading.Event()
    calls = {"n": 0}

    def opener() -> _Store:
        calls["n"] += 1
        if calls["n"] == 2:
            entered.set()
            assert release.wait(5)
        raise _enospc_open_error()

    service = _service(tmp_path / "luca", opener)
    try:
        await service.start()
        clock.advance(MemoryService._OPEN_RETRY_MIN_INTERVAL_SECONDS)
        first = asyncio.create_task(_query(service))
        await asyncio.to_thread(entered.wait, 5)
        # The opener stays blocked across several intervals; callers must not
        # stack extra worker jobs behind it.
        for _ in range(3):
            clock.advance(MemoryService._OPEN_RETRY_MIN_INTERVAL_SECONDS * 2)
            with pytest.raises(MemoryServiceUnavailable):
                await _query(service)
        release.set()
        with pytest.raises(MemoryServiceUnavailable):
            await first
        # Drain the worker; a queued retry would run here.
        await asyncio.get_running_loop().run_in_executor(service._executor, lambda: None)
        assert calls["n"] == 2
        assert not service._open_retry_inflight

        # The cooldown starts from the failed attempt, not from the claim.
        with pytest.raises(MemoryServiceUnavailable):
            await _query(service)
        assert calls["n"] == 2
        clock.advance(MemoryService._OPEN_RETRY_MIN_INTERVAL_SECONDS)
        with pytest.raises(MemoryServiceUnavailable):
            await _query(service)
        assert calls["n"] == 3
    finally:
        release.set()
        await service.close()


@pytest.mark.asyncio
async def test_cancelled_caller_does_not_wedge_the_inflight_flag(tmp_path: Path, clock: _Clock) -> None:
    opener = MagicMock(side_effect=[_enospc_open_error(), _Store()])
    service = _service(tmp_path / "luca", opener)
    try:
        await service.start()
        release = threading.Event()
        blocker = asyncio.get_running_loop().run_in_executor(service._executor, release.wait, 5)
        clock.advance(MemoryService._OPEN_RETRY_MIN_INTERVAL_SECONDS)
        caller = asyncio.create_task(_query(service))
        await asyncio.sleep(0.05)
        assert service._open_retry_inflight
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        release.set()
        await blocker
        await asyncio.get_running_loop().run_in_executor(service._executor, lambda: None)
        assert not service._open_retry_inflight
        clock.advance(MemoryService._OPEN_RETRY_MIN_INTERVAL_SECONDS)
        assert (await _query(service))["results"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_fence_raised_while_retry_is_queued_blocks_the_open(tmp_path: Path, clock: _Clock) -> None:
    fence = {"value": False}
    opener = MagicMock(side_effect=[_enospc_open_error(), _Store()])
    service = _service(tmp_path / "luca", opener, repair_fenced=lambda: fence["value"])
    try:
        await service.start()
        release = threading.Event()
        blocker = asyncio.get_running_loop().run_in_executor(service._executor, release.wait, 5)
        clock.advance(MemoryService._OPEN_RETRY_MIN_INTERVAL_SECONDS)
        caller = asyncio.create_task(_query(service))
        await asyncio.sleep(0.05)
        fence["value"] = True
        release.set()
        await blocker
        with pytest.raises(MemoryServiceUnavailable):
            await caller
        assert opener.call_count == 1
        assert service._store is None
    finally:
        await service.close()


def test_marker_from_dead_runner_is_ignored(tmp_path: Path) -> None:
    import subprocess
    import sys

    from cli.commands.rag_repair_status import collect_rag_repair_status

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    animas_dir = tmp_path / "animas"
    anima_dir = animas_dir / "luca"
    path = store_status_path(anima_dir)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"unavailable_since": "2026-11-03T00:28:31+00:00", "pid": proc.pid, "last_error": "x"}),
        encoding="utf-8",
    )
    assert read_store_status(anima_dir) is None
    assert collect_rag_repair_status(animas_dir)[0]["store_unavailable_since"] is None

    path.write_text(json.dumps({"unavailable_since": "2026-11-03T00:28:31+00:00"}), encoding="utf-8")
    assert read_store_status(anima_dir) is None


@pytest.mark.asyncio
async def test_recovered_handle_error_does_not_reopen_within_interval(tmp_path: Path, clock: _Clock) -> None:
    recovered = _Store(["disk I/O error", "disk I/O error"])
    opener = MagicMock(side_effect=[_enospc_open_error(), recovered, _Store()])
    service = _service(tmp_path / "luca", opener)
    try:
        await service.start()
        clock.advance(MemoryService._OPEN_RETRY_MIN_INTERVAL_SECONDS)
        # Retry opens `recovered`; its first op fails transiently. The native
        # reopen cooldown must hold: no second open in the same call.
        with pytest.raises(MemoryServiceUnavailable, match="disk I/O error"):
            await _query(service)
        assert opener.call_count == 2
        assert service._store is recovered
        assert not recovered.closed
        assert (await _query(service))["results"]
        assert opener.call_count == 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_repair_after_retry_replaces_the_retried_handle(
    tmp_path: Path, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setenv("ANIMAWORKS_DATA_DIR", str(tmp_path))
    anima_dir = tmp_path / "animas" / "luca"
    live = anima_dir / "vectordb"
    live.mkdir(parents=True)
    (live / "old.marker").write_text("old")
    (anima_dir / "index_meta.json").write_text('{"generation":"old"}')
    staging = anima_dir / "vectordb.staging-test"
    (staging / ".rebuild").mkdir(parents=True)
    (staging / "new.marker").write_text("new")
    (staging / ".rebuild" / "sources.json").write_text(json.dumps({"owner": str(anima_dir.resolve()), "sources": {}}))
    (staging / ".rebuild" / "index_meta.json").write_text('{"generation":"new"}')

    retried = _Store()
    repaired = _Store()
    opener = MagicMock(side_effect=[_enospc_open_error(), retried, repaired])
    service = _service(anima_dir, opener)
    service._build_staging_subprocess = AsyncMock(return_value=(staging, 1, {}))  # type: ignore[method-assign]
    monkeypatch.setattr(service, "_rebuild_bm25_sync", lambda: None)
    monkeypatch.setattr(service, "_invalidate_shared_checks_sync", lambda: None)
    monkeypatch.setattr(service, "_verify_store_sync", MagicMock(return_value={"chunks": 1}))
    try:
        await service.start()
        clock.advance(MemoryService._OPEN_RETRY_MIN_INTERVAL_SECONDS)
        assert (await _query(service))["results"]
        assert service._store is retried

        result = await service.repair(include_shared=True)

        assert result["status"] == "success"
        assert retried.closed
        assert service._store is repaired
        assert service._open_error is None
        assert (live / "new.marker").exists()
        assert opener.call_count == 3
    finally:
        await service.close()
