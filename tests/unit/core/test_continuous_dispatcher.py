from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.continuous_dispatcher import DispatchResult, _exclusive_lock, dispatch_once
from core.goals import GoalManager
from core.memory.task_queue import TaskQueueManager


def _setup(tmp_path: Path, candidates: list[dict]) -> Path:
    anima_dir = tmp_path / "sofia"
    (anima_dir / "state" / "pending" / "processing").mkdir(parents=True)
    (anima_dir / "state" / "continuous_backlog.json").write_text(
        json.dumps({"candidates": candidates}), encoding="utf-8"
    )
    return anima_dir


def _candidate(task_id: str, priority: int = 10, **extra: object) -> dict:
    return {
        "task_id": task_id,
        "key": task_id,
        "title": f"Task {task_id}",
        "description": f"Perform safe work {task_id}",
        "priority": priority,
        "enabled": True,
        "approved_safe": True,
        "capabilities": ["local_code"],
        **extra,
    }


def test_dispatches_one_candidate_once(tmp_path: Path) -> None:
    anima_dir = _setup(tmp_path, [_candidate("task-b")])
    first = dispatch_once(anima_dir)
    assert first == DispatchResult("dispatched", "task-b", "one safe candidate published")
    assert (anima_dir / "state" / "pending" / "task-b.json").is_file()
    second = dispatch_once(anima_dir)
    assert second.status == "no_op"
    assert len(TaskQueueManager(anima_dir).list_tasks()) == 1


def test_runner_exists_is_no_op(tmp_path: Path) -> None:
    anima_dir = _setup(tmp_path, [_candidate("task-b")])
    queue = TaskQueueManager(anima_dir)
    queue.add_task(source="anima", original_instruction="A", assignee="sofia", summary="A", task_id="task-a", status="in_progress")
    assert dispatch_once(anima_dir).status == "no_op"
    assert not (anima_dir / "state" / "pending" / "task-b.json").exists()


def test_processing_descriptor_is_no_op(tmp_path: Path) -> None:
    anima_dir = _setup(tmp_path, [_candidate("task-b")])
    (anima_dir / "state" / "pending" / "processing" / "task-a.json").write_text("{}", encoding="utf-8")
    assert dispatch_once(anima_dir).status == "no_op"


def test_pending_descriptor_is_no_op(tmp_path: Path) -> None:
    anima_dir = _setup(tmp_path, [_candidate("task-b")])
    (anima_dir / "state" / "pending" / "task-a.json").write_text("{}", encoding="utf-8")
    assert dispatch_once(anima_dir).status == "no_op"
    assert not (anima_dir / "state" / "pending" / "task-b.json").exists()


def test_blocked_and_waiting_candidate_skipped_for_next(tmp_path: Path) -> None:
    anima_dir = _setup(tmp_path, [_candidate("task-a", 1), _candidate("task-b", 2)])
    queue = TaskQueueManager(anima_dir)
    queue.add_task(source="anima", original_instruction="A", assignee="sofia", summary="A", task_id="task-a")
    queue.update_status("task-a", "blocked", summary="[Waiting] credential")
    result = dispatch_once(anima_dir)
    assert result.task_id == "task-b"
    assert len(list((anima_dir / "state" / "pending").glob("task-b.json"))) == 1
    blocked = queue.get_task_by_id("task-a")
    assert blocked is not None
    assert blocked.status == "blocked"
    assert blocked.summary == "[Waiting] credential"


def test_descriptor_loss_is_restored_without_duplicate_queue_entry(tmp_path: Path) -> None:
    anima_dir = _setup(tmp_path, [_candidate("task-a")])
    queue = TaskQueueManager(anima_dir)
    queue.add_task(source="anima", original_instruction="A", assignee="sofia", summary="A", task_id="task-a")
    result = dispatch_once(anima_dir)
    assert result.task_id == "task-a"
    assert len(queue.list_tasks()) == 1


def test_empty_queue_is_idle(tmp_path: Path) -> None:
    assert dispatch_once(_setup(tmp_path, [])).status == "idle"


def test_denied_capabilities_never_dispatch(tmp_path: Path) -> None:
    denied = ["production_deploy", "production_db", "migration", "production_data", "credential", "external_send"]
    anima_dir = _setup(tmp_path, [_candidate(f"task-{i}", i, capabilities=[cap]) for i, cap in enumerate(denied)])
    assert dispatch_once(anima_dir).status == "idle"
    assert TaskQueueManager(anima_dir).list_tasks() == []


def test_single_exclusive_lock(tmp_path: Path) -> None:
    anima_dir = _setup(tmp_path, [_candidate("task-a")])
    lock = anima_dir / "state" / "continuous_dispatcher.lock"
    with _exclusive_lock(lock) as acquired:
        assert acquired
        assert dispatch_once(anima_dir).reason == "dispatcher lock is held"


def test_concurrent_handoffs_publish_no_duplicates(tmp_path: Path) -> None:
    anima_dir = _setup(tmp_path, [_candidate("task-a", 1), _candidate("task-b", 2)])
    barrier = threading.Barrier(2)

    def _dispatch() -> DispatchResult:
        barrier.wait(timeout=2)
        return dispatch_once(anima_dir)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: _dispatch(), range(2)))

    assert sum(result.status == "dispatched" for result in results) == 1
    assert len(list((anima_dir / "state" / "pending").glob("*.json"))) == 1
    assert len(TaskQueueManager(anima_dir).list_tasks()) == 1


def test_completed_candidate_skipped_for_next(tmp_path: Path) -> None:
    anima_dir = _setup(tmp_path, [_candidate("task-a", 1), _candidate("task-b", 2)])
    queue = TaskQueueManager(anima_dir)
    queue.add_task(source="anima", original_instruction="A", assignee="sofia", summary="A", task_id="task-a")
    queue.update_status("task-a", "done")
    assert dispatch_once(anima_dir).task_id == "task-b"


def test_future_liveness_phase_does_not_block_due_safe_backlog(tmp_path: Path) -> None:
    anima_dir = _setup(tmp_path, [_candidate("task-b")])
    GoalManager(anima_dir).set_goal(
        goal_id="future-goal",
        objective="Run a later phase",
        success_criteria=["phase completes"],
    )
    (anima_dir / "state" / "execution_liveness.json").write_text(
        json.dumps(
            {
                "phases": [
                    {
                        "goal_id": "future-goal",
                        "task_id": "future-task",
                        "description": "Run later",
                        "approved_safe": True,
                        "capabilities": ["local_code"],
                        "checkpoint_path": "state/future.checkpoint",
                        "start_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert dispatch_once(anima_dir) == DispatchResult("dispatched", "task-b", "one safe candidate published")
