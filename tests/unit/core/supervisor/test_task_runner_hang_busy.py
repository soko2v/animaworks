"""Task-level hang recovery and root-owned busy marker tests."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from core.anima import DigitalAnima
from core.supervisor import task_runner, task_runner_supervisor
from core.supervisor.ipc_v2 import IPCV2ConnectionState, IPCV2Identity
from core.supervisor.task_runner_supervisor import TaskRunnerJob, TaskRunnerSupervisor


def _job(
    supervisor: TaskRunnerSupervisor,
    process: asyncio.subprocess.Process | None,
    *,
    job_id: str = "job-hang",
) -> TaskRunnerJob:
    identity = IPCV2Identity(
        job_id=job_id,
        root_epoch=supervisor.root_epoch,
        attempt=1,
        lane="task",
        display_lane="background",
    )
    return TaskRunnerJob(
        identity=identity,
        request_id=f"run-{job_id}",
        params={},
        result=asyncio.get_running_loop().create_future(),
        peer_state=IPCV2ConnectionState(identity),
        process=process,
        pid=process.pid if process else 999_999,
        pgid=process.pid if process else 999_999,
        last_progress_at=asyncio.get_running_loop().time(),
    )


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.skipif(os.name != "posix", reason="process-group assertion requires POSIX")
@pytest.mark.asyncio
async def test_stalled_job_kills_only_target_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_pid_path = tmp_path / "descendant.pid"
    code = (
        "import signal,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)']);"
        "open(sys.argv[1],'w').write(str(child.pid));"
        "time.sleep(60)"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        str(child_pid_path),
        start_new_session=True,
    )
    supervisor = TaskRunnerSupervisor(
        "sakura",
        tmp_path / "animas" / "sakura",
        tmp_path / "shared",
        busy_hang_threshold_sec=0.05,
    )
    job = _job(supervisor, process)
    job.last_progress_at -= 1.0
    supervisor.jobs[job.identity.job_id] = job
    root_pid = os.getpid()
    monkeypatch.setattr(task_runner_supervisor, "_TASK_RUNNER_TERM_TIMEOUT", 0.1)

    try:
        for _ in range(100):
            if child_pid_path.exists():
                break
            await asyncio.sleep(0.01)
        assert child_pid_path.exists()

        await asyncio.wait_for(supervisor._watch_job(job), timeout=3.0)

        assert job.pid == process.pid
        assert job.pid != root_pid
        assert job.hang_kill_started
        assert process.returncode == -signal.SIGTERM
        assert os.getpid() == root_pid
        for _ in range(100):
            if not _group_exists(job.pgid or 0):
                break
            await asyncio.sleep(0.01)
        assert not _group_exists(job.pgid or 0)
    finally:
        if _group_exists(process.pid):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


@pytest.mark.skipif(os.name != "posix", reason="process-group assertion requires POSIX")
@pytest.mark.asyncio
async def test_cancelled_job_escalates_and_releases_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation cannot wait forever on a task group that ignores SIGTERM."""
    code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        start_new_session=True,
    )
    supervisor = TaskRunnerSupervisor("sakura", tmp_path / "animas" / "sakura", tmp_path / "shared")
    job = _job(supervisor, process, job_id="job-cancel")
    supervisor.jobs[job.identity.job_id] = job
    monkeypatch.setattr(task_runner_supervisor, "_TASK_RUNNER_TERM_TIMEOUT", 0.05)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    monkeypatch.setattr(supervisor, "_ensure_started", AsyncMock())
    monkeypatch.setattr(supervisor, "_required_url_environment", lambda: {"ANIMAWORKS_EMBED_URL": "http://x"})

    task = asyncio.create_task(
        supervisor._spawn_and_await(
            lane="cron",
            job_prefix="cron",
            params_builder=lambda _env: {},
            log_context="cancel-test",
            attempt=1,
            display_lane="background",
            on_spawned=None,
            url_env={"ANIMAWORKS_EMBED_URL": "http://x"},
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.0)

    assert process.returncode == -signal.SIGKILL
    assert supervisor.jobs == {"job-cancel": job}
    supervisor.jobs.pop("job-cancel")
    assert supervisor.active_child_count == 0


@pytest.mark.asyncio
async def test_continuing_progress_is_not_killed(tmp_path: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        start_new_session=True,
    )
    supervisor = TaskRunnerSupervisor(
        "sakura",
        tmp_path / "animas" / "sakura",
        tmp_path / "shared",
        busy_hang_threshold_sec=0.12,
    )
    job = _job(supervisor, process, job_id="job-progress")
    supervisor.jobs[job.identity.job_id] = job
    watch = asyncio.create_task(supervisor._watch_job(job))
    try:
        for _ in range(5):
            await asyncio.sleep(0.04)
            job.last_progress_at = asyncio.get_running_loop().time()
        assert not watch.done()
        assert not job.hang_kill_started
        assert process.returncode is None
    finally:
        watch.cancel()
        await asyncio.gather(watch, return_exceptions=True)
        process.kill()
        await process.wait()


@pytest.mark.asyncio
async def test_progress_writes_root_owned_busy_sidecar(tmp_path: Path) -> None:
    owner = object.__new__(DigitalAnima)
    owner.name = "sakura"
    owner.shared_dir = tmp_path / "shared"
    owner._busy_status_enabled = True
    owner._conversation_locks = {}
    owner._background_lock = asyncio.Lock()
    owner._inbox_lock = asyncio.Lock()
    owner._active_background_workers = {}
    owner._last_progress_at = None
    owner._busy_since = None

    supervisor = TaskRunnerSupervisor(
        "sakura",
        tmp_path / "animas" / "sakura",
        owner.shared_dir,
        busy_status_owner=owner,
    )
    job = _job(supervisor, None)
    supervisor.jobs[job.identity.job_id] = job
    supervisor._mark_busy_start()
    before = job.last_progress_at
    supervisor._record_progress(job, {"pid": job.pid, "lane": "task"})

    sidecar = tmp_path / "run" / "animas" / "sakura.busy.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["pid"] == os.getpid()
    assert payload["pid"] != job.pid
    assert payload["lanes"] == ["background"]
    assert job.last_progress_at > before
    assert datetime.fromisoformat(payload["last_progress_at"])

    supervisor.jobs.pop(job.identity.job_id)
    supervisor._clear_busy_if_idle()
    assert not sidecar.exists()


@pytest.mark.asyncio
async def test_task_runner_disables_its_busy_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def make_anima(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    identity = IPCV2Identity(
        job_id="job-cron",
        root_epoch="epoch",
        attempt=1,
        lane="cron",
        display_lane="background",
    )
    params = {
        "task": {
            "name": "daily",
            "schedule": "0 9 * * *",
            "type": "llm",
            "description": "daily",
        }
    }
    monkeypatch.setattr(task_runner, "DigitalAnima", make_anima)
    monkeypatch.setattr(task_runner, "execute_cron_contract", AsyncMock(return_value={"success": True}))

    execution = await task_runner._prepare_execution(
        argparse.Namespace(anima="sakura", lane="cron", job="job-cron"),
        identity,
        params,
    )
    await execution

    assert captured["busy_status_enabled"] is False
