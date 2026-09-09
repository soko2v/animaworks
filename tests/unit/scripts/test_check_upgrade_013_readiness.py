"""Synthetic files only; no runtime server, transport, credentials or models."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_upgrade_013_readiness import inspect_queue, main


def test_real_runner_process_lock_rejects_duplicate_then_allows_handoff(tmp_path):
    """Real child process, synthetic state; never starts a runner or model."""
    from types import SimpleNamespace

    from core.platform.locks import release_file_lock
    from core.supervisor.runner import AnimaRunner

    owner = SimpleNamespace(shared_dir=tmp_path / "shared", anima_name="synthetic", _lock_file=None)
    AnimaRunner._acquire_process_lock(owner)
    pid_path = tmp_path / "run/animas/synthetic.pid"
    lock_path = pid_path.with_suffix(".lock")
    original_pid = pid_path.read_bytes()
    original_inode = lock_path.stat().st_ino
    child = """
import sys
from pathlib import Path
from types import SimpleNamespace
from core.supervisor.runner import AnimaRunner
from core.platform.locks import release_file_lock
owner = SimpleNamespace(shared_dir=Path(sys.argv[1]) / 'shared', anima_name='synthetic', _lock_file=None)
AnimaRunner._acquire_process_lock(owner)
release_file_lock(owner._lock_file)
owner._lock_file.close()
"""
    env = {
        "PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"),
        "ANIMAWORKS_DATA_DIR": str(tmp_path), "ANIMAWORKS_DISABLE_EXTERNAL_SYNC": "1",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(Path(__file__).resolve().parents[3]),
    }
    def attempt():
        return subprocess.run([sys.executable, "-c", child, str(tmp_path)],
                              env=env, capture_output=True, timeout=30)
    try:
        rejected = attempt()
        assert rejected.returncode == 1
        assert b"DUPLICATE PROCESS" in rejected.stderr
        assert pid_path.read_bytes() == original_pid
        assert lock_path.stat().st_ino == original_inode
    finally:
        release_file_lock(owner._lock_file)
        owner._lock_file.close()
    assert attempt().returncode == 0
    assert lock_path.stat().st_ino == original_inode
    assert pid_path.read_bytes() != original_pid


def test_explicit_synthetic_auth_vault_restore_uses_real_resolver(tmp_path, monkeypatch):
    """Manual quiescent fixture copy, NOT a production backup implementation."""
    from core.auth import manager as auth
    from core.auth.models import AuthConfig
    from core.config.vault import VaultManager, resolve_vault_references

    source, restored = tmp_path / "source", tmp_path / "restored"
    source.mkdir(mode=0o700)
    monkeypatch.setattr(auth, "get_data_dir", lambda: source)
    auth.save_auth(AuthConfig())
    vault = VaultManager(source)
    assert vault.is_encryption_available, "Encrypted restoration must not silently use plaintext fallback"
    assert vault.generate_key()
    vault.store("shared", "SYNTHETIC", "synthetic-not-a-real-credential")
    shutil.copytree(source, restored)
    assert (restored.stat().st_mode & 0o777) == 0o700
    for name in ("auth.json", "vault.json", "vault.key"):
        assert (restored / name).read_bytes() == (source / name).read_bytes()
        assert (restored / name).stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(auth, "get_data_dir", lambda: restored)
    assert auth.load_auth() == AuthConfig()
    assert resolve_vault_references({"$vault": "SYNTHETIC"}, restored) == "synthetic-not-a-real-credential"


def test_synthetic_snapshot_restores_config_memory_and_execution_evidence(tmp_path):
    """Empty destination only; no service, real credentials or live data."""
    from core.memory.migration.backup import BackupManager
    from core.memory.task_queue import legacy_execution_hold

    source, restored = tmp_path / "source", tmp_path / "restored"
    files = {
        "config.json": '{"version":1}',
        "animas/synthetic/identity.md": "Synthetic identity",
        "animas/synthetic/knowledge/example.md": "Synthetic memory",
        "animas/synthetic/cron.md": "Synthetic schedule, never executed",
        "animas/synthetic/state/task_queue.jsonl": json.dumps({
            "task_id": "held", "status": "blocked"}) + "\n" + json.dumps({
            "task_id": "running", "status": "in_progress"}) + "\n" + json.dumps({
            "_event": "execution_hold_group", "task_ids": ["held", "child"]}) + "\n",
        "animas/synthetic/state/pending/processing/running.json": '{"task_id":"running"}',
        "animas/synthetic/state/pending/processing/running.lease.json": '{"synthetic":true}',
        "shared/example.md": "Synthetic shared memory",
    }
    for name, content in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    snapshot = BackupManager(source).create(label="synthetic-only")
    shutil.copytree(snapshot, restored / "backup" / snapshot.name)
    BackupManager(restored).restore(snapshot.name)
    for name in files:
        assert (restored / name).read_bytes() == (source / name).read_bytes()
    anima = restored / "animas/synthetic"
    assert legacy_execution_hold(anima, "held")
    assert legacy_execution_hold(anima, "child")
    assert not inspect_queue(anima / "state/task_queue.jsonl")["ready"]


def test_corrupt_snapshot_rejected_before_empty_destination_restore(tmp_path):
    from core.memory.migration.backup import BackupManager

    source, restored = tmp_path / "source", tmp_path / "restored"
    source.mkdir()
    (source / "config.json").write_text('{"synthetic":true}')
    snapshot = BackupManager(source).create(label="synthetic-corrupt")
    copied = restored / "backup" / snapshot.name
    shutil.copytree(snapshot, copied)
    (copied / "config.json").write_text("corrupt synthetic data")
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        BackupManager(restored).restore(snapshot.name)
    assert not (restored / "config.json").exists()


def test_builtin_memory_backup_is_not_a_complete_auth_snapshot(tmp_path):
    """Document a cutover blocker, not an accepted full-runtime backup."""
    from core.memory.migration.backup import BackupManager

    source, restored = tmp_path / "source", tmp_path / "restored"
    source.mkdir()
    (source / "auth.json").write_text('{"auth_mode":"password"}')
    snapshot = BackupManager(source).create(label="synthetic-scope")
    shutil.copytree(snapshot, restored / "backup" / snapshot.name)
    BackupManager(restored).restore(snapshot.name)
    assert not (restored / "auth.json").exists()
    # Never start a server against this incomplete restore.


def queue_at(tmp_path: Path, rows: list[object]) -> Path:
    queue = tmp_path / "state" / "task_queue.jsonl"
    queue.parent.mkdir()
    queue.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return queue


@pytest.mark.parametrize("status", ["blocked", "failed", "pending", "in_progress", "delegated"])
def test_nonterminal_fails_closed_without_rewriting(tmp_path, status):
    queue = queue_at(tmp_path, [{"task_id": "synthetic", "status": status, "summary": "approved-looking text"}])
    before = queue.read_bytes()
    result = inspect_queue(queue)
    assert not result["ready"]
    assert result["review_required_count"] == 1
    assert queue.read_bytes() == before


@pytest.mark.parametrize("status", ["done", "cancelled"])
def test_terminal_clean_is_only_task_state_pass(tmp_path, status):
    queue = queue_at(tmp_path, [{"task_id": "synthetic", "status": status}])
    result = inspect_queue(queue)
    assert result["ready"]
    assert result["gate"] == "legacy_task_state_only"
    assert len(result["queue_sha256"]) == 64


def test_partial_update_keeps_hold(tmp_path):
    queue = queue_at(tmp_path, [{"task_id": "synthetic", "status": "blocked"},
                              {"task_id": "synthetic", "_event": "update", "summary": "new"}])
    assert "legacy_hold_requires_preservation" in inspect_queue(queue)["issues"]


def test_latest_explicit_terminal_status(tmp_path):
    queue = queue_at(tmp_path, [{"task_id": "synthetic", "status": "blocked"},
                              {"task_id": "synthetic", "_event": "update", "status": "done"}])
    assert inspect_queue(queue)["ready"]


@pytest.mark.parametrize("subdir", ["", "processing", "suppressed", "deferred", "failed"])
def test_even_terminal_descriptor_requires_reconciliation(tmp_path, subdir):
    queue = queue_at(tmp_path, [{"task_id": "synthetic", "status": "done"}])
    pending = queue.parent / "pending" / subdir
    pending.mkdir(parents=True)
    (pending / "synthetic.json").write_text("{}")
    result = inspect_queue(queue)
    assert not result["ready"]
    assert result["descriptor_count"] == 1


@pytest.mark.parametrize("row", [[], None, {"task_id": "x", "status": []},
                                     {"task_id": "x", "_event": "update", "status": "done"},
                                     {"task_id": "x", "_event": "unknown", "status": "done"}])
def test_bad_evidence_fails_closed(tmp_path, row):
    assert not inspect_queue(queue_at(tmp_path, [row]))["ready"]


def test_corruption_fails_closed(tmp_path):
    queue = queue_at(tmp_path, [])
    queue.write_text("{broken\n")
    assert "invalid_json" in inspect_queue(queue)["issues"]


def test_missing_queue_fails_closed(tmp_path):
    assert not inspect_queue(tmp_path / "state" / "task_queue.jsonl")["ready"]


def test_symlink_pending_fails_closed(tmp_path):
    queue = queue_at(tmp_path, [])
    (queue.parent / "pending").symlink_to(tmp_path / "absent", target_is_directory=True)
    assert "symlink_pending" in inspect_queue(queue)["issues"]


def test_cli_no_content_disclosure_and_nonzero(tmp_path, capsys):
    queue = queue_at(tmp_path, [{"task_id": "private-id", "status": "blocked", "summary": "private-content"}])
    assert main(["--queue", str(queue)]) == 2
    output = capsys.readouterr().out
    assert "private-id" not in output and "private-content" not in output


def test_cli_requires_explicit_path():
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_upstream_read_mapping_loses_hold_but_preflight_rejects(tmp_path):
    from core.memory.task_queue import TaskQueueManager

    queue = queue_at(tmp_path, [{
        "task_id": "synthetic", "status": "blocked", "source": "human",
        "ts": "2026-09-09T00:00:00+09:00", "updated_at": "2026-09-09T00:00:00+09:00",
        "original_instruction": "Synthetic approval not granted", "assignee": "fixture",
        "summary": "hold", "relay_chain": [], "meta": {},
    }])
    before = queue.read_bytes()
    entry = TaskQueueManager(tmp_path).get_task_by_id("synthetic")
    assert entry is not None and entry.status == "pending"
    assert "legacy_hold_requires_preservation" in inspect_queue(queue)["issues"]
    assert queue.read_bytes() == before


def test_upstream_suppressed_descriptor_display_is_not_execution_proof(tmp_path):
    from core.memory.task_queue import mark_executability

    queue = queue_at(tmp_path, [])
    suppressed = queue.parent / "pending" / "suppressed"
    suppressed.mkdir(parents=True)
    (suppressed / "synthetic.json").write_text("{}")
    entries = [{"task_id": "synthetic", "status": "pending"}]
    mark_executability(entries, tmp_path)
    assert entries[0]["executable"] is True
    # This is a display flag only: this test never starts an executor.
    assert not inspect_queue(queue)["ready"]
