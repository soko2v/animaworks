"""Legacy hold regression: synthetic ledgers and mocked execution only."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.memory.task_queue import TaskQueueManager, legacy_execution_hold
from core.memory.taskboard_housekeeping import _cleanup_pending_processing
from core.platform.processing_lease import processing_lease_path
from core.supervisor.pending_executor import PendingTaskExecutor, TaskExecutionHeld


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_status", ["blocked", "failed"])
@pytest.mark.parametrize("as_update", [False, True])
@pytest.mark.parametrize("task_type", ["command", "llm"])
async def test_legacy_hold_stops_dispatch(tmp_path, legacy_status, as_update, task_type):
    anima_dir = tmp_path / "synthetic-anima"
    state_dir = anima_dir / "state"
    state_dir.mkdir(parents=True)
    queue = state_dir / "task_queue.jsonl"
    entry = {
        "task_id": "held-task",
        "source": "human",
        "original_instruction": "Synthetic hold; never execute",
        "assignee": anima_dir.name,
        "summary": "Synthetic approval pending",
        "status": "pending" if as_update else legacy_status,
        "ts": "2026-09-09T00:00:00+09:00",
        "updated_at": "2026-09-09T00:00:00+09:00",
    }
    rows = [entry]
    if as_update:
        rows.append({"_event": "update", "task_id": "held-task", "status": legacy_status})
    original = "".join(json.dumps(row) + "\n" for row in rows).encode()
    queue.write_bytes(original)
    # Ensure the fixture is a valid ledger, not an absent or discarded row.
    assert TaskQueueManager(anima_dir).get_task_by_id("held-task") is not None

    anima = MagicMock()
    executor = PendingTaskExecutor(
        anima=anima,
        anima_name=anima_dir.name,
        anima_dir=anima_dir,
        shutdown_event=asyncio.Event(),
    )
    executor._background_isolated = False
    executor._execute_llm_task = AsyncMock()
    # submit is a mock: no callback, command, model, worker or transport runs.
    anima.agent.background_manager.submit = MagicMock(return_value="fake-id")
    with pytest.raises(TaskExecutionHeld):
        await executor.execute_pending_task({
            "task_id": "held-task",
            "task_type": task_type,
            "tool_name": "synthetic-never-run",
        })

    assert queue.read_bytes() == original
    executor._execute_llm_task.assert_not_awaited()
    anima.agent.background_manager.submit.assert_not_called()


@pytest.mark.parametrize("caller", ["executor", "callback", "housekeeping"])
@pytest.mark.parametrize("status", ["blocked", "failed"])
def test_dead_lease_does_not_release_hold(tmp_path, caller, status):
    import os

    anima_dir = tmp_path / "animas/synthetic"
    processing = anima_dir / "state/pending/processing"
    processing.mkdir(parents=True)
    (anima_dir / "state/task_queue.jsonl").write_text(json.dumps({"task_id": "task", "status": status}) + "\n")
    path = processing / "task.json"
    path.write_text(json.dumps({"task_id": "task"}))
    processing_lease_path(path).write_text('{"evidence":"dead but held"}')
    os.utime(path, (1, 1))
    before = {p: p.read_bytes() for p in anima_dir.rglob("*") if p.is_file()}
    callback = MagicMock()
    with patch("core.supervisor.pending_executor.is_processing_lease_live", return_value=False), \
         patch("core.memory.taskboard_housekeeping.is_processing_lease_live", return_value=False):
        if caller == "housekeeping":
            _cleanup_pending_processing(tmp_path / "animas", 24, None)
        else:
            PendingTaskExecutor._recover_processing(processing, anima_dir,
                                                   callback if caller == "callback" else None)
    assert {p: p.read_bytes() for p in anima_dir.rglob("*") if p.is_file()} == before
    callback.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("task_type", ["command", "llm"])
async def test_unheld_dispatch_control(tmp_path, task_type):
    (tmp_path / "state").mkdir()
    (tmp_path / "state/task_queue.jsonl").write_text(json.dumps({"task_id": "task", "status": "pending"}) + "\n")
    anima = MagicMock()
    executor = PendingTaskExecutor(anima=anima, anima_name="synthetic",
                                   anima_dir=tmp_path, shutdown_event=asyncio.Event())
    executor._background_isolated = False
    executor._execute_llm_task = AsyncMock()
    await executor.execute_pending_task({"task_id": "task", "task_type": task_type})
    if task_type == "llm":
        executor._execute_llm_task.assert_awaited_once()
        anima.agent.background_manager.submit.assert_not_called()
    else:
        executor._execute_llm_task.assert_not_awaited()
        anima.agent.background_manager.submit.assert_called_once()


@pytest.mark.parametrize("rows, expected", [
    ([{"task_id": "task", "status": "pending"}], False),
    ([{"task_id": "other", "status": "blocked"}], False),
    ([{"task_id": "task", "status": "blocked"},
      {"_event": "update", "task_id": "task", "status": "pending"}], True),
    ([{"task_id": "task", "status": "failed"},
      {"task_id": "task", "status": "pending"}], True),
    ([[]], True),
])
def test_hold_is_sticky_and_task_scoped(tmp_path, rows, expected):
    (tmp_path / "state").mkdir()
    (tmp_path / "state/task_queue.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows))
    assert legacy_execution_hold(tmp_path, "task") is expected


@pytest.mark.parametrize("fault", ["json", "utf8", "directory", "symlink"])
def test_unreadable_existing_ledger_holds(tmp_path, fault):
    (tmp_path / "state").mkdir()
    path = tmp_path / "state/task_queue.jsonl"
    if fault == "directory":
        path.mkdir()
    elif fault == "symlink":
        path.symlink_to(tmp_path / "missing")
    else:
        path.write_bytes(b"{" if fault == "json" else b"\xff")
    assert legacy_execution_hold(tmp_path, "task")


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["claimed", "claim", "defer", "return", "sync", "worker"])
async def test_hold_preserves_processing_and_ledger(tmp_path, route):
    state = tmp_path / "state"
    processing = state / "pending/processing"
    processing.mkdir(parents=True)
    (state / "task_queue.jsonl").write_text(json.dumps({"task_id": "task", "status": "blocked"}) + "\n")
    path = processing / "task.json"
    desc = {"task_id": "task", "task_type": "llm"}
    path.write_text(json.dumps(desc))
    processing_lease_path(path).write_text('{"evidence":"must remain"}')
    before = {p: p.read_bytes() for p in state.rglob("*") if p.is_file()}
    executor = PendingTaskExecutor(anima=MagicMock(), anima_name="synthetic",
                                   anima_dir=tmp_path, shutdown_event=asyncio.Event())
    executor._execute_llm_task = AsyncMock()
    if route == "claimed":
        executor._active_task_ids.add("task")
        await executor._execute_claimed_llm_task(desc, path, None)
        assert "task" not in executor._active_task_ids
    elif route == "claim":
        assert executor._claim_processing_task(path, desc) is None
    elif route == "defer":
        assert executor._should_defer_claim(path, desc, processing)
    elif route == "return":
        executor._return_task_to_pending(desc, "not a crash", stop_kind="crash")
    elif route == "sync":
        executor._sync_task_queue("task", "pending")
    else:
        with pytest.raises(TaskExecutionHeld):
            await executor._run_task_in_worker(desc)
    assert {p: p.read_bytes() for p in state.rglob("*") if p.is_file()} == before
    executor._execute_llm_task.assert_not_awaited()
