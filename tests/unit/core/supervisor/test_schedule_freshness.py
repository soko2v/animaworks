"""Tests for stale schedule detection via mtime reconciliation.

Covers the symmetric fix applied to
``core/supervisor/scheduler_manager.py::_check_schedule_freshness``
and
``core/lifecycle/scheduler.py::_check_schedule_freshness``
for the reverse-variant regression documented in
``sofia/knowledge/yutaka-oneshot-cron-misfire-rca-20260721.md``.

Key invariants under test:

1. A change to ``heartbeat.md`` alone still triggers a schedule reload but
   does NOT flag the currently firing cron as stale (so due one-shots are
   not silently dropped when a heartbeat edit happens hours earlier).
2. A change to ``cron.md`` still flags the currently firing cron as stale
   (the original protection against firing a removed/edited task).
3. ``_heartbeat_check`` polls freshness every minute (closes the forward-
   variant blind window for long-interval Animas).
4. The supervisor and lifecycle paths are behaviourally symmetric.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.supervisor.scheduler_manager import SchedulerManager


# ── Supervisor-side fixtures ─────────────────────────────────────────────


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


# ── Supervisor: mtime snapshotting ───────────────────────────────────────


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


# ── Supervisor: freshness predicate semantics ────────────────────────────


class TestCheckScheduleFreshness:
    def test_no_change_returns_false(self, scheduler_mgr: SchedulerManager, tmp_path: Path) -> None:
        (tmp_path / "cron.md").write_text("# v1")
        scheduler_mgr._record_schedule_mtimes()
        assert scheduler_mgr._check_schedule_freshness() is False

    def test_cron_change_reloads_and_marks_stale(
        self, scheduler_mgr: SchedulerManager, tmp_path: Path
    ) -> None:
        """cron.md change -> reload + True (skip current cron tick)."""
        (tmp_path / "cron.md").write_text("# v1")
        scheduler_mgr._record_schedule_mtimes()

        # Simulate file modification (ensure mtime changes)
        time.sleep(0.05)
        (tmp_path / "cron.md").write_text("# v2")

        with patch.object(scheduler_mgr, "reload_schedule") as mock_reload:
            result = scheduler_mgr._check_schedule_freshness()
        assert result is True, "cron.md change must mark the current tick as stale"
        mock_reload.assert_called_once_with("test")

    def test_heartbeat_only_change_reloads_but_returns_false(
        self, scheduler_mgr: SchedulerManager, tmp_path: Path
    ) -> None:
        """heartbeat.md-only change -> reload happens but current cron is NOT stale.

        This is the regression fix for the reverse variant: a heartbeat.md
        edit hours earlier must not cause the next due cron (potentially a
        one-shot) to be silently dropped.
        """
        (tmp_path / "cron.md").write_text("# cron v1")
        (tmp_path / "heartbeat.md").write_text("# hb v1")
        scheduler_mgr._record_schedule_mtimes()

        time.sleep(0.05)
        (tmp_path / "heartbeat.md").write_text("# hb v2")

        with patch.object(scheduler_mgr, "reload_schedule") as mock_reload:
            result = scheduler_mgr._check_schedule_freshness()
        assert result is False, "heartbeat-only change must NOT skip due cron tasks"
        mock_reload.assert_called_once_with("test"), "reload must still occur to pick up hb change"

    def test_both_changes_marks_stale(
        self, scheduler_mgr: SchedulerManager, tmp_path: Path
    ) -> None:
        """When both files change, cron.md change dominates and marks stale."""
        (tmp_path / "cron.md").write_text("# cron v1")
        (tmp_path / "heartbeat.md").write_text("# hb v1")
        scheduler_mgr._record_schedule_mtimes()

        time.sleep(0.05)
        (tmp_path / "cron.md").write_text("# cron v2")
        (tmp_path / "heartbeat.md").write_text("# hb v2")

        with patch.object(scheduler_mgr, "reload_schedule") as mock_reload:
            result = scheduler_mgr._check_schedule_freshness()
        assert result is True
        mock_reload.assert_called_once()

    def test_deleted_cron_marks_stale(
        self, scheduler_mgr: SchedulerManager, tmp_path: Path
    ) -> None:
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


# ── Supervisor: forward-variant fix (C-plan) ─────────────────────────────


class TestHeartbeatCheckPollsFreshness:
    """The minute-poll _heartbeat_check must call freshness check every tick.

    This closes the blind window for long-interval Animas (e.g. yutaka's
    1440-minute HB), where without polling a fresh cron.md/heartbeat.md
    edit would not be detected until the next scheduled heartbeat/cron tick.
    """

    def test_heartbeat_check_calls_freshness_every_minute(
        self, scheduler_mgr: SchedulerManager, tmp_path: Path
    ) -> None:
        (tmp_path / "cron.md").write_text("# v1")
        (tmp_path / "heartbeat.md").write_text("# v1")
        scheduler_mgr._record_schedule_mtimes()

        # Force _in_active_hours to False so the check exits early after
        # freshness — we only care that freshness was consulted.
        with patch.object(scheduler_mgr, "_in_active_hours", return_value=False), patch.object(
            scheduler_mgr, "_check_schedule_freshness"
        ) as mock_freshness, patch.object(scheduler_mgr, "heartbeat_tick") as mock_tick:
            asyncio.run(scheduler_mgr._heartbeat_check())

        mock_freshness.assert_called_once_with()
        mock_tick.assert_not_called()  # Sanity: outside active hours, no HB fire

    def test_heartbeat_check_freshness_runs_before_active_hours_gate(
        self, scheduler_mgr: SchedulerManager, tmp_path: Path
    ) -> None:
        """Freshness must run BEFORE the active-hours early return so long-
        interval Animas outside their window still detect schedule edits."""
        (tmp_path / "cron.md").write_text("# v1")
        scheduler_mgr._record_schedule_mtimes()

        call_order: list[str] = []

        def rec_freshness() -> bool:
            call_order.append("freshness")
            return False

        def rec_active(_now: object) -> bool:
            call_order.append("active_hours")
            return False

        with patch.object(scheduler_mgr, "_check_schedule_freshness", side_effect=rec_freshness), patch.object(
            scheduler_mgr, "_in_active_hours", side_effect=rec_active
        ):
            asyncio.run(scheduler_mgr._heartbeat_check())

        assert call_order == ["freshness", "active_hours"]


# ── Supervisor: scenario regression ──────────────────────────────────────


class TestScheduleFreshnessRegression:
    """P0 regression: heartbeat edit must not silently drop a due one-shot cron.

    Simulates the yutaka 2026-07-27 10:10 scenario where a heartbeat.md
    edit earlier the same day caused the next cron_tick to skip a due
    one-shot task.
    """

    def test_heartbeat_edit_then_due_one_shot_fires(
        self, scheduler_mgr: SchedulerManager, tmp_path: Path
    ) -> None:
        # Initial snapshot with both files present.
        (tmp_path / "cron.md").write_text("# cron v1 (contains the due one-shot)")
        (tmp_path / "heartbeat.md").write_text("# hb v1")
        scheduler_mgr._record_schedule_mtimes()

        # Someone edits heartbeat.md (e.g. daily HB overwrite) hours before
        # the one-shot cron is scheduled to fire.
        time.sleep(0.05)
        (tmp_path / "heartbeat.md").write_text("# hb v2 — edited mid-day")

        # Now the due one-shot fires: cron_tick calls _check_schedule_freshness.
        # Under the fix, freshness reloads (hb changed) but returns False, so
        # the task is NOT marked stale and would run.
        with patch.object(scheduler_mgr, "reload_schedule") as mock_reload:
            is_stale = scheduler_mgr._check_schedule_freshness()

        assert is_stale is False, (
            "REGRESSION: heartbeat-only edit caused due cron to be flagged as stale. "
            "This is exactly the yutaka 07-27 10:10 one-shot loss scenario."
        )
        mock_reload.assert_called_once_with("test")

    def test_cron_md_edit_causes_stale_skip(
        self, scheduler_mgr: SchedulerManager, tmp_path: Path
    ) -> None:
        """Complement: cron.md edits still cause stale skip (removed/edited task safety)."""
        (tmp_path / "cron.md").write_text("# cron v1")
        scheduler_mgr._record_schedule_mtimes()

        time.sleep(0.05)
        (tmp_path / "cron.md").write_text("# cron v2 — task removed")

        with patch.object(scheduler_mgr, "reload_schedule"):
            assert scheduler_mgr._check_schedule_freshness() is True


# ── Lifecycle-side symmetry ──────────────────────────────────────────────


class _StubLifecycleScheduler:
    """Minimal harness for the lifecycle SchedulerMixin freshness predicate.

    We don't need a full ``LifecycleManager`` — the predicate only touches
    ``self.animas``, ``self._schedule_mtimes`` and ``self.reload_anima_schedule``.
    """

    def __init__(self, name: str, anima_dir: Path) -> None:
        anima = MagicMock()
        anima.memory.anima_dir = anima_dir
        self.animas = {name: anima}
        self._schedule_mtimes: dict[str, tuple[float, float]] = {}
        self.reload_calls: list[str] = []

    def reload_anima_schedule(self, name: str) -> None:
        self.reload_calls.append(name)

    def snapshot(self, name: str) -> None:
        anima_dir: Path = self.animas[name].memory.anima_dir
        cron_path = anima_dir / "cron.md"
        hb_path = anima_dir / "heartbeat.md"
        cron_mt = cron_path.stat().st_mtime if cron_path.is_file() else 0.0
        hb_mt = hb_path.stat().st_mtime if hb_path.is_file() else 0.0
        self._schedule_mtimes[name] = (cron_mt, hb_mt)


def _bind_freshness_to_stub(stub: _StubLifecycleScheduler):
    """Bind the real lifecycle _check_schedule_freshness to our stub."""
    from core.lifecycle.scheduler import SchedulerMixin

    return SchedulerMixin._check_schedule_freshness.__get__(stub, _StubLifecycleScheduler)


class TestLifecycleSchedulerFreshnessSymmetry:
    """The lifecycle path must behave symmetrically with the supervisor path.

    Both share the same invariant: heartbeat.md-only edits reload the
    schedule but must NOT flag the current cron tick as stale.
    """

    def test_heartbeat_only_change_reloads_but_returns_false(self, tmp_path: Path) -> None:
        (tmp_path / "cron.md").write_text("# cron v1")
        (tmp_path / "heartbeat.md").write_text("# hb v1")
        stub = _StubLifecycleScheduler("test", tmp_path)
        stub.snapshot("test")
        check = _bind_freshness_to_stub(stub)

        time.sleep(0.05)
        (tmp_path / "heartbeat.md").write_text("# hb v2")

        assert check("test") is False
        assert stub.reload_calls == ["test"]

    def test_cron_change_reloads_and_marks_stale(self, tmp_path: Path) -> None:
        (tmp_path / "cron.md").write_text("# cron v1")
        (tmp_path / "heartbeat.md").write_text("# hb v1")
        stub = _StubLifecycleScheduler("test", tmp_path)
        stub.snapshot("test")
        check = _bind_freshness_to_stub(stub)

        time.sleep(0.05)
        (tmp_path / "cron.md").write_text("# cron v2")

        assert check("test") is True
        assert stub.reload_calls == ["test"]

    def test_both_changes_marks_stale(self, tmp_path: Path) -> None:
        (tmp_path / "cron.md").write_text("# cron v1")
        (tmp_path / "heartbeat.md").write_text("# hb v1")
        stub = _StubLifecycleScheduler("test", tmp_path)
        stub.snapshot("test")
        check = _bind_freshness_to_stub(stub)

        time.sleep(0.05)
        (tmp_path / "cron.md").write_text("# cron v2")
        (tmp_path / "heartbeat.md").write_text("# hb v2")

        assert check("test") is True
        assert stub.reload_calls == ["test"]

    def test_no_change_returns_false(self, tmp_path: Path) -> None:
        (tmp_path / "cron.md").write_text("# cron v1")
        (tmp_path / "heartbeat.md").write_text("# hb v1")
        stub = _StubLifecycleScheduler("test", tmp_path)
        stub.snapshot("test")
        check = _bind_freshness_to_stub(stub)

        assert check("test") is False
        assert stub.reload_calls == []

    def test_unknown_anima_returns_false(self, tmp_path: Path) -> None:
        stub = _StubLifecycleScheduler("test", tmp_path)
        check = _bind_freshness_to_stub(stub)
        assert check("does-not-exist") is False

    def test_heartbeat_edit_then_due_one_shot_fires_lifecycle(self, tmp_path: Path) -> None:
        """Symmetric regression: yutaka 07-27 10:10 scenario, lifecycle path."""
        (tmp_path / "cron.md").write_text("# cron v1")
        (tmp_path / "heartbeat.md").write_text("# hb v1")
        stub = _StubLifecycleScheduler("test", tmp_path)
        stub.snapshot("test")
        check = _bind_freshness_to_stub(stub)

        time.sleep(0.05)
        (tmp_path / "heartbeat.md").write_text("# hb v2 — mid-day edit")

        # Due one-shot fires — must NOT be flagged stale.
        assert check("test") is False, (
            "REGRESSION (lifecycle): heartbeat-only edit skipped a due cron"
        )
        assert stub.reload_calls == ["test"]
