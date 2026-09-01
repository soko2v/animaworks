from __future__ import annotations

"""Fail-closed reconciliation of declared work with real execution evidence."""

import hashlib
import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.blocked_recovery import regenerate_pending_json
from core.continuous_dispatcher import DENIED_CAPABILITIES, _exclusive_lock, _read_json
from core.memory._io import atomic_write_text
from core.memory.task_queue import TaskQueueManager

MAX_PHASES = 64
RECOVERY_COOLDOWN_SECONDS = 600


@dataclass(frozen=True)
class LivenessResult:
    """Outcome of one execution-liveness reconciliation pass."""

    status: str
    task_id: str | None = None
    reason: str = ""


def _default_process_probe(pid: int) -> str | None:
    try:
        import psutil

        process = psutil.Process(pid)
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return None
        return " ".join(process.cmdline())
    except (OSError, psutil.Error, ValueError):
        return None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _safe_relative_path(anima_dir: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    root = anima_dir.resolve()
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _observation(anima_dir: Path, phase: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("checkpoint_path", "progress_path"):
        path = _safe_relative_path(anima_dir, phase.get(key))
        if path is None or not path.is_file():
            result[key] = None
            continue
        stat = path.stat()
        raw = path.read_bytes()
        result[key] = {
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "count": _finite_count(raw) if key == "progress_path" else None,
        }
    return result


def _finite_count(raw: bytes) -> float | None:
    try:
        value = float(raw.decode("utf-8").strip())
    except (UnicodeDecodeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _progressed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    for key in ("checkpoint_path", "progress_path"):
        old = before.get(key)
        new = after.get(key)
        if new is None:
            continue
        if old is None:
            return True
        if key == "progress_path":
            if old.get("count") is not None and new.get("count") is not None and new["count"] > old["count"]:
                return True
            continue
        if new.get("sha256") != old.get("sha256"):
            return True
    return False


def _external_runner_live(phase: dict[str, Any], probe: Callable[[int], str | None]) -> bool:
    spec = phase.get("external_runner")
    if not isinstance(spec, dict):
        return False
    pid = spec.get("pid")
    marker = spec.get("command_contains")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1 or not isinstance(marker, str) or not marker:
        return False
    command = probe(pid)
    return isinstance(command, str) and marker in command


def _load_attempts(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"tasks": {}}
    value = _read_json(path)
    if not isinstance(value.get("tasks"), dict):
        raise ValueError("execution liveness state must contain a tasks object")
    return value


def _save_attempts(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _descriptor_exists(anima_dir: Path, task_id: str) -> bool:
    pending = anima_dir / "state" / "pending"
    return (pending / f"{task_id}.json").is_file() or (pending / "processing" / f"{task_id}.json").is_file()


def _eligible_phase(phase: dict[str, Any]) -> bool:
    return (
        phase.get("approved_safe") is True
        and isinstance(phase.get("goal_id"), str)
        and bool(phase["goal_id"].strip())
        and isinstance(phase.get("task_id"), str)
        and bool(phase["task_id"].strip())
        and isinstance(phase.get("description"), str)
        and bool(phase["description"].strip())
    )


def _invalid_phase_reason(anima_dir: Path, phase: dict[str, Any]) -> str | None:
    """Return why an otherwise eligible phase cannot be reconciled safely."""
    if "start_at" in phase and _parse_datetime(phase.get("start_at")) is None:
        return "start_at must be a valid ISO-8601 timestamp"
    if not any(_safe_relative_path(anima_dir, phase.get(key)) is not None for key in ("checkpoint_path", "progress_path")):
        return "a safe checkpoint_path or progress_path is required"
    external = phase.get("external_runner")
    if external is not None:
        if not isinstance(external, dict):
            return "external_runner must be an object"
        pid = external.get("pid")
        marker = external.get("command_contains")
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 1
            or not isinstance(marker, str)
            or not marker.strip()
        ):
            return "external_runner requires a valid pid and command marker"
    return None


def _recover(anima_dir: Path, queue: TaskQueueManager, phase: dict[str, Any]) -> None:
    task_id = str(phase["task_id"])
    entry = queue.get_task_by_id(task_id)
    if entry is None:
        context = str(phase.get("resume_context", ""))
        task_desc = {
            "title": str(phase.get("title") or task_id),
            "description": str(phase["description"]),
            "context": context,
            "acceptance_criteria": phase.get("acceptance_criteria", []),
            "constraints": phase.get("constraints", []),
            "file_paths": phase.get("file_paths", []),
            "working_directory": str(phase.get("working_directory", "")),
            "reply_to": str(phase.get("reply_to", anima_dir.name)),
            "model": str(phase.get("model", "")),
        }
        entry = queue.add_task(
            source="anima",
            original_instruction=str(phase["description"]),
            assignee=anima_dir.name,
            summary=str(phase.get("title") or task_id),
            task_id=task_id,
            meta={
                "executor": "taskexec",
                "goal_id": str(phase.get("goal_id", "")),
                "resume_context": context,
                "task_desc": task_desc,
                "model": task_desc["model"],
            },
        )
    elif entry.status == "in_progress":
        updated = queue.update_status(task_id, "pending", summary="auto-recovered: execution path missing")
        if updated is not None:
            entry = updated
    regenerate_pending_json(anima_dir, anima_dir.name, entry)


def reconcile_execution_once(
    anima_dir: Path,
    *,
    config_path: Path | None = None,
    now: datetime | None = None,
    process_probe: Callable[[int], str | None] = _default_process_probe,
) -> LivenessResult:
    """Reconcile one declared phase without crossing approval or scheduling boundaries."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    config_file = config_path or anima_dir / "state" / "execution_liveness.json"
    if not config_file.is_file():
        return LivenessResult("idle", reason="liveness configuration is absent")
    phases = _read_json(config_file).get("phases", [])
    if not isinstance(phases, list):
        raise ValueError("phases must be a list")
    state_path = anima_dir / "state" / "execution_liveness_state.json"
    lock_path = anima_dir / "state" / "execution_liveness.lock"
    with _exclusive_lock(lock_path) as acquired:
        if not acquired:
            return LivenessResult("no_op", reason="liveness lock is held")
        attempts = _load_attempts(state_path)
        queue = TaskQueueManager(anima_dir)
        from core.goals import GoalManager

        goals = GoalManager(anima_dir)
        for raw_phase in phases[:MAX_PHASES]:
            if not isinstance(raw_phase, dict) or not _eligible_phase(raw_phase):
                continue
            phase = raw_phase
            task_id = str(phase["task_id"])
            goal = goals.get_goal(str(phase["goal_id"]))
            if goal is None or goal.status != "active":
                continue
            invalid_reason = _invalid_phase_reason(anima_dir, phase)
            if invalid_reason is not None:
                return LivenessResult("invalid_config", task_id, invalid_reason)
            capabilities = phase.get("capabilities", [])
            if not isinstance(capabilities, list) or any(str(item) in DENIED_CAPABILITIES for item in capabilities):
                return LivenessResult("approval_boundary", task_id, "phase requires explicit approval")
            if isinstance(phase.get("blocker"), str) and phase["blocker"].strip():
                return LivenessResult("blocked", task_id, str(phase["blocker"]))
            start_at = _parse_datetime(phase.get("start_at"))
            if start_at is not None and current < start_at:
                return LivenessResult("scheduled", task_id, "start time has not arrived")
            predecessor_id = phase.get("predecessor_task_id")
            if isinstance(predecessor_id, str) and predecessor_id:
                predecessor = queue.get_task_by_id(predecessor_id)
                if predecessor is None or predecessor.status != "done":
                    continue
            if _external_runner_live(phase, process_probe):
                return LivenessResult("external_runner_live", task_id, "verified external runner remains live")
            record = attempts["tasks"].get(task_id)
            if isinstance(record, dict) and record.get("verified") is not True:
                after = _observation(anima_dir, phase)
                if _progressed(record.get("baseline", {}), after):
                    record["verified"] = True
                    record["verified_at"] = current.isoformat()
                    _save_attempts(state_path, attempts)
                    return LivenessResult("progress_verified", task_id, "checkpoint or processed count advanced")
            if _descriptor_exists(anima_dir, task_id):
                if isinstance(record, dict) and record.get("verified") is not True:
                    return LivenessResult("awaiting_progress", task_id, "runner exists but progress is not yet proven")
                return LivenessResult("runner_live", task_id, "descriptor exists")
            if isinstance(record, dict):
                attempted_at = _parse_datetime(record.get("attempted_at"))
                if attempted_at is not None and (current - attempted_at).total_seconds() < RECOVERY_COOLDOWN_SECONDS:
                    return LivenessResult("cooldown", task_id, "single recovery attempt is cooling down")
                return LivenessResult("recovery_exhausted", task_id, "single automatic recovery was already used")
            entry = queue.get_task_by_id(task_id)
            if entry is not None and entry.status in {"blocked", "delegated", "done", "cancelled", "failed"}:
                continue
            baseline = _observation(anima_dir, phase)
            _recover(anima_dir, queue, phase)
            attempts["tasks"][task_id] = {
                "attempted_at": current.isoformat(),
                "baseline": baseline,
                "pid": os.getpid(),
                "verified": False,
            }
            _save_attempts(state_path, attempts)
            return LivenessResult("recovered", task_id, "missing execution path restored")
    return LivenessResult("idle", reason="no due recoverable phase")
