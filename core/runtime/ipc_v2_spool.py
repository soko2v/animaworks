"""Out-of-band spool for oversized IPC v2 ``run`` parameters.

IPC v2 frames are capped at :data:`IPC_V2_MAX_FRAME_BYTES` (4 MiB) and the
ACK window holds at most 4 MiB of unacknowledged bytes.  A chat turn with
large image attachments (base64, up to 20 MB accepted by the HTTP layer) can
therefore never fit into the single ``run`` request frame that the Anima main
sends to a task runner.

Instead of raising the wire limit, the Anima main writes such parameters to a
private spool file next to the IPC socket directory and sends only a small
reference.  The task runner resolves the reference itself (the file name is
derived from its own job id, never from the peer), verifies size and SHA-256,
and continues with the original parameters.  The Anima main owns the file and
removes it when the job ends, so reconnect replays of the ``run`` request keep
working for the whole job lifetime.  Each Anima main owns its own spool
subdirectory and removes every file there that does not belong to one of its
live jobs whenever a job ends and when its endpoint starts, so leftovers from
a crashed main never outlive the next job.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from core.runtime.ipc_v2 import IPCV2ProtocolError

logger = logging.getLogger(__name__)

IPC_V2_SPOOL_KEY = "_ipc_v2_spooled_params"
IPC_V2_SPOOL_DIRNAME = "ipc-v2-spool"
# Keep the inline run frame well below both the 4 MiB frame cap and the
# 4 MiB ACK window so other in-flight frames never block it.
IPC_V2_RUN_PARAMS_INLINE_MAX_BYTES = 1 * 1024 * 1024
# Upper bound for a spooled payload (20 MB images + 10 MB message + headroom).
IPC_V2_SPOOL_MAX_BYTES = 128 * 1024 * 1024

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


def spool_dir_for_socket(socket_path: Path) -> Path:
    """Return the per-endpoint spool directory shared by an IPC v2 socket's two peers.

    ``run/sockets/<anima>.task-v2.sock`` maps to ``run/ipc-v2-spool/<anima>.task-v2``.
    """
    socket_path = Path(socket_path)
    owner = socket_path.name.removesuffix(".sock") or "default"
    return socket_path.parent.parent / IPC_V2_SPOOL_DIRNAME / owner


def _ensure_private_dir(spool_dir: Path) -> None:
    for directory in (spool_dir.parent, spool_dir):
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)


def _spool_file(spool_dir: Path, job_id: str) -> Path:
    if not _JOB_ID_RE.fullmatch(job_id):
        raise IPCV2ProtocolError(f"invalid job_id for spool: {job_id!r}")
    return spool_dir / f"{job_id}.json"


def encode_params(params: dict[str, Any]) -> bytes:
    """Encode params exactly like :meth:`IPCV2Envelope.to_bytes` does."""
    return json.dumps(params, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


def spool_run_params_if_large(
    params: dict[str, Any],
    *,
    spool_dir: Path,
    job_id: str,
    inline_max_bytes: int = IPC_V2_RUN_PARAMS_INLINE_MAX_BYTES,
) -> tuple[dict[str, Any], Path | None]:
    """Return wire params for a ``run`` request, spooling large ones to disk.

    Args:
        params: Original run parameters (left untouched).
        spool_dir: Directory shared with the task runner.
        job_id: Job id; the spool file is ``<job_id>.json``.
        inline_max_bytes: Params up to this encoded size are sent inline.

    Returns:
        ``(wire_params, spool_path)``. ``spool_path`` is ``None`` when the
        params were small enough to send inline.

    Raises:
        IPCV2ProtocolError: The payload exceeds :data:`IPC_V2_SPOOL_MAX_BYTES`.
        OSError: The spool file could not be written.
    """
    encoded = encode_params(params)
    if len(encoded) <= inline_max_bytes:
        return params, None
    if len(encoded) > IPC_V2_SPOOL_MAX_BYTES:
        raise IPCV2ProtocolError(f"run params are {len(encoded)} bytes; spool maximum is {IPC_V2_SPOOL_MAX_BYTES}")
    target = _spool_file(spool_dir, job_id)
    spool_dir.parent.parent.mkdir(parents=True, exist_ok=True)
    _ensure_private_dir(spool_dir)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(encoded)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    reference = {
        IPC_V2_SPOOL_KEY: {
            "name": target.name,
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
    }
    logger.info("IPC v2 run params spooled job=%s bytes=%d", job_id, len(encoded))
    return reference, target


def load_spooled_run_params(params: Any, *, spool_dir: Path, job_id: str) -> Any:
    """Resolve a spool reference produced by :func:`spool_run_params_if_large`.

    Params without a spool reference are returned unchanged. The file path is
    derived from ``job_id`` (never from the peer-supplied name), and its size
    and SHA-256 must match the reference.

    Raises:
        IPCV2ProtocolError: The reference is malformed or the file does not match.
    """
    if not isinstance(params, dict) or IPC_V2_SPOOL_KEY not in params:
        return params
    ref = params[IPC_V2_SPOOL_KEY]
    if len(params) != 1 or not isinstance(ref, dict):
        raise IPCV2ProtocolError("spooled run params reference is malformed")
    target = _spool_file(spool_dir, job_id)
    expected_bytes = ref.get("bytes")
    expected_sha = ref.get("sha256")
    if (
        ref.get("name") != target.name
        or not isinstance(expected_bytes, int)
        or isinstance(expected_bytes, bool)
        or not 0 < expected_bytes <= IPC_V2_SPOOL_MAX_BYTES
        or not isinstance(expected_sha, str)
    ):
        raise IPCV2ProtocolError("spooled run params reference is malformed")
    try:
        with target.open("rb") as fh:
            data = fh.read(IPC_V2_SPOOL_MAX_BYTES + 1)
    except OSError as exc:
        raise IPCV2ProtocolError(f"spooled run params are unreadable: {exc}") from exc
    if len(data) != expected_bytes or hashlib.sha256(data).hexdigest() != expected_sha:
        raise IPCV2ProtocolError("spooled run params do not match their reference")
    try:
        loaded = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IPCV2ProtocolError(f"spooled run params are not valid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise IPCV2ProtocolError("spooled run params must be a JSON object")
    return loaded


def remove_spool_file(path: Path | None) -> None:
    """Best-effort removal of one spool file."""
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Failed to remove IPC v2 spool file %s", path, exc_info=True)


def _job_id_from_spool_name(name: str) -> str | None:
    if name.endswith(".json") and not name.startswith("."):
        return name[: -len(".json")]
    if name.startswith(".") and name.endswith(".tmp") and ".json." in name:
        return name[1:].split(".json.", 1)[0]
    return None


def sweep_orphan_spool_files(spool_dir: Path, live_job_ids: set[str] | frozenset[str]) -> int:
    """Remove spool files in an endpoint's own directory not owned by a live job.

    Args:
        spool_dir: The directory returned by :func:`spool_dir_for_socket` for
            the caller's own endpoint (never another Anima's directory).
        live_job_ids: Job ids that are still registered and may need their file.

    Returns:
        Number of files removed.
    """
    if not spool_dir.is_dir():
        return 0
    removed = 0
    for entry in spool_dir.iterdir():
        job_id = _job_id_from_spool_name(entry.name)
        if job_id is None or job_id in live_job_ids:
            continue
        try:
            if entry.is_file() and not entry.is_symlink():
                entry.unlink(missing_ok=True)
                removed += 1
        except OSError:
            logger.debug("Failed to sweep IPC v2 spool file %s", entry, exc_info=True)
    return removed


__all__ = [
    "IPC_V2_RUN_PARAMS_INLINE_MAX_BYTES",
    "IPC_V2_SPOOL_KEY",
    "IPC_V2_SPOOL_MAX_BYTES",
    "load_spooled_run_params",
    "remove_spool_file",
    "spool_dir_for_socket",
    "spool_run_params_if_large",
    "sweep_orphan_spool_files",
]
