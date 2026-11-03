"""Cross-process visibility for an unavailable Anima main vector store.

The Anima main ``MemoryService`` lives in the runner process, while
``rag-repair-status`` and ``/api/animas/{name}`` run elsewhere.  Transient
Chroma errors (for example ``disk I/O error`` under ENOSPC) are intentionally
not recorded in ``rag_repair.json``, so a store that stays closed after such an
error was previously invisible to operators.  This small sidecar file exists
only while the store is unavailable and is removed as soon as it reopens.
"""

# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from core.platform.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

STORE_STATUS_FILENAME = "memory_store_status.json"
_MAX_ERROR_CHARS = 500


def store_status_path(anima_dir: Path) -> Path:
    """Return the sidecar path for one Anima directory."""
    return anima_dir / "state" / STORE_STATUS_FILENAME


def write_store_unavailable(
    anima_dir: Path,
    *,
    unavailable_since: str,
    last_error: str,
    last_attempt_at: str | None,
    next_retry_at: str | None,
    free_bytes: int | None,
) -> bool:
    """Persist the unavailable state; return False when the write failed.

    Writing can itself fail under ENOSPC.  Callers treat this as best effort
    and retry on the next open attempt.
    """
    payload: dict[str, Any] = {
        "status": "unavailable",
        "pid": os.getpid(),
        "unavailable_since": unavailable_since,
        "last_error": last_error[:_MAX_ERROR_CHARS],
        "last_attempt_at": last_attempt_at,
        "next_retry_at": next_retry_at,
        "free_bytes": free_bytes,
    }
    path = store_status_path(anima_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, payload, indent=2, ensure_ascii=False)
    except OSError:
        logger.warning("Failed to write memory store status for %s", anima_dir.name, exc_info=True)
        return False
    return True


def clear_store_unavailable(anima_dir: Path) -> bool:
    """Remove the sidecar; unlink works even on a full volume."""
    try:
        store_status_path(anima_dir).unlink(missing_ok=True)
    except OSError:
        logger.warning("Failed to clear memory store status for %s", anima_dir.name, exc_info=True)
        return False
    return True


def read_store_status(anima_dir: Path) -> dict[str, Any] | None:
    """Return the unavailable-state payload, or None when the store is not marked unavailable."""
    path = store_status_path(anima_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        logger.debug("Unreadable memory store status for %s", anima_dir.name, exc_info=True)
        return None
    if not isinstance(data, dict) or not data.get("unavailable_since"):
        return None
    return data
