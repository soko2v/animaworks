"""Anima-main-owned vector memory service."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import shutil
import sys
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, perf_counter
from typing import Any, TypeVar

from core.memory.rag.owner_lock import VectorOwnerBusy, VectorOwnerLock
from core.memory.rag.store import ChromaVectorStore, Document, SearchResult
from core.platform.subprocess_entries import SubprocessEntry, module_args

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


class MemoryServiceUnavailable(RuntimeError):
    """The Anima main memory service cannot safely answer a request."""


class MemoryService:
    """Serialize one anima's native vector operations on one bounded worker."""

    _REOPEN_MIN_INTERVAL_SECONDS = 60.0
    # A store left closed by a non-busy open failure (e.g. ENOSPC during a
    # reopen) is retried from handle() at most this often.
    _OPEN_RETRY_MIN_INTERVAL_SECONDS = 60.0
    # Do not close/reopen Chroma when the volume is nearly full: the reopen
    # needs room for the WAL and -shm files and would otherwise strand the
    # service without a handle.
    _REOPEN_MIN_FREE_BYTES = 128 * 1024 * 1024
    _UNAVAILABLE_WARN_INTERVAL_SECONDS = 300.0

    def __init__(
        self,
        anima_name: str,
        anima_dir: Path,
        *,
        queue_limit: int = 64,
        opener: Callable[[], ChromaVectorStore] | None = None,
        repair_fenced: Callable[[], bool] | None = None,
        owner_label: str = "root",
    ) -> None:
        if queue_limit < 1:
            raise ValueError("queue_limit must be >= 1")
        self.anima_name = anima_name
        self.anima_dir = anima_dir
        self.queue_limit = queue_limit
        self._opener = opener or self._open_native_store
        self._repair_fenced = repair_fenced or (lambda: False)
        self._owner_lock = VectorOwnerLock(anima_dir, owner_label)
        self._owner_retry_at = 0.0
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"memory-{anima_name}")
        self._store: ChromaVectorStore | None = None
        self._open_error: Exception | None = None
        self._last_reopen_monotonic: float | None = None
        self._open_retry_at = 0.0
        self._open_retry_inflight = False
        self._unavailable_since: datetime | None = None
        self._unavailable_since_monotonic: float | None = None
        self._last_unavailable_warn_monotonic: float | None = None
        self._publish_status = owner_label == "root"
        self._pending = 0
        self._started = False
        self._closing = False
        self._repairing = False
        self._repair_lock = asyncio.Lock()

    async def start(self) -> None:
        """Open Chroma off the Anima main event loop; failure leaves the Anima main available."""
        if self._started:
            return
        self._started = True
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(self._executor, self._owner_lock.acquire)
        except VectorOwnerBusy as exc:
            self._open_error = exc
            self._started = False
            self._owner_retry_at = monotonic() + 5.0
            logger.info("Vector store owner is busy for %s; retrying later: %s", self.anima_name, exc)
            return
        try:
            self._store = await loop.run_in_executor(self._executor, self._opener)
            self._mark_available()
        except Exception as exc:
            self._mark_unavailable(exc)
            logger.warning("Anima main memory store open failed for %s: %s", self.anima_name, exc)
            try:
                self._request_startup_repair(exc)
            except Exception:
                logger.warning("Failed to mark background RAG repair for %s", self.anima_name, exc_info=True)

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Run one checked operation or raise an explicit unavailable error."""
        if self._closing:
            raise MemoryServiceUnavailable("memory service is closing")
        if isinstance(self._open_error, VectorOwnerBusy):
            if monotonic() >= self._owner_retry_at:
                await self.start()
            if isinstance(self._open_error, VectorOwnerBusy):
                from core.i18n import t

                raise MemoryServiceUnavailable(t("rag.owner_busy", anima=self.anima_name)) from self._open_error
        elif not self._started:
            await self.start()
            if isinstance(self._open_error, VectorOwnerBusy):
                from core.i18n import t

                raise MemoryServiceUnavailable(t("rag.owner_busy", anima=self.anima_name)) from self._open_error
        if self._closing:
            raise MemoryServiceUnavailable("memory service is closing")
        if self._repairing:
            raise MemoryServiceUnavailable("RAG repair in progress")
        if self._store is None and self._retryable_open_error():
            await self._maybe_retry_open()
            if self._closing:
                raise MemoryServiceUnavailable("memory service is closing")
            if self._repairing:
                raise MemoryServiceUnavailable("RAG repair in progress")
        if self._store is None:
            self._warn_if_unavailable_too_long()
            since = f" (since {self._unavailable_since.isoformat()})" if self._unavailable_since is not None else ""
            raise MemoryServiceUnavailable(f"memory store unavailable: {self._open_error or 'not open'}{since}")
        try:
            fenced = self._repair_fenced()
        except Exception as exc:
            raise MemoryServiceUnavailable(f"repair fence state unavailable: {exc}") from exc
        if fenced:
            raise MemoryServiceUnavailable("RAG repair in progress")
        if self._pending >= self.queue_limit:
            raise MemoryServiceUnavailable("memory queue is full")
        if method == "memory.apply_access_updates":
            self._validate_access_updates(params)

        self._pending += 1
        queued_at = perf_counter()
        if method == "memory.apply_access_updates":
            future = asyncio.get_running_loop().run_in_executor(
                self._executor,
                self._dispatch_timed,
                method,
                params,
                queued_at,
            )
            future.add_done_callback(self._deferred_operation_done)
            return {"ok": True}
        try:
            return await asyncio.get_running_loop().run_in_executor(
                self._executor,
                self._dispatch_timed,
                method,
                params,
                queued_at,
            )
        except MemoryServiceUnavailable:
            raise
        except ValueError:
            raise
        except Exception as exc:
            logger.warning("Anima main memory operation failed for %s: %s", self.anima_name, exc)
            raise MemoryServiceUnavailable(f"memory operation failed: {exc}") from exc
        finally:
            self._pending -= 1

    def _deferred_operation_done(self, future: asyncio.Future) -> None:
        self._pending -= 1
        try:
            future.result()
        except Exception:
            logger.warning("Deferred Anima main memory operation failed for %s", self.anima_name, exc_info=True)

    def _dispatch_timed(self, method: str, params: dict[str, Any], queued_at: float) -> dict[str, Any]:
        started = perf_counter()
        try:
            return self._dispatch(method, params)
        finally:
            if method in {"memory.query", "memory.apply_access_updates"}:
                logger.info(
                    "Anima main memory operation: anima=%s method=%s queue_wait=%.3fs execute=%.3fs",
                    self.anima_name,
                    method,
                    started - queued_at,
                    perf_counter() - started,
                )

    async def repair(self, *, include_shared: bool) -> dict[str, Any]:
        """Rebuild, swap, reopen, and verify this Anima main's sole vector store."""
        if not include_shared:
            from core.i18n import t

            # This operation replaces the whole DB, not just personal
            # collections. A partial rebuild would discard shared memories.
            raise ValueError(t("rag.phase3_repair_requires_shared"))
        if self._repair_lock.locked():
            raise MemoryServiceUnavailable("RAG repair already in progress")
        async with self._repair_lock:
            self._repairing = True
            try:
                staging, chunks, shared_hashes = await self._build_staging_subprocess(include_shared)
                return await self._promote_and_verify(staging, chunks, shared_hashes)
            finally:
                self._repairing = False

    async def _build_staging_subprocess(self, include_shared: bool) -> tuple[Path, int, dict[str, str]]:
        token = uuid.uuid4().hex
        staging = self.anima_dir / f"vectordb.staging-root-{token}"
        result_path = self.anima_dir / "state" / f".rag-repair-result-{token}.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable,
            *module_args(SubprocessEntry.RAG_REPAIR_REBUILD),
            "--build-staging",
            "--anima",
            self.anima_name,
            "--anima-dir",
            str(self.anima_dir),
            "--staging",
            str(staging),
            "--result",
            str(result_path),
        ]
        if include_shared:
            cmd.append("--shared")
        from core.config import load_config
        from core.memory.rag.endpoints import child_env, get_endpoints

        timeout = int(getattr(load_config().rag, "repair_timeout_seconds", 1800))
        cwd = await asyncio.to_thread(lambda: Path(__file__).resolve().parents[2])
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env={**os.environ, **child_env(get_endpoints())},
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            try:
                _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                proc.kill()
                await proc.communicate()
                raise RuntimeError(f"Anima main RAG staging rebuild timed out after {timeout}s") from None
            if proc.returncode != 0:
                detail = stderr[-4000:].decode(errors="replace").strip()
                raise RuntimeError(detail or f"staging rebuild exited with code {proc.returncode}")
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            chunks = payload.get("chunks")
            shared_hashes = payload.get("shared_hashes")
            if not isinstance(chunks, int) or isinstance(chunks, bool) or chunks < 0:
                raise RuntimeError("staging rebuild returned an invalid chunk count")
            if not isinstance(shared_hashes, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in shared_hashes.items()
            ):
                raise RuntimeError("staging rebuild returned invalid shared hashes")
            return staging, chunks, shared_hashes
        except BaseException:
            if proc.returncode is None:
                proc.kill()
                await proc.communicate()
            await asyncio.to_thread(shutil.rmtree, staging, ignore_errors=True)
            raise
        finally:
            result_path.unlink(missing_ok=True)

    async def _promote_and_verify(
        self,
        staging: Path,
        chunks: int,
        shared_hashes: dict[str, str],
    ) -> dict[str, Any]:
        """Keep promotion consistent before releasing a cancelled caller's fence.

        Executor futures cannot stop a running filesystem operation. Shield the
        entire transaction, not just individual awaits whose return values
        determine whether rollback is required.
        """
        operation = asyncio.create_task(self._promote_and_verify_consistent(staging, chunks, shared_hashes))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            # Repeated caller cancellation must not abandon the native worker
            # or drop repair()'s fence early. A cancelled child is already done
            # and must not be awaited forever.
            while not operation.done():
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not operation.cancelled():
                try:
                    operation.result()
                except Exception:
                    logger.warning(
                        "RAG promotion failed while caller was cancelled: %s", self.anima_name, exc_info=True
                    )
            raise

    async def _promote_and_verify_consistent(
        self,
        staging: Path,
        chunks: int,
        shared_hashes: dict[str, str],
    ) -> dict[str, Any]:
        from core.memory.rag.repair.rebuild import (
            _backup_repair_metadata,
            _restore_repair_metadata,
            publish_rebuild_metadata,
            validate_rebuild_sources,
        )

        loop = asyncio.get_running_loop()
        backup_dir: Path | None = None
        existing: set[str] = set()
        archive: Path | None = None
        closed = False
        promoted = False
        rollback_incomplete = False
        try:
            await loop.run_in_executor(self._executor, validate_rebuild_sources, staging, self.anima_dir)
            backup_dir, existing = await loop.run_in_executor(self._executor, _backup_repair_metadata, self.anima_dir)
            closed = True
            await loop.run_in_executor(self._executor, self._close_store_sync)
            archive = await loop.run_in_executor(self._executor, self._promote_staging_sync, staging)
            promoted = True
            await loop.run_in_executor(self._executor, self._rebuild_bm25_sync)
            store = await loop.run_in_executor(self._executor, self._opener)
            self._store = store
            self._mark_available()
            self._last_reopen_monotonic = monotonic()
            verification = await loop.run_in_executor(self._executor, self._verify_store_sync, store, chunks)
            if shared_hashes:
                await loop.run_in_executor(self._executor, self._write_shared_hashes_sync, shared_hashes)
            await loop.run_in_executor(self._executor, publish_rebuild_metadata, self.anima_dir)
            await loop.run_in_executor(self._executor, self._invalidate_shared_checks_sync)
            return {
                "ok": True,
                "status": "success",
                "chunks_indexed": chunks,
                "archive_path": str(archive) if archive is not None else None,
                "verification": verification,
            }
        except BaseException as exc:
            rollback_errors: list[str] = []
            if promoted:
                try:
                    await loop.run_in_executor(self._executor, self._close_store_sync)
                    await loop.run_in_executor(self._executor, self._rollback_sync, archive)
                    assert backup_dir is not None
                    await loop.run_in_executor(
                        self._executor,
                        _restore_repair_metadata,
                        self.anima_dir,
                        backup_dir,
                        existing,
                    )
                    self._store = await loop.run_in_executor(self._executor, self._opener)
                    self._mark_available()
                    self._last_reopen_monotonic = monotonic()
                except Exception as rollback_exc:
                    self._store = None
                    self._mark_unavailable(rollback_exc)
                    rollback_errors.append(str(rollback_exc))
            elif closed:
                try:
                    self._store = await loop.run_in_executor(self._executor, self._opener)
                    self._mark_available()
                    self._last_reopen_monotonic = monotonic()
                except Exception as reopen_exc:
                    self._mark_unavailable(reopen_exc)
                    rollback_errors.append(str(reopen_exc))
            if rollback_errors:
                rollback_incomplete = True
                raise RuntimeError(f"{exc}; rollback incomplete: {'; '.join(rollback_errors)}") from exc
            raise
        finally:
            await loop.run_in_executor(self._executor, shutil.rmtree, staging, True)
            if backup_dir is not None and not rollback_incomplete:
                await loop.run_in_executor(self._executor, shutil.rmtree, backup_dir, True)

    def _close_store_sync(self) -> None:
        store, self._store = self._store, None
        if store is not None:
            store.close()

    def _promote_staging_sync(self, staging: Path) -> Path | None:
        live = self.anima_dir / "vectordb"
        archive: Path | None = None
        if live.exists():
            archive_dir = self.anima_dir / "archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive = (
                archive_dir / f"vectordb-corrupt-{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}-{uuid.uuid4().hex[:8]}"
            )
            shutil.move(str(live), str(archive))
        try:
            shutil.move(str(staging), str(live))
        except BaseException:
            if archive is not None and archive.exists() and not live.exists():
                shutil.move(str(archive), str(live))
            raise
        return archive

    def _rollback_sync(self, archive: Path | None) -> None:
        live = self.anima_dir / "vectordb"
        if live.exists():
            failed_dir = self.anima_dir / "archive"
            failed_dir.mkdir(parents=True, exist_ok=True)
            failed = (
                failed_dir
                / f"vectordb-rebuild-failed-{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}-{uuid.uuid4().hex[:8]}"
            )
            shutil.move(str(live), str(failed))
        if archive is not None and archive.exists():
            shutil.move(str(archive), str(live))

    def _rebuild_bm25_sync(self) -> None:
        from core.memory.retrieval.bm25 import rebuild_longterm_bm25_index

        rebuild_longterm_bm25_index(self.anima_dir)

    @staticmethod
    def _verify_store_sync(store: ChromaVectorStore, expected_chunks: int) -> dict[str, int]:
        verify = getattr(store, "verify_rebuilt_data", None)
        if not callable(verify):
            raise RuntimeError("reopened Anima main vector store cannot verify rebuilt data")
        result = verify(expected_chunks=expected_chunks)
        if not isinstance(result, dict):
            raise RuntimeError("reopened Anima main vector store returned invalid verification")
        return result

    def _write_shared_hashes_sync(self, shared_hashes: dict[str, str]) -> None:
        from core.memory.rag.shared_meta import write_shared_hashes

        write_shared_hashes(self.anima_dir, shared_hashes)

    def _invalidate_shared_checks_sync(self) -> None:
        from core.memory.rag.shared_check_registry import invalidate_shared_checks

        invalidate_shared_checks(self.anima_name)

    def _retryable_open_error(self) -> bool:
        """Return True when the store is closed by a non-busy open failure."""
        return (
            self._started
            and self._open_error is not None
            and not isinstance(self._open_error, VectorOwnerBusy)
            and not self._repairing
        )

    async def _maybe_retry_open(self) -> None:
        """Retry the opener at most once per interval without overlapping repair."""
        now = monotonic()
        if self._open_retry_inflight or now < self._open_retry_at or self._repair_lock.locked():
            return
        if self._fenced_for_retry():
            return
        # Claim the slot before awaiting so concurrent callers fail fast
        # instead of queueing a second open behind this one. The in-flight
        # flag is cleared by the worker future itself (also on cancellation),
        # so a slow opener cannot accumulate queued retries across intervals.
        self._open_retry_inflight = True
        self._open_retry_at = now + self._OPEN_RETRY_MIN_INTERVAL_SECONDS
        try:
            job = self._executor.submit(self._retry_open_sync)
        except RuntimeError:
            self._open_retry_inflight = False
            return
        job.add_done_callback(self._clear_open_retry_inflight)
        await asyncio.wrap_future(job)

    def _clear_open_retry_inflight(self, _job: object) -> None:
        self._open_retry_inflight = False

    def _fenced_for_retry(self) -> bool:
        try:
            return bool(self._repair_fenced())
        except Exception:
            logger.debug("Repair fence state unavailable; skipping store retry for %s", self.anima_name, exc_info=True)
            return True

    def _retry_open_sync(self) -> None:
        """Reopen on the single native worker; serialised with repair and close."""
        if self._store is not None or self._repairing or self._closing or self._fenced_for_retry():
            return
        free_bytes = self._free_bytes()
        if free_bytes is not None and free_bytes < self._REOPEN_MIN_FREE_BYTES:
            error = OSError(
                errno.ENOSPC,
                f"insufficient free disk space for vector store reopen: {free_bytes} bytes free "
                f"(< {self._REOPEN_MIN_FREE_BYTES})",
            )
            logger.warning("Anima main memory store retry deferred for %s: %s", self.anima_name, error)
            self._mark_unavailable(error)
            return
        try:
            store = self._opener()
        except Exception as exc:
            logger.warning("Anima main memory store reopen retry failed for %s: %s", self.anima_name, exc)
            self._mark_unavailable(exc)
            return
        if self._closing or self._repairing or self._fenced_for_retry():
            store.close()
            return
        since = self._unavailable_since
        self._store = store
        self._mark_available()
        logger.warning(
            "Anima main memory store reopened for %s after being unavailable since %s",
            self.anima_name,
            since.isoformat() if since is not None else "unknown",
        )

    def _mark_unavailable(self, error: Exception) -> None:
        """Record a non-busy open failure, schedule the next retry, and publish it."""
        now = monotonic()
        attempt_at = datetime.now(UTC)
        self._open_error = error
        self._open_retry_at = now + self._OPEN_RETRY_MIN_INTERVAL_SECONDS
        if self._unavailable_since is None:
            self._unavailable_since = attempt_at
            self._unavailable_since_monotonic = now
            self._last_unavailable_warn_monotonic = None
        if not self._publish_status:
            return
        from core.memory.rag.store_status import write_store_unavailable

        write_store_unavailable(
            self.anima_dir,
            unavailable_since=self._unavailable_since.isoformat(),
            last_error=f"{type(error).__name__}: {error}",
            last_attempt_at=attempt_at.isoformat(),
            next_retry_at=datetime.fromtimestamp(
                attempt_at.timestamp() + self._OPEN_RETRY_MIN_INTERVAL_SECONDS, UTC
            ).isoformat(),
            free_bytes=self._free_bytes(),
        )

    def _mark_available(self) -> None:
        """Clear open-failure state after a successful open."""
        self._open_error = None
        self._open_retry_at = 0.0
        self._unavailable_since = None
        self._unavailable_since_monotonic = None
        self._last_unavailable_warn_monotonic = None
        if self._publish_status:
            from core.memory.rag.store_status import clear_store_unavailable

            # Also removes a stale marker left by a previous crashed runner.
            clear_store_unavailable(self.anima_dir)

    def _warn_if_unavailable_too_long(self) -> None:
        """Warn (rate limited) while a non-busy open failure persists."""
        since = self._unavailable_since_monotonic
        if since is None or isinstance(self._open_error, VectorOwnerBusy):
            return
        now = monotonic()
        if now - since < self._UNAVAILABLE_WARN_INTERVAL_SECONDS:
            return
        last = self._last_unavailable_warn_monotonic
        if last is not None and now - last < self._UNAVAILABLE_WARN_INTERVAL_SECONDS:
            return
        self._last_unavailable_warn_monotonic = now
        logger.warning(
            "Anima main memory store for %s unavailable since %s (%.0fs); last error: %s; free_bytes=%s",
            self.anima_name,
            self._unavailable_since.isoformat() if self._unavailable_since is not None else "unknown",
            now - since,
            self._open_error,
            self._free_bytes(),
        )

    def _free_bytes(self) -> int | None:
        """Free bytes on the volume holding this Anima's vector store, or None if unknown."""
        path = self.anima_dir
        while not path.exists() and path.parent != path:
            path = path.parent
        try:
            return shutil.disk_usage(path).free
        except OSError:
            return None

    def _request_startup_repair(self, error: Exception) -> None:
        from core.memory.rag.repair import state as repair_state

        current = repair_state.read_state(self.anima_name, animas_dir=self.anima_dir.parent)
        if current.get("status") in repair_state.ACTIVE_REPAIR_STATUSES:
            return
        repair_state.write_repair_request_state(
            self.anima_name,
            reason="store_init_failed",
            collection=None,
            source="phase3_root_startup",
            include_shared=True,
            animas_dir=self.anima_dir.parent,
        )
        logger.warning("Marked phase3 Anima main RAG for background repair after open failure: %s", error)

    @staticmethod
    def _read_or_empty(collection: str, read: Callable[[], list[Any]]) -> list[Any]:
        """Run a read; a collection that does not exist yet reads as empty, like the vector worker."""
        from core.memory.rag.store import _is_missing_collection_error, log_missing_collection_once

        try:
            return read()
        except Exception as exc:
            if not _is_missing_collection_error(exc):
                raise
            log_missing_collection_once(collection, "Collection %s does not exist yet; reading as empty", collection)
            return []

    def _run_native(self, method: str, collection: str, op: Callable[[ChromaVectorStore], _T]) -> _T:
        """Run a native store operation with bounded corruption recovery."""
        if self._repairing:
            raise MemoryServiceUnavailable("RAG repair in progress")
        if self._store is None:
            raise MemoryServiceUnavailable("memory store unavailable")

        try:
            return op(self._store)
        except Exception as error:
            from core.memory.rag.repair.detect import classify_corruption_error

            reason = classify_corruption_error(error)
            if reason is None:
                raise
            if reason == "chroma_transient":
                try:
                    return op(self._store)
                except Exception as retry_error:
                    error = retry_error
                    reason = classify_corruption_error(retry_error)
                    if reason is None:
                        raise

            from core.memory.rag.repair import record_chroma_error

            record_chroma_error(
                anima_name=self.anima_name,
                collection=collection,
                error=error,
                source=f"root:{method}",
            )

            now = monotonic()
            if (
                self._last_reopen_monotonic is not None
                and now - self._last_reopen_monotonic < self._REOPEN_MIN_INTERVAL_SECONDS
            ):
                raise error

            free_bytes = self._free_bytes()
            if free_bytes is not None and free_bytes < self._REOPEN_MIN_FREE_BYTES:
                # Keep the current handle: closing it now cannot be undone
                # until space returns, which is how ENOSPC stranded the store.
                logger.warning(
                    "Skipping Anima main memory store reopen for %s: %d bytes free (< %d); keeping current handle",
                    self.anima_name,
                    free_bytes,
                    self._REOPEN_MIN_FREE_BYTES,
                )
                raise error

            self._last_reopen_monotonic = now
            try:
                self._close_store_sync()
                self._store = self._opener()
                self._mark_available()
            except Exception as reopen_error:
                self._store = None
                self._mark_unavailable(reopen_error)
                raise reopen_error from error

            return op(self._store)

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "memory.query":
            collection = self._string(params, "collection")
            embedding = params.get("embedding")
            top_k = params.get("top_k", 10)
            filter_metadata = params.get("filter_metadata")
            if not isinstance(embedding, list) or not all(isinstance(value, (int, float)) for value in embedding):
                raise ValueError("embedding must be a list of numbers")
            if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1:
                raise ValueError("top_k must be an integer >= 1")
            if filter_metadata is not None and not isinstance(filter_metadata, dict):
                raise ValueError("filter_metadata must be an object or null")
            results = self._run_native(
                method,
                collection,
                lambda store: self._read_or_empty(
                    collection,
                    lambda: store._query_once(collection, embedding, top_k, filter_metadata),
                ),
            )
            return {"results": self._search_results(results)}
        if method == "memory.list_collections":
            collections = self._run_native(
                method,
                "<list_collections>",
                lambda store: store._list_collections_once(),
            )
            return {"collections": list(collections)}
        if method == "memory.get_by_metadata":
            collection = self._string(params, "collection")
            where = params.get("where")
            limit = params.get("limit", 20)
            if not isinstance(where, dict):
                raise ValueError("where must be an object")
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                raise ValueError("limit must be an integer >= 1")
            results = self._run_native(
                method,
                collection,
                lambda store: self._read_or_empty(
                    collection,
                    lambda: store._get_by_metadata_once(collection, where, limit),
                ),
            )
            return {"results": self._search_results(results)}
        if method == "memory.get_all":
            collection = self._string(params, "collection")
            limit = params.get("limit", 100_000)
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                raise ValueError("limit must be an integer >= 1")
            results = self._run_native(
                method,
                collection,
                lambda store: self._read_or_empty(
                    collection,
                    lambda: store._get_all_once(collection, limit),
                ),
            )
            return {"results": self._search_results(results)}
        if method == "memory.count":
            collection = self._string(params, "collection")
            count = self._run_native(method, collection, lambda store: store._count_once(collection))
            return {"count": int(count)}
        if method == "memory.get_by_ids":
            collection = self._string(params, "collection")
            ids = params.get("ids")
            if not isinstance(ids, list) or not all(isinstance(value, str) for value in ids):
                raise ValueError("ids must be a list of strings")
            documents = self._run_native(
                method,
                collection,
                lambda store: self._read_or_empty(
                    collection,
                    lambda: store._get_by_ids_once(collection, ids),
                ),
            )
            return {"documents": self._documents(documents)}
        if method == "memory.create_collection":
            collection = self._string(params, "collection")
            result = self._run_native(method, collection, lambda store: store._create_collection_once(collection))
            return {"ok": bool(result)}
        if method == "memory.delete_collection":
            collection = self._string(params, "collection")
            result = self._run_native(method, collection, lambda store: store._delete_collection_once(collection))
            return {"ok": bool(result)}
        if method == "memory.upsert":
            collection = self._string(params, "collection")
            documents = self._document_params(params.get("documents"))
            result = self._run_native(method, collection, lambda store: store._upsert_once(collection, documents))
            return {"ok": bool(result)}
        if method == "memory.delete_documents":
            collection = self._string(params, "collection")
            ids = self._strings(params.get("ids"), "ids")
            result = self._run_native(method, collection, lambda store: store._delete_documents_once(collection, ids))
            return {"ok": bool(result)}
        if method == "memory.update_metadata":
            collection = self._string(params, "collection")
            ids = self._strings(params.get("ids"), "ids")
            metadatas = params.get("metadatas")
            if not isinstance(metadatas, list) or not all(isinstance(item, dict) for item in metadatas):
                raise ValueError("metadatas must be a list of objects")
            if len(ids) != len(metadatas):
                raise ValueError("ids and metadatas must have the same length")
            result = self._run_native(
                method,
                collection,
                lambda store: store._update_metadata_once(collection, ids, metadatas),
            )
            return {"ok": bool(result)}
        if method == "memory.apply_access_updates":
            return {"ok": self._apply_access_updates(params["operations"])}
        raise ValueError(f"unsupported memory method: {method}")

    @staticmethod
    def _validate_access_updates(params: dict[str, Any]) -> None:
        operations = params.get("operations")
        if not isinstance(operations, list):
            raise ValueError("operations must be a list")
        for operation in operations:
            if not isinstance(operation, dict):
                raise ValueError("operations must contain objects")
            if not isinstance(operation.get("collection"), str) or not operation["collection"]:
                raise ValueError("collection must be a non-empty string")
            if not isinstance(operation.get("doc_id"), str) or not operation["doc_id"]:
                raise ValueError("doc_id must be a non-empty string")
            for key in ("access_delta", "retrieved_delta", "used_delta"):
                value = operation.get(key, 0)
                if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                    raise ValueError(f"{key} must be a non-negative number")

    def _apply_access_updates(self, operations: list[dict[str, Any]]) -> bool:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for operation in operations:
            grouped.setdefault(operation["collection"], []).append(operation)
        for collection, rows in grouped.items():
            ids = [row["doc_id"] for row in rows]
            documents = self._run_native(
                "memory.apply_access_updates",
                collection,
                lambda store, collection=collection, ids=ids: store._get_by_ids_once(collection, ids),
            )
            current = {document.id: dict(document.metadata) for document in documents}
            metadatas: list[dict[str, str | int | float]] = []
            for row in rows:
                metadata = current.get(row["doc_id"], {})
                access_count = self._metadata_number(metadata, "access_count")
                last_accessed_at = max(
                    str(metadata.get("last_accessed_at", "") or ""),
                    str(row.get("last_accessed_at", "") or ""),
                )
                patch: dict[str, str | int | float] = {
                    "access_count": access_count + float(row.get("access_delta", 0)),
                    "retrieved_count": self._metadata_number(metadata, "retrieved_count")
                    + int(row.get("retrieved_delta", 0)),
                    "used_count": self._metadata_number(metadata, "used_count", default=access_count)
                    + int(row.get("used_delta", 0)),
                    "last_accessed_at": last_accessed_at,
                }
                for kind in ("retrieved", "used"):
                    timestamp = row.get(f"last_{kind}_at")
                    if isinstance(timestamp, str):
                        patch[f"last_{kind}_at"] = max(str(metadata.get(f"last_{kind}_at", "") or ""), timestamp)
                per_anima_key = row.get("per_anima_access_key")
                if isinstance(per_anima_key, str) and per_anima_key:
                    patch[per_anima_key] = self._metadata_number(metadata, per_anima_key) + float(
                        row.get("access_delta", 0)
                    )
                metadatas.append(patch)
            updated = self._run_native(
                "memory.apply_access_updates",
                collection,
                lambda store, collection=collection, ids=ids, metadatas=metadatas: store._update_metadata_once(
                    collection, ids, metadatas
                ),
            )
            if not updated:
                return False
        return True

    @staticmethod
    def _metadata_number(metadata: dict[str, object], field: str, *, default: float = 0.0) -> float:
        try:
            return max(0.0, float(str(metadata.get(field, default))))
        except (TypeError, ValueError):
            return max(0.0, default)

    @staticmethod
    def _string(params: dict[str, Any], key: str) -> str:
        value = params.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{key} must be a non-empty string")
        return value

    @staticmethod
    def _strings(value: Any, key: str) -> list[str]:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"{key} must be a list of strings")
        return value

    @staticmethod
    def _document_params(value: Any) -> list[Document]:
        if not isinstance(value, list):
            raise ValueError("documents must be a list")
        documents: list[Document] = []
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("documents must contain objects")
            doc_id = item.get("id")
            content = item.get("content", "")
            embedding = item.get("embedding")
            metadata = item.get("metadata", {})
            if not isinstance(doc_id, str) or not doc_id:
                raise ValueError("document id must be a non-empty string")
            if not isinstance(content, str):
                raise ValueError("document content must be a string")
            if embedding is not None and (
                not isinstance(embedding, list)
                or not all(isinstance(number, (int, float)) and not isinstance(number, bool) for number in embedding)
            ):
                raise ValueError("document embedding must be a list of numbers or null")
            if not isinstance(metadata, dict):
                raise ValueError("document metadata must be an object")
            documents.append(Document(id=doc_id, content=content, embedding=embedding, metadata=metadata))
        return documents

    @staticmethod
    def _documents(documents: list[Document]) -> list[dict[str, Any]]:
        return [{"id": doc.id, "content": doc.content, "metadata": doc.metadata} for doc in documents]

    @classmethod
    def _search_results(cls, results: list[SearchResult]) -> list[dict[str, Any]]:
        return [{"document": cls._documents([result.document])[0], "score": result.score} for result in results]

    def _open_native_store(self) -> ChromaVectorStore:
        from core.memory.rag.direct_access import OWNER_CAPABILITY
        from core.memory.rag.store import create_chroma_vector_store
        from core.paths import get_anima_vectordb_dir

        return create_chroma_vector_store(
            persist_dir=get_anima_vectordb_dir(self.anima_name),
            anima_name=self.anima_name,
            allow_direct=OWNER_CAPABILITY,
        )

    async def close(self) -> None:
        """Close the sole native handle after queued operations drain."""
        if self._closing:
            return
        self._closing = True
        loop = asyncio.get_running_loop()
        # Read the handle on the worker, after any queued open retry has
        # finished, so a store opened concurrently with close() is not leaked.
        try:
            await loop.run_in_executor(self._executor, self._close_store_sync)
        except Exception:
            logger.warning("Failed to close Anima main memory store for %s", self.anima_name, exc_info=True)
        self._store = None
        if self._publish_status and self._unavailable_since is not None:
            from core.memory.rag.store_status import clear_store_unavailable

            clear_store_unavailable(self.anima_dir)
        try:
            await loop.run_in_executor(self._executor, self._owner_lock.release)
        except Exception:
            logger.warning("Failed to release vector owner lock for %s", self.anima_name, exc_info=True)
        self._executor.shutdown(wait=False)
