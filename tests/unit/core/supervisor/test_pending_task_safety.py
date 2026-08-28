# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
"""Tests for pending task failure safety — file move lifecycle and failure notifications.

Validates:
- File lifecycle: pending/ → processing/ → success: delete | fail: failed/
- _execute_llm_task writes FAILED result and sends reply_to notification on error
- Orphaned processing/ files are recovered to failed/ on startup
- i18n template for task_fail_notify
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.messenger import Messenger
from core.platform.processing_lease import (
    is_processing_lease_live,
    processing_lease_path,
    write_processing_lease,
)
from core.supervisor.pending_executor import PendingTaskExecutor


@pytest.fixture(autouse=True)
def _legacy_completion_semantics():
    with patch(
        "core.supervisor.pending_executor._completion_declaration_required",
        return_value=False,
    ):
        yield

# ── Helpers ──────────────────────────────────────────────────


def _make_executor(tmp_path: Path) -> PendingTaskExecutor:
    """Create a PendingTaskExecutor with mocked anima."""
    anima_dir = tmp_path / "animas" / "test-anima"
    anima_dir.mkdir(parents=True, exist_ok=True)

    mock_anima = MagicMock()
    mock_anima.agent.background_manager = MagicMock()
    mock_anima._background_lock = asyncio.Lock()
    mock_anima._status_slots = {"background": "idle"}
    mock_anima._task_slots = {"background": ""}
    mock_anima.messenger = MagicMock()
    mock_anima._task_semaphore = None

    return PendingTaskExecutor(
        anima=mock_anima,
        anima_name="test-anima",
        anima_dir=anima_dir,
        shutdown_event=asyncio.Event(),
    )


def _stop_after_first(executor: PendingTaskExecutor):
    """Return a mock for asyncio.wait_for that stops the loop after one iteration."""

    async def _mock(coro, *, timeout):
        executor._shutdown_event.set()
        raise TimeoutError

    return _mock


# ── TestCommandPendingFileLifecycle ──────────────────────────


class TestCommandPendingFileLifecycle:
    """Command-type pending tasks use processing/ → failed/ lifecycle."""

    @pytest.mark.asyncio
    async def test_success_removes_processing_file(self, tmp_path: Path) -> None:
        """On success, processing/ file is deleted."""
        executor = _make_executor(tmp_path)
        pending_dir = executor._anima_dir / "state" / "background_tasks" / "pending"
        pending_dir.mkdir(parents=True, exist_ok=True)

        task = {"task_id": "cmd-1", "tool_name": "test_tool", "subcommand": "", "raw_args": []}
        (pending_dir / "cmd-1.json").write_text(json.dumps(task))

        with patch("core.supervisor.pending_executor.asyncio.wait_for", side_effect=_stop_after_first(executor)):
            await executor.watcher_loop()

        assert not (pending_dir / "cmd-1.json").exists()
        processing_dir = pending_dir / "processing"
        assert not (processing_dir / "cmd-1.json").exists()

    @pytest.mark.asyncio
    async def test_failure_moves_to_failed(self, tmp_path: Path) -> None:
        """On execution failure, file is moved from processing/ to failed/."""
        executor = _make_executor(tmp_path)
        pending_dir = executor._anima_dir / "state" / "background_tasks" / "pending"
        pending_dir.mkdir(parents=True, exist_ok=True)

        task = {"task_id": "cmd-fail", "tool_name": "bad_tool", "subcommand": "", "raw_args": []}
        (pending_dir / "cmd-fail.json").write_text(json.dumps(task))

        async def failing_execute(task_desc):
            raise RuntimeError("Simulated failure")

        executor.execute_pending_task = failing_execute  # type: ignore[assignment]

        with patch("core.supervisor.pending_executor.asyncio.wait_for", side_effect=_stop_after_first(executor)):
            await executor.watcher_loop()

        failed_dir = pending_dir / "failed"
        assert (failed_dir / "cmd-fail.json").exists()
        assert not (pending_dir / "cmd-fail.json").exists()

    @pytest.mark.asyncio
    async def test_invalid_json_still_deleted(self, tmp_path: Path) -> None:
        """Invalid JSON files are deleted (not moved)."""
        executor = _make_executor(tmp_path)
        pending_dir = executor._anima_dir / "state" / "background_tasks" / "pending"
        pending_dir.mkdir(parents=True, exist_ok=True)

        (pending_dir / "bad.json").write_text("{invalid")

        with patch("core.supervisor.pending_executor.asyncio.wait_for", side_effect=_stop_after_first(executor)):
            await executor.watcher_loop()

        assert not (pending_dir / "bad.json").exists()


# ── TestLLMPendingFileLifecycle ──────────────────────────────


class TestLLMPendingFileLifecycle:
    """LLM-type pending tasks use processing/ → failed/ lifecycle."""

    @pytest.mark.asyncio
    async def test_success_removes_processing_file(self, tmp_path: Path) -> None:
        """On success, LLM pending processing/ file is deleted."""
        executor = _make_executor(tmp_path)
        llm_dir = executor._anima_dir / "state" / "pending"
        llm_dir.mkdir(parents=True, exist_ok=True)

        task = {"task_type": "llm", "task_id": "llm-1", "description": "test"}
        (llm_dir / "llm-1.json").write_text(json.dumps(task))

        with (
            patch.object(executor, "execute_pending_task", new_callable=AsyncMock),
            patch("core.supervisor.pending_executor.asyncio.wait_for", side_effect=_stop_after_first(executor)),
        ):
            await executor.watcher_loop()

        assert not (llm_dir / "llm-1.json").exists()
        assert not (llm_dir / "processing" / "llm-1.json").exists()

    @pytest.mark.asyncio
    async def test_failure_moves_to_failed(self, tmp_path: Path) -> None:
        """On LLM execution failure, file moves to failed/."""
        executor = _make_executor(tmp_path)
        llm_dir = executor._anima_dir / "state" / "pending"
        llm_dir.mkdir(parents=True, exist_ok=True)

        task = {"task_type": "llm", "task_id": "llm-fail", "description": "failing task"}
        (llm_dir / "llm-fail.json").write_text(json.dumps(task))

        async def failing_execute(task_desc):
            raise RuntimeError("LLM failure")

        executor.execute_pending_task = failing_execute  # type: ignore[assignment]

        with patch("core.supervisor.pending_executor.asyncio.wait_for", side_effect=_stop_after_first(executor)):
            await executor.watcher_loop()

        failed_dir = llm_dir / "failed"
        assert (failed_dir / "llm-fail.json").exists()
        assert not (llm_dir / "llm-fail.json").exists()


# ── TestRecoverProcessing ────────────────────────────────────


class TestRecoverProcessing:
    """Orphaned files in processing/ are moved to failed/ on startup."""

    def test_recovers_orphaned_files(self, tmp_path: Path) -> None:
        processing_dir = tmp_path / "processing"
        processing_dir.mkdir()
        failed_dir = tmp_path / "failed"
        failed_dir.mkdir()

        (processing_dir / "orphan1.json").write_text('{"task_id":"o1"}')
        (processing_dir / "orphan2.json").write_text('{"task_id":"o2"}')

        PendingTaskExecutor._recover_processing(processing_dir, failed_dir)

        assert not list(processing_dir.glob("*.json"))
        assert (failed_dir / "orphan1.json").exists()
        assert (failed_dir / "orphan2.json").exists()

    def test_no_op_when_processing_dir_missing(self, tmp_path: Path) -> None:
        processing_dir = tmp_path / "processing"
        failed_dir = tmp_path / "failed"
        failed_dir.mkdir()
        PendingTaskExecutor._recover_processing(processing_dir, failed_dir)

    def test_live_lease_skips_recovery(self, tmp_path: Path) -> None:
        from core.memory.task_queue import TaskQueueManager

        anima_dir = tmp_path / "test-anima"
        (anima_dir / "state").mkdir(parents=True)
        queue = TaskQueueManager(anima_dir)
        queue.add_task(
            source="human",
            original_instruction="still running",
            assignee="test-anima",
            summary="live",
            status="in_progress",
            task_id="live",
        )
        processing_dir = tmp_path / "processing"
        processing_dir.mkdir()
        failed_dir = tmp_path / "failed"
        failed_dir.mkdir()
        orphan = processing_dir / "live.json"
        orphan.write_text('{"task_id":"live"}', encoding="utf-8")
        lease = write_processing_lease(
            orphan,
            anima="test-anima",
            task_id="live",
            pid=os.getpid(),
        )

        with patch(
            "core.platform.processing_lease._read_proc_cmdline",
            return_value="python -m core.supervisor.runner --anima-name test-anima",
        ):
            PendingTaskExecutor._recover_processing(processing_dir, failed_dir, anima_dir)

        assert orphan.exists()
        assert lease.exists()
        assert not list(failed_dir.iterdir())
        assert queue.get_task_by_id("live").status == "in_progress"

    def test_live_lease_rejects_reused_pid_with_wrong_cmdline(self, tmp_path: Path) -> None:
        descriptor = tmp_path / "reused.json"
        descriptor.write_text('{"task_id":"reused"}', encoding="utf-8")
        write_processing_lease(
            descriptor,
            anima="test-anima",
            task_id="reused",
            pid=os.getpid(),
        )

        with patch(
            "core.platform.processing_lease._read_proc_cmdline",
            return_value="python unrelated-process --anima-name test-anima",
        ):
            assert not is_processing_lease_live(descriptor, expected_anima="test-anima")

    def test_live_lease_treats_unreadable_proc_as_live(self, tmp_path: Path) -> None:
        descriptor = tmp_path / "unknown.json"
        descriptor.write_text('{"task_id":"unknown"}', encoding="utf-8")
        write_processing_lease(
            descriptor,
            anima="test-anima",
            task_id="unknown",
            pid=os.getpid(),
        )

        with patch(
            "core.platform.processing_lease._read_proc_cmdline",
            side_effect=PermissionError("denied"),
        ):
            assert is_processing_lease_live(descriptor, expected_anima="test-anima")

    @pytest.mark.parametrize("malformed", ["invalid_utf8", "huge_json_integer", "huge_pid"])
    def test_malformed_lease_is_not_live(self, tmp_path: Path, malformed: str) -> None:
        descriptor = tmp_path / f"{malformed}.json"
        descriptor.write_text('{"task_id":"malformed"}', encoding="utf-8")
        if malformed == "invalid_utf8":
            processing_lease_path(descriptor).write_bytes(b"\xff\xfe")
        elif malformed == "huge_json_integer":
            processing_lease_path(descriptor).write_text(
                '{"pid":' + ("9" * 5000) + ',"anima":"test-anima","leased_at":1,"task_id":"malformed"}',
                encoding="utf-8",
            )
        else:
            write_processing_lease(
                descriptor,
                anima="test-anima",
                task_id="malformed",
                pid=10**100,
            )

        assert not is_processing_lease_live(descriptor, expected_anima="test-anima")

    def test_syncs_layer2_task_queue_to_failed(self, tmp_path: Path) -> None:
        # F7: recovering an orphaned processing file also transitions the
        # matching Layer2 task_queue entry from in_progress to failed.
        from core.memory.task_queue import TaskQueueManager

        anima_dir = tmp_path / "anima"
        (anima_dir / "state").mkdir(parents=True)
        tqm = TaskQueueManager(anima_dir)
        entry = tqm.add_task(
            source="anima",
            original_instruction="do work",
            assignee="alice",
            summary="orphaned work",
            status="in_progress",
        )

        processing_dir = tmp_path / "processing"
        processing_dir.mkdir()
        failed_dir = tmp_path / "failed"
        failed_dir.mkdir()
        (processing_dir / f"{entry.task_id}.json").write_text(
            f'{{"task_id":"{entry.task_id}"}}'
        )

        PendingTaskExecutor._recover_processing(processing_dir, failed_dir, anima_dir)

        assert (failed_dir / f"{entry.task_id}.json").exists()
        recovered = tqm.get_task_by_id(entry.task_id)
        assert recovered.status == "failed"
        assert recovered.summary == (
            "INTERRUPTED: task was interrupted by a restart and may have PARTIALLY EXECUTED "
            "(commits/messages may already exist). Verify actual completion state before re-delegating."
        )

    def test_layer2_sync_leaves_terminal_tasks_untouched(self, tmp_path: Path) -> None:
        # A task that already reached a terminal state must not be flipped.
        from core.memory.task_queue import TaskQueueManager

        anima_dir = tmp_path / "anima"
        (anima_dir / "state").mkdir(parents=True)
        tqm = TaskQueueManager(anima_dir)
        entry = tqm.add_task(
            source="anima",
            original_instruction="do work",
            assignee="alice",
            summary="already done",
            status="in_progress",
        )
        tqm.update_status(entry.task_id, "done")

        processing_dir = tmp_path / "processing"
        processing_dir.mkdir()
        failed_dir = tmp_path / "failed"
        failed_dir.mkdir()
        (processing_dir / f"{entry.task_id}.json").write_text(
            f'{{"task_id":"{entry.task_id}"}}'
        )

        PendingTaskExecutor._recover_processing(processing_dir, failed_dir, anima_dir)

        assert tqm.get_task_by_id(entry.task_id).status == "done"

    @pytest.mark.asyncio
    async def test_watcher_loop_recovers_on_startup(self, tmp_path: Path) -> None:
        """watcher_loop recovers processing/ orphans before entering main loop."""
        executor = _make_executor(tmp_path)

        cmd_processing = executor._anima_dir / "state" / "background_tasks" / "pending" / "processing"
        cmd_processing.mkdir(parents=True)
        cmd_failed = executor._anima_dir / "state" / "background_tasks" / "pending" / "failed"
        cmd_failed.mkdir(parents=True)
        (cmd_processing / "orphan-cmd.json").write_text('{"task_id":"oc"}')

        llm_processing = executor._anima_dir / "state" / "pending" / "processing"
        llm_processing.mkdir(parents=True)
        llm_failed = executor._anima_dir / "state" / "pending" / "failed"
        llm_failed.mkdir(parents=True)
        (llm_processing / "orphan-llm.json").write_text('{"task_id":"ol"}')

        with patch("core.supervisor.pending_executor.asyncio.wait_for", side_effect=_stop_after_first(executor)):
            await executor.watcher_loop()

        assert (cmd_failed / "orphan-cmd.json").exists()
        assert (llm_failed / "orphan-llm.json").exists()
        assert not list(cmd_processing.glob("*.json"))
        assert not list(llm_processing.glob("*.json"))


@pytest.mark.asyncio
async def test_processing_descriptor_touch_loop_updates_mtime(tmp_path: Path) -> None:
    executor = _make_executor(tmp_path)
    processing_path = tmp_path / "processing.json"
    processing_path.write_text("{}", encoding="utf-8")

    with (
        patch(
            "core.supervisor.pending_executor.asyncio.sleep",
            new=AsyncMock(side_effect=[None, asyncio.CancelledError()]),
        ),
        patch("core.supervisor.pending_executor.os.utime") as touch,
        pytest.raises(asyncio.CancelledError),
    ):
        await executor._touch_processing_descriptor(processing_path)

    touch.assert_called_once_with(processing_path, None)


# ── TestExecuteLLMTaskFailureHandling ────────────────────────


class TestExecuteLLMTaskFailureHandling:
    """_execute_llm_task writes FAILED result and notifies reply_to on error."""

    @pytest.mark.asyncio
    async def test_writes_failed_result(self, tmp_path: Path) -> None:
        """_execute_llm_task calls _write_failed_result on exception."""
        executor = _make_executor(tmp_path)

        with patch.object(executor, "_run_llm_task", side_effect=RuntimeError("boom")):
            task_desc = {"task_id": "fail-1", "description": "test task"}
            await executor._execute_llm_task(task_desc)

        result_path = executor._anima_dir / "state" / "task_results" / "fail-1.md"
        assert result_path.exists()
        content = result_path.read_text()
        assert content.startswith("FAILED:")
        assert "RuntimeError" in content

    @pytest.mark.asyncio
    async def test_sends_reply_to_notification_dict(self, tmp_path: Path) -> None:
        """When reply_to is a dict with 'name', sends failure notification."""
        executor = _make_executor(tmp_path)

        with (
            patch.object(executor, "_run_llm_task", side_effect=RuntimeError("oops")),
            patch("core.i18n.t", return_value="failure msg"),
        ):
            task_desc = {
                "task_id": "fail-notify",
                "description": "failing task",
                "reply_to": {"name": "manager-anima", "content": "please notify"},
            }
            await executor._execute_llm_task(task_desc)

        executor._anima.messenger.send.assert_called_once()
        call_kwargs = executor._anima.messenger.send.call_args
        assert call_kwargs[1]["to"] == "manager-anima"

    @pytest.mark.asyncio
    async def test_sends_reply_to_notification_string(self, tmp_path: Path) -> None:
        """When reply_to is a string, uses it as the recipient."""
        executor = _make_executor(tmp_path)

        with (
            patch.object(executor, "_run_llm_task", side_effect=RuntimeError("oops")),
            patch("core.i18n.t", return_value="failure msg"),
        ):
            task_desc = {
                "task_id": "fail-str",
                "description": "task",
                "reply_to": "some-anima",
            }
            await executor._execute_llm_task(task_desc)

        executor._anima.messenger.send.assert_called_once()
        call_kwargs = executor._anima.messenger.send.call_args
        assert call_kwargs[1]["to"] == "some-anima"

    @pytest.mark.asyncio
    async def test_self_notification_without_reply_to(self, tmp_path: Path) -> None:
        """When no reply_to, sends the failure notification to self."""
        executor = _make_executor(tmp_path)

        with patch.object(executor, "_run_llm_task", side_effect=RuntimeError("fail")):
            task_desc = {"task_id": "fail-noreply", "description": "test"}
            await executor._execute_llm_task(task_desc)

        executor._anima.messenger.send.assert_called_once()
        assert executor._anima.messenger.send.call_args.kwargs["to"] == "test-anima"
        result_path = executor._anima_dir / "state" / "task_results" / "fail-noreply.md"
        assert result_path.exists()

    @pytest.mark.asyncio
    async def test_non_shutdown_cancel_fails_and_notifies_reply_to(self, tmp_path: Path) -> None:
        executor = _make_executor(tmp_path)
        task_desc = {
            "task_type": "llm",
            "task_id": "cancelled",
            "title": "Cancelled task",
            "reply_to": "manager-anima",
        }
        processing = executor._anima_dir / "state" / "pending" / "processing"
        failed = executor._anima_dir / "state" / "pending" / "failed"
        processing.mkdir(parents=True)
        failed.mkdir()
        processing_path = processing / "cancelled.json"
        processing_path.write_text(json.dumps(task_desc), encoding="utf-8")

        from core.memory.task_queue import TaskQueueManager

        queue = TaskQueueManager(executor._anima_dir)
        queue.add_task(
            source="anima",
            original_instruction="work",
            assignee="test-anima",
            summary="work",
            status="in_progress",
            task_id="cancelled",
        )
        executor.execute_pending_task = AsyncMock(side_effect=asyncio.CancelledError)

        with pytest.raises(asyncio.CancelledError):
            await executor._execute_claimed_llm_task(task_desc, processing_path, failed, None)

        assert not processing_path.exists()
        assert (failed / "cancelled.json").exists()
        assert queue.get_task_by_id("cancelled").status == "failed"
        assert executor._anima.messenger.send.call_args.kwargs["to"] == "manager-anima"

    @pytest.mark.asyncio
    async def test_shutdown_preserves_live_lease_before_restart_recovery(self, tmp_path: Path) -> None:
        executor = _make_executor(tmp_path)
        task_desc = {
            "task_type": "llm",
            "task_id": "shutdown",
            "title": "Restarted task",
            "context": "original",
        }
        pending = executor._anima_dir / "state" / "pending"
        processing = pending / "processing"
        failed = pending / "failed"
        processing.mkdir(parents=True)
        failed.mkdir()
        processing_path = processing / "shutdown.json"
        processing_path.write_text(json.dumps(task_desc), encoding="utf-8")
        write_processing_lease(
            processing_path,
            anima="test-anima",
            task_id="shutdown",
        )

        from core.memory.task_queue import TaskQueueManager

        queue = TaskQueueManager(executor._anima_dir)
        queue.add_task(
            source="anima",
            original_instruction="work",
            assignee="test-anima",
            summary="work",
            status="in_progress",
            task_id="shutdown",
        )
        executor.execute_pending_task = AsyncMock(side_effect=asyncio.CancelledError)
        executor._shutdown_event.set()

        with pytest.raises(asyncio.CancelledError):
            await executor._execute_claimed_llm_task(task_desc, processing_path, failed, None)

        assert processing_path.exists()
        assert processing_lease_path(processing_path).exists()
        assert not list(failed.glob("*.json"))
        assert queue.get_task_by_id("shutdown").status == "in_progress"
        executor._anima.messenger.send.assert_not_called()

        with (
            patch(
                "core.supervisor.pending_executor._completion_declaration_required",
                return_value=True,
            ),
            patch("core.supervisor.pending_executor.is_processing_lease_live", return_value=True),
        ):
            PendingTaskExecutor._recover_processing(
                processing,
                failed,
                executor._anima_dir,
                executor._fail_task_terminal,
            )

        assert processing_path.exists()
        assert not (pending / "shutdown.json").exists()

        with (
            patch(
                "core.supervisor.pending_executor._completion_declaration_required",
                return_value=True,
            ),
            patch("core.supervisor.pending_executor.is_processing_lease_live", return_value=False),
        ):
            PendingTaskExecutor._recover_processing(
                processing,
                failed,
                executor._anima_dir,
                executor._fail_task_terminal,
            )

        recovered = json.loads((pending / "shutdown.json").read_text(encoding="utf-8"))
        assert recovered["continuation_count"] == 1
        assert "continuation_not_before" not in recovered
        assert "プロセス異常終了" in recovered["context"]
        assert queue.get_task_by_id("shutdown").status == "in_progress"
        executor._anima.messenger.send.assert_not_called()

    def test_periodic_recovery_keeps_live_lease_then_requeues_when_dead(self, tmp_path: Path) -> None:
        executor = _make_executor(tmp_path)
        task_desc = {
            "task_type": "llm",
            "task_id": "periodic-recovery",
            "title": "Periodic recovery",
            "context": "original",
        }
        pending = executor._anima_dir / "state" / "pending"
        processing = pending / "processing"
        processing.mkdir(parents=True)
        processing_path = processing / "periodic-recovery.json"
        processing_path.write_text(json.dumps(task_desc), encoding="utf-8")
        write_processing_lease(
            processing_path,
            anima="test-anima",
            task_id="periodic-recovery",
        )

        from core.memory.task_queue import TaskQueueManager

        queue = TaskQueueManager(executor._anima_dir)
        queue.add_task(
            source="anima",
            original_instruction="work",
            assignee="test-anima",
            summary="work",
            status="in_progress",
            task_id="periodic-recovery",
        )

        with (
            patch(
                "core.supervisor.pending_executor._completion_declaration_required",
                return_value=True,
            ),
            patch("core.supervisor.pending_executor.is_processing_lease_live", return_value=True),
            patch("core.blocked_recovery.revalidate_blocked_tasks"),
        ):
            executor._recover_blocked_and_orphaned_tasks()

        assert processing_path.exists()
        assert not (pending / "periodic-recovery.json").exists()

        with (
            patch(
                "core.supervisor.pending_executor._completion_declaration_required",
                return_value=True,
            ),
            patch("core.supervisor.pending_executor.is_processing_lease_live", return_value=False),
            patch("core.blocked_recovery.revalidate_blocked_tasks"),
        ):
            executor._recover_blocked_and_orphaned_tasks()

        recovered = json.loads((pending / "periodic-recovery.json").read_text(encoding="utf-8"))
        assert recovered["continuation_count"] == 1
        assert not processing_path.exists()
        assert queue.get_task_by_id("periodic-recovery").status == "in_progress"

    def test_recovery_at_continuation_limit_notifies_reply_to(self, tmp_path: Path) -> None:
        executor = _make_executor(tmp_path)
        task_desc = {
            "task_type": "llm",
            "task_id": "exhausted",
            "title": "Exhausted task",
            "reply_to": "manager-anima",
            "continuation_count": 3,
        }
        processing = executor._anima_dir / "state" / "pending" / "processing"
        failed = executor._anima_dir / "state" / "pending" / "failed"
        processing.mkdir(parents=True)
        failed.mkdir()
        (processing / "exhausted.json").write_text(json.dumps(task_desc), encoding="utf-8")

        from core.memory.task_queue import TaskQueueManager

        queue = TaskQueueManager(executor._anima_dir)
        queue.add_task(
            source="anima",
            original_instruction="work",
            assignee="test-anima",
            summary="work",
            status="in_progress",
            task_id="exhausted",
        )

        with patch(
            "core.supervisor.pending_executor._completion_declaration_required",
            return_value=True,
        ):
            PendingTaskExecutor._recover_processing(
                processing,
                failed,
                executor._anima_dir,
                executor._fail_task_terminal,
            )

        assert (failed / "exhausted.json").exists()
        assert queue.get_task_by_id("exhausted").status == "failed"
        assert executor._anima.messenger.send.call_args.kwargs["to"] == "manager-anima"

    @pytest.mark.asyncio
    async def test_notification_failure_does_not_propagate(self, tmp_path: Path) -> None:
        """If messenger.send fails, the exception is swallowed."""
        executor = _make_executor(tmp_path)
        executor._anima.messenger.send.side_effect = ConnectionError("network error")

        with (
            patch.object(executor, "_run_llm_task", side_effect=RuntimeError("boom")),
            patch("core.i18n.t", return_value="msg"),
        ):
            task_desc = {
                "task_id": "fail-notif-err",
                "description": "task",
                "reply_to": {"name": "boss"},
            }
            await executor._execute_llm_task(task_desc)

        result_path = executor._anima_dir / "state" / "task_results" / "fail-notif-err.md"
        assert result_path.exists()

    @pytest.mark.asyncio
    async def test_status_slots_reset_on_failure(self, tmp_path: Path) -> None:
        """Status slots are reset to idle even on failure."""
        executor = _make_executor(tmp_path)

        with patch.object(executor, "_run_llm_task", side_effect=RuntimeError("err")):
            task_desc = {"task_id": "slot-test", "description": "test"}
            await executor._execute_llm_task(task_desc)

        assert executor._anima._status_slots["background"] == "idle"
        assert executor._anima._task_slots["background"] == ""

    @pytest.mark.asyncio
    async def test_completion_notification_can_be_self_addressed(self, tmp_path: Path) -> None:
        """Task completion notifications may target the submitting anima itself."""
        executor = _make_executor(tmp_path)
        shared_dir = tmp_path / "shared"
        executor._anima.messenger = Messenger(shared_dir, "test-anima")

        async def _run_cycle_streaming(*_args, **_kwargs):
            yield {
                "type": "cycle_done",
                "cycle_result": {"summary": "task finished", "action": "responded"},
            }

        executor._anima.agent.run_cycle_streaming = _run_cycle_streaming
        task_desc = {
            "task_id": "self-complete",
            "title": "Self completion",
            "description": "notify the submitter",
            "reply_to": "test-anima",
        }

        result = await executor._run_llm_task(task_desc)

        assert result == "task finished"
        inbox_files = list((shared_dir / "inbox" / "test-anima").glob("*.json"))
        assert len(inbox_files) == 1
        delivered = json.loads(inbox_files[0].read_text(encoding="utf-8"))
        assert delivered["from_person"] == "test-anima"
        assert delivered["to_person"] == "test-anima"
        assert "self-complete" in delivered["content"]


# ── TestI18nTemplate ─────────────────────────────────────────


class TestI18nTemplate:
    """Verify task_fail_notify i18n template exists and formats correctly."""

    def test_ja_template(self) -> None:
        from core.i18n import t

        result = t(
            "pending_executor.task_fail_notify",
            locale="ja",
            task_id="test-123",
            title="テストタスク",
            error="RuntimeError: boom",
        )
        assert "test-123" in result
        assert "テストタスク" in result
        assert "RuntimeError: boom" in result
        assert "再委譲" in result

    def test_en_template(self) -> None:
        from core.i18n import t

        result = t(
            "pending_executor.task_fail_notify",
            locale="en",
            task_id="test-456",
            title="Test Task",
            error="ValueError: bad",
        )
        assert "test-456" in result
        assert "Test Task" in result
        assert "ValueError: bad" in result
        assert "re-delegate" in result

    def test_ko_template(self) -> None:
        from core.i18n import t

        result = t(
            "pending_executor.task_fail_notify",
            locale="ko",
            task_id="test-789",
            title="테스트 작업",
            error="RuntimeError: 실패",
        )
        assert "test-789" in result
        assert "테스트 작업" in result
        assert "재위임" in result
