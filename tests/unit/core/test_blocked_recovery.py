from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import IO
from unittest.mock import Mock, patch

import pytest

from core import blocked_recovery
from core._anima_heartbeat import HeartbeatMixin
from core.blocked_recovery import revalidate_blocked_tasks
from core.config.schemas import BackgroundTaskConfig
from core.memory.task_queue import TaskQueueManager
from core.time_utils import now_local


def _config(**overrides):
    defaults = {
        "blocked_recovery_enabled": True,
        "blocked_reprobe_after_hours": 6.0,
        "blocked_reprobe_batch_limit": 3,
        "blocked_max_reprobes": 4,
        "blocked_check_timeout_seconds": 60,
        "blocked_checkless_reprobe_enabled": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(background_task=SimpleNamespace(**defaults))


def _blocked_task(anima_dir: Path, *, task_id: str, meta: dict) -> TaskQueueManager:
    manager = TaskQueueManager(anima_dir)
    manager.add_task(
        source="human",
        original_instruction="finish the task",
        assignee=anima_dir.name,
        summary="finish task",
        task_id=task_id,
        meta=meta,
    )
    manager.update_status(task_id, "blocked", summary="waiting")
    return manager


def _stderr_pipe(data: bytes = b"", *, hold: bool = False) -> tuple[IO[bytes], int | None]:
    """A real stderr pipe for a mocked Popen: ``data`` then EOF, or kept open (writer fd returned) when ``hold``."""
    reader, writer = os.pipe()
    if data:
        os.write(writer, data)
    stream = open(reader, "rb", buffering=0)  # noqa: SIM115 - closed by _run_sandboxed
    if hold:
        return stream, writer
    os.close(writer)
    return stream, None


def _heartbeat(anima_dir: Path) -> HeartbeatMixin:
    heartbeat = HeartbeatMixin()
    heartbeat.anima_dir = anima_dir
    heartbeat.name = anima_dir.name
    heartbeat.memory = SimpleNamespace(read_heartbeat_config=lambda: "checklist")
    heartbeat._build_state_cleanup_instruction = lambda: None
    heartbeat._build_background_context_parts = lambda: []
    return heartbeat


def test_check_success_republishes_without_consuming_retry(tmp_path: Path) -> None:
    anima_dir = tmp_path / "animas" / "worker"
    manager = _blocked_task(
        anima_dir,
        task_id="check-pass",
        meta={
            "unblock_check": "test -w .",
            "retry_count": 3,
        },
    )

    with (
        patch("core.config.models.load_config", return_value=_config()),
        patch("core.blocked_recovery._sandbox_route", return_value="bwrap"),
        patch("core.blocked_recovery._run_sandboxed", return_value=0) as run,
    ):
        result = revalidate_blocked_tasks(anima_dir, "worker")

    assert result == ["check-pass"]
    current = manager.get_task_by_id("check-pass")
    assert current is not None
    assert current.status == "pending"
    assert current.meta["retry_count"] == 3
    pending = anima_dir / "state" / "pending" / "check-pass.json"
    assert json.loads(pending.read_text(encoding="utf-8"))["description"] == "finish the task"
    assert run.call_args.args[0] == [
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
        "/bin/sh",
        "-c",
        blocked_recovery._SH_WRAPPER,
        "unblock_check",
        "60",
        "test -w .",
    ]
    kwargs = run.call_args.kwargs
    assert kwargs["cwd"] == anima_dir
    assert kwargs["timeout"] == 60
    assert kwargs["reject_stderr"] is False
    assert set(kwargs["env"]) == {"PATH", "HOME", "ANIMAWORKS_ANIMA_DIR", "ANIMAWORKS_UNBLOCK_CHECK_ID"}
    assert kwargs["env"]["ANIMAWORKS_ANIMA_DIR"] == str(anima_dir)
    assert kwargs["env"]["ANIMAWORKS_UNBLOCK_CHECK_ID"] == kwargs["marker"]
    assert kwargs["marker"].startswith("check-pass:")
    events = _activity_events(anima_dir)
    assert events[0]["meta"] == {"task_id": "check-pass", "method": "check", "sandbox": "bwrap"}


def _activity_events(anima_dir: Path) -> list[dict]:
    files = list((anima_dir / "activity_log").glob("*.jsonl"))
    return [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]


def test_macos_sandbox_exec_fallback_when_bwrap_missing(tmp_path: Path) -> None:
    """No bwrap + macOS sandbox-exec: run the check through a fixed Seatbelt profile."""
    anima_dir = tmp_path / "animas" / "worker"
    manager = _blocked_task(
        anima_dir,
        task_id="mac-pass",
        meta={"unblock_check": "test -w .", "task_desc": {"title": "finish"}},
    )

    with (
        patch("core.config.models.load_config", return_value=_config()),
        patch("core.blocked_recovery.shutil.which", return_value=None),
        patch("core.blocked_recovery.sys.platform", "darwin"),
        patch("core.blocked_recovery.os.access", return_value=True) as access,
        patch("core.blocked_recovery._run_sandboxed", return_value=0) as run,
    ):
        assert revalidate_blocked_tasks(anima_dir, "worker") == ["mac-pass"]

    access.assert_called_once_with("/usr/bin/sandbox-exec", os.X_OK)
    argv = run.call_args.args[0]
    assert argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
    assert argv[3:] == ["/bin/sh", "-c", blocked_recovery._SH_WRAPPER, "unblock_check", "60", "test -w ."]
    profile = argv[2]
    assert profile is blocked_recovery._MACOS_SANDBOX_PROFILE
    assert "test -w ." not in profile
    kwargs = run.call_args.kwargs
    assert kwargs["cwd"] == anima_dir
    assert kwargs["timeout"] == 60
    assert kwargs["reject_stderr"] is True
    assert set(kwargs["env"]) == {"PATH", "HOME", "ANIMAWORKS_ANIMA_DIR", "ANIMAWORKS_UNBLOCK_CHECK_ID"}
    assert manager.get_task_by_id("mac-pass").status == "pending"
    assert _activity_events(anima_dir)[0]["meta"]["sandbox"] == "sandbox-exec"


def test_macos_profile_is_static_read_only_and_network_denied() -> None:
    profile = blocked_recovery._MACOS_SANDBOX_PROFILE
    lines = [line.strip() for line in profile.splitlines() if line.strip()]
    assert lines[0] == "(version 1)"
    assert lines[1] == "(deny default)"
    assert "(deny network*)" in lines
    assert '(allow file-write* (literal "/dev/null"))' in lines
    assert not any(line.startswith("(allow file-write*") and "/dev/null" not in line for line in lines)
    assert not any(line.startswith("(allow network") for line in lines)
    # The check string is never interpolated: a hostile check cannot alter the profile.
    hostile = '") (allow default) (; echo pwned #'
    argv = blocked_recovery._sandbox_argv("sandbox-exec", hostile, cpu_seconds=60)
    assert argv[2] == profile
    assert argv[-1] == hostile
    # The wrapper script is a constant; the check only ever appears as the trailing positional argument.
    assert argv[-4] == blocked_recovery._SH_WRAPPER
    assert hostile not in blocked_recovery._SH_WRAPPER


def test_bwrap_preferred_over_sandbox_exec() -> None:
    with (
        patch("core.blocked_recovery.shutil.which", return_value="/usr/bin/bwrap"),
        patch("core.blocked_recovery.sys.platform", "darwin"),
        patch("core.blocked_recovery.os.access", return_value=True),
    ):
        assert blocked_recovery._sandbox_route() == "bwrap"


def test_sandbox_exec_not_used_outside_darwin() -> None:
    with (
        patch("core.blocked_recovery.shutil.which", return_value=None),
        patch("core.blocked_recovery.sys.platform", "linux"),
        patch("core.blocked_recovery.os.access", return_value=True),
    ):
        assert blocked_recovery._sandbox_route() is None


def test_no_sandbox_available_fails_closed_without_running_check(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    anima_dir = tmp_path / "animas" / "worker"
    manager = _blocked_task(
        anima_dir,
        task_id="no-sandbox",
        meta={"unblock_check": "touch escaped", "task_desc": {"title": "finish"}},
    )

    with (
        patch("core.config.models.load_config", return_value=_config()),
        patch("core.blocked_recovery._sandbox_route", return_value=None),
        patch("core.blocked_recovery.subprocess.Popen") as popen,
        caplog.at_level("WARNING", logger="animaworks.blocked_recovery"),
    ):
        assert revalidate_blocked_tasks(anima_dir, "worker") == []

    popen.assert_not_called()
    current = manager.get_task_by_id("no-sandbox")
    assert current is not None
    assert current.status == "blocked"
    assert current.meta["unblock_check_failures"] == 1
    assert not (anima_dir / "escaped").exists()
    assert "sandbox unavailable" in caplog.text


def test_run_sandboxed_kills_descendants_and_process_group_on_timeout() -> None:
    proc = Mock()
    proc.pid = 4242
    proc.wait.side_effect = [subprocess.TimeoutExpired("sh", 60), -9]
    proc.stderr, _ = _stderr_pipe()

    # Pass 1 sees two descendants; pass 2 sees one more forked meanwhile; pass 3 is stable.
    snapshots = [[4300, 4301], [4300, 4301, 4302], [4300, 4301, 4302]]
    with (
        patch("core.blocked_recovery.subprocess.Popen", return_value=proc) as popen,
        patch("core.blocked_recovery._descendant_pids", side_effect=snapshots) as descendants,
        patch("core.blocked_recovery._marker_pids", return_value=([], True)),
        patch("core.blocked_recovery.os.kill") as kill,
        patch("core.blocked_recovery.os.killpg") as killpg,
        pytest.raises(subprocess.TimeoutExpired),
    ):
        blocked_recovery._run_sandboxed(
            ["/bin/sh", "-c", "sleep 99"], cwd=Path("/"), env={}, timeout=60, marker="m", reject_stderr=True
        )

    kwargs = popen.call_args.kwargs
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.PIPE
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["env"] == {}
    assert descendants.call_count == 3
    stop, kill_sig = blocked_recovery.signal.SIGSTOP, blocked_recovery.signal.SIGKILL
    assert [c.args for c in kill.call_args_list] == [
        (4300, stop),
        (4301, stop),
        (4302, stop),
        (4300, kill_sig),
        (4301, kill_sig),
        (4302, kill_sig),
    ]
    assert [c.args for c in killpg.call_args_list] == [(4242, stop), (4242, kill_sig)]
    proc.kill.assert_called_once_with()
    assert proc.wait.call_count == 2
    assert proc.stderr.closed
    assert not any(t.name == "unblock-check-stderr" for t in threading.enumerate())


def test_kill_tree_freezes_root_group_before_first_snapshot() -> None:
    proc = Mock()
    proc.pid = 77
    order: list[str] = []
    with (
        patch("core.blocked_recovery._descendant_pids", side_effect=lambda _pid: order.append("snapshot") or []),
        patch("core.blocked_recovery._marker_pids", side_effect=lambda _m: order.append("marker") or ([], True)),
        patch("core.blocked_recovery.os.killpg", side_effect=lambda _pid, sig: order.append(f"killpg:{int(sig)}")),
    ):
        blocked_recovery._kill_tree(proc, "m")
    stop, kill_sig = int(blocked_recovery.signal.SIGSTOP), int(blocked_recovery.signal.SIGKILL)
    assert order == [f"killpg:{stop}", "marker", "snapshot", f"killpg:{kill_sig}", "marker"]


def test_kill_tree_marker_sweep_catches_reparented_daemon() -> None:
    """A daemon that left the tree (not a descendant) but carries the marker is frozen and killed."""
    proc = Mock()
    proc.pid = 77
    marker_snapshots = [([900], True), ([900], True), ([], True)]
    with (
        patch("core.blocked_recovery._descendant_pids", return_value=[]),
        patch("core.blocked_recovery._marker_pids", side_effect=marker_snapshots),
        patch("core.blocked_recovery.os.kill") as kill,
        patch("core.blocked_recovery.os.killpg"),
    ):
        blocked_recovery._kill_tree(proc, "m")
    stop, kill_sig = blocked_recovery.signal.SIGSTOP, blocked_recovery.signal.SIGKILL
    assert [c.args for c in kill.call_args_list] == [(900, stop), (900, kill_sig)]


def test_kill_tree_post_kill_sweep_kills_and_warns_on_strays(caplog: pytest.LogCaptureFixture) -> None:
    proc = Mock()
    proc.pid = 77
    with (
        patch("core.blocked_recovery._descendant_pids", return_value=[]),
        patch("core.blocked_recovery._marker_pids", side_effect=[([], True), ([901], True)]),
        patch("core.blocked_recovery.os.kill") as kill,
        patch("core.blocked_recovery.os.killpg"),
        caplog.at_level("WARNING", logger="animaworks.blocked_recovery"),
    ):
        blocked_recovery._kill_tree(proc, "m")
    assert (901, blocked_recovery.signal.SIGKILL) in [c.args for c in kill.call_args_list]
    assert "cleanup incomplete" in caplog.text
    assert "stray_pids=[901]" in caplog.text


def test_kill_tree_ps_unavailable_still_kills_group_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    """Fail closed but visible: without process enumeration, the group is killed and a warning is logged."""
    proc = Mock()
    proc.pid = 77
    with (
        patch("core.blocked_recovery._descendant_pids", side_effect=blocked_recovery._ProcessListingUnavailable("ps")),
        patch("core.blocked_recovery._marker_pids", side_effect=blocked_recovery._ProcessListingUnavailable("ps")),
        patch("core.blocked_recovery.os.killpg") as killpg,
        caplog.at_level("WARNING", logger="animaworks.blocked_recovery"),
    ):
        blocked_recovery._kill_tree(proc, "m")
    assert [c.args for c in killpg.call_args_list] == [
        (77, blocked_recovery.signal.SIGSTOP),
        (77, blocked_recovery.signal.SIGKILL),
    ]
    proc.kill.assert_called_once_with()
    assert "cleanup incomplete" in caplog.text
    assert "enumeration_ok=False" in caplog.text


def test_ps_lines_retries_then_raises() -> None:
    with (
        patch("core.blocked_recovery.subprocess.run", side_effect=FileNotFoundError("ps")) as run,
        patch("core.blocked_recovery.time.sleep"),
        pytest.raises(blocked_recovery._ProcessListingUnavailable),
    ):
        blocked_recovery._ps_lines(["-axo", "pid=,ppid="])
    assert run.call_count == blocked_recovery._PS_RETRIES


def test_marker_pids_parses_ps_env_output_and_skips_zombies_and_self() -> None:
    listing = (
        "500 S    python3 -c x ANIMAWORKS_UNBLOCK_CHECK_ID=m1 PATH=/bin\n"
        "501 Z    (python3) ANIMAWORKS_UNBLOCK_CHECK_ID=m1\n"
        "502 S    sleep 5 ANIMAWORKS_UNBLOCK_CHECK_ID=other\n"
        f"{os.getpid()} S    pytest ANIMAWORKS_UNBLOCK_CHECK_ID=m1\n"
    )
    with (
        patch("core.blocked_recovery.sys.platform", "darwin"),
        patch("core.blocked_recovery._ps_lines", return_value=listing.splitlines()) as ps,
    ):
        assert blocked_recovery._marker_pids("m1") == ([500], True)
    ps.assert_called_once_with(["-axEo", "pid=,stat=,command="])


def test_marker_pids_linux_reports_incomplete_when_environ_unreadable(tmp_path: Path) -> None:
    """A non-dumpable daemon hides its environ (EACCES); that must count as incomplete, not 'no marker'."""
    fake_proc = tmp_path / "proc"
    (fake_proc / "600").mkdir(parents=True)
    (fake_proc / "600" / "environ").write_bytes(b"ANIMAWORKS_UNBLOCK_CHECK_ID=m1\0PATH=/bin\0")
    (fake_proc / "601").mkdir()
    (fake_proc / "601" / "environ").write_bytes(b"OTHER=1\0")
    (fake_proc / "602").mkdir()  # environ unreadable
    (fake_proc / "notapid").mkdir()
    real_read_bytes = Path.read_bytes

    def read_bytes(self: Path) -> bytes:
        if self.parent.name == "602":
            raise PermissionError(13, "Permission denied")
        return real_read_bytes(self)

    with (
        patch("core.blocked_recovery.sys.platform", "linux"),
        patch("core.blocked_recovery.Path", side_effect=lambda p: fake_proc if p == "/proc" else Path(p)),
        patch.object(Path, "read_bytes", read_bytes),
    ):
        assert blocked_recovery._marker_pids("m1") == ([600], False)


def test_kill_tree_incomplete_marker_enumeration_warns(caplog: pytest.LogCaptureFixture) -> None:
    proc = Mock()
    proc.pid = 77
    with (
        patch("core.blocked_recovery._descendant_pids", return_value=[]),
        patch("core.blocked_recovery._marker_pids", return_value=([], False)),
        patch("core.blocked_recovery.os.killpg"),
        caplog.at_level("WARNING", logger="animaworks.blocked_recovery"),
    ):
        blocked_recovery._kill_tree(proc, "m")
    assert "cleanup incomplete" in caplog.text
    assert "enumeration_ok=False" in caplog.text


def test_kill_tree_pass_limit_bounds_a_runaway_forker() -> None:
    proc = Mock()
    proc.pid = 1
    counter = iter(range(10, 10_000))
    with (
        patch("core.blocked_recovery._descendant_pids", side_effect=lambda _pid: [next(counter)]) as descendants,
        patch("core.blocked_recovery._marker_pids", return_value=([], True)),
        patch("core.blocked_recovery.os.kill"),
        patch("core.blocked_recovery.os.killpg") as killpg,
    ):
        blocked_recovery._kill_tree(proc, "m")
    assert descendants.call_count == blocked_recovery._KILL_TREE_MAX_PASSES
    assert killpg.call_count == 2


def test_descendant_pids_walks_ps_tree() -> None:
    listing = "1 0\n100 1\n200 100\n300 200\n301 200\n400 1\n"
    with patch(
        "core.blocked_recovery.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, stdout=listing),
    ) as run:
        assert blocked_recovery._descendant_pids(100) == [200, 300, 301]
    assert run.call_args.args[0] == ["ps", "-axo", "pid=,ppid="]
    assert run.call_args.kwargs["stdin"] is subprocess.DEVNULL


def test_descendant_pids_raises_when_ps_unavailable() -> None:
    with (
        patch("core.blocked_recovery.subprocess.run", side_effect=FileNotFoundError("ps")),
        patch("core.blocked_recovery.time.sleep"),
        pytest.raises(blocked_recovery._ProcessListingUnavailable),
    ):
        blocked_recovery._descendant_pids(1)


def _live_survivors(marker: str) -> list[str]:
    """PIDs whose argv contains ``marker`` and that are not zombies (killed but not yet reaped)."""
    import time

    for _ in range(40):  # up to ~2s for PID 1 to reap zombies of killed parents
        listing = subprocess.run(
            ["ps", "-axo", "pid=,stat=,command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
        ).stdout
        live = [
            line.split()[0]
            for line in listing.splitlines()
            if marker in line and "python" in line and not line.split()[1].startswith("Z")
        ]
        zombies = [line for line in listing.splitlines() if marker in line and line.split()[1].startswith("Z")]
        if not zombies:
            return live
        time.sleep(0.05)
    return live


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_real_timeout_kills_continuously_forking_setsid_escapees() -> None:
    """Integration: a parent that keeps forking setsid() children during cleanup leaves no survivor."""
    marker = f"aw_unblock_forker_{os.getpid()}"
    forker = (
        "import os, time\n"
        "while True:\n"
        "    pid = os.fork()\n"
        "    if pid == 0:\n"
        "        os.setsid(); time.sleep(120); os._exit(0)\n"
        "    time.sleep(0.005)\n"
        f"# {marker}"
    )
    argv = ["/bin/sh", "-c", f"exec python3 -c '{forker}'"]
    env = {"PATH": os.environ.get("PATH", ""), blocked_recovery._CHECK_MARKER_ENV: marker}
    with pytest.raises(subprocess.TimeoutExpired):
        blocked_recovery._run_sandboxed(argv, cwd=Path("/"), env=env, timeout=1, marker=marker)
    survivors = _live_survivors(marker)
    for pid in survivors:
        try:
            os.kill(int(pid), blocked_recovery.signal.SIGKILL)
        except (ProcessLookupError, PermissionError, ValueError):
            pass
    assert survivors == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_real_timeout_kills_setsid_escapee() -> None:
    """Integration: a descendant that setsid()s out of the group must not survive the timeout."""
    marker = f"aw_unblock_escapee_{os.getpid()}"
    escapee = f"import os, time; os.setsid(); time.sleep(120)  # {marker}"
    argv = ["/bin/sh", "-c", f"python3 -c '{escapee}' & sleep 120"]
    env = {"PATH": os.environ.get("PATH", ""), blocked_recovery._CHECK_MARKER_ENV: marker}
    with pytest.raises(subprocess.TimeoutExpired):
        blocked_recovery._run_sandboxed(argv, cwd=Path("/"), env=env, timeout=2, marker=marker)


@pytest.mark.skipif(sys.platform != "darwin", reason="marker sweep via ps -E is darwin-specific here")
def test_real_timeout_kills_double_forked_daemon_via_marker() -> None:
    """Integration (deterministic): a daemon already reparented to PID 1 before the timeout still dies."""
    marker = f"aw_unblock_daemon_{os.getpid()}"
    daemon = (
        "import os, time\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    if os.fork() == 0:\n"
        "        time.sleep(120)\n"
        "    os._exit(0)\n"
        "os.wait(); time.sleep(120)\n"
        f"# {marker}"
    )
    argv = ["/bin/sh", "-c", f"exec python3 -c '{daemon}'"]
    env = {"PATH": os.environ.get("PATH", ""), blocked_recovery._CHECK_MARKER_ENV: marker}
    with pytest.raises(subprocess.TimeoutExpired):
        blocked_recovery._run_sandboxed(argv, cwd=Path("/"), env=env, timeout=2, marker=marker)
    survivors = _live_survivors(marker)
    for pid in survivors:
        try:
            os.kill(int(pid), blocked_recovery.signal.SIGKILL)
        except (ProcessLookupError, PermissionError, ValueError):
            pass
    assert survivors == []
    survivors = _live_survivors(marker)
    for pid in survivors:  # never leave a stray sleeper behind even if the assertion fails
        try:
            os.kill(int(pid), blocked_recovery.signal.SIGKILL)
        except (ProcessLookupError, PermissionError, ValueError):
            pass
    assert survivors == []


def test_run_sandboxed_returns_exit_code() -> None:
    proc = Mock()
    proc.wait.return_value = 3
    proc.stderr = None
    with patch("core.blocked_recovery.subprocess.Popen", return_value=proc) as popen:
        assert blocked_recovery._run_sandboxed(["/bin/true"], cwd=Path("/"), env={}, timeout=5, marker="m") == 3
    assert popen.call_args.kwargs["stderr"] is subprocess.DEVNULL


def test_run_sandboxed_default_route_keeps_exit_status_contract() -> None:
    """bwrap route: stderr is discarded and a zero exit is accepted as before."""
    proc = Mock()
    proc.wait.return_value = 0
    proc.stderr = None
    with patch("core.blocked_recovery.subprocess.Popen", return_value=proc) as popen:
        assert blocked_recovery._run_sandboxed(["/bin/true"], cwd=Path("/"), env={}, timeout=5, marker="m") == 0
    assert popen.call_args.kwargs["stderr"] is subprocess.DEVNULL


def test_run_sandboxed_fails_closed_when_zero_exit_writes_stderr() -> None:
    proc = Mock()
    proc.wait.return_value = 0
    proc.stderr, _ = _stderr_pipe(b"Operation not permitted")
    with patch("core.blocked_recovery.subprocess.Popen", return_value=proc) as popen:
        assert (
            blocked_recovery._run_sandboxed(
                ["/bin/true"], cwd=Path("/"), env={}, timeout=5, marker="m", reject_stderr=True
            )
            == 1
        )
    assert popen.call_args.kwargs["stderr"] is subprocess.PIPE
    assert proc.stderr.closed


def test_run_sandboxed_accepts_quiet_zero_exit_without_marked_child() -> None:
    proc = Mock()
    proc.wait.return_value = 0
    proc.stderr, _ = _stderr_pipe()
    with patch("core.blocked_recovery.subprocess.Popen", return_value=proc):
        assert (
            blocked_recovery._run_sandboxed(
                ["/bin/true"], cwd=Path("/"), env={}, timeout=5, marker="m", reject_stderr=True
            )
            == 0
        )
    assert proc.stderr.closed


def test_run_sandboxed_fails_closed_when_stderr_read_fails() -> None:
    proc = Mock()
    proc.wait.return_value = 0
    proc.stderr, _ = _stderr_pipe(b"x")
    with (
        patch("core.blocked_recovery.subprocess.Popen", return_value=proc),
        patch("core.blocked_recovery.os.read", side_effect=OSError("bad pipe")),
    ):
        assert (
            blocked_recovery._run_sandboxed(
                ["/bin/true"], cwd=Path("/"), env={}, timeout=5, marker="m", reject_stderr=True
            )
            == 1
        )


def test_run_sandboxed_sweeps_and_rejects_when_pipe_stays_open_after_zero_exit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stray holding stderr past the grace period is frozen, killed (group + marker) and the check is rejected."""
    proc = Mock()
    proc.pid = 4242
    proc.wait.return_value = 0
    proc.stderr, writer = _stderr_pipe(hold=True)
    try:
        with (
            patch("core.blocked_recovery.subprocess.Popen", return_value=proc),
            patch("core.blocked_recovery._STDERR_EOF_GRACE_SECONDS", 0.05),
            patch("core.blocked_recovery._process_group_pids", side_effect=[[4300], [4300], []]) as group,
            patch("core.blocked_recovery._marker_pids", side_effect=[([4301], True), ([4301], True), ([], True)]),
            patch("core.blocked_recovery._signal_all") as signal_all,
            patch("core.blocked_recovery.os.killpg") as killpg,
            caplog.at_level("WARNING", logger="animaworks.blocked_recovery"),
        ):
            assert (
                blocked_recovery._run_sandboxed(
                    ["/bin/true"], cwd=Path("/"), env={}, timeout=5, marker="m", reject_stderr=True
                )
                == 1
            )
    finally:
        os.close(writer)
    stop, kill_sig = blocked_recovery.signal.SIGSTOP, blocked_recovery.signal.SIGKILL
    assert group.call_args_list[0].args == (4242,)
    assert [c.args for c in signal_all.call_args_list] == [([4300, 4301], stop), ([4300, 4301], kill_sig)]
    assert [c.args for c in killpg.call_args_list] == [(4242, stop), (4242, kill_sig)]
    assert "open stderr pipe" in caplog.text
    assert "reader_eof=False" in caplog.text  # the test itself still held the writer
    assert proc.stderr.closed
    assert not any(t.name == "unblock-check-stderr" for t in threading.enumerate())


def test_reject_open_stderr_freezes_group_first_and_resweeps_fork_race(caplog: pytest.LogCaptureFixture) -> None:
    """Group SIGSTOP precedes any listing; a carrier forked meanwhile is frozen; a post-kill survivor is re-swept."""
    order: list[object] = []
    stream, writer = _stderr_pipe(hold=True)
    drain = blocked_recovery._StderrDrain(stream)
    drain.start()
    group_snapshots = iter([[4300], [4300], [4300], [], []])
    marker_snapshots = iter([([4301], True), ([4301, 4302], True), ([4301, 4302], True), ([4303], True), ([], True)])

    def _list_group(_pid: int) -> list[int]:
        order.append("list")
        return next(group_snapshots)

    def _signal_all(pids: list[int], sig: int) -> None:
        order.append((tuple(pids), int(sig)))
        if int(sig) == int(blocked_recovery.signal.SIGKILL) and 4303 in pids:
            os.close(writer)  # the last holder died: the pipe reaches EOF

    try:
        with (
            patch("core.blocked_recovery._process_group_pids", side_effect=_list_group),
            patch("core.blocked_recovery._marker_pids", side_effect=lambda _m: next(marker_snapshots)),
            patch("core.blocked_recovery._signal_all", side_effect=_signal_all),
            patch("core.blocked_recovery.os.killpg", side_effect=lambda pid, sig: order.append(f"killpg:{int(sig)}")),
            patch("core.blocked_recovery._RESWEEP_PAUSE_SECONDS", 0.0),
            caplog.at_level("WARNING", logger="animaworks.blocked_recovery"),
        ):
            blocked_recovery._reject_open_stderr(4242, "m", drain)
    finally:
        drain.close()
    stop, kill_sig = int(blocked_recovery.signal.SIGSTOP), int(blocked_recovery.signal.SIGKILL)
    assert order == [
        f"killpg:{stop}",
        "list",
        ((4300, 4301), stop),
        "list",
        ((4302,), stop),
        "list",
        ((4300, 4301, 4302), kill_sig),
        f"killpg:{kill_sig}",
        "list",
        ((4303,), kill_sig),
        f"killpg:{kill_sig}",
        "list",
    ]
    assert "stray_pids=[4303]" in caplog.text
    assert "converged=True settled=True reader_eof=True" in caplog.text
    assert drain.eof


def test_reject_open_stderr_does_not_kill_a_frozen_pid_that_left_the_listing() -> None:
    """A PID that vanished between listing and freeze (exited or reused) is continued, never killed."""
    stream, _ = _stderr_pipe()
    drain = blocked_recovery._StderrDrain(stream)
    drain.start()
    try:
        with (
            patch("core.blocked_recovery._process_group_pids", side_effect=[[4300, 4301], [4300], []]),
            patch("core.blocked_recovery._marker_pids", return_value=([], True)),
            patch("core.blocked_recovery._signal_all") as signal_all,
            patch("core.blocked_recovery.os.killpg"),
        ):
            blocked_recovery._reject_open_stderr(4242, "m", drain)
    finally:
        drain.close()
    stop, cont, kill_sig = (
        blocked_recovery.signal.SIGSTOP,
        blocked_recovery.signal.SIGCONT,
        blocked_recovery.signal.SIGKILL,
    )
    assert [c.args for c in signal_all.call_args_list] == [([4300, 4301], stop), ([4301], cont), ([4300], kill_sig)]


def test_reject_open_stderr_ps_unavailable_still_kills_frozen_and_group_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stream, _ = _stderr_pipe()
    drain = blocked_recovery._StderrDrain(stream)
    drain.start()
    try:
        with (
            patch(
                "core.blocked_recovery._process_group_pids",
                side_effect=[[4300], *([blocked_recovery._ProcessListingUnavailable("ps")] * 2)],
            ),
            patch("core.blocked_recovery._marker_pids", return_value=([], True)),
            patch("core.blocked_recovery._signal_all") as signal_all,
            patch("core.blocked_recovery.os.killpg") as killpg,
            caplog.at_level("WARNING", logger="animaworks.blocked_recovery"),
        ):
            blocked_recovery._reject_open_stderr(4242, "m", drain)
    finally:
        drain.close()
    stop, kill_sig = blocked_recovery.signal.SIGSTOP, blocked_recovery.signal.SIGKILL
    assert [c.args for c in signal_all.call_args_list] == [([4300], stop), ([4300], kill_sig)]
    assert [c.args for c in killpg.call_args_list] == [(4242, stop), (4242, kill_sig)]
    assert "enumeration_ok=False converged=False" in caplog.text


def test_run_sandboxed_reaps_child_when_stderr_reader_cannot_start() -> None:
    """Thread.start failure after Popen must not leave the child running or the pipe open."""
    proc = Mock()
    proc.pid = 4242
    proc.poll.return_value = None
    proc.stderr, _ = _stderr_pipe()
    with (
        patch("core.blocked_recovery.subprocess.Popen", return_value=proc),
        patch("core.blocked_recovery._kill_tree") as kill_tree,
        patch.object(threading.Thread, "start", side_effect=RuntimeError("can't start new thread")),
        pytest.raises(RuntimeError, match="start new thread"),
    ):
        blocked_recovery._run_sandboxed(["/bin/true"], cwd=Path("/"), env={}, timeout=5, marker="m", reject_stderr=True)
    kill_tree.assert_called_once_with(proc, "m")
    proc.wait.assert_called_once_with()
    assert proc.stderr.closed


def test_stderr_drain_close_retires_reader_while_pipe_is_still_held() -> None:
    """close() stops the reader without waiting for EOF and only then closes the descriptor."""
    stream, writer = _stderr_pipe(hold=True)
    drain = blocked_recovery._StderrDrain(stream)
    drain.start()
    try:
        assert not drain.finished(0.05)
        drain.close()
        assert not any(t.name == "unblock-check-stderr" for t in threading.enumerate())
        assert stream.closed
        assert not drain.eof and not drain.failed
    finally:
        os.close(writer)


def test_process_group_pids_selects_only_the_requested_group() -> None:
    listing = ["41 12 S", "42 77 S", "43 77 Z", "44 77 R+", "bad", "45 nope S", "77 77 S"]
    with patch("core.blocked_recovery._ps_lines", return_value=listing) as ps:
        assert blocked_recovery._process_group_pids(77) == [42, 44]
    ps.assert_called_once_with(["-axo", "pid=,pgid=,stat="])


@pytest.mark.skipif(
    sys.platform != "darwin" or not os.access("/usr/bin/sandbox-exec", os.X_OK),
    reason="requires macOS sandbox-exec",
)
@pytest.mark.parametrize(
    ("check", "expected"),
    [
        ("exit 0", 0),
        ("exit 7", 7),
        ("test -r . && true >/dev/null", 0),
        ("touch escaped", 1),
        ("mkdir escaped_dir", 1),
        ("python3 -c \"import socket; socket.create_connection(('127.0.0.1', 22), 1)\"", 1),
        ('python3 -c "while True: pass"', 1),  # killed by the inherited CPU rlimit, not the timeout
    ],
)
def test_real_sandbox_exec_denies_writes_and_network(tmp_path: Path, check: str, expected: int) -> None:
    """Integration: the shipped profile really blocks writes/network and propagates exit codes."""
    marker = f"real-{os.getpid()}"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "ANIMAWORKS_ANIMA_DIR": str(tmp_path),
        blocked_recovery._CHECK_MARKER_ENV: marker,
    }
    code = blocked_recovery._run_sandboxed(
        blocked_recovery._sandbox_argv("sandbox-exec", check, cpu_seconds=1),
        cwd=tmp_path,
        env=env,
        timeout=30,
        marker=marker,
        reject_stderr=True,
    )
    assert (code == 0) == (expected == 0)
    if expected not in (0, 1):
        assert code == expected
    assert not (tmp_path / "escaped").exists()
    assert not (tmp_path / "escaped_dir").exists()


@pytest.mark.skipif(
    sys.platform != "darwin" or not os.access("/usr/bin/sandbox-exec", os.X_OK),
    reason="requires macOS sandbox-exec",
)
def test_real_sandbox_exec_fails_closed_when_negated_ps_is_denied(tmp_path: Path) -> None:
    """A denied process listing must not become success through shell negation."""
    marker = f"stderr-guard-{os.getpid()}"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "ANIMAWORKS_ANIMA_DIR": str(tmp_path),
        blocked_recovery._CHECK_MARKER_ENV: marker,
    }
    code = blocked_recovery._run_sandboxed(
        blocked_recovery._sandbox_argv("sandbox-exec", "! /bin/ps -axo pid= >/dev/null", cpu_seconds=1),
        cwd=tmp_path,
        env=env,
        timeout=30,
        marker=marker,
        reject_stderr=True,
    )
    assert code == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_run_sandboxed_fails_closed_and_kills_background_stderr_holder_after_direct_success(tmp_path: Path) -> None:
    """A silent child retaining stderr cannot bypass the completed-parent timeout."""
    marker = f"stderr-holder-{os.getpid()}"
    env = {"PATH": os.environ.get("PATH", ""), blocked_recovery._CHECK_MARKER_ENV: marker}
    pid_file = tmp_path / "child.pid"
    child_pid: int | None = None
    try:
        assert (
            blocked_recovery._run_sandboxed(
                ["/bin/sh", "-c", 'sleep 120 & printf "%s" "$!" > child.pid; exit 0'],
                cwd=tmp_path,
                env=env,
                timeout=2,
                marker=marker,
                reject_stderr=True,
            )
            == 1
        )
    finally:
        if pid_file.exists():
            child_pid = int(pid_file.read_text(encoding="utf-8"))
        if child_pid is not None:
            try:
                os.kill(child_pid, blocked_recovery.signal.SIGKILL)
            except (ProcessLookupError, PermissionError, ValueError):
                pass
    if child_pid is not None:
        for _ in range(40):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"background child {child_pid} survived")


def _wait_group_empty(root_pid: int) -> list[int]:
    for _ in range(40):
        left = blocked_recovery._process_group_pids(root_pid)
        if not left:
            return []
        time.sleep(0.05)
    return left


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_run_sandboxed_nonzero_exit_with_silent_stderr_holder_leaves_no_reader_or_child(tmp_path: Path) -> None:
    """A failing check with a silent background stderr holder must not leak the reader thread or the holder."""
    marker = f"stderr-holder-nonzero-{os.getpid()}"
    env = {"PATH": os.environ.get("PATH", ""), blocked_recovery._CHECK_MARKER_ENV: marker}
    roots: list[int] = []
    real_popen = subprocess.Popen

    def _capture(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        roots.append(proc.pid)
        return proc

    started = time.monotonic()
    try:
        with patch("core.blocked_recovery.subprocess.Popen", side_effect=_capture):
            code = blocked_recovery._run_sandboxed(
                ["/bin/sh", "-c", "sleep 120 & exit 3"],
                cwd=tmp_path,
                env=env,
                timeout=10,
                marker=marker,
                reject_stderr=True,
            )
        elapsed = time.monotonic() - started
        assert code == 3
        assert elapsed < 8, f"nonzero exit with holder took {elapsed:.1f}s"
        assert not any(t.name == "unblock-check-stderr" for t in threading.enumerate())
        assert _wait_group_empty(roots[0]) == []
    finally:
        for root in roots:
            try:
                os.killpg(root, blocked_recovery.signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_run_sandboxed_kills_forking_stderr_holder_group_after_zero_exit(tmp_path: Path) -> None:
    """A holder that keeps forking new stderr holders during cleanup is frozen first, so none survives."""
    marker = f"stderr-forker-{os.getpid()}"
    env = {"PATH": os.environ.get("PATH", ""), blocked_recovery._CHECK_MARKER_ENV: marker}
    roots: list[int] = []
    real_popen = subprocess.Popen

    def _capture(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        roots.append(proc.pid)
        return proc

    started = time.monotonic()
    try:
        with patch("core.blocked_recovery.subprocess.Popen", side_effect=_capture):
            code = blocked_recovery._run_sandboxed(
                ["/bin/sh", "-c", "( while :; do sleep 60 & sleep 0.01; done ) & exit 0"],
                cwd=tmp_path,
                env=env,
                timeout=15,
                marker=marker,
                reject_stderr=True,
            )
        elapsed = time.monotonic() - started
        assert code == 1
        assert elapsed < 10, f"forking holder cleanup took {elapsed:.1f}s"
        assert _wait_group_empty(roots[0]) == []
        assert not any(t.name == "unblock-check-stderr" for t in threading.enumerate())
    finally:
        for root in roots:
            try:
                os.killpg(root, blocked_recovery.signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
def test_run_sandboxed_rejects_large_stderr_promptly_without_pipe_stall(tmp_path: Path) -> None:
    """Output far beyond the pipe buffer must be drained, rejected, and never ride out the timeout."""
    marker = f"stderr-flood-{os.getpid()}"
    env = {"PATH": os.environ.get("PATH", ""), blocked_recovery._CHECK_MARKER_ENV: marker}
    started = time.monotonic()
    code = blocked_recovery._run_sandboxed(
        ["/bin/sh", "-c", 'head -c 400000 /dev/zero | tr "\\0" x >&2; exit 0'],
        cwd=tmp_path,
        env=env,
        timeout=20,
        marker=marker,
        reject_stderr=True,
    )
    elapsed = time.monotonic() - started
    assert code == 1
    assert elapsed < 10, f"stderr flood took {elapsed:.1f}s; child was stalled on a full pipe"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
@pytest.mark.parametrize(("reject_stderr", "expected"), [(False, 0), (True, 1)])
def test_run_sandboxed_stderr_rejection_is_route_scoped(tmp_path: Path, reject_stderr: bool, expected: int) -> None:
    """Only the stderr-rejecting route turns a zero-exit warning into failure."""
    marker = f"stderr-scope-{os.getpid()}"
    env = {"PATH": os.environ.get("PATH", ""), blocked_recovery._CHECK_MARKER_ENV: marker}
    code = blocked_recovery._run_sandboxed(
        ["/bin/sh", "-c", "echo warning >&2; exit 0"],
        cwd=tmp_path,
        env=env,
        timeout=10,
        marker=marker,
        reject_stderr=reject_stderr,
    )
    assert code == expected


def test_recovery_batch_limit_uses_oldest_blocked_tasks(tmp_path: Path) -> None:
    anima_dir = tmp_path / "animas" / "worker"
    base = now_local() - timedelta(days=1)
    for index in range(5):
        _blocked_task(
            anima_dir,
            task_id=f"task-{index}",
            meta={"blocked_at": (base + timedelta(hours=index)).isoformat(), "unblock_check": "true"},
        )

    with (
        patch("core.config.models.load_config", return_value=_config()),
        patch("core.blocked_recovery._sandbox_route", return_value="bwrap"),
        patch("core.blocked_recovery._run_sandboxed", return_value=0),
    ):
        assert revalidate_blocked_tasks(anima_dir, "worker") == ["task-0", "task-1", "task-2"]

    manager = TaskQueueManager(anima_dir)
    assert [manager.get_task_by_id(f"task-{index}").status for index in range(5)] == [
        "pending",
        "pending",
        "pending",
        "blocked",
        "blocked",
    ]
    activity_files = list((anima_dir / "activity_log").glob("*.jsonl"))
    events = [json.loads(line) for line in activity_files[0].read_text(encoding="utf-8").splitlines()]
    assert [event["meta"]["task_id"] for event in events] == ["task-0", "task-1", "task-2"]
    assert all(event["type"] == "blocked_recovery" for event in events)


def test_blocked_reprobe_batch_limit_default_and_validation() -> None:
    assert BackgroundTaskConfig().blocked_reprobe_batch_limit == 3
    with pytest.raises(ValueError):
        BackgroundTaskConfig(blocked_reprobe_batch_limit=0)


def test_missing_bwrap_fails_closed_and_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    anima_dir = tmp_path / "animas" / "worker"
    manager = _blocked_task(
        anima_dir,
        task_id="no-bwrap",
        meta={
            "unblock_check": "touch escaped",
            "task_desc": {"title": "finish"},
        },
    )

    with (
        patch("core.config.models.load_config", return_value=_config()),
        patch("core.blocked_recovery._sandbox_route", return_value="bwrap"),
        patch("core.blocked_recovery._run_sandboxed", side_effect=FileNotFoundError("bwrap")),
        caplog.at_level("WARNING", logger="animaworks.blocked_recovery"),
    ):
        assert revalidate_blocked_tasks(anima_dir, "worker") == []

    current = manager.get_task_by_id("no-bwrap")
    assert current is not None
    assert current.status == "blocked"
    assert current.meta["unblock_check_failures"] == 1
    assert not (anima_dir / "escaped").exists()
    assert "sandbox unavailable" in caplog.text


def test_taskboard_suppression_skips_recovery(tmp_path: Path) -> None:
    anima_dir = tmp_path / "animas" / "worker"
    manager = _blocked_task(
        anima_dir,
        task_id="suppressed",
        meta={"unblock_check": "true", "task_desc": {"title": "finish"}},
    )
    should_execute = Mock(return_value=SimpleNamespace(executable=False))
    resolver = SimpleNamespace(should_execute=should_execute)

    with (
        patch("core.config.models.load_config", return_value=_config()),
        patch("core.taskboard.attention_resolver.resolver_for_anima_dir", return_value=resolver) as factory,
        patch("core.blocked_recovery._run_sandboxed") as run,
    ):
        assert revalidate_blocked_tasks(anima_dir, "worker") == []

    factory.assert_called_once_with(anima_dir)
    should_execute.assert_called_once_with("worker", "suppressed", queue_status="pending")
    run.assert_not_called()
    current = manager.get_task_by_id("suppressed")
    assert current is not None
    assert current.status == "blocked"


def test_publish_failure_restores_blocked_status(tmp_path: Path) -> None:
    anima_dir = tmp_path / "animas" / "worker"
    manager = _blocked_task(
        anima_dir,
        task_id="publish-fails",
        meta={"unblock_check": "true"},
    )

    def fail_publish(*_args, **_kwargs):
        assert manager.get_task_by_id("publish-fails").status == "pending"
        raise OSError("read-only filesystem")

    with (
        patch("core.config.models.load_config", return_value=_config()),
        patch("core.blocked_recovery._sandbox_route", return_value="bwrap"),
        patch("core.blocked_recovery._run_sandboxed", return_value=0),
        patch("core.blocked_recovery.regenerate_pending_json", side_effect=fail_publish),
    ):
        assert revalidate_blocked_tasks(anima_dir, "worker") == []

    assert manager.get_task_by_id("publish-fails").status == "blocked"


async def test_heartbeat_does_not_revalidate_blocked_tasks(tmp_path: Path) -> None:
    anima_dir = tmp_path / "animas" / "worker"
    anima_dir.mkdir(parents=True)

    with (
        patch("core.blocked_recovery.revalidate_blocked_tasks") as revalidate,
        patch("core._anima_heartbeat.load_prompt", return_value="heartbeat"),
        patch("core._anima_heartbeat._build_curator_review_part", return_value=None),
    ):
        assert await _heartbeat(anima_dir)._build_heartbeat_prompt() == ["heartbeat"]
    revalidate.assert_not_called()


def test_checkless_task_waits_for_reprobe_interval(tmp_path: Path) -> None:
    anima_dir = tmp_path / "animas" / "worker"
    manager = _blocked_task(
        anima_dir,
        task_id="too-new",
        meta={
            "blocked_at": now_local().isoformat(),
            "unblock_check": "   ",
            "task_desc": {"title": "finish"},
        },
    )

    with (
        patch("core.config.models.load_config", return_value=_config()),
        patch("core.blocked_recovery._run_sandboxed") as run,
    ):
        assert revalidate_blocked_tasks(anima_dir, "worker") == []

    run.assert_not_called()
    current = manager.get_task_by_id("too-new")
    assert current is not None
    assert current.status == "blocked"
    assert "blocked_reprobe_count" not in current.meta


@pytest.mark.parametrize("failure", ["nonzero", "timeout"])
def test_failed_check_stays_blocked_and_counts_failure(tmp_path: Path, failure: str) -> None:
    anima_dir = tmp_path / "animas" / "worker"
    manager = _blocked_task(
        anima_dir,
        task_id=f"check-{failure}",
        meta={
            "unblock_check": "false",
            "task_desc": {"title": "finish"},
        },
    )
    outcome = 1 if failure == "nonzero" else subprocess.TimeoutExpired("false", 60)

    with (
        patch("core.config.models.load_config", return_value=_config()),
        patch("core.blocked_recovery._sandbox_route", return_value="sandbox-exec"),
        patch(
            "core.blocked_recovery._run_sandboxed",
            return_value=outcome if failure == "nonzero" else None,
            side_effect=outcome if failure == "timeout" else None,
        ),
    ):
        result = revalidate_blocked_tasks(anima_dir, "worker")

    current = manager.get_task_by_id(f"check-{failure}")
    assert result == []
    assert current is not None
    assert current.status == "blocked"
    assert current.meta["unblock_check_failures"] == 1
    assert not (anima_dir / "state" / "pending" / f"check-{failure}.json").exists()


def test_checkless_task_stays_blocked_and_alerts_once(tmp_path: Path) -> None:
    """Fail closed: checkless tasks never auto-reprobe; alert once past threshold."""
    animas_dir = tmp_path / "animas"
    anima_dir = animas_dir / "worker"
    supervisor_dir = animas_dir / "boss"
    supervisor_dir.mkdir(parents=True)
    anima_dir.mkdir(parents=True)
    (anima_dir / "status.json").write_text('{"supervisor": "boss"}', encoding="utf-8")
    manager = _blocked_task(
        anima_dir,
        task_id="no-check",
        meta={
            "blocked_at": (now_local() - timedelta(hours=7)).isoformat(),
            "task_desc": {"title": "finish", "description": "finish the task"},
        },
    )

    with patch("core.config.models.load_config", return_value=_config()):
        assert revalidate_blocked_tasks(anima_dir, "worker") == []
        assert revalidate_blocked_tasks(anima_dir, "worker") == []
        assert revalidate_blocked_tasks(anima_dir, "worker") == []

    current = manager.get_task_by_id("no-check")
    assert current is not None
    assert current.status == "blocked"
    assert current.meta.get("blocked_recovery_alerted") is True
    assert "blocked_reprobe_count" not in current.meta
    assert not (anima_dir / "state" / "pending" / "no-check.json").exists()
    alerts = [
        task
        for task in TaskQueueManager(supervisor_dir).list_tasks()
        if task.meta.get("kind") == "blocked_task_manual_intervention_required"
    ]
    assert len(alerts) == 1
    assert "unblock_check を持たない" in alerts[0].original_instruction


def test_checkless_legacy_task_uses_updated_at_for_alert_only(tmp_path: Path) -> None:
    """Legacy checkless (no blocked_at) falls back to updated_at; still fail closed."""
    animas_dir = tmp_path / "animas"
    anima_dir = animas_dir / "worker"
    supervisor_dir = animas_dir / "boss"
    supervisor_dir.mkdir(parents=True)
    anima_dir.mkdir(parents=True)
    (anima_dir / "status.json").write_text('{"supervisor": "boss"}', encoding="utf-8")
    manager = _blocked_task(
        anima_dir,
        task_id="legacy-blocked",
        meta={"task_desc": {"title": "finish"}},
    )
    entry = manager.get_task_by_id("legacy-blocked")
    assert entry is not None
    future = datetime.fromisoformat(entry.updated_at) + timedelta(hours=7)

    with (
        patch("core.config.models.load_config", return_value=_config()),
        patch("core.blocked_recovery.now_local", return_value=future),
    ):
        assert revalidate_blocked_tasks(anima_dir, "worker") == []

    current = manager.get_task_by_id("legacy-blocked")
    assert current is not None
    assert current.status == "blocked"
    assert current.meta.get("blocked_recovery_alerted") is True
    assert "blocked_reprobe_count" not in current.meta
    assert not (anima_dir / "state" / "pending" / "legacy-blocked.json").exists()
    alerts = [
        task
        for task in TaskQueueManager(supervisor_dir).list_tasks()
        if task.meta.get("kind") == "blocked_task_manual_intervention_required"
    ]
    assert len(alerts) == 1


def test_checkless_reprobe_enabled_restores_legacy_time_based_reprobe(
    tmp_path: Path,
) -> None:
    """blocked_checkless_reprobe_enabled=True restores pre-fail-closed behavior."""
    anima_dir = tmp_path / "animas" / "worker"
    manager = _blocked_task(
        anima_dir,
        task_id="legacy-on",
        meta={
            "blocked_at": (now_local() - timedelta(hours=7)).isoformat(),
            "task_desc": {"title": "finish", "description": "finish the task"},
        },
    )

    with patch(
        "core.config.models.load_config",
        return_value=_config(blocked_checkless_reprobe_enabled=True),
    ):
        assert revalidate_blocked_tasks(anima_dir, "worker") == ["legacy-on"]

    current = manager.get_task_by_id("legacy-on")
    assert current is not None
    assert current.status == "pending"
    assert current.meta["blocked_reprobe_count"] == 1
    desc = json.loads((anima_dir / "state" / "pending" / "legacy-on.json").read_text(encoding="utf-8"))["description"]
    assert "blockerが解消済みか確認" in desc


def test_checkless_many_tasks_generate_zero_pending_json(tmp_path: Path) -> None:
    """Many checkless blocked tasks must not produce any pending JSON under fail closed."""
    anima_dir = tmp_path / "animas" / "worker"
    for index in range(50):
        _blocked_task(
            anima_dir,
            task_id=f"checkless-{index:02d}",
            meta={
                "blocked_at": (now_local() - timedelta(hours=24)).isoformat(),
                "task_desc": {"title": f"finish-{index}"},
            },
        )

    with patch("core.config.models.load_config", return_value=_config()):
        assert revalidate_blocked_tasks(anima_dir, "worker") == []

    pending_dir = anima_dir / "state" / "pending"
    pending_files = list(pending_dir.glob("*.json")) if pending_dir.is_dir() else []
    assert pending_files == []
    manager = TaskQueueManager(anima_dir)
    for index in range(50):
        current = manager.get_task_by_id(f"checkless-{index:02d}")
        assert current is not None
        assert current.status == "blocked"


def test_blocked_checkless_reprobe_enabled_default_is_false() -> None:
    assert BackgroundTaskConfig().blocked_checkless_reprobe_enabled is False


def test_recovery_can_be_disabled(tmp_path: Path) -> None:
    anima_dir = tmp_path / "animas" / "worker"
    manager = _blocked_task(
        anima_dir,
        task_id="disabled",
        meta={"unblock_check": "true", "task_desc": {"title": "finish"}},
    )

    with patch(
        "core.config.models.load_config",
        return_value=_config(blocked_recovery_enabled=False),
    ):
        assert revalidate_blocked_tasks(anima_dir, "worker") == []

    current = manager.get_task_by_id("disabled")
    assert current is not None
    assert current.status == "blocked"
