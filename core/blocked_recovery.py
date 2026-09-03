from __future__ import annotations

# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0

"""Heartbeat-driven recovery for blocked TaskExec tasks."""

import ctypes
import errno
import json
import logging
import os
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import IO, NamedTuple

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
_WAITID_POLL_SECONDS = 0.01


class _ProcessListingUnavailable(RuntimeError):
    """``ps`` (or /proc) could not be consulted; cleanup cannot enumerate strays."""


class _ProcessIdentity(NamedTuple):
    """Durable-enough process identity available on both Linux and macOS."""

    pid: int
    pgid: int
    started: str


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


def _ps_lines(args: list[str], *, empty_returncodes: tuple[int, ...] = ()) -> list[str]:
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
            if result.returncode in empty_returncodes:
                return []
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


def _parse_process_identity(line: str) -> _ProcessIdentity | None:
    """Parse one ``pid,pgid,lstart,stat`` line, excluding zombies."""
    parts = line.split()
    if len(parts) < 8 or parts[7].startswith("Z"):
        return None
    try:
        pid, pgid = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return _ProcessIdentity(pid, pgid, " ".join(parts[2:7]))


def _process_identities() -> dict[int, _ProcessIdentity]:
    """Return live process identities keyed by PID.

    ``lstart`` is deliberately obtained from ``ps`` on every snapshot. A PID
    alone is not an identity once an earlier process has exited.
    """
    identities: dict[int, _ProcessIdentity] = {}
    for line in _ps_lines(["-axo", "pid=,pgid=,lstart=,stat="]):
        identity = _parse_process_identity(line)
        if identity is not None:
            identities[identity.pid] = identity
    return identities


def _process_group_pids(group_id: int) -> list[_ProcessIdentity]:
    """Return live identities in a process group, excluding its leader."""
    return [
        identity
        for identity in _process_identities().values()
        if identity.pgid == group_id and identity.pid != group_id
    ]


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


def _pid_has_marker(pid: int, marker: str) -> tuple[bool, bool]:
    """Return whether ``pid`` currently carries ``marker`` and whether inspection succeeded."""
    needle = f"{_CHECK_MARKER_ENV}={marker}"
    if sys.platform == "darwin":
        for line in _ps_lines(["-E", "-p", str(pid), "-o", "command="], empty_returncodes=(1,)):
            if needle in line:
                return True, True
        return False, True
    try:
        environ = (Path("/proc") / str(pid) / "environ").read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        return False, True
    except OSError:
        return False, False
    return needle.encode() + b"\0" in environ, True


def _current_identity(pid: int) -> _ProcessIdentity | None:
    """Return the current live identity for ``pid``, if any."""
    lines = _ps_lines(
        ["-o", "pid=,pgid=,lstart=,stat=", "-p", str(pid)],
        empty_returncodes=(1,),
    )
    return next((identity for line in lines if (identity := _parse_process_identity(line)) is not None), None)


def _signal_owned(
    identity: _ProcessIdentity,
    root_pgid: int,
    marker: str,
    sig: signal.Signals,
) -> tuple[bool, bool]:
    """Signal only the same process while it remains in the group or carries the marker.

    Returns ``(signalled, inspection_complete)``. A changed start time is a
    reused PID and is never signalled.
    """
    try:
        current = _current_identity(identity.pid)
    except _ProcessListingUnavailable:
        return False, False
    if current is None or current.started != identity.started:
        return False, True
    if current.pgid != root_pgid:
        try:
            marked, complete = _pid_has_marker(identity.pid, marker)
        except _ProcessListingUnavailable:
            return False, False
        if not complete:
            return False, False
        if not marked:
            return False, True
    try:
        os.kill(identity.pid, sig)
    except (ProcessLookupError, PermissionError):
        return False, True
    return True, True


def _continue_same_identity(identity: _ProcessIdentity) -> tuple[bool, bool]:
    """Continue ``identity`` only if it still matches, returning signal and inspection status."""
    try:
        current = _current_identity(identity.pid)
    except _ProcessListingUnavailable:
        return False, False
    if current != identity:
        return False, True
    try:
        os.kill(identity.pid, signal.SIGCONT)
    except (ProcessLookupError, PermissionError):
        return False, True
    return True, True


def _signal_owned_all(
    identities: list[_ProcessIdentity],
    root_pgid: int,
    marker: str,
    sig: signal.Signals,
) -> tuple[list[_ProcessIdentity], bool]:
    """Revalidate and signal each identity, returning successful signals and completeness."""
    signalled: list[_ProcessIdentity] = []
    complete = True
    for identity in identities:
        sent, inspected = _signal_owned(identity, root_pgid, marker, sig)
        complete = complete and inspected
        if sent:
            signalled.append(identity)
    return signalled, complete


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
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
    _signal_group(proc.pid, signal.SIGSTOP)
    frozen: dict[int, _ProcessIdentity] = {}
    enumeration_ok = True
    for _ in range(_KILL_TREE_MAX_PASSES):
        try:
            marked, complete = _marker_pids(marker)
            seen_pids = [*_descendant_pids(proc.pid), *marked]
            identities = _process_identities()
        except _ProcessListingUnavailable:
            enumeration_ok = False
            break
        if not complete:
            enumeration_ok = False
        new_identities = [
            identities[pid]
            for pid in dict.fromkeys(seen_pids)
            if pid != proc.pid and pid in identities and identities[pid] != frozen.get(pid)
        ]
        if not new_identities:
            break
        stopped, inspected = _signal_owned_all(new_identities, proc.pid, marker, signal.SIGSTOP)
        enumeration_ok = enumeration_ok and inspected
        frozen.update((identity.pid, identity) for identity in stopped)
    _, inspected = _signal_owned_all(list(frozen.values()), proc.pid, marker, signal.SIGKILL)
    enumeration_ok = enumeration_ok and inspected
    _signal_group(proc.pid, signal.SIGKILL)
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    strays: list[int] = []
    try:
        marked, complete = _marker_pids(marker)
        identities = _process_identities()
        stray_identities = [identities[pid] for pid in marked if pid != proc.pid and pid in identities]
        strays = [identity.pid for identity in stray_identities]
        if not complete:
            enumeration_ok = False
    except _ProcessListingUnavailable:
        enumeration_ok = False
        stray_identities = []
    if stray_identities:
        _, inspected = _signal_owned_all(stray_identities, proc.pid, marker, signal.SIGKILL)
        enumeration_ok = enumeration_ok and inspected
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
# Poll tick of the stderr reader thread; bounds how long a stop request takes to land.
_STDERR_POLL_SECONDS = 0.05
# Pause between post-kill re-sweeps so ``ps`` can observe delivered SIGKILLs.
_RESWEEP_PAUSE_SECONDS = 0.05


def _wait_readable(fd: int, timeout: float) -> bool:
    """Block up to ``timeout`` seconds until ``fd`` is readable (data, EOF or error)."""
    if hasattr(select, "poll"):  # no FD_SETSIZE limit, unlike select() in a descriptor-rich runner
        poller = select.poll()
        poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
        return bool(poller.poll(int(timeout * 1000)))
    ready, _, _ = select.select([fd], [], [], timeout)
    return bool(ready)


class _StderrDrain:
    """Drain a child's stderr on a daemon thread, recording only whether bytes arrived.

    Draining concurrently with ``Popen.wait`` keeps a chatty child from
    blocking on a full pipe until the timeout. Nothing is accumulated, so
    parent memory stays flat regardless of output volume. The reader waits
    with a bounded poll so that ``close()`` can normally retire it promptly.
    The descriptor is closed only after the thread has finished (or was never
    started), so a reused descriptor number is never touched. If the bounded
    join unexpectedly expires, the reader and descriptor are left alive and
    a warning is logged.
    """

    def __init__(self, stream: IO[bytes]) -> None:
        self._stream = stream
        self._fd = stream.fileno()
        self.seen = False
        self.eof = False
        self.failed = False
        self._stop = threading.Event()
        self._started = False
        self._thread = threading.Thread(target=self._run, name="unblock-check-stderr", daemon=True)

    def start(self) -> None:
        self._thread.start()
        self._started = True

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                if not _wait_readable(self._fd, _STDERR_POLL_SECONDS):
                    continue
                if self._stop.is_set():
                    return
                chunk = os.read(self._fd, 65536)
                if not chunk:
                    self.eof = True
                    return
                self.seen = True
        except (OSError, ValueError):
            self.failed = True

    def finished(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds and return True only for confirmed EOF."""
        if not self._started:
            return False
        self._thread.join(timeout)
        return self.eof

    def close(self) -> None:
        """Request reader retirement and close the stream if the bounded join succeeds."""
        self._stop.set()
        if self._started:
            self._thread.join(_STDERR_POLL_SECONDS * 20)
            if self._thread.is_alive():
                logger.warning("unblock_check stderr reader did not stop; leaving its descriptor open")
                return
        self._stream.close()


def _open_stderr_holders(root_pid: int, marker: str) -> tuple[list[_ProcessIdentity], bool]:
    """Live identities in the check's group or carrying its marker, plus completeness."""
    marked, complete = _marker_pids(marker)
    group = _process_group_pids(root_pid)
    identities = _process_identities()
    holders = {identity.pid: identity for identity in group}
    holders.update((pid, identities[pid]) for pid in marked if pid != root_pid and pid in identities)
    return list(holders.values()), complete


def _reject_open_stderr(root_pid: int, marker: str, drain: _StderrDrain) -> None:
    """Contain and kill whatever still holds the check's stderr after the root exited.

    The exited root remains unreaped, so its PID and process-group ID cannot be
    reused and group signals stay bound to the check's own processes. Order:
    SIGSTOP the group (a stopped process cannot fork); pass by pass, SIGSTOP
    every newly listed group member or marker carrier (an out-of-group
    escapee) until a pass lists nothing new; SIGKILL only identities still
    listed as ours, and SIGCONT a frozen process only while its recorded
    identity still matches; SIGKILL the group; then re-sweep and kill until
    nothing is listed. Finally require the reader to reach EOF, the only proof
    that no unlisted holder (e.g. one that scrubbed the marker and left the
    group) survived. Every shortfall is logged; the caller rejects the check
    regardless.
    """
    _signal_group(root_pid, signal.SIGSTOP)
    frozen: dict[int, _ProcessIdentity] = {}
    targets: list[_ProcessIdentity] = []
    enumeration_ok = True
    converged = False
    for _ in range(_KILL_TREE_MAX_PASSES):
        try:
            holders, complete = _open_stderr_holders(root_pid, marker)
        except _ProcessListingUnavailable:
            enumeration_ok = False
            break
        enumeration_ok = enumeration_ok and complete
        holder_map = {identity.pid: identity for identity in holders}
        new_identities = [identity for identity in holders if identity != frozen.get(identity.pid)]
        if not new_identities:
            converged = True
            targets = list(frozen.values())
            released = [identity for pid, identity in frozen.items() if holder_map.get(pid) != identity]
            for identity in released:
                _, inspected = _continue_same_identity(identity)
                enumeration_ok = enumeration_ok and inspected
                frozen.pop(identity.pid, None)
            targets = list(frozen.values())
            break
        stopped, inspected = _signal_owned_all(new_identities, root_pid, marker, signal.SIGSTOP)
        enumeration_ok = enumeration_ok and inspected
        frozen.update((identity.pid, identity) for identity in stopped)
    if not converged:
        targets = list(frozen.values())  # enumeration failed or a runaway forker: revalidate the frozen set
    if targets:
        _, inspected = _signal_owned_all(targets, root_pid, marker, signal.SIGKILL)
        enumeration_ok = enumeration_ok and inspected
    _signal_group(root_pid, signal.SIGKILL)
    strays: list[int] = []
    settled = False
    for _ in range(_KILL_TREE_MAX_PASSES):
        try:
            holders, complete = _open_stderr_holders(root_pid, marker)
        except _ProcessListingUnavailable:
            enumeration_ok = False
            break
        enumeration_ok = enumeration_ok and complete
        if not holders:
            settled = True
            break
        target_pids = {identity.pid for identity in targets}
        strays.extend(
            identity.pid for identity in holders if identity.pid not in target_pids and identity.pid not in strays
        )
        _, inspected = _signal_owned_all(holders, root_pid, marker, signal.SIGKILL)
        enumeration_ok = enumeration_ok and inspected
        _signal_group(root_pid, signal.SIGKILL)
        time.sleep(_RESWEEP_PAUSE_SECONDS)
    reader_eof = drain.finished(_STDERR_EOF_GRACE_SECONDS)
    logger.warning(
        "unblock_check completed with open stderr pipe; rejecting: killed_pids=%s stray_pids=%s "
        "enumeration_ok=%s converged=%s settled=%s reader_eof=%s",
        [identity.pid for identity in targets],
        strays,
        enumeration_ok,
        converged,
        settled,
        reader_eof,
    )


def _wait_unreaped(proc: subprocess.Popen, timeout: float) -> None:
    """Wait for ``proc`` to exit without reaping it, bounded by ``timeout``."""
    deadline = time.monotonic() + timeout
    flags = os.WEXITED | os.WNOWAIT | os.WNOHANG
    while True:
        waitid = getattr(os, "waitid", None)
        if waitid is not None:
            try:
                result = waitid(os.P_PID, proc.pid, flags)
            except InterruptedError:
                exited = False
            else:
                exited = result is not None and result.si_pid != 0
        elif sys.platform == "darwin":
            # Some otherwise supported CPython macOS builds omit os.waitid().
            # Darwin's siginfo_t stores si_pid at byte offset 12.
            info = ctypes.create_string_buffer(128)
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.waitid(os.P_PID, proc.pid, ctypes.byref(info), flags) != 0:
                error = ctypes.get_errno()
                if error == errno.EINTR:
                    exited = False
                else:
                    raise OSError(error, os.strerror(error))
            else:
                exited = ctypes.c_int.from_buffer(info, 12).value != 0
        else:
            raise NotImplementedError("waitid with WNOWAIT is required")
        if exited:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(proc.args, timeout)
        time.sleep(min(_WAITID_POLL_SECONDS, remaining))


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
    exit. A pipe still open shortly after exit (whatever the exit status)
    means a child retained it, so the process group and inherited marker are
    frozen, killed and re-swept (see ``_reject_open_stderr``) before failing
    closed; an uninspectable pipe also fails closed. On every exit path the
    reader is asked to retire and its pipe is closed when the bounded join
    succeeds. A child left running by a failure before exit observation is
    killed and reaped. Checks on that route must be stderr-silent and must
    never negate an observation command.
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
    drain: _StderrDrain | None = None
    try:
        try:
            if reject_stderr and proc.stderr is not None:
                drain = _StderrDrain(proc.stderr)
                drain.start()
            if not reject_stderr:
                return proc.wait(timeout=timeout)
            _wait_unreaped(proc, timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc, marker)
            proc.wait()
            raise
        except BaseException:
            if reject_stderr or proc.poll() is None:
                _kill_tree(proc, marker)
                proc.wait()
            raise
        if drain is None:
            proc.wait()
            return 1
        settled = drain.finished(_STDERR_EOF_GRACE_SECONDS)
        if not settled:
            _reject_open_stderr(proc.pid, marker, drain)
        returncode = proc.wait()
        if returncode != 0:
            return returncode
        if not settled:
            return 1
        return 0 if drain.eof and not drain.seen else 1
    finally:
        if drain is not None:
            drain.close()
        elif proc.stderr is not None:
            proc.stderr.close()


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
