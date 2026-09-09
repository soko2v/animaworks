"""Unmet upgrade acceptance: legacy holds must stop actual dispatch.

Strict xfails document a known safety gap, NOT passing safety acceptance.
Only synthetic ledgers and mocked execution boundaries are used.
Remove xfail when runtime enforcement is implemented, then retain assertions.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.memory.task_queue import TaskQueueManager
from core.supervisor.pending_executor import PendingTaskExecutor


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_status", ["blocked", "failed"])
@pytest.mark.parametrize("as_update", [False, True])
@pytest.mark.parametrize("task_type", ["command", "llm"])
@pytest.mark.xfail(
    strict=True,
    reason="Known gap: execute_pending_task does not enforce legacy ledger holds",
)
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
    await executor.execute_pending_task({
        "task_id": "held-task",
        "task_type": task_type,
        "tool_name": "synthetic-never-run",
    })

    assert queue.read_bytes() == original
    executor._execute_llm_task.assert_not_awaited()
    anima.agent.background_manager.submit.assert_not_called()
