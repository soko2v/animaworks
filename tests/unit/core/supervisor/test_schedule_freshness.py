"""Tests for stale schedule detection via mtime reconciliation + identity check.

Covers the symmetric fix applied to
``core/supervisor/scheduler_manager.py::_check_schedule_freshness``
and
``core/lifecycle/scheduler.py::_check_schedule_freshness``.

Two-stage evolution:

1. First fix (commit ``412521d5``, sofia 2026-07-29):
   heartbeat.md-only edits no longer skip due cron tasks.  See
   ``sofia/knowledge/yutaka-oneshot-cron-misfire-rca-20260721.md`` for the
   reverse-variant regression.
2. Second fix (this suite, alex 2026-07-29 22:44 review):
   cron.md edits that leave the currently firing job's
   full definition unchanged must also NOT skip. Staleness is claimed
   when the fired job is removed or any execution field is mutated.

Key invariants under test (all four call-paths must honour them):

  (1) cron.md change + identical task definition still present    → False (run)
  (2) job removed OR any task field mutated         → True  (skip)
  (3) heartbeat.md-only change                                          → False (run)
  (4) ``fired_job=None`` (heartbeat call-site, no job context)          → False
  (5) ``_heartbeat_check`` polls freshness every minute (forward-variant fix)
  (6) supervisor and lifecycle paths are behaviourally symmetric.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.schemas import CronTask
from core.supervisor.scheduler_manager import SchedulerManager


# ── Cron.md fixture helpers ──────────────────────────────────────────────


def _cron_md(*jobs: tuple[str, str, str]) -> str:
    """Build a cron.md document from (name, schedule, type) tuples.

    Each tuple emits a ``## name\\nschedule: ...\\ntype: ...`` section that
    ``core.schedule_parser.parse_cron_md`` accepts. Description matches
    ``_job`` so unchanged tasks compare equal across all fields.
    """
    parts: list[str] = []
    for name, schedule, task_type in jobs:
        parts.append(
            f"## {name}\n"
            f"schedule: {schedule}\n"
            f"type: {task_type}\n"
            "Description stub.\n"
        )
    return "\n".join(parts)


def _job(name: str, schedule: str, task_type: str) -> CronTask:
    return CronTask(name=name, schedule=schedule, type=task_type, description="Description stub.")


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
        (tmp_path / "cron.md").write_text(_cron_md(("alpha", "0 9 * * *", "llm")))
        scheduler_mgr._record_schedule_mtimes()
        assert scheduler_mgr._check_schedule_freshness() is False

    def test_heartbeat_only_change_reloads_but_returns_false(
        self, scheduler_mgr: SchedulerManager, tmp_path: Path
    ) -> None:
        """heartbeat.md-only change -> reload happens but current cron is NOT stale.

        Regression fix (a): a heartbeat.md edit hours earlier must not cause
        the next due cron (potentially a one-shot) to be silently dropped.
        """
        (tmp_path / "cron.md").write_text(_cron_md(("alpha", "0 9 * * *", "llm")))
        (tmp_path / "heartbeat.md").write_text("# hb v1")
        scheduler_mgr._record_schedule_mtimes()

        time.sleep(0.05)
        (tmp_path / "heartbeat.md").write_text("# hb v2")

        with patch.object(scheduler_mgr, "reload_schedule") as mock_reload:
            result = scheduler_mgr._check_schedule_freshness(
                _job("alpha", "0 9 * * *", "llm")
            )
        assert result is False, "heartbeat-only change must NOT skip due cron tasks"
        mock_reload.assert_called_once_with("test"), "reload must still occur to pick up hb change"

    def test_cron_change_same_definition_returns_false(
        self, scheduler_mgr: SchedulerManager, tmp_path: Path
    ) -> None:
        """cron.md edited but fired job (name, schedule, type) unchanged -> RUN.

        Regression fix (b, alex 07-29 22:44 review): touching cron.md (e.g.
        adding an unrelated task or editing a comment) must not skip a due
        job whose identity is preserved.
        """
        (tmp_path / "cron.md").write_text(_cron_md(("alpha", "0 9 * * *", "llm")))
        scheduler_mgr._record_schedule_mtimes()
        # Wire mock to return the *new* cron.md content when reload_schedule fires.
        scheduler_mgr._anima.memory.read_cron_config.return_value = _cron_md(
            ("alpha", "0 9 * * *", "llm"),
            ("beta", "*/15 * * * *", "command"),
        )

        time.sleep(0.05)
        (tmp_path / "cron.md").write_text(_cron_md(
            ("alpha", "0 9 * * *", "llm"),
            ("beta", "*/15 * * * *", "command"),
        ))

        with patch.object(scheduler_mgr, "reload_schedule") as mock_reload:
            result = scheduler_mgr._check_schedule_freshness(
                _job("alpha", "0 9 * * *", "llm")
            )
        assert result is False, (
            "cron.md edit that preserves the fired job's identity must NOT skip"
        )
        mock_reload.assert_called_once_with("test")

    def test_cron_change_fired_job_removed_marks_stale(
        self, scheduler_mgr: SchedulerManager, tmp_path: Path
    ) -> None:
        """Fired job removed from cron.md -> SKIP (safety)."""
        (tmp_path / "cron.md").write_text(_cron_md(("alpha", "0 9 * * *", "llm")))
        scheduler_mgr._record_schedule_mtimes()
        scheduler_mgr._anima.memory.read_cron_config.return_value = _cron_md(
            ("beta", "*/15 * * * *", "llm"),
        )

        time.sleep(0.05)
        (tmp_path / "cron.md").write_text(_cron_md(("beta", "*/15 * * * *", "llm")))

        with patch.object(scheduler_mgr, "reload_schedule"):
            result = scheduler_mgr._check_schedule_freshness(
                _job("alpha", "0 9 * * *", "llm")
            )
        assert result is True

    def test_cron_change_schedule_mutated_marks_stale(
        self, scheduler_mgr: SchedulerManager, tmp_path: Path
    ) -> None:
        """Fired job's schedule mutated -> SKIP."""
        (tmp_path / "cron.md").write_text(_cron_md(("alpha", "0 9 * * *", "llm")))
        scheduler_mgr._record_schedule_mtimes()
        scheduler_mgr._anima.memory.read_cron_config.return_value = _cron_md(
            ("alpha", "0 10 * * *", "llm"),
        )

        time.sleep(0.05)
        (tmp_path / "cron.md").write_text(_cron_md(("alpha", "0 10 * * *", "llm")))

        with patch.object(scheduler_mgr, "reload_schedule"):
            result = scheduler_mgr._check_schedule_freshness(
                _job("alpha", "0 9 * * *", "llm")
            )
        assert result is True

    def test_cron_change_type_mutated_marks_stale(
        self, scheduler_mgr: SchedulerManager, tmp_path: Path
    ) -> None:
        """Fired job's type mutated (llm <-> command) -> SKIP."""
        (tmp_path / "cron.md").write_text(_cron_md(("alpha", "0 9 * * *", "llm")))
        scheduler_mgr._record_schedule_mtimes()
        scheduler_mgr._anima.memory.read_cron_config.return_value = _cron_md(
            ("alpha", "0 9 * * *", "command"),
        )

        time.sleep(0.05)
        (tmp_path / "cron.md").write_text(_cron_md(("alpha", "0 9 * * *", "command")))

        with patch.object(scheduler_mgr, "reload_schedule"):
            result = scheduler_mgr._check_schedule_freshness(
                _job("alpha", "0 9 * * *", "llm")
            )
        assert result is True

    def test_cron_change_name_mutated_marks_stale(
        self, scheduler_mgr: SchedulerManager, tmp_path: Path
    ) -> None:
        """Fired job's name changed (rename) -> SKIP.  Rename is 'delete + add'."""
        (tmp_path / "cron.md").write_text(_cron_md(("alpha", "0 9 * * *", "llm")))
        scheduler_mgr._record_schedule_mtimes()
        scheduler_mgr._anima.memory.read_cron_config.return_value = _cron_md(
            ("alpha-renamed", "0 9 * * *", "llm"),
        )

        time.sleep(0.05)
        (tmp_path / "cron.md").write_text(_cron_md(("alpha-renamed", "0 9 * * *", "llm")))

        with patch.object(scheduler_mgr, "reload_schedule"):
            result = scheduler_mgr._check_schedule_freshness(
                _job("alpha", "0 9 * * *", "llm")
            )
        assert result is True

    def test_cron_change_without_fired_job_returns_false(
        self, scheduler_mgr: SchedulerManager, tmp_path: Path
    ) -> None:
        """cron.md changed but no fired_job context (heartbeat call path) -> reload only, no skip signal."""
        (tmp_path / "cron.md").write_text(_cron_md(("alpha", "0 9 * * *", "llm")))
        scheduler_mgr._record_schedule_mtimes()

        time.sleep(0.05)
        (tmp_path / "cron.md").write_text(_cron_md(("beta", "*/15 * * * *", "llm")))

        with patch.object(scheduler_mgr, "reload_schedule") as mock_reload:
            result = scheduler_mgr._check_schedule_freshness()
        assert result is False, (
            "heartbeat call-path (fired_job=None) must never return stale=True"
        )
        mock_reload.assert_called_once_with("test")

    def test_deleted_cron_marks_stale(
        self, scheduler_mgr: SchedulerManager, tmp_path: Path
    ) -> None:
        """cron.md file removed entirely -> fired job cannot be found -> SKIP."""
        (tmp_path / "cron.md").write_text(_cron_md(("alpha", "0 9 * * *", "llm")))
        scheduler_mgr._record_schedule_mtimes()
        scheduler_mgr._anima.memory.read_cron_config.return_value = ""

        (tmp_path / "cron.md").unlink()

        with patch.object(scheduler_mgr, "reload_schedule"):
            result = scheduler_mgr._check_schedule_freshness(
                _job("alpha", "0 9 * * *", "llm")
            )
        assert result is True

    def test_both_changes_with_same_definition_returns_false(
        self, scheduler_mgr: SchedulerManager, tmp_path: Path
    ) -> None:
        """When both files change but fired job identity is preserved -> RUN.

        Composite of invariants (1) + (3): cron.md and heartbeat.md both
        touched, but the fired job's (name, schedule, type) is untouched.
        """
        (tmp_path / "cron.md").write_text(_cron_md(("alpha", "0 9 * * *", "llm")))
        (tmp_path / "heartbeat.md").write_text("# hb v1")
        scheduler_mgr._record_schedule_mtimes()
        scheduler_mgr._anima.memory.read_cron_config.return_value = _cron_md(
            ("alpha", "0 9 * * *", "llm"),
            ("gamma", "0 12 * * *", "command"),
        )

        time.sleep(0.05)
        (tmp_path / "cron.md").write_text(_cron_md(
            ("alpha", "0 9 * * *", "llm"),
            ("gamma", "0 12 * * *", "command"),
        ))
        (tmp_path / "heartbeat.md").write_text("# hb v2")

        with patch.object(scheduler_mgr, "reload_schedule"):
            result = scheduler_mgr._check_schedule_freshness(
                _job("alpha", "0 9 * * *", "llm")
            )
        assert result is False

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
        # freshness — we only care that freshness was consulted with no
        # job context (heartbeat call-site).
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

        def rec_freshness(*_args: object, **_kwargs: object) -> bool:
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
    """P0 regression: schedule edits must not silently drop a due one-shot cron.

    Simulates two scenarios:
      (a) yutaka 2026-07-27 10:10: a heartbeat.md edit hours earlier caused
          the next cron_tick to skip a due one-shot task.
      (b) alex 2026-07-29 22:44 review: a cron.md edit that leaves the fired
          job's identity intact must not skip either.
    """

    def test_heartbeat_edit_then_due_one_shot_fires(
        self, scheduler_mgr: SchedulerManager, tmp_path: Path
    ) -> None:
        (tmp_path / "cron.md").write_text(_cron_md(("one_shot", "10 10 27 7 *", "llm")))
        (tmp_path / "heartbeat.md").write_text("# hb v1")
        scheduler_mgr._record_schedule_mtimes()

        time.sleep(0.05)
        (tmp_path / "heartbeat.md").write_text("# hb v2 — edited mid-day")

        with patch.object(scheduler_mgr, "reload_schedule") as mock_reload:
            is_stale = scheduler_mgr._check_schedule_freshness(
                _job("one_shot", "10 10 27 7 *", "llm")
            )

        assert is_stale is False, (
            "REGRESSION: heartbeat-only edit caused due cron to be flagged as stale. "
            "This is exactly the yutaka 07-27 10:10 one-shot loss scenario."
        )
        mock_reload.assert_called_once_with("test")

    def test_cron_edit_comment_only_does_not_skip_due_job(
        self, scheduler_mgr: SchedulerManager, tmp_path: Path
    ) -> None:
        """alex 07-29 22:44: cron.md touched with unrelated changes must not skip.

        Simulates a maintainer adding an unrelated task or comment to
        cron.md while a due one-shot is about to fire.  The fired job's
        identity is preserved, so it MUST run.
        """
        (tmp_path / "cron.md").write_text(_cron_md(("critical", "30 9 * * *", "llm")))
        scheduler_mgr._record_schedule_mtimes()
        scheduler_mgr._anima.memory.read_cron_config.return_value = _cron_md(
            ("critical", "30 9 * * *", "llm"),
            ("noise", "0 3 * * *", "command"),
        )

        time.sleep(0.05)
        (tmp_path / "cron.md").write_text(_cron_md(
            ("critical", "30 9 * * *", "llm"),
            ("noise", "0 3 * * *", "command"),
        ))

        with patch.object(scheduler_mgr, "reload_schedule"):
            is_stale = scheduler_mgr._check_schedule_freshness(
                _job("critical", "30 9 * * *", "llm")
            )

        assert is_stale is False, (
            "REGRESSION: unrelated cron.md edit skipped a due job whose "
            "(name, schedule, type) identity was preserved."
        )

    def test_cron_edit_that_removes_fired_job_skips(
        self, scheduler_mgr: SchedulerManager, tmp_path: Path
    ) -> None:
        """Complement: if the fired job is *actually* removed from cron.md, SKIP."""
        (tmp_path / "cron.md").write_text(_cron_md(("critical", "30 9 * * *", "llm")))
        scheduler_mgr._record_schedule_mtimes()
        scheduler_mgr._anima.memory.read_cron_config.return_value = _cron_md(
            ("noise", "0 3 * * *", "command"),
        )

        time.sleep(0.05)
        (tmp_path / "cron.md").write_text(_cron_md(("noise", "0 3 * * *", "command")))

        with patch.object(scheduler_mgr, "reload_schedule"):
            is_stale = scheduler_mgr._check_schedule_freshness(
                _job("critical", "30 9 * * *", "llm")
            )
        assert is_stale is True


# ── Lifecycle-side symmetry ──────────────────────────────────────────────


class _StubLifecycleScheduler:
    """Minimal harness for the lifecycle SchedulerMixin freshness predicate.

    We don't need a full ``LifecycleManager`` — the predicate only touches
    ``self.animas``, ``self._schedule_mtimes`` and
    ``self.reload_anima_schedule``.
    """

    def __init__(self, name: str, anima_dir: Path) -> None:
        anima = MagicMock()
        anima.memory.anima_dir = anima_dir
        anima.memory.read_cron_config.return_value = ""
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

    All six invariants ((1)–(4), plus the composite scenarios) must hold on
    both call sites.
    """

    def test_heartbeat_only_change_reloads_but_returns_false(self, tmp_path: Path) -> None:
        (tmp_path / "cron.md").write_text(_cron_md(("alpha", "0 9 * * *", "llm")))
        (tmp_path / "heartbeat.md").write_text("# hb v1")
        stub = _StubLifecycleScheduler("test", tmp_path)
        stub.snapshot("test")
        check = _bind_freshness_to_stub(stub)

        time.sleep(0.05)
        (tmp_path / "heartbeat.md").write_text("# hb v2")

        assert check("test", _job("alpha", "0 9 * * *", "llm")) is False
        assert stub.reload_calls == ["test"]

    def test_cron_change_same_definition_returns_false(self, tmp_path: Path) -> None:
        (tmp_path / "cron.md").write_text(_cron_md(("alpha", "0 9 * * *", "llm")))
        stub = _StubLifecycleScheduler("test", tmp_path)
        stub.snapshot("test")
        stub.animas["test"].memory.read_cron_config.return_value = _cron_md(
            ("alpha", "0 9 * * *", "llm"),
            ("beta", "*/15 * * * *", "command"),
        )
        check = _bind_freshness_to_stub(stub)

        time.sleep(0.05)
        (tmp_path / "cron.md").write_text(_cron_md(
            ("alpha", "0 9 * * *", "llm"),
            ("beta", "*/15 * * * *", "command"),
        ))

        assert check("test", _job("alpha", "0 9 * * *", "llm")) is False
        assert stub.reload_calls == ["test"]

    def test_cron_change_fired_job_removed_marks_stale(self, tmp_path: Path) -> None:
        (tmp_path / "cron.md").write_text(_cron_md(("alpha", "0 9 * * *", "llm")))
        stub = _StubLifecycleScheduler("test", tmp_path)
        stub.snapshot("test")
        stub.animas["test"].memory.read_cron_config.return_value = _cron_md(
            ("beta", "*/15 * * * *", "llm"),
        )
        check = _bind_freshness_to_stub(stub)

        time.sleep(0.05)
        (tmp_path / "cron.md").write_text(_cron_md(("beta", "*/15 * * * *", "llm")))

        assert check("test", _job("alpha", "0 9 * * *", "llm")) is True
        assert stub.reload_calls == ["test"]

    def test_cron_change_schedule_mutated_marks_stale(self, tmp_path: Path) -> None:
        (tmp_path / "cron.md").write_text(_cron_md(("alpha", "0 9 * * *", "llm")))
        stub = _StubLifecycleScheduler("test", tmp_path)
        stub.snapshot("test")
        stub.animas["test"].memory.read_cron_config.return_value = _cron_md(
            ("alpha", "0 10 * * *", "llm"),
        )
        check = _bind_freshness_to_stub(stub)

        time.sleep(0.05)
        (tmp_path / "cron.md").write_text(_cron_md(("alpha", "0 10 * * *", "llm")))

        assert check("test", _job("alpha", "0 9 * * *", "llm")) is True

    def test_cron_change_type_mutated_marks_stale(self, tmp_path: Path) -> None:
        (tmp_path / "cron.md").write_text(_cron_md(("alpha", "0 9 * * *", "llm")))
        stub = _StubLifecycleScheduler("test", tmp_path)
        stub.snapshot("test")
        stub.animas["test"].memory.read_cron_config.return_value = _cron_md(
            ("alpha", "0 9 * * *", "command"),
        )
        check = _bind_freshness_to_stub(stub)

        time.sleep(0.05)
        (tmp_path / "cron.md").write_text(_cron_md(("alpha", "0 9 * * *", "command")))

        assert check("test", _job("alpha", "0 9 * * *", "llm")) is True

    def test_cron_change_name_mutated_marks_stale(self, tmp_path: Path) -> None:
        (tmp_path / "cron.md").write_text(_cron_md(("alpha", "0 9 * * *", "llm")))
        stub = _StubLifecycleScheduler("test", tmp_path)
        stub.snapshot("test")
        stub.animas["test"].memory.read_cron_config.return_value = _cron_md(
            ("alpha-renamed", "0 9 * * *", "llm"),
        )
        check = _bind_freshness_to_stub(stub)

        time.sleep(0.05)
        (tmp_path / "cron.md").write_text(_cron_md(("alpha-renamed", "0 9 * * *", "llm")))

        assert check("test", _job("alpha", "0 9 * * *", "llm")) is True

    def test_cron_change_without_fired_job_returns_false(self, tmp_path: Path) -> None:
        (tmp_path / "cron.md").write_text(_cron_md(("alpha", "0 9 * * *", "llm")))
        stub = _StubLifecycleScheduler("test", tmp_path)
        stub.snapshot("test")
        check = _bind_freshness_to_stub(stub)

        time.sleep(0.05)
        (tmp_path / "cron.md").write_text(_cron_md(("beta", "*/15 * * * *", "llm")))

        # No fired_job (heartbeat call-path).  Must reload but not signal stale.
        assert check("test") is False
        assert stub.reload_calls == ["test"]

    def test_both_changes_with_same_definition_returns_false(self, tmp_path: Path) -> None:
        (tmp_path / "cron.md").write_text(_cron_md(("alpha", "0 9 * * *", "llm")))
        (tmp_path / "heartbeat.md").write_text("# hb v1")
        stub = _StubLifecycleScheduler("test", tmp_path)
        stub.snapshot("test")
        stub.animas["test"].memory.read_cron_config.return_value = _cron_md(
            ("alpha", "0 9 * * *", "llm"),
            ("gamma", "0 12 * * *", "command"),
        )
        check = _bind_freshness_to_stub(stub)

        time.sleep(0.05)
        (tmp_path / "cron.md").write_text(_cron_md(
            ("alpha", "0 9 * * *", "llm"),
            ("gamma", "0 12 * * *", "command"),
        ))
        (tmp_path / "heartbeat.md").write_text("# hb v2")

        assert check("test", _job("alpha", "0 9 * * *", "llm")) is False

    def test_no_change_returns_false(self, tmp_path: Path) -> None:
        (tmp_path / "cron.md").write_text(_cron_md(("alpha", "0 9 * * *", "llm")))
        (tmp_path / "heartbeat.md").write_text("# hb v1")
        stub = _StubLifecycleScheduler("test", tmp_path)
        stub.snapshot("test")
        check = _bind_freshness_to_stub(stub)

        assert check("test", _job("alpha", "0 9 * * *", "llm")) is False
        assert stub.reload_calls == []

    def test_unknown_anima_returns_false(self, tmp_path: Path) -> None:
        stub = _StubLifecycleScheduler("test", tmp_path)
        check = _bind_freshness_to_stub(stub)
        assert check("does-not-exist") is False

    def test_heartbeat_edit_then_due_one_shot_fires_lifecycle(self, tmp_path: Path) -> None:
        """Symmetric regression: yutaka 07-27 10:10 scenario, lifecycle path."""
        (tmp_path / "cron.md").write_text(_cron_md(("one_shot", "10 10 27 7 *", "llm")))
        (tmp_path / "heartbeat.md").write_text("# hb v1")
        stub = _StubLifecycleScheduler("test", tmp_path)
        stub.snapshot("test")
        check = _bind_freshness_to_stub(stub)

        time.sleep(0.05)
        (tmp_path / "heartbeat.md").write_text("# hb v2 — mid-day edit")

        assert check("test", _job("one_shot", "10 10 27 7 *", "llm")) is False, (
            "REGRESSION (lifecycle): heartbeat-only edit skipped a due cron"
        )
        assert stub.reload_calls == ["test"]

    def test_cron_edit_comment_only_does_not_skip_due_job_lifecycle(self, tmp_path: Path) -> None:
        """Symmetric regression: alex 07-29 22:44, lifecycle path."""
        (tmp_path / "cron.md").write_text(_cron_md(("critical", "30 9 * * *", "llm")))
        stub = _StubLifecycleScheduler("test", tmp_path)
        stub.snapshot("test")
        stub.animas["test"].memory.read_cron_config.return_value = _cron_md(
            ("critical", "30 9 * * *", "llm"),
            ("noise", "0 3 * * *", "command"),
        )
        check = _bind_freshness_to_stub(stub)

        time.sleep(0.05)
        (tmp_path / "cron.md").write_text(_cron_md(
            ("critical", "30 9 * * *", "llm"),
            ("noise", "0 3 * * *", "command"),
        ))

        assert check("test", _job("critical", "30 9 * * *", "llm")) is False, (
            "REGRESSION (lifecycle): unrelated cron.md edit skipped a due job "
            "whose (name, schedule, type) identity was preserved."
        )


@pytest.mark.parametrize("field,value", [
    ("description", "New instructions"), ("command", "echo new"),
    ("tool", "new_tool"), ("args", {"target": "new"}),
    ("skills", ["new-skill"]), ("skip_pattern", "skip"),
    ("trigger_heartbeat", False),
])
@pytest.mark.parametrize("lifecycle", [False, True])
def test_execution_field_edit_skips_stale_task(scheduler_mgr, tmp_path, field, value, lifecycle):
    fired = _job("alpha", "0 9 * * *", "llm")
    changed = fired.model_copy(update={field: value})
    (tmp_path / "cron.md").write_text("changed")
    if lifecycle:
        stub = _StubLifecycleScheduler("test", tmp_path)
        stub._schedule_mtimes["test"] = (0.0, 0.0)
        stub.animas["test"].memory.read_cron_config.return_value = "changed"
        with patch("core.lifecycle.scheduler._parse_cron_md", return_value=[changed]):
            assert _bind_freshness_to_stub(stub)("test", fired) is True
        assert stub.reload_calls == ["test"]
    else:
        scheduler_mgr._anima.memory.read_cron_config.return_value = "changed"
        with (
            patch.object(scheduler_mgr, "reload_schedule") as reload,
            patch("core.supervisor.scheduler_manager.parse_cron_md", return_value=[changed]),
        ):
            assert scheduler_mgr._check_schedule_freshness(fired) is True
        reload.assert_called_once_with("test")
