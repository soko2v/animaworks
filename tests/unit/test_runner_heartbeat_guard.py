# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for heartbeat collision prevention.

Verifies that the heartbeat_running flag and _cron_running set properly
prevent overlapping heartbeat and cron executions in SchedulerManager
and InboxRateLimiter.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.supervisor.inbox_rate_limiter import InboxRateLimiter
from core.supervisor.scheduler_manager import SchedulerManager


def _legacy_anima_dir(tmp_path):
    d = tmp_path / "animas" / "guard-test"
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text('{"process_model": "legacy"}', encoding="utf-8")
    return d


def _make_scheduler_mgr(tmp_path: Path) -> SchedulerManager:
    """Create a SchedulerManager with minimal dependencies."""
    mock_anima = MagicMock()
    mock_anima.run_heartbeat = AsyncMock()
    return SchedulerManager(
        anima=mock_anima,
        anima_name="guard-test",
        anima_dir=_legacy_anima_dir(tmp_path),
        emit_event=MagicMock(),
    )


def _make_inbox_limiter(
    tmp_path: Path, scheduler_mgr: SchedulerManager | None = None,
) -> InboxRateLimiter:
    """Create an InboxRateLimiter with minimal dependencies."""
    mock_anima = MagicMock()
    # Nonexistent status.json → _read_anima_enabled defaults to True.
    mock_anima.anima_dir = tmp_path / "animas" / "alice"
    mock_anima.run_heartbeat = AsyncMock()
    mock_anima.messenger.receive.return_value = []
    mock_anima._lock = asyncio.Lock()

    if scheduler_mgr is None:
        scheduler_mgr = MagicMock(spec=SchedulerManager)
        scheduler_mgr.heartbeat_running = False

    return InboxRateLimiter(
        anima=mock_anima,
        anima_name="guard-test",
        shutdown_event=asyncio.Event(),
        scheduler_mgr=scheduler_mgr,
    )


class TestHeartbeatGuard:
    """Verify heartbeat overlap prevention in SchedulerManager."""

    def test_initial_heartbeat_running_is_false(self, tmp_path):
        """SchedulerManager initializes with heartbeat_running=False."""
        mgr = _make_scheduler_mgr(tmp_path)
        assert mgr.heartbeat_running is False

    def test_initial_cron_running_is_empty(self, tmp_path):
        """SchedulerManager initializes with empty _cron_running set."""
        mgr = _make_scheduler_mgr(tmp_path)
        assert mgr._cron_running == set()

    @pytest.mark.asyncio
    async def test_heartbeat_tick_skips_when_already_running(self, tmp_path):
        """heartbeat_tick should skip immediately when heartbeat_running is True."""
        mgr = _make_scheduler_mgr(tmp_path)
        mgr._heartbeat_running = True

        await mgr.heartbeat_tick()

        # run_heartbeat should NOT have been called
        mgr._anima.run_heartbeat.assert_not_called()

    @pytest.mark.asyncio
    async def test_heartbeat_tick_runs_when_not_already_running(self, tmp_path):
        """heartbeat_tick should execute when heartbeat_running is False."""
        mgr = _make_scheduler_mgr(tmp_path)
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {"summary": "ok"}
        mgr._anima.run_heartbeat = AsyncMock(return_value=mock_result)

        assert mgr.heartbeat_running is False

        await mgr.heartbeat_tick()

        mgr._anima.run_heartbeat.assert_called_once()

    @pytest.mark.asyncio
    async def test_heartbeat_tick_resets_flag_after_completion(self, tmp_path):
        """heartbeat_running flag resets to False after heartbeat completes."""
        mgr = _make_scheduler_mgr(tmp_path)
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {"summary": "ok"}
        mgr._anima.run_heartbeat = AsyncMock(return_value=mock_result)

        await mgr.heartbeat_tick()

        assert mgr.heartbeat_running is False

    @pytest.mark.asyncio
    async def test_heartbeat_tick_resets_flag_on_exception(self, tmp_path):
        """heartbeat_running flag resets even when heartbeat raises an exception."""
        mgr = _make_scheduler_mgr(tmp_path)
        mgr._anima.run_heartbeat = AsyncMock(side_effect=RuntimeError("boom"))

        await mgr.heartbeat_tick()

        # Flag must be reset even after failure
        assert mgr.heartbeat_running is False

    @pytest.mark.asyncio
    async def test_heartbeat_tick_skips_when_no_anima(self, tmp_path):
        """heartbeat_tick returns early when anima is None."""
        mgr = _make_scheduler_mgr(tmp_path)
        mgr._anima = None

        # Should not raise
        await mgr.heartbeat_tick()


class TestCronGuard:
    """Verify cron overlap prevention in SchedulerManager."""

    @pytest.mark.asyncio
    async def test_cron_tick_skips_when_already_running(self, tmp_path):
        """cron_tick should skip when the same task name is in _cron_running."""
        from core.schemas import CronTask

        mgr = _make_scheduler_mgr(tmp_path)
        mgr._anima.run_cron_task = AsyncMock()

        task = CronTask(name="daily_report", schedule="0 9 * * *", description="test", type="llm")
        mgr._cron_running.add("daily_report")

        await mgr.cron_tick(task)

        # The task's LLM call should NOT be started
        mgr._anima.run_cron_task.assert_not_called()
        mgr._anima.memory.append_cron_event.assert_called_once_with(
            "daily_report",
            "skipped",
            reason="already running",
            schedule="0 9 * * *",
        )

    @pytest.mark.asyncio
    async def test_cron_tick_runs_when_not_already_running(self, tmp_path):
        """cron_tick should dispatch when the task name is not in _cron_running."""
        from core.schemas import CronTask

        mgr = _make_scheduler_mgr(tmp_path)
        mgr._anima.run_cron_task = AsyncMock()

        task = CronTask(name="weekly_review", schedule="0 9 * * 1", description="test", type="llm")
        assert "weekly_review" not in mgr._cron_running

        # cron_tick creates a background task via asyncio.create_task
        await mgr.cron_tick(task)

        # Give the background task a moment to start
        await asyncio.sleep(0.05)
        mgr._anima.memory.append_cron_event.assert_any_call(
            "weekly_review",
            "fired",
            reason="",
            schedule="0 9 * * 1",
        )

    @pytest.mark.asyncio
    async def test_concurrent_ticks_claim_name_before_dispatch(self, tmp_path):
        from core.schemas import CronTask

        mgr = _make_scheduler_mgr(tmp_path)
        result = MagicMock(action="completed", usage={})
        result.model_dump.return_value = {}
        mgr._anima.run_cron_task = AsyncMock(return_value=result)
        task = CronTask(name="daily", schedule="0 9 * * *", description="test", type="llm")

        await mgr.cron_tick(task)
        await mgr.cron_tick(task)
        await asyncio.sleep(0.05)

        events = [call.args[1] for call in mgr._anima.memory.append_cron_event.call_args_list]
        assert events.count("fired") == 1
        assert events.count("skipped") == 1

    @pytest.mark.asyncio
    async def test_cron_running_tracks_task_name(self, tmp_path):
        """_cron_running should contain task names that are currently executing."""
        mgr = _make_scheduler_mgr(tmp_path)

        mgr._cron_running.add("daily_report")
        mgr._cron_running.add("weekly_review")

        assert "daily_report" in mgr._cron_running
        assert "weekly_review" in mgr._cron_running
        assert "monthly_summary" not in mgr._cron_running

    @pytest.mark.asyncio
    async def test_run_cron_task_removes_name_on_completion(self, tmp_path):
        """_run_cron_task should discard task name from _cron_running after completion."""
        from core.schemas import CronTask

        mgr = _make_scheduler_mgr(tmp_path)
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {"summary": "done"}
        mgr._anima.run_cron_task = AsyncMock(return_value=mock_result)

        task = CronTask(name="daily_report", schedule="0 9 * * *", description="test", type="llm")

        await mgr._run_cron_task(task)

        assert "daily_report" not in mgr._cron_running

    @pytest.mark.asyncio
    async def test_run_cron_task_removes_name_on_exception(self, tmp_path):
        """_run_cron_task should discard task name even when execution fails."""
        from core.schemas import CronTask

        mgr = _make_scheduler_mgr(tmp_path)
        mgr._anima.run_cron_task = AsyncMock(side_effect=RuntimeError("cron failed"))

        task = CronTask(name="daily_report", schedule="0 9 * * *", description="test", type="llm")

        await mgr._run_cron_task(task)

        # Must be cleaned up despite error
        assert "daily_report" not in mgr._cron_running
        mgr._anima.memory.append_cron_event.assert_called_with(
            "daily_report",
            "failed",
            reason="execution failed",
            schedule="0 9 * * *",
        )

    @pytest.mark.asyncio
    async def test_hard_timeout_releases_slot_and_next_tick_runs(self, tmp_path):
        """A hung watchdog times out without permanently occupying single-flight."""
        from core.schemas import CronTask

        mgr = _make_scheduler_mgr(tmp_path)
        blocker = asyncio.Event()
        calls = 0

        async def run_cron(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                await blocker.wait()
            result = MagicMock(action="completed", usage={})
            result.model_dump.return_value = {}
            return result

        mgr._anima.run_cron_task = AsyncMock(side_effect=run_cron)
        task = CronTask(
            name="watchdog",
            schedule="*/10 * * * *",
            description="bounded exact-path check",
            hard_timeout_seconds=0.02,
        )

        await mgr.cron_tick(task)
        await asyncio.sleep(0.05)
        assert "watchdog" not in mgr._cron_running
        assert mgr._anima.memory.append_cron_event.call_args_list[-1].args[1] == "timeout"

        await mgr.cron_tick(task)
        await asyncio.sleep(0.05)
        assert calls == 2
        assert "watchdog" not in mgr._cron_running
        assert mgr._anima.memory.append_cron_event.call_args_list[-1].args[1] == "succeeded"

    @pytest.mark.asyncio
    async def test_watchdog_precheck_is_inside_hard_timeout(self, tmp_path, monkeypatch):
        """Even the bounded-path precheck cannot overrun the job budget."""
        import time

        from core.schemas import CronTask

        mgr = _make_scheduler_mgr(tmp_path)
        monkeypatch.setattr(mgr, "_watchdog_stop_suspicion", lambda _task: time.sleep(0.05))
        task = CronTask(
            name="watchdog",
            schedule="*/10 * * * *",
            description="bounded check",
            hard_timeout_seconds=0.01,
        )

        await mgr._run_cron_task(task)

        mgr._anima.run_cron_task.assert_not_called()
        assert "watchdog" not in mgr._cron_running
        assert mgr._anima.memory.append_cron_event.call_args_list[-1].args[1] == "timeout"

    @pytest.mark.asyncio
    async def test_isolated_timeout_cancels_runner_and_releases_slot(self, tmp_path):
        """Timeout cancellation reaches the isolated runner lease owner."""
        from core.schemas import CronTask

        mgr = _make_scheduler_mgr(tmp_path)
        mgr._cron_isolated = True
        runner = AsyncMock()
        cancelled = asyncio.Event()

        async def hang(_task):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        runner.run_cron.side_effect = hang
        mgr._task_runner_supervisor = runner
        task = CronTask(name="watchdog", schedule="*/10 * * * *", hard_timeout_seconds=0.02)

        await mgr._run_cron_task(task)

        assert cancelled.is_set()
        assert "watchdog" not in mgr._cron_running

    @pytest.mark.asyncio
    async def test_missing_descriptor_beats_old_artifact_mtime(self, tmp_path):
        """An old result artifact cannot mask a missing unfinished descriptor."""
        import json
        import os
        import time

        from core.schemas import CronTask

        mgr = _make_scheduler_mgr(tmp_path)
        target = tmp_path / "animas" / "sofia"
        (target / "state" / "task_results").mkdir(parents=True)
        queue_entry = {
            "task_id": "durable1",
            "source": "human",
            "original_instruction": "continue",
            "assignee": "sofia",
            "status": "in_progress",
            "summary": "running",
            "ts": "2026-08-29T03:00:00+09:00",
            "updated_at": "2026-08-29T03:35:45+09:00",
            "deadline": "2026-08-29T12:00:00+09:00",
            "relay_chain": [],
            "meta": {},
        }
        (target / "state" / "task_queue.jsonl").write_text(json.dumps(queue_entry) + "\n", encoding="utf-8")
        artifact = target / "state" / "task_results" / "durable1.md"
        artifact.write_text("old progress", encoding="utf-8")
        old = time.time() - 3600
        os.utime(artifact, (old, old))
        task = CronTask(
            name="watchdog",
            schedule="*/10 * * * *",
            watchdog_anima="sofia",
            watchdog_task_id="durable1",
            hard_timeout_seconds=120,
        )

        assert mgr._watchdog_stop_suspicion(task) == "unfinished task descriptor is missing"
        result = MagicMock(action="completed", usage={})
        result.model_dump.return_value = {}
        mgr._anima.run_cron_task = AsyncMock(return_value=result)
        await mgr._run_cron_task(task)
        prompt = mgr._anima.run_cron_task.await_args.args[1]
        assert prompt.startswith("STOP_SUSPECTED: unfinished task descriptor is missing")
        events = [call.args[1] for call in mgr._anima.memory.append_cron_event.call_args_list]
        assert events[-2:] == ["stop_suspected", "succeeded"]

    @pytest.mark.asyncio
    async def test_dead_processing_lease_is_stop_suspected(self, tmp_path):
        """An unfinished processing descriptor without a runner is fail-closed."""
        import json

        from core.schemas import CronTask

        mgr = _make_scheduler_mgr(tmp_path)
        target = tmp_path / "animas" / "sofia"
        processing = target / "state" / "pending" / "processing"
        processing.mkdir(parents=True)
        queue_entry = {
            "task_id": "durable2",
            "ts": "2026-08-29T03:00:00+09:00",
            "source": "human",
            "original_instruction": "continue",
            "assignee": "sofia",
            "status": "in_progress",
            "summary": "running",
            "updated_at": "2026-08-29T03:35:45+09:00",
            "relay_chain": [],
            "meta": {},
        }
        (target / "state" / "task_queue.jsonl").write_text(json.dumps(queue_entry) + "\n", encoding="utf-8")
        (processing / "durable2.json").write_text(json.dumps(queue_entry), encoding="utf-8")
        task = CronTask(
            name="watchdog",
            schedule="*/10 * * * *",
            watchdog_anima="sofia",
            watchdog_task_id="durable2",
            hard_timeout_seconds=120,
        )

        assert mgr._watchdog_stop_suspicion(task) == "unfinished task has no live runner lease"

    @pytest.mark.asyncio
    async def test_live_processing_lease_is_not_stop_suspected(self, tmp_path, monkeypatch):
        """A live runner lease preserves the normal watchdog path."""
        import json

        from core.schemas import CronTask

        mgr = _make_scheduler_mgr(tmp_path)
        target = tmp_path / "animas" / "sofia"
        processing = target / "state" / "pending" / "processing"
        processing.mkdir(parents=True)
        queue_entry = {
            "task_id": "durable3",
            "ts": "2026-08-29T03:00:00+09:00",
            "source": "human",
            "original_instruction": "continue",
            "assignee": "sofia",
            "status": "in_progress",
            "summary": "running",
            "updated_at": "2026-08-29T03:35:45+09:00",
            "relay_chain": [],
            "meta": {},
        }
        (target / "state" / "task_queue.jsonl").write_text(json.dumps(queue_entry) + "\n", encoding="utf-8")
        (processing / "durable3.json").write_text(json.dumps(queue_entry), encoding="utf-8")
        monkeypatch.setattr(
            "core.supervisor.scheduler_manager.classify_processing_lease",
            lambda *_args, **_kwargs: "live",
        )
        task = CronTask(
            name="watchdog",
            schedule="*/10 * * * *",
            watchdog_anima="sofia",
            watchdog_task_id="durable3",
            hard_timeout_seconds=120,
        )
        result = MagicMock(action="completed", usage={})
        result.model_dump.return_value = {}
        mgr._anima.run_cron_task = AsyncMock(return_value=result)

        assert mgr._watchdog_stop_suspicion(task) is None
        await mgr._run_cron_task(task)
        assert "STOP_SUSPECTED" not in mgr._anima.run_cron_task.await_args.args[1]
        assert mgr._anima.memory.append_cron_event.call_args_list[-1].args[1] == "succeeded"


class TestMessageTriggeredHeartbeatGuard:
    """Verify message-triggered heartbeat respects the guard flag."""

    @pytest.mark.asyncio
    async def test_message_inbox_skips_when_already_running(self, tmp_path):
        """message_triggered_inbox should skip when heartbeat_running is True."""
        mock_scheduler_mgr = MagicMock(spec=SchedulerManager)
        mock_scheduler_mgr.heartbeat_running = True

        limiter = _make_inbox_limiter(tmp_path, mock_scheduler_mgr)
        limiter._pending_trigger = True

        await limiter.message_triggered_inbox()

        limiter._anima.process_inbox_message.assert_not_called()
        assert limiter._pending_trigger is False


class TestRunnerHeartbeat24hDefault:
    """Verify SchedulerManager._setup_heartbeat respects 2-tier active hours resolution."""

    def test_default_24h_when_no_time_range(self, tmp_path):
        """No time range in heartbeat.md => hour='*' (24h)."""
        mock_anima = MagicMock()
        mock_anima.memory.read_heartbeat_config.return_value = "- チェック項目A"

        mgr = SchedulerManager(
            anima=mock_anima,
            anima_name="guard-test",
            anima_dir=_legacy_anima_dir(tmp_path),
            emit_event=MagicMock(),
        )
        mock_scheduler = MagicMock()
        mgr.scheduler = mock_scheduler

        mgr._setup_heartbeat()

        mock_scheduler.add_job.assert_called_once()
        call_kwargs = mock_scheduler.add_job.call_args
        trigger = call_kwargs[1]["trigger"] if "trigger" in (call_kwargs[1] or {}) else call_kwargs[0][1]
        hour_field = str(trigger.fields[5])
        assert hour_field == "*"

    def test_heartbeat_md_time_range_restricts_hours(self, tmp_path):
        """Time range in heartbeat.md restricts heartbeat hours."""
        mock_anima = MagicMock()
        mock_anima.memory.read_heartbeat_config.return_value = "稼働時間: 8:00 - 20:00"

        mgr = SchedulerManager(
            anima=mock_anima,
            anima_name="guard-test",
            anima_dir=_legacy_anima_dir(tmp_path),
            emit_event=MagicMock(),
        )
        mock_scheduler = MagicMock()
        mgr.scheduler = mock_scheduler

        mgr._setup_heartbeat()

        mock_scheduler.add_job.assert_called_once()
        call_kwargs = mock_scheduler.add_job.call_args
        trigger = call_kwargs[1]["trigger"] if "trigger" in (call_kwargs[1] or {}) else call_kwargs[0][1]
        hour_field = str(trigger.fields[5])
        assert "8-19" in hour_field
