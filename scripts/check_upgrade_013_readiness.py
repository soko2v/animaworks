#!/usr/bin/env python3
"""Read-only legacy task gate; NOT a complete production cutover approval.

Require explicit queue paths; never discover or mutate the live data directory.
Do not use TaskQueueManager: its compatibility loader erases blocked/failed.
Exit 2 means retain the old runtime until a reviewed migration/drain is ready.
A clean result does not prove absence of running processes or authorize deploy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

_STATUSES = {"pending", "in_progress", "delegated", "blocked", "failed", "done", "cancelled"}
_TERMINAL = {"done", "cancelled"}


def inspect_queue(queue: Path) -> dict[str, Any]:
    """Inspect raw journal and neighboring descriptors without exposing task text.

    Every nonterminal legacy task requires explicit migration review, not an
    inferred approval from its summary. Even terminal descriptors must be
    reconciled separately. Invalid or missing evidence fails closed.
    """
    result: dict[str, Any] = {
        "gate": "legacy_task_state_only",
        "ready": False,
        "queue_sha256": None,
        "task_count": 0,
        "review_required_count": 0,
        "descriptor_count": 0,
        "issues": [],
    }
    issues = result["issues"]
    if queue.name != "task_queue.jsonl" or queue.parent.name != "state":
        issues.append("unexpected_queue_layout")
        return result
    if any(p.is_symlink() for p in (queue, *queue.parents)):
        issues.append("symlink_queue_path")
        return result
    try:
        raw_bytes = queue.read_bytes()
        raw_text = raw_bytes.decode("utf-8")
    except (OSError, UnicodeError):
        issues.append("queue_unreadable")
        return result
    result["queue_sha256"] = hashlib.sha256(raw_bytes).hexdigest()
    states: dict[str, dict[str, Any]] = {}
    for line in raw_text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            issues.append("invalid_json")
            continue
        if not isinstance(row, dict) or not isinstance(row.get("task_id"), str) or not row["task_id"]:
            issues.append("invalid_task_row")
            continue
        tid = row["task_id"]
        event = row.get("_event")
        if event == "update":
            if tid not in states:
                issues.append("orphan_update")
                continue
            states[tid].update(row)
        elif event is None:
            if tid in states:
                issues.append("duplicate_creation")
            states[tid] = row
        else:
            issues.append("unknown_event")
    for row in states.values():
        status = row.get("status")
        if not isinstance(status, str) or status not in _STATUSES:
            issues.append("invalid_status")
        elif status not in _TERMINAL:
            result["review_required_count"] += 1
            issues.append("legacy_hold_requires_preservation" if status in {"blocked", "failed"}
                          else "active_task_requires_migration_review")
    result["task_count"] = len(states)
    pending = queue.parent / "pending"
    try:
        if pending.is_symlink():
            issues.append("symlink_pending")
        elif pending.exists():
            if not pending.is_dir():
                issues.append("invalid_pending_directory")
            else:
                for item in pending.rglob("*"):
                    if item.is_symlink():
                        issues.append("symlink_pending_entry")
                    elif item.is_file() and item.suffix == ".json":
                        result["descriptor_count"] += 1
                if result["descriptor_count"]:
                    issues.append("descriptors_require_reconciliation")
        if queue.read_bytes() != raw_bytes:
            issues.append("queue_changed_during_inspection")
    except OSError:
        issues.append("state_unreadable")
    result["issues"] = sorted(set(issues))
    result["ready"] = not result["issues"]
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, action="append", required=True)
    args = parser.parse_args(argv)
    results = [inspect_queue(queue) for queue in args.queue]
    print(json.dumps({"scope": "legacy_task_state_only", "results": results}, sort_keys=True))
    return 0 if all(result["ready"] for result in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
