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
        (tmp_path / f"{name}.md").write_text(
            "## synthetic\nschedule: 0 9 * * *\nno real execution\n"
        )
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
            path.write_text("## changed\nschedule: 0 9 * * *\n")
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


@pytest.mark.parametrize("lifecycle", [False, True])
@pytest.mark.parametrize("pre_reloaded", [False, True])
@pytest.mark.parametrize("edit", ["other", "reorder", "comment", "same_name", "deleted", "duplicate", "invalid_utf8", "missing"])
@pytest.mark.asyncio
async def test_due_callback_checks_own_definition_after_reload(
    tmp_path: Path, lifecycle: bool, pre_reloaded: bool, edit: str,
) -> None:
    from core.schedule_parser import parse_cron_md

    original = "## due\nschedule: 0 9 * * *\ntype: command\ncommand: echo synthetic\n"
    other = "## other\nschedule: 0 10 * * *\nnot executed\n"
    path = tmp_path / "cron.md"
    path.write_text(original + other)
    task = parse_cron_md(original)[0]
    anima = MagicMock()
    anima.memory.anima_dir = tmp_path
    run = AsyncMock()
    if lifecycle:
        manager = SchedulerMixin()
        manager.animas = {"test": anima}
        manager._schedule_mtimes = {}
        manager._record_schedule_mtimes("test", tmp_path)
        manager.reload_anima_schedule = MagicMock()
        manager._run_cron_and_broadcast = run
    else:
        manager = SchedulerManager(anima=anima, anima_name="test", anima_dir=tmp_path, emit_event=MagicMock())
        manager._awaiting_initial_setup = MagicMock(return_value=False)
        manager._log_cron_event = MagicMock()
        manager.reload_schedule = MagicMock()
        manager._run_cron_task = run
        manager._record_schedule_mtimes()
    if edit == "other":
        path.write_text(original + other.replace("not executed", "edited other job"))
    elif edit == "reorder":
        path.write_text(other + original)
    elif edit == "comment":
        path.write_text("<!-- unrelated comment -->\n" + original + other)
    elif edit == "same_name":
        path.write_text(original.replace("echo synthetic", "echo changed") + other)
    elif edit == "deleted":
        path.write_text(other)
    elif edit == "duplicate":
        path.write_text(original + original)
    elif edit == "invalid_utf8":
        path.write_bytes(b"\xff")
    elif edit == "missing":
        path.unlink()
    # Simulate another callback/heartbeat already having refreshed mtimes.
    # An obsolete due callback must still be rejected, even with fresh mtimes.
    if lifecycle:
        if pre_reloaded:
            manager._record_schedule_mtimes("test", tmp_path)
        await manager._cron_wrapper("test", task)
    else:
        if pre_reloaded:
            manager._record_schedule_mtimes()
        await manager.cron_tick(task)
    await asyncio.sleep(0)
    if edit in ("other", "reorder", "comment"):
        run.assert_awaited_once()
    else:
        run.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("schedule", "0 8 * * *"), ("type", "command"), ("description", "new"),
    ("command", "echo new"), ("tool", "new"), ("args", {"key": "new"}),
    ("skills", ["new"]), ("skip_pattern", "new"), ("trigger_heartbeat", False),
])
def test_current_definition_compares_all_execution_fields(tmp_path: Path, field: str, value: object) -> None:
    from core.schedule_parser import cron_task_is_current, parse_cron_md

    path = tmp_path / "cron.md"
    path.write_text("## due\nschedule: 0 9 * * *\noriginal\n")
    task = parse_cron_md(path.read_text())[0]
    assert cron_task_is_current(path, task)
    assert not cron_task_is_current(path, task.model_copy(update={field: value}))
