"""Synthetic files only; no runtime server, transport, credentials or models."""

import json
from pathlib import Path

import pytest

from scripts.check_upgrade_013_readiness import inspect_queue, main


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
