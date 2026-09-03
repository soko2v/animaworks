from __future__ import annotations

# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0

"""Heartbeat-driven recovery for blocked TaskExec tasks."""

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from core.i18n import t
from core.memory._io import atomic_write_text
from core.memory.task_queue import TaskQueueManager
from core.schemas import TaskEntry
from core.time_utils import ensure_aware, now_iso, now_local

logger = logging.getLogger("animaworks.blocked_recovery")


def regenerate_pending_json(
    anima_dir: Path,
    anima_name: str,
    entry: TaskEntry,
    *,
    description_suffix: str = "",
) -> bool:
    """Publish a task queue entry for PendingTaskExecutor without retry accounting."""
    pending_dir = anima_dir / "state" / "pending"
    task_file = f"{entry.task_id}.json"
    if (pending_dir / task_file).exists() or (pending_dir / "processing" / task_file).exists():
        return True

    task_desc_meta = entry.meta.get("task_desc", {}) or {}
    description = task_desc_meta.get("description", entry.original_instruction)
    if description_suffix:
        description = f"{description}\n\n{description_suffix}"
    # Restore the per-task model override so blocked-recovery re-execution keeps it.
    # SSoT is the pending task_desc (task_desc_meta.model) with a fallback to the
    # queue entry meta (entry.meta.model) for direct-dispatch submissions.
    model = entry.meta.get("model") or task_desc_meta.get("model")
    task_desc = {
        "task_type": "llm",
        "task_id": entry.task_id,
        "batch_id": entry.meta.get("batch_id", ""),
        "title": task_desc_meta.get("title", entry.summary),
        "description": description,
        "parallel": False,
        "depends_on": [],
        "context": task_desc_meta.get("context", ""),
        "acceptance_criteria": task_desc_meta.get("acceptance_criteria", []),
        "constraints": task_desc_meta.get("constraints", []),
        "file_paths": task_desc_meta.get("file_paths", []),
        "submitted_by": anima_name,
        "submitted_at": now_iso(),
        "reply_to": task_desc_meta.get("reply_to", anima_name),
        "working_directory": task_desc_meta.get("working_directory", ""),
        "model": model if isinstance(model, str) else "",
    }
    atomic_write_text(
        pending_dir / task_file,
        json.dumps(task_desc, ensure_ascii=False, indent=2) + "\n",
    )
    return True


def _age_hours(entry: TaskEntry) -> float | None:
    value = entry.meta.get("blocked_at") or entry.updated_at
    try:
        blocked_at = ensure_aware(datetime.fromisoformat(str(value)))
    except (TypeError, ValueError):
        return None
    return max(0.0, (now_local() - blocked_at).total_seconds() / 3600)


def _blocked_since(entry: TaskEntry) -> datetime:
    """Return a stable timestamp for oldest-first recovery."""
    value = entry.meta.get("blocked_at") or entry.updated_at
    try:
        return ensure_aware(datetime.fromisoformat(str(value)))
    except (TypeError, ValueError):
        return datetime.max.replace(tzinfo=now_local().tzinfo)


def _count(meta: dict, key: str) -> int:
    value = meta.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _record_check_failure(manager: TaskQueueManager, entry: TaskEntry) -> None:
    manager.update_meta(
        entry.task_id,
        {"unblock_check_failures": _count(entry.meta, "unblock_check_failures") + 1},
    )


def _alert_manual_intervention_required(
    manager: TaskQueueManager,
    anima_dir: Path,
    anima_name: str,
    entry: TaskEntry,
) -> None:
    if entry.meta.get("blocked_recovery_alerted"):
        return

    from core.config.models import read_anima_supervisor
    from core.delegation_recovery import _add_alert_task

    supervisor = read_anima_supervisor(anima_dir)
    if not supervisor:
        return
    supervisor_dir = anima_dir.parent / supervisor
    if not supervisor_dir.is_dir():
        return
    _add_alert_task(
        supervisor_dir,
        kind="blocked_task_manual_intervention_required",
        target_name=anima_name,
        delegated_task_id=entry.task_id,
        summary=f"Blocked task needs intervention: {anima_name}/{entry.task_id}",
        instruction=t(
            "blocked_recovery.manual_intervention_instruction",
            task_id=entry.task_id,
            anima_name=anima_name,
            original_instruction=entry.original_instruction,
        ),
        extra_meta={"blocked_task_id": entry.task_id},
    )
    manager.update_meta(entry.task_id, {"blocked_recovery_alerted": True})


# Linux route: bubblewrap. Root is bind-mounted read-only, /tmp is a private
# tmpfs, the network namespace is unshared, and children die with the parent.
_BWRAP_ARGV_PREFIX: tuple[str, ...] = (
    "bwrap",
    "--ro-bind",
    "/",
    "/",
    "--dev",
    "/dev",
    "--proc",
    "/proc",
    "--tmpfs",
    "/tmp",
    "--unshare-net",
    "--die-with-parent",
    "--",
)

# macOS route: Seatbelt via /usr/bin/sandbox-exec. The profile is a fixed
# constant -- the unblock_check string is never interpolated into it; the check
# is passed verbatim as a single argv element to ``/bin/sh -c``. Everything is
# denied by default; only read access, process exec/fork, self-signalling,
# sysctl reads and writes to the /dev/null device are allowed. All file writes
# (including /tmp) and all network access are denied.
_MACOS_SANDBOX_EXEC = "/usr/bin/sandbox-exec"
_MACOS_SANDBOX_PROFILE = """(version 1)
(deny default)
(allow process-exec*)
(allow process-fork)
(allow signal (target self))
(allow sysctl-read)
(allow file-read*)
(allow file-write* (literal "/dev/null"))
(deny network*)
"""


def _sandbox_route() -> str | None:
    """Return the available read-only/no-network sandbox route, or None (fail closed)."""
    if shutil.which("bwrap"):
        return "bwrap"
    if sys.platform == "darwin" and os.access(_MACOS_SANDBOX_EXEC, os.X_OK):
        return "sandbox-exec"
    return None


def _sandbox_argv(route: str, check: str) -> list[str]:
    """Build the sandboxed ``/bin/sh -c <check>`` argv for ``route``."""
    inner = ["/bin/sh", "-c", check]
    if route == "bwrap":
        return [*_BWRAP_ARGV_PREFIX, *inner]
    if route == "sandbox-exec":
        return [_MACOS_SANDBOX_EXEC, "-p", _MACOS_SANDBOX_PROFILE, *inner]
    raise ValueError(f"unknown sandbox route: {route}")


def _descendant_pids(root_pid: int) -> list[int]:
    """Return all live descendants of ``root_pid`` (via ``ps``), children first.

    This catches descendants that left the process group/session (``setsid``)
    but whose parent chain is still alive. A fully detached double-fork daemon
    is reparented to PID 1 and cannot be attributed; that residual risk is
    accepted and documented.
    """
    try:
        listing = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
            text=True,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    children: dict[int, list[int]] = {}
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    found: list[int] = []
    queue = [root_pid]
    while queue:
        current = queue.pop(0)
        for child in children.get(current, []):
            if child not in found and child != root_pid:
                found.append(child)
                queue.append(child)
    return found


# Upper bound on freeze passes; each pass stops every newly seen descendant so
# a stopped parent cannot fork again, and the tree converges quickly.
_KILL_TREE_MAX_PASSES = 8


def _kill_tree(proc: subprocess.Popen) -> None:
    """Freeze, then SIGKILL, the whole descendant tree of ``proc`` and its process group.

    A single ``ps`` snapshot is racy: a descendant may fork a child that
    ``setsid()``s between the snapshot and the kill. So the root's process
    group is SIGSTOPped first, then descendants that already left the group
    are SIGSTOPped pass by pass (a stopped process cannot fork) until a pass
    finds nothing new, and only then is everything SIGKILLed. Fully detached
    double-fork daemons (already reparented to PID 1 before the timeout) remain
    out of reach; that is the documented accepted residual.
    """
    # Freeze the root and everything still in its session first: the root is
    # not part of its own descendant list, and an unfrozen root could keep
    # forking new sessions between the last snapshot and the kill.
    try:
        os.killpg(proc.pid, signal.SIGSTOP)
    except (ProcessLookupError, PermissionError):
        pass
    frozen: list[int] = []
    for _ in range(_KILL_TREE_MAX_PASSES):
        new_pids = [pid for pid in _descendant_pids(proc.pid) if pid not in frozen]
        if not new_pids:
            break
        for pid in new_pids:
            try:
                os.kill(pid, signal.SIGSTOP)
            except (ProcessLookupError, PermissionError):
                continue
            frozen.append(pid)
    for pid in frozen:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def _run_sandboxed(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> int:
    """Run ``argv`` with suppressed output and return its exit code.

    The child is started in its own session so that, on timeout, the whole
    process group is killed; before that, the live descendant tree is walked
    and killed so that a check which ``setsid()``s out of the group does not
    survive either. bwrap already provides ``--die-with-parent``; sandbox-exec
    does not, so this is what keeps a timed-out check from leaving orphans on
    macOS. Raises ``OSError`` when the sandbox binary cannot be started and
    ``subprocess.TimeoutExpired`` on timeout.
    """
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        proc.wait()
        raise


def revalidate_blocked_tasks(anima_dir: Path, anima_name: str) -> list[str]:
    """Revalidate blocked tasks and return task IDs changed back to pending."""
    from core.config.models import load_config

    config = load_config().background_task
    if not config.blocked_recovery_enabled:
        return []

    manager = TaskQueueManager(anima_dir)
    unblocked: list[str] = []
    entries = sorted(
        manager.list_tasks(status="blocked"),
        key=lambda entry: (_blocked_since(entry), entry.task_id),
    )
    for entry in entries:
        if len(unblocked) >= config.blocked_reprobe_batch_limit:
            break
        try:
            try:
                from core.taskboard.attention_resolver import resolver_for_anima_dir

                decision = resolver_for_anima_dir(anima_dir).should_execute(
                    anima_name, entry.task_id, queue_status="pending"
                )
                if not decision.executable:
                    continue
            except Exception:
                logger.warning(
                    "TaskBoard recovery gate unavailable for task %s; failing open",
                    entry.task_id,
                    exc_info=True,
                )

            check = entry.meta.get("unblock_check")
            has_check = isinstance(check, str) and bool(check.strip())
            route: str | None = None
            if has_check:
                env = {
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": os.environ.get("HOME", ""),
                    "ANIMAWORKS_ANIMA_DIR": str(anima_dir),
                }
                route = _sandbox_route()
                if route is None:
                    logger.warning(
                        "unblock_check sandbox unavailable for task %s (no bwrap, no macOS sandbox-exec); failing closed",
                        entry.task_id,
                    )
                    _record_check_failure(manager, entry)
                    continue
                try:
                    returncode = _run_sandboxed(
                        _sandbox_argv(route, check),
                        cwd=anima_dir,
                        env=env,
                        timeout=config.blocked_check_timeout_seconds,
                    )
                except OSError:
                    logger.warning(
                        "unblock_check sandbox unavailable for task %s (route=%s); failing closed",
                        entry.task_id,
                        route,
                        exc_info=True,
                    )
                    _record_check_failure(manager, entry)
                    continue
                except subprocess.TimeoutExpired:
                    _record_check_failure(manager, entry)
                    continue
                if returncode != 0:
                    _record_check_failure(manager, entry)
                    continue
                suffix = ""
            else:
                # checkless: fail closed by default (no automatic pending requeue).
                age_hours = _age_hours(entry)
                if age_hours is None or age_hours < config.blocked_reprobe_after_hours:
                    continue
                if not config.blocked_checkless_reprobe_enabled:
                    _alert_manual_intervention_required(manager, anima_dir, anima_name, entry)
                    continue
                # Legacy time-based reprobe (opt-in via blocked_checkless_reprobe_enabled).
                reprobes = _count(entry.meta, "blocked_reprobe_count")
                if reprobes >= config.blocked_max_reprobes:
                    _alert_manual_intervention_required(manager, anima_dir, anima_name, entry)
                    continue
                manager.update_meta(entry.task_id, {"blocked_reprobe_count": reprobes + 1})
                suffix = t("blocked_recovery.reprobe_instruction")

            pending = manager.update_status(
                entry.task_id,
                "pending",
                summary=("auto-unblocked: check passed" if has_check else "auto-unblocked: scheduled reprobe"),
            )
            if pending is None:
                continue
            try:
                regenerate_pending_json(anima_dir, anima_name, pending, description_suffix=suffix)
            except Exception:
                manager.update_status(entry.task_id, "blocked", summary=entry.summary)
                raise
            unblocked.append(entry.task_id)
            from core.memory.activity import ActivityLogger

            ActivityLogger(anima_dir).log(
                "blocked_recovery",
                summary=("Unblock check passed" if has_check else "Scheduled blocked task reprobe"),
                meta={
                    "task_id": entry.task_id,
                    "method": "check" if has_check else "reprobe",
                    "sandbox": route if has_check else "",
                },
                safe=True,
            )
        except Exception:
            logger.warning("Failed to revalidate blocked task %s", entry.task_id, exc_info=True)
    return unblocked
