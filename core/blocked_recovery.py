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
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import IO

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


# Environment variable carrying a per-run marker. It is inherited by every
# process the check spawns (including double-forked daemons that leave the
# parent chain), so timeout cleanup can find strays without relying on ppid.
_CHECK_MARKER_ENV = "ANIMAWORKS_UNBLOCK_CHECK_ID"

# Outer shell wrapper. The CPU-time rlimit is inherited by every descendant
# (fork, setsid, double-fork alike) and bounds how long any escapee can burn
# CPU. The check itself is passed as a positional parameter ($2) so it is
# never parsed as part of this script; only the inner ``/bin/sh -c "$2"``
# interprets it, exactly as before.
_SH_WRAPPER = 'ulimit -t "$1" 2>/dev/null; exec /bin/sh -c "$2"'

# Upper bound on freeze passes; each pass stops every newly seen process so
# a stopped parent cannot fork again, and the set converges quickly.
_KILL_TREE_MAX_PASSES = 8
_PS_RETRIES = 3


class _ProcessListingUnavailable(RuntimeError):
    """``ps`` (or /proc) could not be consulted; cleanup cannot enumerate strays."""


def _sandbox_route() -> str | None:
    """Return the available read-only/no-network sandbox route, or None (fail closed)."""
    if shutil.which("bwrap"):
        return "bwrap"
    if sys.platform == "darwin" and os.access(_MACOS_SANDBOX_EXEC, os.X_OK):
        return "sandbox-exec"
    return None


def _sandbox_argv(route: str, check: str, *, cpu_seconds: int) -> list[str]:
    """Build the sandboxed ``/bin/sh -c <check>`` argv for ``route``.

    ``check`` is always the last argv element and is never interpolated into
    the profile, the wrapper script, or any other shell text.
    """
    inner = ["/bin/sh", "-c", _SH_WRAPPER, "unblock_check", str(int(cpu_seconds)), check]
    if route == "bwrap":
        return [*_BWRAP_ARGV_PREFIX, *inner]
    if route == "sandbox-exec":
        return [_MACOS_SANDBOX_EXEC, "-p", _MACOS_SANDBOX_PROFILE, *inner]
    raise ValueError(f"unknown sandbox route: {route}")


def _ps_lines(args: list[str]) -> list[str]:
    """Run ``ps`` with retries; raise ``_ProcessListingUnavailable`` if it cannot be consulted."""
    last: BaseException | None = None
    for attempt in range(_PS_RETRIES):
        try:
            result = subprocess.run(
                ["ps", *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
                text=True,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            last = exc
        else:
            if result.returncode == 0:
                return result.stdout.splitlines()
            last = RuntimeError(f"ps exited {result.returncode}")
        if attempt + 1 < _PS_RETRIES:
            time.sleep(0.1)
    raise _ProcessListingUnavailable(str(last))


def _descendant_pids(root_pid: int) -> list[int]:
    """Return all live descendants of ``root_pid`` (via ``ps``), children first."""
    children: dict[int, list[int]] = {}
    for line in _ps_lines(["-axo", "pid=,ppid="]):
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


def _process_group_pids(group_id: int) -> list[int]:
    """Return live PIDs in a process group without trusting a dead group leader."""
    pids: list[int] = []
    for line in _ps_lines(["-axo", "pid=,pgid="]):
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, pgid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if pgid == group_id and pid != group_id:
            pids.append(pid)
    return pids


def _marker_pids(marker: str) -> tuple[list[int], bool]:
    """Return ``(pids, complete)`` for live (non-zombie) processes whose environment carries ``marker``.

    Independent of the parent chain: a double-forked daemon reparented to
    PID 1 still inherits the environment. ``complete`` is False when at least
    one process's environment could not be inspected (e.g. Linux denies
    ``/proc/<pid>/environ`` for a non-dumpable process); callers must then
    treat enumeration as incomplete rather than as "no marker". Processes
    that scrub or replace their environment are the documented residual.
    """
    needle = f"{_CHECK_MARKER_ENV}={marker}"
    own = os.getpid()
    pids: list[int] = []
    if sys.platform == "darwin":
        for line in _ps_lines(["-axEo", "pid=,stat=,command="]):
            parts = line.split(None, 2)
            if len(parts) < 3 or needle not in parts[2] or parts[1].startswith("Z"):
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            if pid != own:
                pids.append(pid)
        return pids, True
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        raise _ProcessListingUnavailable("/proc unavailable")
    needle_b = needle.encode() + b"\0"
    complete = True
    for entry in proc_root.iterdir():
        if not entry.name.isdigit() or int(entry.name) == own:
            continue
        try:
            if needle_b in (entry / "environ").read_bytes():
                pids.append(int(entry.name))
        except (FileNotFoundError, ProcessLookupError):
            continue  # exited between listing and read
        except OSError:
            complete = False  # EACCES/EPERM (non-dumpable) or transient I/O error: cannot rule out a marker
    return pids, complete


def _signal_all(pids: list[int], sig: signal.Signals) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _kill_tree(proc: subprocess.Popen, marker: str) -> None:
    """Freeze, then SIGKILL, everything the check spawned.

    Order: SIGSTOP the root's process group (the root is not in its own
    descendant list and would otherwise keep forking); then, pass by pass,
    SIGSTOP every newly seen process found either as a live descendant or by
    the inherited environment marker, until a pass finds nothing new (a
    stopped process cannot fork); then SIGKILL the frozen set, the group and
    the direct child; finally sweep the marker once more and kill anything
    that still shows up. If process enumeration is unavailable the group is
    still killed and a warning is logged so the incomplete cleanup is visible.
    Residual (documented): processes that scrubbed the inherited marker
    environment before detaching.
    """
    try:
        os.killpg(proc.pid, signal.SIGSTOP)
    except (ProcessLookupError, PermissionError):
        pass
    frozen: list[int] = []
    enumeration_ok = True
    for _ in range(_KILL_TREE_MAX_PASSES):
        try:
            marked, complete = _marker_pids(marker)
            seen = [*_descendant_pids(proc.pid), *marked]
        except _ProcessListingUnavailable:
            enumeration_ok = False
            break
        if not complete:
            enumeration_ok = False
        new_pids = [pid for pid in dict.fromkeys(seen) if pid not in frozen and pid != proc.pid]
        if not new_pids:
            break
        _signal_all(new_pids, signal.SIGSTOP)
        frozen.extend(new_pids)
    _signal_all(frozen, signal.SIGKILL)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    strays: list[int] = []
    try:
        marked, complete = _marker_pids(marker)
        strays = [pid for pid in marked if pid != proc.pid]
        if not complete:
            enumeration_ok = False
    except _ProcessListingUnavailable:
        enumeration_ok = False
    if strays:
        _signal_all(strays, signal.SIGKILL)
    if strays or not enumeration_ok:
        logger.warning(
            "unblock_check timeout cleanup incomplete for marker %s: enumeration_ok=%s stray_pids=%s",
            marker,
            enumeration_ok,
            strays,
        )


# After a zero exit on a stderr-rejecting route, how long to wait for the child's
# stderr pipe to reach EOF before a still-open pipe is treated as a stray holder.
_STDERR_EOF_GRACE_SECONDS = 1.0


class _StderrDrain:
    """Drain a child's stderr on a daemon thread, recording only whether bytes arrived.

    Draining concurrently with ``Popen.wait`` keeps a chatty child from
    blocking on a full pipe until the timeout. Nothing is accumulated, so
    parent memory stays flat regardless of output volume. Reads go through
    the raw file object: once the parent closes it, any further read raises
    ``ValueError`` instead of touching a possibly reused descriptor number.
    """

    def __init__(self, stream: IO[bytes]) -> None:
        self._raw = getattr(stream, "raw", stream)
        self.seen = False
        self.eof = False
        self.failed = False
        self._thread = threading.Thread(target=self._run, name="unblock-check-stderr", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while True:
                chunk = self._raw.read(65536)
                if not chunk:
                    self.eof = True
                    return
                self.seen = True
        except (OSError, ValueError):
            self.failed = True

    def finished(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for EOF or failure; False means the pipe is still open."""
        self._thread.join(timeout)
        return not self._thread.is_alive()


def _reject_open_stderr(root_pid: int, marker: str) -> None:
    """Sweep and kill whatever still holds the check's stderr after the root exited."""
    group_pids: list[int] = []
    marked: list[int] = []
    group_ok = True
    marker_ok = True
    try:
        group_pids = _process_group_pids(root_pid)
    except _ProcessListingUnavailable:
        group_ok = False
    try:
        marked, marker_ok = _marker_pids(marker)
    except _ProcessListingUnavailable:
        marker_ok = False
    strays = list(dict.fromkeys([*group_pids, *marked]))
    if strays:
        _signal_all(strays, signal.SIGKILL)
    logger.warning(
        "unblock_check completed with open stderr pipe; rejecting: group_ok=%s marker_ok=%s stray_pids=%s",
        group_ok,
        marker_ok,
        strays,
    )


def _run_sandboxed(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    marker: str,
    reject_stderr: bool = False,
) -> int:
    """Run ``argv`` with suppressed output and return a fail-closed result.

    ``env`` must carry ``marker`` under ``_CHECK_MARKER_ENV``. The child is
    started in its own session; on timeout ``_kill_tree`` freezes and kills
    the whole tree (see there). Raises ``OSError`` when the sandbox binary
    cannot be started and ``subprocess.TimeoutExpired`` on timeout.

    Without ``reject_stderr`` (the bwrap route) stderr is discarded and the
    exit status alone decides, as before. With ``reject_stderr`` (the macOS
    sandbox-exec route) the exit status is not trusted on its own: Seatbelt
    reports a denied operation only through the child's stderr, and a check
    such as ``! ps ... | grep -q .`` would negate that denial into exit 0. On
    that route stderr is drained concurrently and any output rejects a zero
    exit. A pipe still open shortly after exit means a child retained it, so
    the process group and inherited marker are swept before failing closed;
    an uninspectable pipe also fails closed. Checks on that route must be
    stderr-silent and must never negate an observation command.
    """
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE if reject_stderr else subprocess.DEVNULL,
        start_new_session=True,
    )
    stderr = proc.stderr
    drain = _StderrDrain(stderr) if reject_stderr and stderr is not None else None
    try:
        returncode = proc.wait(timeout=timeout)
        if not reject_stderr or returncode != 0:
            return returncode
        if drain is None:
            return 1
        if not drain.finished(_STDERR_EOF_GRACE_SECONDS):
            _reject_open_stderr(proc.pid, marker)
            return 1
        return 0 if drain.eof and not drain.seen else 1
    except subprocess.TimeoutExpired:
        _kill_tree(proc, marker)
        proc.wait()
        raise
    finally:
        if stderr is not None:
            stderr.close()


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
                marker = f"{entry.task_id}:{uuid.uuid4().hex}"
                env = {
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": os.environ.get("HOME", ""),
                    "ANIMAWORKS_ANIMA_DIR": str(anima_dir),
                    _CHECK_MARKER_ENV: marker,
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
                        _sandbox_argv(route, check, cpu_seconds=config.blocked_check_timeout_seconds),
                        cwd=anima_dir,
                        env=env,
                        timeout=config.blocked_check_timeout_seconds,
                        marker=marker,
                        reject_stderr=route == "sandbox-exec",
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
