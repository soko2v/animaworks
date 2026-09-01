from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.execution_liveness import LivenessResult, reconcile_execution_once
from core.goals import GoalManager
from core.memory.task_queue import TaskQueueManager

NOW = datetime(2026, 9, 2, 1, 45, tzinfo=UTC)


def _setup(tmp_path: Path, phase: dict) -> Path:
    anima_dir = tmp_path / "clio"
    (anima_dir / "state" / "pending" / "processing").mkdir(parents=True)
    (anima_dir / "state" / "execution_liveness.json").write_text(
        json.dumps({"phases": [phase]}), encoding="utf-8"
    )
    GoalManager(anima_dir).set_goal(
        goal_id=str(phase["goal_id"]),
        objective="Complete the DocVault pipeline",
        success_criteria=["all phases complete"],
    )
    return anima_dir


def _phase(**extra: object) -> dict:
    return {
        "goal_id": "docvault-goal",
        "task_id": "docvault-review",
        "title": "Review DocVault",
        "description": "Resume the saved DocVault review checkpoint",
        "approved_safe": True,
        "capabilities": ["local_code"],
        "checkpoint_path": "state/docvault.checkpoint",
        "progress_path": "state/docvault.count",
        **extra,
    }


def test_goal_plan_without_task_descriptor_or_runner_recovers_same_task(tmp_path: Path) -> None:
    anima_dir = _setup(tmp_path, _phase(resume_context="sheet=12,row=48"))
    result = reconcile_execution_once(anima_dir, now=NOW)
    assert result == LivenessResult("recovered", "docvault-review", "missing execution path restored")
    entry = TaskQueueManager(anima_dir).get_task_by_id("docvault-review")
    assert entry is not None
    assert entry.meta["resume_context"] == "sheet=12,row=48"
    descriptor = json.loads((anima_dir / "state" / "pending" / "docvault-review.json").read_text())
    assert descriptor["task_id"] == "docvault-review"
    assert "sheet=12,row=48" in descriptor["context"]
    assert reconcile_execution_once(anima_dir, now=NOW).status == "awaiting_progress"


def test_completed_phase_hands_off_once_and_requires_progress(tmp_path: Path) -> None:
    anima_dir = _setup(tmp_path, _phase(predecessor_task_id="extract", resume_context="batch=9"))
    queue = TaskQueueManager(anima_dir)
    queue.add_task(
        source="anima", original_instruction="extract", assignee="clio", summary="extract", task_id="extract"
    )
    queue.update_status("extract", "done")
    first = reconcile_execution_once(anima_dir, now=NOW)
    assert first.status == "recovered"
    assert first.task_id == "docvault-review"
    assert queue.get_task_by_id("extract") is not None
    assert queue.get_task_by_id("docvault-review") is not None
    second = reconcile_execution_once(anima_dir, now=NOW + timedelta(minutes=1))
    assert second.status == "awaiting_progress"
    (anima_dir / "state" / "pending" / "docvault-review.json").unlink()
    (anima_dir / "state" / "docvault.count").write_text("41", encoding="utf-8")
    third = reconcile_execution_once(anima_dir, now=NOW + timedelta(minutes=2))
    assert third.status == "progress_verified"
    assert queue.get_task_by_id("extract") is not None
    assert queue.get_task_by_id("docvault-review") is not None


def test_live_external_runner_wins_over_failed_tracking_task(tmp_path: Path) -> None:
    anima_dir = _setup(
        tmp_path,
        _phase(
            external_runner={"pid": 4242, "command_contains": "docvault-systemd"},
        ),
    )
    queue = TaskQueueManager(anima_dir)
    queue.add_task(
        source="anima",
        original_instruction="track",
        assignee="clio",
        summary="track",
        task_id="docvault-review",
    )
    queue.update_status("docvault-review", "failed")
    result = reconcile_execution_once(
        anima_dir,
        now=NOW,
        process_probe=lambda pid: "python docvault-systemd --resume" if pid == 4242 else None,
    )
    assert result.status == "external_runner_live"
    assert not (anima_dir / "state" / "pending" / "docvault-review.json").exists()
    assert queue.get_task_by_id("docvault-review").status == "failed"


def test_in_progress_without_execution_or_progress_recovers_only_once(tmp_path: Path) -> None:
    anima_dir = _setup(tmp_path, _phase())
    queue = TaskQueueManager(anima_dir)
    queue.add_task(
        source="anima",
        original_instruction="review",
        assignee="clio",
        summary="review",
        task_id="docvault-review",
        status="in_progress",
    )
    assert reconcile_execution_once(anima_dir, now=NOW).status == "recovered"
    (anima_dir / "state" / "pending" / "docvault-review.json").unlink()
    result = reconcile_execution_once(anima_dir, now=NOW + timedelta(minutes=20))
    assert result.status == "recovery_exhausted"
    assert not (anima_dir / "state" / "pending" / "docvault-review.json").exists()


def test_corrupt_attempt_state_fails_closed_instead_of_recovering_again(tmp_path: Path) -> None:
    anima_dir = _setup(tmp_path, _phase())
    assert reconcile_execution_once(anima_dir, now=NOW).status == "recovered"
    (anima_dir / "state" / "pending" / "docvault-review.json").unlink()
    (anima_dir / "state" / "execution_liveness_state.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        reconcile_execution_once(anima_dir, now=NOW + timedelta(minutes=20))
    assert not (anima_dir / "state" / "pending" / "docvault-review.json").exists()


def test_scheduled_phase_waits_until_start_time(tmp_path: Path) -> None:
    start_at = (NOW + timedelta(minutes=10)).isoformat()
    anima_dir = _setup(tmp_path, _phase(start_at=start_at))
    early = reconcile_execution_once(anima_dir, now=NOW)
    assert early.status == "scheduled"
    assert not (anima_dir / "state" / "pending" / "docvault-review.json").exists()
    due = reconcile_execution_once(anima_dir, now=NOW + timedelta(minutes=10))
    assert due.status == "recovered"


def test_invalid_schedule_and_external_probe_fail_closed(tmp_path: Path) -> None:
    invalid_schedule = _setup(tmp_path / "schedule", _phase(start_at="tomorrow morning"))
    assert reconcile_execution_once(invalid_schedule, now=NOW).status == "invalid_config"
    assert not (invalid_schedule / "state" / "pending" / "docvault-review.json").exists()

    invalid_runner = _setup(tmp_path / "runner", _phase(external_runner={"pid": "4242"}))
    assert reconcile_execution_once(invalid_runner, now=NOW).status == "invalid_config"
    assert not (invalid_runner / "state" / "pending" / "docvault-review.json").exists()


def test_touching_progress_file_without_count_increase_is_not_progress(tmp_path: Path) -> None:
    anima_dir = _setup(tmp_path, _phase())
    progress = anima_dir / "state" / "docvault.count"
    progress.write_text("41", encoding="utf-8")
    assert reconcile_execution_once(anima_dir, now=NOW).status == "recovered"
    progress.write_text("40", encoding="utf-8")
    assert reconcile_execution_once(anima_dir, now=NOW + timedelta(minutes=1)).status == "awaiting_progress"


def test_explicit_blocker_and_approval_boundary_are_not_recovered(tmp_path: Path) -> None:
    blocked = _setup(tmp_path / "blocked", _phase(blocker="waiting for operator"))
    assert reconcile_execution_once(blocked, now=NOW).status == "blocked"
    frozen = _setup(tmp_path / "frozen", _phase(capabilities=["production_deploy"]))
    assert reconcile_execution_once(frozen, now=NOW).status == "approval_boundary"
