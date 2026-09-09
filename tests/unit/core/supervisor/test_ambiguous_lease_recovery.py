"""Exercise real recovery callers using synthetic files, without a worker/model."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from core.memory.task_queue import TaskQueueManager
from core.memory.taskboard_housekeeping import _cleanup_pending_processing
from core.platform.processing_lease import processing_lease_path, write_processing_lease
from core.supervisor.pending_executor import PendingTaskExecutor


@pytest.mark.parametrize("caller", ["executor", "callback", "housekeeping"])
@pytest.mark.parametrize("fault", ["missing", "malformed", "wrong_anima", "unknown_start", "live", "dead"])
def test_recovery_preserves_ambiguous_evidence(tmp_path: Path, caller: str, fault: str) -> None:
    anima_dir = tmp_path / "animas" / "synthetic"
    processing = anima_dir / "state" / "pending" / "processing"
    processing.mkdir(parents=True)
    queue = TaskQueueManager(anima_dir)
    queue.add_task(source="human", original_instruction="synthetic only", assignee="synthetic",
                   summary="preserve", status="in_progress", task_id="task")
    descriptor = processing / "task.json"
    descriptor.write_text(json.dumps({"task_id": "task", "description": "synthetic only"}))
    os.utime(descriptor, (1, 1))
    lease = processing_lease_path(descriptor)
    if fault == "malformed":
        lease.write_bytes(b"\xff")
    elif fault != "missing":
        write_processing_lease(
            descriptor, anima="other" if fault == "wrong_anima" else "synthetic",
            task_id="task", pid=os.getpid(), job_id="job", task_pid=os.getpid(),
            pgid=os.getpid(), root_epoch="epoch", attempt=1, process_start_time=1.0,
        )
    before = {p.relative_to(anima_dir): p.read_bytes() for p in anima_dir.rglob("*") if p.is_file()}
    callback = Mock()
    with (
        patch("core.platform.processing_lease._pid_exists", return_value=fault != "dead"),
        patch("core.platform.processing_lease._process_create_time",
              return_value=None if fault == "unknown_start" else 1.0),
        patch("core.platform.processing_lease._read_proc_cmdline",
              return_value="python -m core.supervisor.task_runner synthetic job"),
    ):
        if caller == "housekeeping":
            _cleanup_pending_processing(tmp_path / "animas", 24, None)
        else:
            PendingTaskExecutor._recover_processing(
                processing, anima_dir, callback if caller == "callback" else None,
            )
    if fault != "dead":
        after = {p.relative_to(anima_dir): p.read_bytes() for p in anima_dir.rglob("*") if p.is_file()}
        assert after == before
        callback.assert_not_called()
        assert queue.get_task_by_id("task").status == "in_progress"
    else:
        assert not descriptor.exists()
        if caller == "callback":
            callback.assert_called_once()
        else:
            assert queue.get_task_by_id("task").status == "pending"
