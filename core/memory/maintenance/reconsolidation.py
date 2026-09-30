from __future__ import annotations

# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
#
# This file is part of AnimaWorks core/server, licensed under Apache-2.0.
# See LICENSE for the full license text.


"""Failure-count-based memory reconsolidation engine.

When a procedure or knowledge file accumulates failures since its last
reconsolidation, the system revises it with an LLM. A failure-count checkpoint
prevents the same unchanged failure history from triggering repeated revisions.

Pipeline:
  1. Scan procedure/knowledge frontmatter for unprocessed failures
  2. LLM-based revision
  3. Version-controlled update with a failure-count checkpoint
  4. Activity log event recording
"""

import logging
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.paths import load_prompt

logger = logging.getLogger("animaworks.reconsolidation")


def _metadata_count(value: Any) -> int:
    """Read a count from frontmatter, tolerating stringified vector metadata."""
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


_BARE_LLM_FAILURE_RE = re.compile(
    r"""
    ^\s*(?:
        you(?:['’]ve|\s+have)\s+(?:hit|reached)\s+(?:your\s+)?[^\n]{0,64}\blimit\b
        | (?:usage|weekly|daily|monthly|token|request|rate)\s+limit\s+(?:exceeded|reached)\b
        | quota\s+(?:exceeded|exhausted)\b
        | insufficient[_\s-]+quota\b
        | too\s+many\s+requests\b
        | authentication\s+(?:failed|error)\b
        | unauthorized\b
        | forbidden\b
        | (?:request\s+)?timed\s+out\b
        | timeout\s+(?:error|occurred)\b
        | (?:unable\s+to\s+connect|connection\s+(?:refused|reset|aborted|error))\b
        | (?:internal\s+server\s+error|service\s+unavailable|bad\s+gateway|gateway\s+timeout|overloaded)\b
        | (?:context\s+length\s+exceeded|maximum\s+context\s+length)\b
        | (?:payment\s+required|insufficient\s+credits)\b
        | (?:an?\s+)?(?:unexpected\s+)?error\s+(?:occurred|while\s+processing)\b
        | something\s+went\s+wrong\b
    )
    (?=
        $
        | [\t ]*(?:[.!:·,\n]
        | (?:please|try|retry|reset|resets|switch|visit|for|on|until|at|while|after|because|due)\b)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

_BARE_QUOTA_FAILURE_RE = re.compile(
    r"""
    ^\s*(?:
        you(?:['’]ve|\s+have)\s+(?:hit|reached)\s+(?:your\s+)?[^\n]{0,64}\blimit\b
        | (?:usage|weekly|daily|monthly|token|request|rate)\s+limit\s+(?:exceeded|reached)\b
        | quota\s+(?:exceeded|exhausted)\b
        | insufficient[_\s-]+quota\b
        | too\s+many\s+requests\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


class InvalidReconsolidationOutput(ValueError):
    """Raised when an LLM revision is unsafe to persist as canonical memory."""

    def __init__(
        self,
        reason: str,
        *,
        output_chars: int,
        minimum_chars: int = 0,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.output_chars = output_chars
        self.minimum_chars = minimum_chars


def _iter_reconsolidation_files(root: Path) -> list[Path]:
    """Recursively collect ``*.md`` files under ``root``, excluding archives.

    Uses ``rglob`` so knowledge/procedure files kept in subdirectories are
    scanned for reconsolidation (F4). Any path with an ``archive`` directory
    component is skipped so superseded/merged/versioned copies (which live
    under ``archive/``) are never treated as live reconsolidation targets.

    Args:
        root: Directory to scan (e.g. ``knowledge/`` or ``procedures/``).

    Returns:
        Sorted list of live ``*.md`` paths.
    """
    return sorted(p for p in root.rglob("*.md") if "archive" not in p.relative_to(root).parts)


# ── ReconsolidationEngine ──────────────────────────────────────


class ReconsolidationEngine:
    """Failure-count-based memory reconsolidation engine.

    Scans procedure files for failures that occurred since the last
    reconsolidation, then uses an LLM to revise their content.
    Each revision is version-controlled with an archived copy.
    """

    def __init__(
        self,
        anima_dir: Path,
        anima_name: str,
        *,
        memory_manager: Any | None = None,
        activity_logger: Any | None = None,
    ) -> None:
        """Initialize the reconsolidation engine.

        Args:
            anima_dir: Path to anima's directory (~/.animaworks/animas/{name}).
            anima_name: Name of the anima for logging.
            memory_manager: Optional MemoryManager instance. Created if None.
            activity_logger: Optional ActivityLogger instance. Created if None.
        """
        self.anima_dir = anima_dir
        self.anima_name = anima_name

        if memory_manager is not None:
            self.memory_manager = memory_manager
        else:
            from core.memory.manager import MemoryManager

            self.memory_manager = MemoryManager(anima_dir)

        if activity_logger is not None:
            self.activity_logger = activity_logger
        else:
            from core.memory.activity.logger import ActivityLogger

            self.activity_logger = ActivityLogger(anima_dir)

    # ── Target Detection ───────────────────────────────────────

    async def find_reconsolidation_targets(
        self,
        *,
        max_files: int | None = None,
    ) -> list[Path]:
        """Find procedures with failures since their last reconsolidation.

        Scans all ``*.md`` files in the anima's ``procedures/`` directory,
        prioritizing higher failure counts and then newer failure timestamps.

        Args:
            max_files: Optional cap for nightly batch processing.

        Returns:
            List of paths to procedure files that need reconsolidation.
        """
        procedures_dir = self.anima_dir / "procedures"
        if not procedures_dir.exists():
            return []
        ranked_targets: list[tuple[int, str, Path]] = []
        for md_file in _iter_reconsolidation_files(procedures_dir):
            meta = self.memory_manager.read_procedure_metadata(md_file)
            failure_count = _metadata_count(meta.get("failure_count", 0))
            checkpoint = _metadata_count(meta.get("reconsolidated_failure_count", 0))
            if failure_count > checkpoint:
                ranked_targets.append((failure_count, str(meta.get("last_failure_at", "") or ""), md_file))
        ranked_targets.sort(key=lambda item: (item[0], item[1]), reverse=True)
        targets = [item[2] for item in ranked_targets]
        return targets[:max_files] if max_files is not None else targets

    # ── Reconsolidation Application ────────────────────────────

    async def apply_reconsolidation(
        self,
        targets: list[Path],
        model: str = "",
    ) -> dict[str, int]:
        """Revise procedures using an LLM and checkpoint handled failures.

        For each target procedure:
          1. Read current metadata and content
          2. Use LLM to generate a revised procedure
          3. Archive the current version
          4. Write revised content with the current failure-count checkpoint
          5. Record activity log event

        Args:
            targets: List of procedure file paths to reconsolidate.
            model: LLM model identifier (LiteLLM format) for revision.

        Returns:
            Dict with counts: "updated", "skipped", "errors".
        """
        if not model:
            from core.memory._llm_utils import get_consolidation_llm_kwargs

            model = get_consolidation_llm_kwargs()["model"]
        results: dict[str, int] = {"updated": 0, "skipped": 0, "errors": 0}

        for proc_path in targets:
            try:
                meta = self.memory_manager.read_procedure_metadata(proc_path)
                content = self.memory_manager.read_procedure_content(proc_path)

                # Use LLM to revise the procedure
                revised = await self._revise_procedure(content, meta, model)
                if revised:
                    version = meta.get("version", 1)
                    self._archive_version(proc_path, content, version)

                    meta["version"] = version + 1
                    meta["reconsolidated_failure_count"] = _metadata_count(meta.get("failure_count", 0))
                    meta["success_count"] = 0
                    meta["previous_version"] = f"v{version}"
                    meta["reconsolidated_at"] = datetime.now(UTC).isoformat()

                    self.memory_manager.write_procedure_with_meta(
                        proc_path,
                        revised,
                        meta,
                    )

                    # Activity log event
                    await self._log_reconsolidation(proc_path, version)
                    results["updated"] += 1

                    logger.info(
                        "Reconsolidated %s: v%d -> v%d",
                        proc_path.name,
                        version,
                        version + 1,
                    )
                else:
                    results["skipped"] += 1
                    logger.debug(
                        "Skipping reconsolidation (LLM returned no revision) for %s",
                        proc_path.name,
                    )

            except Exception as e:
                logger.warning(
                    "Reconsolidation failed for %s: %s",
                    proc_path,
                    e,
                )
                results["errors"] += 1

        logger.info(
            "Reconsolidation complete for anima=%s: updated=%d skipped=%d errors=%d",
            self.anima_name,
            results["updated"],
            results["skipped"],
            results["errors"],
        )
        return results

    # ── Procedure Creation from Resolved Events ──────────────

    async def create_procedures_from_resolved(
        self,
        model: str = "",
        days: int = 1,
    ) -> dict[str, Any]:
        """Create new procedures from issue_resolved activity events.

        Scans recent ``issue_resolved`` entries in the activity log and,
        for each entry that does not already have a matching procedure
        (checked via RAG similarity), uses an LLM to extract a reusable
        procedure from the resolution details.

        Args:
            model: LLM model identifier (LiteLLM format) for generation.
            days: Number of days of activity history to scan.

        Returns:
            Dict with counts: ``created``, ``skipped``, ``errors``.
        """
        if not model:
            from core.memory._llm_utils import get_consolidation_llm_kwargs

            model = get_consolidation_llm_kwargs()["model"]

        results: dict[str, int] = {"created": 0, "skipped": 0, "errors": 0}

        # Collect resolved events
        try:
            from core.memory.activity.logger import ActivityLogger

            activity = ActivityLogger(self.anima_dir)
            entries = activity.recent(days=days, limit=50, types=["issue_resolved"])
        except Exception:
            logger.debug("Failed to collect resolved events", exc_info=True)
            return results

        if not entries:
            logger.info(
                "No issue_resolved events for procedure creation, anima=%s",
                self.anima_name,
            )
            return results

        from core.memory.maintenance.distillation import ProceduralDistiller

        distiller = ProceduralDistiller(self.anima_dir, self.anima_name)
        existing_procedures = distiller._load_existing_procedures()

        for entry in entries:
            resolution_text = entry.content or entry.summary or ""
            if not resolution_text.strip():
                results["skipped"] += 1
                continue

            # RAG duplicate check
            existing = distiller._check_rag_duplicate(resolution_text)
            if existing:
                logger.debug(
                    "Skipping resolved event (similar to %s): %.80s",
                    existing,
                    resolution_text,
                )
                results["skipped"] += 1
                continue

            # Use LLM to extract procedure from resolution
            try:
                prompt = load_prompt(
                    "memory/procedure_from_resolved",
                    resolution_text=resolution_text[:3000],
                    existing_procedures=existing_procedures[:2000],
                )

                from core.memory._llm_utils import one_shot_completion

                text = await one_shot_completion(prompt, model=model, max_tokens=2048)
                if text is None:
                    raise RuntimeError("LLM failed")
                text = text or ""

                # Sanitize and parse
                from core.memory.maintenance.consolidation import ConsolidationEngine

                text = ConsolidationEngine._sanitize_llm_output(text)

                procedure_items = distiller._parse_procedure_items(text)
                for item in procedure_items:
                    filename = item.get("filename", "")
                    title = filename.replace("procedures/", "").replace(".md", "")
                    if not title or not item.get("content"):
                        continue
                    proc_item = {
                        "title": title,
                        "description": item.get("description", ""),
                        "tags": item.get("tags", []),
                        "content": item["content"],
                    }
                    path = distiller.save_procedure(proc_item)
                    if path is not None:
                        results["created"] += 1
                        logger.info(
                            "Created procedure from resolved event: %s",
                            path.name,
                        )

                if not procedure_items:
                    results["skipped"] += 1

            except Exception as e:
                logger.warning(
                    "Failed to create procedure from resolved event: %s",
                    e,
                )
                results["errors"] += 1

        logger.info(
            "Resolved-to-procedure complete for anima=%s: created=%d skipped=%d errors=%d",
            self.anima_name,
            results["created"],
            results["skipped"],
            results["errors"],
        )
        return results

    # ── LLM Procedure Revision ─────────────────────────────────

    async def _revise_procedure(
        self,
        content: str,
        meta: dict[str, Any],
        model: str,
    ) -> str | None:
        """Use LLM to revise a procedure that has been failing.

        Args:
            content: Current procedure body text.
            meta: Current procedure metadata (failure_count, confidence, etc.).
            model: LLM model identifier (LiteLLM format).

        Returns:
            Revised procedure text, or None if revision was not possible.
        """
        failure_count = meta.get("failure_count", 0)
        confidence = meta.get("confidence", 0.5)
        description = meta.get("description", "")

        prompt = load_prompt(
            "memory/procedure_revision",
            description=description,
            content=content[:3000],
            failure_count=failure_count,
            confidence=confidence,
        )

        try:
            from core.memory._llm_utils import one_shot_completion

            text = await one_shot_completion(prompt, model=model, max_tokens=2048) or ""

            # Sanitize LLM output
            from core.memory.maintenance.consolidation import ConsolidationEngine

            text = ConsolidationEngine._sanitize_llm_output(text)

            if text.strip():
                return text.strip()
            return None

        except Exception as e:
            logger.warning(
                "LLM procedure revision failed: %s",
                e,
            )
            return None

    # ── Version Management ─────────────────────────────────────

    def _archive_version(
        self,
        file_path: Path,
        content: str,
        version: int,
    ) -> None:
        """Archive the current version of a procedure before reconsolidation.

        Saves a timestamped copy to archive/versions/ for audit trail.

        Args:
            file_path: Path to the procedure file to archive.
            content: Current content of the file (body text without frontmatter).
            version: Current version number from metadata.
        """
        if not file_path.exists():
            return

        archive_dir = self.anima_dir / "archive" / "versions"
        archive_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        # Flatten the path relative to the anima dir into the archive name so
        # same-named files in different subdirectories (procedures/a/deploy.md
        # vs procedures/b/deploy.md) cannot collide within the same second.
        try:
            rel = file_path.relative_to(self.anima_dir)
            rel_stem = "__".join([*rel.parts[:-1], file_path.stem])
        except ValueError:
            rel_stem = file_path.stem
        dest = archive_dir / f"{rel_stem}_v{version}_{timestamp}{file_path.suffix}"
        shutil.copy2(str(file_path), str(dest))

        logger.debug(
            "Archived version %d of %s to %s",
            version,
            file_path.name,
            dest.name,
        )

    # ── Knowledge Reconsolidation ─────────────────────────────

    async def find_knowledge_reconsolidation_targets(
        self,
        *,
        max_files: int | None = None,
        exclude_paths: set[Path] | None = None,
    ) -> list[Path]:
        """Find knowledge files with failures since their last reconsolidation.

        Scans all ``*.md`` files in the anima's ``knowledge/`` directory,
        prioritizing higher failure counts and then newer failure timestamps.

        Args:
            max_files: Optional cap for nightly batch processing.
            exclude_paths: Paths to skip because an earlier post-processing
                stage superseded, merged, or otherwise consumed them.

        Returns:
            List of paths to knowledge files that need reconsolidation.
        """
        excluded = {p.resolve() for p in (exclude_paths or set())}
        knowledge_dir = self.anima_dir / "knowledge"
        if not knowledge_dir.exists():
            return []
        ranked_targets: list[tuple[int, str, Path]] = []
        for md_file in _iter_reconsolidation_files(knowledge_dir):
            if md_file.resolve() in excluded:
                continue
            meta = self.memory_manager.read_knowledge_metadata(md_file)
            if meta.get("valid_until"):
                continue
            failure_count = _metadata_count(meta.get("failure_count", 0))
            checkpoint = _metadata_count(meta.get("reconsolidated_failure_count", 0))
            if failure_count > checkpoint:
                ranked_targets.append((failure_count, str(meta.get("last_failure_at", "") or ""), md_file))
        ranked_targets.sort(key=lambda item: (item[0], item[1]), reverse=True)
        targets = [item[2] for item in ranked_targets]
        return targets[:max_files] if max_files is not None else targets

    async def reconsolidate_knowledge(
        self,
        model: str = "",
        *,
        max_files: int | None = None,
        exclude_paths: set[Path] | None = None,
    ) -> dict[str, int]:
        """Revise knowledge files using an LLM and checkpoint handled failures.

        Scans knowledge/*.md files with failures since their previous
        reconsolidation checkpoint, then uses an LLM to revise the content.
        Each revision is version-controlled with an archived copy.

        Args:
            model: LLM model identifier (LiteLLM format) for revision.
            max_files: Optional cap for nightly batch processing.
            exclude_paths: Paths to skip because an earlier post-processing
                stage superseded, merged, or otherwise consumed them.

        Returns:
            Dict with counts: "targets_found", "updated", "skipped", "errors".
        """
        if not model:
            from core.memory._llm_utils import get_consolidation_llm_kwargs

            model = get_consolidation_llm_kwargs()["model"]
        targets = await self.find_knowledge_reconsolidation_targets(
            max_files=max_files,
            exclude_paths=exclude_paths,
        )
        results: dict[str, Any] = {
            "targets_found": len(targets),
            "updated": 0,
            "skipped": 0,
            "errors": 0,
            "updated_files": [],
        }

        if not targets:
            return results

        for know_path in targets:
            try:
                meta = self.memory_manager.read_knowledge_metadata(know_path)
                content = self.memory_manager.read_knowledge_content(know_path)

                # Use LLM to revise the knowledge
                revised = await self._revise_knowledge(content, meta, model)
                revised = self._validate_knowledge_revision(
                    revised,
                    original_content=content,
                )
                if revised:
                    version = meta.get("version", 1)
                    self._archive_version(know_path, content, version)

                    meta["version"] = version + 1
                    meta["reconsolidated_failure_count"] = _metadata_count(meta.get("failure_count", 0))
                    meta["success_count"] = 0
                    meta["previous_version"] = f"v{version}"
                    meta["reconsolidated_at"] = datetime.now(UTC).isoformat()

                    self.memory_manager.write_knowledge_with_meta(
                        know_path,
                        revised,
                        meta,
                    )

                    # Activity log event
                    self.activity_logger.log(
                        "knowledge_reconsolidated",
                        summary=(f"Reconsolidated {know_path.name}: v{version} -> v{version + 1}"),
                        meta={
                            "knowledge": know_path.name,
                            "old_version": version,
                            "new_version": version + 1,
                        },
                    )
                    results["updated"] += 1
                    results["updated_files"].append(know_path.name)

                    logger.info(
                        "Reconsolidated knowledge %s: v%d -> v%d",
                        know_path.name,
                        version,
                        version + 1,
                    )
                else:
                    results["skipped"] += 1
                    logger.debug(
                        "Skipping knowledge reconsolidation (LLM returned no revision) for %s",
                        know_path.name,
                    )

            except Exception as e:
                if isinstance(e, InvalidReconsolidationOutput):
                    failure_reason = e.reason
                    output_chars = e.output_chars
                    minimum_chars = e.minimum_chars
                else:
                    failure_reason = f"exception:{type(e).__name__}"
                    output_chars = 0
                    minimum_chars = 0

                self._log_knowledge_reconsolidation_failure(
                    know_path,
                    reason=failure_reason,
                    output_chars=output_chars,
                    minimum_chars=minimum_chars,
                )
                logger.warning(
                    "Knowledge reconsolidation failed for %s: reason=%s output_chars=%d minimum_chars=%d",
                    know_path,
                    failure_reason,
                    output_chars,
                    minimum_chars,
                    exc_info=not isinstance(e, InvalidReconsolidationOutput),
                )
                results["errors"] += 1

        logger.info(
            "Knowledge reconsolidation complete for anima=%s: targets=%d updated=%d skipped=%d errors=%d",
            self.anima_name,
            results["targets_found"],
            results["updated"],
            results["skipped"],
            results["errors"],
        )
        return results

    @staticmethod
    def _validate_knowledge_revision(
        revised: str | None,
        *,
        original_content: str,
    ) -> str:
        """Validate an LLM revision immediately before canonical persistence.

        One-shot SDK backends can occasionally return a provider error as
        ordinary assistant text.  Treating that text as a successful revision
        would replace the canonical knowledge file and archive the corrupted
        value on a later run.  This persistence-boundary guard therefore fails
        closed for empty, provider-error, and implausibly short responses.

        Args:
            revised: Sanitized LLM output, or ``None`` when every backend failed.
            original_content: Current canonical body used to size the minimum
                plausible replacement.

        Returns:
            Stripped, validated revision text.

        Raises:
            InvalidReconsolidationOutput: If the output is unsafe to persist.
        """
        text = (revised or "").strip()
        original_chars = len(original_content.strip())
        minimum_chars = max(32, min(160, original_chars // 20))

        if not text:
            raise InvalidReconsolidationOutput(
                "empty_response",
                output_chars=0,
                minimum_chars=minimum_chars,
            )

        # Provider transports sometimes return a synthetic failure envelope as
        # ordinary assistant text.  Envelope detection is unconditional: a
        # long diagnostic must not bypass the persistence guard.  The narrower
        # bare-message check covers quota/rate-limit text that lacks an API
        # prefix, without treating normal documents about those topics as an
        # error merely because they contain classifier keywords.
        from core.execution.error_classifier import (
            FailoverReason,
            classify_llm_error_message,
            detect_cli_error_envelope,
        )

        explicit_envelope = detect_cli_error_envelope(text) is not None or re.match(
            r"^(?:API\s+Error\s*:|Failed\s+to\s+authenticate\b)",
            text,
            re.IGNORECASE,
        )
        nonempty_lines = [line for line in text.splitlines() if line.strip()]
        has_setext_heading = len(nonempty_lines) >= 2 and re.fullmatch(r"[=-]+", nonempty_lines[1].strip())
        bare_failure = _BARE_QUOTA_FAILURE_RE.match(text) or (
            not has_setext_heading and _BARE_LLM_FAILURE_RE.match(text)
        )
        if explicit_envelope or bare_failure:
            reason, _ = classify_llm_error_message(text)
            reason_name = reason.value if reason is not FailoverReason.UNKNOWN else "provider_error"
            raise InvalidReconsolidationOutput(
                f"llm_error_response:{reason_name}",
                output_chars=len(text),
                minimum_chars=minimum_chars,
            )

        if len(text) < minimum_chars:
            raise InvalidReconsolidationOutput(
                "response_too_short",
                output_chars=len(text),
                minimum_chars=minimum_chars,
            )

        return text

    def _log_knowledge_reconsolidation_failure(
        self,
        know_path: Path,
        *,
        reason: str,
        output_chars: int,
        minimum_chars: int,
    ) -> None:
        """Record a diagnostic activity event without storing rejected text."""
        try:
            relative_path = str(know_path.relative_to(self.anima_dir))
        except ValueError:
            relative_path = know_path.name

        self.activity_logger.log(
            "error",
            summary=(f"Knowledge reconsolidation failed for {know_path.name}: {reason}"),
            meta={
                "phase": "knowledge_reconsolidation",
                "knowledge": relative_path,
                "reason": reason,
                "output_chars": output_chars,
                "minimum_chars": minimum_chars,
            },
            safe=True,
        )

    async def _revise_knowledge(
        self,
        content: str,
        meta: dict[str, Any],
        model: str,
    ) -> str | None:
        """Use LLM to revise knowledge that has been reported as inaccurate.

        Args:
            content: Current knowledge body text.
            meta: Current knowledge metadata (failure_count, confidence, etc.).
            model: LLM model identifier (LiteLLM format).

        Returns:
            Revised knowledge text, or None if revision was not possible.
        """
        failure_count = meta.get("failure_count", 0)
        confidence = meta.get("confidence", 0.5)

        prompt = load_prompt(
            "memory/knowledge_revision",
            content=content[:3000],
            failure_count=failure_count,
            confidence=confidence,
        )

        try:
            from core.memory._llm_utils import one_shot_completion

            text = await one_shot_completion(prompt, model=model, max_tokens=2048) or ""

            # Sanitize LLM output
            from core.memory.maintenance.consolidation import ConsolidationEngine

            text = ConsolidationEngine._sanitize_llm_output(text)

            if text.strip():
                return text.strip()
            return None

        except Exception as e:
            logger.warning(
                "LLM knowledge revision failed: %s",
                e,
            )
            return None

    # ── Activity Logging ───────────────────────────────────────

    async def _log_reconsolidation(
        self,
        proc_path: Path,
        old_version: int,
    ) -> None:
        """Record a procedure_reconsolidated event in the activity log.

        Args:
            proc_path: Path to the reconsolidated procedure file.
            old_version: Version number before reconsolidation.
        """
        self.activity_logger.log(
            "procedure_reconsolidated",
            summary=(f"Reconsolidated {proc_path.name}: v{old_version} -> v{old_version + 1}"),
            meta={
                "procedure": proc_path.name,
                "old_version": old_version,
                "new_version": old_version + 1,
            },
        )
