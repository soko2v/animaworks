from __future__ import annotations

"""Bounded, fail-closed continuous backlog dispatcher for one Anima."""

import argparse
import fcntl
import json
import logging
import math
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.blocked_recovery import regenerate_pending_json
from core.memory._io import atomic_write_text
from core.memory.task_queue import TaskQueueManager
from core.time_utils import now_iso

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 64
MAX_CONFIG_BYTES = 256_000
MAX_SCAN_SECONDS = 110.0
DENIED_CAPABILITIES = frozenset(
    {
        "production_deploy",
        "production_db",
        "migration",
        "production_data",
        "credential",
        "external_send",
        "external_publish",
        "safety_frozen",
    }
)
TERMINAL = frozenset({"done", "cancelled", "failed"})


@dataclass(frozen=True)
class DispatchResult:
    """Outcome of one dispatcher pass."""

    status: str
    task_id: str | None = None
    reason: str = ""


def _read_json(path: Path, *, max_bytes: int = MAX_CONFIG_BYTES) -> dict[str, Any]:
    if path.stat().st_size > max_bytes:
        raise ValueError(f"configuration exceeds {max_bytes} bytes")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("configuration root must be an object")
    return value


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _record_state(anima_dir: Path, result: DispatchResult, *, error_count: int = 0) -> None:
    payload = {
        "checked_at": now_iso(),
        "status": result.status,
        "task_id": result.task_id,
        "reason": result.reason,
        "consecutive_errors": error_count,
        "pid": os.getpid(),
    }
    atomic_write_text(
        anima_dir / "state" / "continuous_dispatcher_state.json",
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def _is_dormant_waiting_reenqueue(
    anima_dir: Path,
    queue: TaskQueueManager,
    task_id: str | None,
) -> bool:
    """Return whether task_id is a safely identifiable deferred Waiting task."""
    if not task_id:
        return False
    entry = queue.get_task_by_id(task_id)
    if (
        entry is None
        or entry.status != "in_progress"
        or entry.summary != "background work waiting; automatic recheck scheduled"
    ):
        return False
    descriptor_path = anima_dir / "state" / "pending" / f"{task_id}.json"
    try:
        descriptor = _read_json(descriptor_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    waiting_count = descriptor.get("waiting_reenqueue_count")
    not_before = descriptor.get("continuation_not_before")
    context = descriptor.get("context")
    return (
        descriptor.get("task_id") == task_id
        and isinstance(waiting_count, int)
        and not isinstance(waiting_count, bool)
        and waiting_count > 0
        and isinstance(not_before, (int, float))
        and not isinstance(not_before, bool)
        and math.isfinite(not_before)
        and not_before > time.time()
        and isinstance(context, str)
        and any(line.strip().endswith(": waiting") for line in context.splitlines())
    )


def _active_runner_exists(
    anima_dir: Path,
    queue: TaskQueueManager,
) -> bool:
    pending = anima_dir / "state" / "pending"
    processing = pending / "processing"
    if any(processing.glob("*.json")):
        return True
    dormant_waiting: set[str] = set()
    for path in pending.glob("*.json"):
        if not _is_dormant_waiting_reenqueue(anima_dir, queue, path.stem):
            return True
        dormant_waiting.add(path.stem)
    return any(entry.task_id not in dormant_waiting for entry in queue.list_tasks(status="in_progress"))


def _eligible(candidate: dict[str, Any]) -> bool:
    if candidate.get("enabled") is not True or candidate.get("approved_safe") is not True:
        return False
    capabilities = candidate.get("capabilities", [])
    if not isinstance(capabilities, list) or any(str(item) in DENIED_CAPABILITIES for item in capabilities):
        return False
    required = ("task_id", "title", "description", "priority")
    return all(isinstance(candidate.get(key), (str, int)) and bool(str(candidate[key]).strip()) for key in required)


def _priority(candidate: dict[str, Any]) -> tuple[int, str]:
    value = candidate.get("priority")
    rank = value if isinstance(value, int) and not isinstance(value, bool) else 10_000
    return rank, str(candidate.get("task_id", ""))


def dispatch_once(
    anima_dir: Path,
    *,
    config_path: Path | None = None,
) -> DispatchResult:
    """Select and publish at most one safe candidate using bounded explicit inputs."""
    started = time.monotonic()
    config_file = config_path or anima_dir / "state" / "continuous_backlog.json"
    lock_file = anima_dir / "state" / "continuous_dispatcher.lock"
    with _exclusive_lock(lock_file) as acquired:
        if not acquired:
            return DispatchResult("no_op", reason="dispatcher lock is held")
        if not config_file.is_file():
            return DispatchResult("idle", reason="backlog configuration is absent")
        config = _read_json(config_file)
        candidates = config.get("candidates", [])
        if not isinstance(candidates, list):
            raise ValueError("candidates must be a list")
        candidates = candidates[:MAX_CANDIDATES]
        queue = TaskQueueManager(anima_dir)
        # Reconcile durable, explicitly declared execution phases before
        # selecting unrelated backlog work.  The import is local to avoid a
        # module cycle: execution_liveness reuses this module's bounds/lock.
        from core.execution_liveness import reconcile_execution_once

        liveness = reconcile_execution_once(anima_dir)
        if liveness.status in {
            "recovered",
            "runner_live",
            "external_runner_live",
            "awaiting_progress",
            "progress_verified",
            "cooldown",
            "invalid_config",
            "no_op",
        }:
            return DispatchResult("no_op", liveness.task_id, f"execution liveness: {liveness.status}")
        if _active_runner_exists(anima_dir, queue):
            return DispatchResult("no_op", reason="a pending, in_progress, or processing runner exists")

        entries = {
            entry.task_id: entry
            for status in ("pending", "in_progress", "blocked", "delegated", "done", "cancelled", "failed")
            for entry in queue.list_tasks(status=status)
        }
        pending_dir = anima_dir / "state" / "pending"
        processing_dir = pending_dir / "processing"
        for candidate in sorted((item for item in candidates if isinstance(item, dict)), key=_priority):
            if time.monotonic() - started >= MAX_SCAN_SECONDS:
                return DispatchResult("idle", reason="bounded scan deadline reached")
            if not _eligible(candidate):
                continue
            task_id = str(candidate["task_id"])
            task_file = f"{task_id}.json"
            if (pending_dir / task_file).exists() or (processing_dir / task_file).exists():
                continue
            existing = entries.get(task_id)
            if existing is not None:
                if existing.status in TERMINAL or existing.status in {"blocked", "delegated", "in_progress"}:
                    continue
                entry = existing
            else:
                task_desc = {
                    "title": str(candidate["title"]),
                    "description": str(candidate["description"]),
                    "context": str(candidate.get("context", "")),
                    "acceptance_criteria": candidate.get("acceptance_criteria", []),
                    "constraints": candidate.get("constraints", []),
                    "file_paths": candidate.get("file_paths", []),
                    "working_directory": str(candidate.get("working_directory", "")),
                    "reply_to": str(candidate.get("reply_to", "alex")),
                    "model": str(candidate.get("model", "c:codex/gpt-5.6-sol")),
                }
                entry = queue.add_task_if_absent(
                    lambda item, stable_id=task_id: item.task_id == stable_id,
                    source="anima",
                    original_instruction=str(candidate["description"]),
                    assignee=anima_dir.name,
                    summary=str(candidate["title"]),
                    task_id=task_id,
                    meta={
                        "continuous_dispatch_key": str(candidate.get("key", task_id)),
                        "task_desc": task_desc,
                        "model": task_desc["model"],
                    },
                )
                if entry is None:
                    continue
            regenerate_pending_json(anima_dir, anima_dir.name, entry)
            return DispatchResult("dispatched", task_id=task_id, reason="one safe candidate published")
        return DispatchResult("idle", reason="no safe executable candidate")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anima-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    state_path = args.anima_dir / "state" / "continuous_dispatcher_state.json"
    previous_errors = 0
    if state_path.is_file():
        try:
            previous_errors = int(_read_json(state_path).get("consecutive_errors", 0))
        except (OSError, ValueError, json.JSONDecodeError, TypeError):
            previous_errors = 0
    try:
        result = dispatch_once(args.anima_dir, config_path=args.config)
    except Exception as exc:
        logger.exception("Continuous dispatcher failed")
        result = DispatchResult("error", reason=f"{type(exc).__name__}: {str(exc)[:300]}")
        _record_state(args.anima_dir, result, error_count=previous_errors + 1)
        return 1
    _record_state(args.anima_dir, result, error_count=0)
    print(json.dumps(result.__dict__, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
