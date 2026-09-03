from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
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
        "test -w .",
    ]
    kwargs = run.call_args.kwargs
    assert kwargs["cwd"] == anima_dir
    assert kwargs["timeout"] == 60
    assert set(kwargs["env"]) == {"PATH", "HOME", "ANIMAWORKS_ANIMA_DIR"}
    assert kwargs["env"]["ANIMAWORKS_ANIMA_DIR"] == str(anima_dir)
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
    assert argv[3:] == ["/bin/sh", "-c", "test -w ."]
    profile = argv[2]
    assert profile is blocked_recovery._MACOS_SANDBOX_PROFILE
    assert "test -w ." not in profile
    kwargs = run.call_args.kwargs
    assert kwargs["cwd"] == anima_dir
    assert kwargs["timeout"] == 60
    assert set(kwargs["env"]) == {"PATH", "HOME", "ANIMAWORKS_ANIMA_DIR"}
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
    hostile = '") (allow default) ('
    argv = blocked_recovery._sandbox_argv("sandbox-exec", hostile)
    assert argv[2] == profile
    assert argv[-1] == hostile


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


def test_run_sandboxed_kills_process_group_on_timeout() -> None:
    proc = Mock()
    proc.pid = 4242
    proc.wait.side_effect = [subprocess.TimeoutExpired("sh", 60), -9]

    with (
        patch("core.blocked_recovery.subprocess.Popen", return_value=proc) as popen,
        patch("core.blocked_recovery.os.killpg") as killpg,
        pytest.raises(subprocess.TimeoutExpired),
    ):
        blocked_recovery._run_sandboxed(["/bin/sh", "-c", "sleep 99"], cwd=Path("/"), env={}, timeout=60)

    kwargs = popen.call_args.kwargs
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["env"] == {}
    killpg.assert_called_once_with(4242, blocked_recovery.signal.SIGKILL)
    assert proc.wait.call_count == 2


def test_run_sandboxed_returns_exit_code() -> None:
    proc = Mock()
    proc.wait.return_value = 3
    with patch("core.blocked_recovery.subprocess.Popen", return_value=proc):
        assert blocked_recovery._run_sandboxed(["/bin/true"], cwd=Path("/"), env={}, timeout=5) == 3


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
    ],
)
def test_real_sandbox_exec_denies_writes_and_network(tmp_path: Path, check: str, expected: int) -> None:
    """Integration: the shipped profile really blocks writes/network and propagates exit codes."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "ANIMAWORKS_ANIMA_DIR": str(tmp_path)}
    code = blocked_recovery._run_sandboxed(
        blocked_recovery._sandbox_argv("sandbox-exec", check), cwd=tmp_path, env=env, timeout=30
    )
    assert (code == 0) == (expected == 0)
    if expected not in (0, 1):
        assert code == expected
    assert not (tmp_path / "escaped").exists()
    assert not (tmp_path / "escaped_dir").exists()


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
