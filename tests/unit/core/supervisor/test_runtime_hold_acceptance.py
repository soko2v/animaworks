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


@pytest.mark.parametrize("status", ["blocked", "failed"])
@pytest.mark.parametrize("latest", ["pending", "in_progress", "done", "cancelled"])
def test_compaction_preserves_hold_independent_of_display(tmp_path, status, latest):
    manager = TaskQueueManager(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    entry = {"task_id": "held", "source": "human", "original_instruction": "synthetic",
             "assignee": "synthetic", "summary": "", "status": status,
             "ts": "2026-09-09T00:00:00+09:00", "updated_at": "2026-09-09T00:00:00+09:00"}
    rows = [entry, {"_event": "update", "task_id": "held", "status": latest},
            {**entry, "task_id": "completed", "status": "done"}]
    manager.queue_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert legacy_execution_hold(tmp_path, "held")
    assert manager.compact() == (2 if latest in ("done", "cancelled") else 1)
    assert legacy_execution_hold(tmp_path, "held")
    current = manager.get_task_by_id("held")
    assert (current.status if current else None) == (None if latest in ("done", "cancelled") else latest)
    # Recreate ID and force another rewrite; neither action grants approval.
    manager._append({**entry, "status": "pending"})
    manager._append({**entry, "task_id": "another", "status": "done"})
    assert manager.compact() == 1
    assert legacy_execution_hold(tmp_path, "held")
    assert not legacy_execution_hold(tmp_path, "other")
    rows = [json.loads(line) for line in manager.queue_path.read_text().splitlines()]
    assert sum(row.get("_event") == "execution_hold" for row in rows) == 1


@pytest.mark.parametrize("bad", ["{", "[]", '{"status":"blocked"}',
                                '{"_event":"execution_hold","task_id":null}'])
def test_compaction_retains_untrusted_evidence(tmp_path, bad):
    manager = TaskQueueManager(tmp_path)
    manager.queue_path.parent.mkdir()
    manager.queue_path.write_text(bad + "\n")
    before = manager.queue_path.read_bytes()
    assert manager.compact() == 0
    assert manager.queue_path.read_bytes() == before
    assert not manager.archive_path.exists()
    assert legacy_execution_hold(tmp_path, "task")


@pytest.mark.parametrize("row", [{"task_id": "task", "status": "blocked"},
                                 {"task_id": "task", "status": "failed"},
                                 {"task_id": "task", "_event": "execution_hold"}, []])
def test_archived_hold_is_enforced_with_missing_live_ledger(tmp_path, row):
    (tmp_path / "state").mkdir()
    (tmp_path / "state/task_queue_archive.jsonl").write_text(json.dumps(row) + "\n")
    assert legacy_execution_hold(tmp_path, "task")


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_status", ["blocked", "failed"])
@pytest.mark.parametrize("isolated", [False, True])
async def test_command_background_hold_retains_claim(tmp_path, legacy_status, isolated):
    from core.background import BackgroundTaskManager, TaskStatus
    from core.supervisor.task_runner import execute_background_contract

    state = tmp_path / "state"
    pending = state / "background_tasks/pending"
    processing = pending / "processing"
    processing.mkdir(parents=True)
    ledger = state / "task_queue.jsonl"
    ledger.write_text(json.dumps({"task_id": "task", "status": "pending"}) + "\n")
    desc = {"task_id": "task", "task_type": "command", "tool_name": "synthetic-never-run"}
    path = (pending if isolated else processing) / "task.json"
    path.write_text(json.dumps(desc))
    expected_descriptor = path.read_bytes()
    if not isolated:
        processing_lease_path(path).write_text('{"evidence":"synthetic claim"}')
    manager = BackgroundTaskManager(tmp_path, result_memory_retention_minutes=10,
                                    max_completed_tasks_in_memory=100)
    manager.on_complete = AsyncMock()
    anima = MagicMock()
    anima.anima_dir = tmp_path
    anima.agent.background_manager = manager
    shutdown = asyncio.Event()
    executor = PendingTaskExecutor(anima=anima, anima_name="synthetic", anima_dir=tmp_path,
                                   shutdown_event=shutdown)
    executor._background_isolated = isolated
    executor._maybe_run_orphan_sweep = AsyncMock()
    executor._recover_processing = MagicMock()
    expected = {}
    def set_hold():
        with ledger.open("a") as stream:
            stream.write(json.dumps({"_event": "update", "task_id": "task", "status": legacy_status}) + "\n")
        expected[ledger] = ledger.read_bytes()
        lease = processing_lease_path(processing / "task.json")
        expected[lease] = lease.read_bytes()
    with patch("subprocess.run") as command:
        if isolated:
            async def child(**kwargs):
                assert kwargs["payload"]["task_id"] == "task"
                set_hold()  # Arrives after root admission, before child execution.
                result = await execute_background_contract(anima, kind="command", payload=kwargs["payload"])
                shutdown.set()
                executor.wake()
                return result
            executor._task_runner_supervisor = MagicMock()
            executor._task_runner_supervisor.run_background = AsyncMock(side_effect=child)
            await executor.watcher_loop()
            executor._task_runner_supervisor.run_background.assert_awaited_once()
        else:
            job = await executor.execute_pending_task(desc)
            executor._track_command_claim(job, task_id="task", processing_path=path)
            set_hold()  # Background thread has not started yet.
            with pytest.raises(TaskExecutionHeld):
                await job
            await asyncio.sleep(0)  # Run the claim completion callback.
        command.assert_not_called()
    held = list(manager._tasks.values())
    assert len(held) == 1 and held[0].status == TaskStatus.HELD
    assert held[0].completed_at is None and held[0].result is None
    assert manager._load_task(held[0].task_id).status == TaskStatus.HELD
    manager.on_complete.assert_not_awaited()
    kept = processing / "task.json"
    assert kept.read_bytes() == expected_descriptor
    assert processing_lease_path(kept).exists()
    assert all(p.read_bytes() == data for p, data in expected.items())
    assert legacy_execution_hold(tmp_path, "task")
    assert "task" not in executor._active_task_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["blocked", "failed", "pending"])
async def test_command_child_boundary(tmp_path, status):
    from types import SimpleNamespace

    from core.supervisor.task_runner import execute_background_contract

    (tmp_path / "state").mkdir()
    ledger = tmp_path / "state/task_queue.jsonl"
    ledger.write_text(json.dumps({"task_id": "task", "status": status}) + "\n")
    before = ledger.read_bytes()
    with patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="synthetic", stderr="")) as command:
        result = await execute_background_contract(SimpleNamespace(anima_dir=tmp_path), kind="command",
            payload={"task_id": "task", "tool_name": "synthetic-never-run"})
    assert command.call_count == (1 if status == "pending" else 0)
    assert result.get("execution_held", False) is (status != "pending")
    assert result["success"] is (status == "pending")
    assert ledger.read_bytes() == before


def test_compaction_requires_os_lock(tmp_path):
    manager = TaskQueueManager(tmp_path)
    manager.queue_path.parent.mkdir()
    manager.queue_path.write_text('{"task_id":"task","status":"blocked"}\n')
    before = manager.queue_path.read_bytes()
    with patch("core.platform.locks.acquire_file_lock", side_effect=OSError("synthetic lock error")):
        assert manager.compact() == 0
    assert manager.queue_path.read_bytes() == before
    assert not manager.archive_path.exists()


@pytest.mark.asyncio
async def test_llm_wrapper_does_not_absorb_child_hold(tmp_path):
    anima = MagicMock()
    anima._background_lock = asyncio.Lock()
    anima._status_slots = {}
    anima._task_slots = {}
    anima._active_background_workers = {}
    executor = PendingTaskExecutor(anima=anima, anima_name="synthetic", anima_dir=tmp_path,
                                   shutdown_event=asyncio.Event())
    executor._task_isolated = False
    executor._run_llm_task = AsyncMock(side_effect=TaskExecutionHeld("synthetic child hold"))
    executor._return_task_to_pending = MagicMock()
    executor._sync_task_queue = MagicMock()
    with pytest.raises(TaskExecutionHeld):
        await executor._execute_llm_task({"task_id": "task", "task_type": "llm"})
    executor._run_llm_task.assert_awaited_once()
    executor._return_task_to_pending.assert_not_called()
    executor._sync_task_queue.assert_not_called()
    assert not anima._background_lock.locked()


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


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["child", "low_level", "core", "serial", "parallel"])
@pytest.mark.parametrize("status", ["blocked", "failed"])
async def test_child_and_batch_hold_boundaries(tmp_path, route, status):
    from core.supervisor.task_runner import execute_task_contract

    state = tmp_path / "state"
    state.mkdir()
    ledger = state / "task_queue.jsonl"
    ledger.write_text(json.dumps({"task_id": "task", "status": status}) + "\n")
    before = ledger.read_bytes()
    anima = MagicMock(name="synthetic")
    anima.name = "synthetic"
    anima.anima_dir = tmp_path
    anima._active_parallel_tasks = {}
    executor = PendingTaskExecutor(anima=anima, anima_name="synthetic",
                                   anima_dir=tmp_path, shutdown_event=asyncio.Event())
    executor._save_task_result = MagicMock()
    executor._handle_goal_completion = AsyncMock()
    desc = {"task_id": "task", "task_type": "llm"}
    if route == "child":
        result = await execute_task_contract(anima, desc)
        assert result == {"task_type": "llm", "result": "", "success": False, "execution_held": True}
    else:
        with pytest.raises(TaskExecutionHeld):
            if route == "low_level":
                await executor._run_llm_task(desc)
            elif route == "core":
                await executor._run_llm_task_under_agent_session_context(desc)
            elif route == "serial":
                await executor._execute_serial_batch_task(desc, {}, "batch")
            else:
                await executor._execute_parallel_task(desc, {}, "batch")
    executor._save_task_result.assert_not_called()
    executor._handle_goal_completion.assert_not_awaited()
    anima.agent.run_cycle_streaming.assert_not_called()
    assert anima._active_parallel_tasks == {}
    assert ledger.read_bytes() == before


@pytest.mark.asyncio
async def test_child_hold_wire_retains_claimed_evidence(tmp_path):
    state = tmp_path / "state"
    processing = state / "pending/processing"
    processing.mkdir(parents=True)
    (state / "task_queue.jsonl").write_text(json.dumps({"task_id": "task", "status": "pending"}) + "\n")
    desc = {"task_id": "task", "task_type": "llm"}
    path = processing / "task.json"
    path.write_text(json.dumps(desc))
    processing_lease_path(path).write_text('{"evidence":"held child"}')
    before = {p: p.read_bytes() for p in state.rglob("*") if p.is_file()}
    executor = PendingTaskExecutor(anima=MagicMock(), anima_name="synthetic",
                                   anima_dir=tmp_path, shutdown_event=asyncio.Event())
    executor._task_runner_supervisor = MagicMock()
    executor._task_runner_supervisor.run_task = AsyncMock(return_value={
        "task_type": "llm", "success": False, "result": "", "execution_held": True})
    executor._execute_llm_task = executor._run_llm_task_isolated
    await executor._execute_claimed_llm_task(desc, path, None)
    executor._task_runner_supervisor.run_task.assert_awaited_once()
    assert {p: p.read_bytes() for p in state.rglob("*") if p.is_file()} == before


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
