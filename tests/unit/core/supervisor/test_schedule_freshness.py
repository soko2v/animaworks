"""Tests for stale schedule detection via mtime reconciliation."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.lifecycle.scheduler import SchedulerMixin
from core.schemas import CronTask
from core.supervisor.scheduler_manager import SchedulerManager


@pytest.fixture
def scheduler_mgr(tmp_path: Path) -> SchedulerManager:
    """Create a SchedulerManager with a temp anima dir."""
    anima = MagicMock()
    anima.memory.read_heartbeat_config.return_value = ""
    anima.memory.read_cron_config.return_value = ""
    mgr = SchedulerManager(
        anima=anima,
        anima_name="test",
        anima_dir=tmp_path,
        emit_event=MagicMock(),
    )
    return mgr


class TestRecordScheduleMtimes:
    def test_records_existing_files(self, scheduler_mgr: SchedulerManager, tmp_path: Path) -> None:
        (tmp_path / "cron.md").write_text("# test")
        (tmp_path / "heartbeat.md").write_text("# test")
        scheduler_mgr._record_schedule_mtimes()
        assert scheduler_mgr._cron_md_mtime > 0
        assert scheduler_mgr._heartbeat_md_mtime > 0

    def test_records_zero_for_missing_files(self, scheduler_mgr: SchedulerManager) -> None:
        scheduler_mgr._record_schedule_mtimes()
        assert scheduler_mgr._cron_md_mtime == 0.0
        assert scheduler_mgr._heartbeat_md_mtime == 0.0


class TestCheckScheduleFreshness:
    def test_no_change_returns_false(self, scheduler_mgr: SchedulerManager, tmp_path: Path) -> None:
        (tmp_path / "cron.md").write_text("# v1")
        scheduler_mgr._record_schedule_mtimes()
        assert scheduler_mgr._check_schedule_freshness() is False

    def test_cron_change_triggers_reload(self, scheduler_mgr: SchedulerManager, tmp_path: Path) -> None:
        (tmp_path / "cron.md").write_text("# v1")
        scheduler_mgr._record_schedule_mtimes()

        # Simulate file modification (ensure mtime changes)
        time.sleep(0.05)
        (tmp_path / "cron.md").write_text("# v2")

        with patch.object(scheduler_mgr, "reload_schedule") as mock_reload:
            result = scheduler_mgr._check_schedule_freshness()
        assert result is True
        mock_reload.assert_called_once_with("test")

    def test_heartbeat_change_reloads_without_marking_cron_stale(self, scheduler_mgr: SchedulerManager, tmp_path: Path) -> None:
        (tmp_path / "heartbeat.md").write_text("# v1")
        scheduler_mgr._record_schedule_mtimes()

        time.sleep(0.05)
        (tmp_path / "heartbeat.md").write_text("# v2")

        with patch.object(scheduler_mgr, "reload_schedule") as mock_reload:
            result = scheduler_mgr._check_schedule_freshness()
        assert result is False
        mock_reload.assert_called_once()

    def test_deleted_cron_triggers_reload(self, scheduler_mgr: SchedulerManager, tmp_path: Path) -> None:
        (tmp_path / "cron.md").write_text("# v1")
        scheduler_mgr._record_schedule_mtimes()

        (tmp_path / "cron.md").unlink()

        with patch.object(scheduler_mgr, "reload_schedule") as mock_reload:
            result = scheduler_mgr._check_schedule_freshness()
        assert result is True
        mock_reload.assert_called_once()

    def test_missing_files_initially_no_reload(self, scheduler_mgr: SchedulerManager) -> None:
        """When files never existed, no reload needed."""
        scheduler_mgr._record_schedule_mtimes()
        assert scheduler_mgr._check_schedule_freshness() is False


@pytest.mark.parametrize("lifecycle", [False, True])
@pytest.mark.parametrize("changed", ["heartbeat", "cron", "both", "none"])
@pytest.mark.asyncio
async def test_due_cron_survives_only_unrelated_heartbeat_edits(tmp_path: Path, lifecycle: bool, changed: str) -> None:
    for name in ("cron", "heartbeat"):
        (tmp_path / f"{name}.md").write_text("synthetic")
    task = CronTask(name="synthetic", schedule="0 9 * * *", description="no real execution")
    if lifecycle:
        manager = SchedulerMixin()
        anima = MagicMock()
        anima.memory.anima_dir = tmp_path
        manager.animas = {"test": anima}
        manager._schedule_mtimes = {}
        manager._record_schedule_mtimes("test", tmp_path)
        manager.reload_anima_schedule = MagicMock()
        run = AsyncMock()
        manager._run_cron_and_broadcast = run
    else:
        manager = SchedulerManager(anima=MagicMock(), anima_name="test", anima_dir=tmp_path, emit_event=MagicMock())
        manager._record_schedule_mtimes()
        manager._awaiting_initial_setup = MagicMock(return_value=False)
        manager._log_cron_event = MagicMock()
        manager.reload_schedule = MagicMock()
        run = AsyncMock()
        manager._run_cron_task = run
    for name in ("cron", "heartbeat"):
        if changed in (name, "both"):
            path = tmp_path / f"{name}.md"
            stamp = path.stat().st_mtime + 10
            os.utime(path, (stamp, stamp))
    if lifecycle:
        await manager._cron_wrapper("test", task)
        reload_mock = manager.reload_anima_schedule
    else:
        await manager.cron_tick(task)
        reload_mock = manager.reload_schedule
    await asyncio.sleep(0)
    if changed in ("cron", "both"):
        run.assert_not_called()
    else:
        run.assert_awaited_once()
    assert reload_mock.call_count == (changed != "none")
