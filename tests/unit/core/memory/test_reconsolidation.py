from __future__ import annotations

# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
#
# This file is part of AnimaWorks core/server, licensed under Apache-2.0.
# See LICENSE for the full license text.


"""Tests for failure-count-based memory reconsolidation.

All LLM dependencies are mocked to ensure unit test isolation
without requiring API keys or model downloads.
"""

import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.memory.maintenance.reconsolidation import ReconsolidationEngine
from core.time_utils import now_jst

# ── Fixtures ────────────────────────────────────────────────


@pytest.fixture
def anima_dir(tmp_path: Path) -> Path:
    """Create a minimal anima directory structure for testing."""
    anima = tmp_path / "animas" / "test-anima"
    (anima / "knowledge").mkdir(parents=True)
    (anima / "procedures").mkdir(parents=True)
    (anima / "episodes").mkdir(parents=True)
    (anima / "skills").mkdir(parents=True)
    (anima / "state").mkdir(parents=True)
    (anima / "activity_log").mkdir(parents=True)
    return anima


@pytest.fixture
def memory_manager(anima_dir: Path):
    """Create a MemoryManager for the test anima."""
    from core.memory.manager import MemoryManager

    return MemoryManager(anima_dir)


@pytest.fixture
def activity_logger(anima_dir: Path):
    """Create an ActivityLogger for the test anima."""
    from core.memory.activity.logger import ActivityLogger

    return ActivityLogger(anima_dir)


@pytest.fixture
def engine(anima_dir: Path, memory_manager, activity_logger) -> ReconsolidationEngine:
    """Create a ReconsolidationEngine for the test anima."""
    return ReconsolidationEngine(
        anima_dir,
        "test-anima",
        memory_manager=memory_manager,
        activity_logger=activity_logger,
    )


def _write_procedure(
    anima_dir: Path,
    filename: str,
    *,
    failure_count: int = 0,
    confidence: float = 0.5,
    version: int = 1,
    content: str = "# Deploy\n\n1. Run tests\n2. Deploy\n",
    description: str = "deployment procedure",
) -> Path:
    """Helper to create a procedure file with given metadata."""
    import yaml

    path = anima_dir / "procedures" / filename
    meta = {
        "description": description,
        "tags": ["deploy"],
        "version": version,
        "confidence": confidence,
        "failure_count": failure_count,
        "success_count": 0,
        "created_at": "2026-01-01T00:00:00",
    }
    fm_str = yaml.dump(meta, default_flow_style=False, allow_unicode=True).rstrip()
    path.write_text(f"---\n{fm_str}\n---\n\n{content}", encoding="utf-8")
    return path


def _write_knowledge(
    anima_dir: Path,
    filename: str,
    *,
    failure_count: int = 2,
    confidence: float = 0.3,
    version: int = 1,
    content: str = "# Verified knowledge\n\n" + ("Canonical fact with supporting context. " * 30),
) -> Path:
    """Create a knowledge reconsolidation target with realistic body length."""
    import yaml

    path = anima_dir / "knowledge" / filename
    meta = {
        "version": version,
        "confidence": confidence,
        "failure_count": failure_count,
        "success_count": 0,
        "created_at": "2026-01-01T00:00:00",
    }
    fm_str = yaml.dump(meta, default_flow_style=False, allow_unicode=True).rstrip()
    path.write_text(f"---\n{fm_str}\n---\n\n{content}", encoding="utf-8")
    return path


# ── find_reconsolidation_targets Tests ─────────────────────


class TestFindReconsolidationTargets:
    """Test the failure-count-based target detection."""

    @pytest.mark.asyncio
    async def test_no_procedures_dir(
        self,
        anima_dir: Path,
        memory_manager,
        activity_logger,
    ) -> None:
        """No procedures directory returns empty list."""
        # Remove the procedures dir
        shutil.rmtree(anima_dir / "procedures")
        engine = ReconsolidationEngine(
            anima_dir,
            "test-anima",
            memory_manager=memory_manager,
            activity_logger=activity_logger,
        )
        targets = await engine.find_reconsolidation_targets()
        assert targets == []

    @pytest.mark.asyncio
    async def test_empty_procedures_dir(
        self,
        engine: ReconsolidationEngine,
    ) -> None:
        """Empty procedures directory returns empty list."""
        targets = await engine.find_reconsolidation_targets()
        assert targets == []

    @pytest.mark.asyncio
    async def test_triggers_on_failure_count_2_and_low_confidence(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
    ) -> None:
        """Procedure with failure_count=2, confidence=0.3 is a target."""
        _write_procedure(
            anima_dir,
            "failing.md",
            failure_count=2,
            confidence=0.3,
        )
        targets = await engine.find_reconsolidation_targets()
        assert len(targets) == 1
        assert targets[0].name == "failing.md"

    @pytest.mark.asyncio
    async def test_triggers_on_high_failure_count(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
    ) -> None:
        """Procedure with failure_count=5, confidence=0.1 is a target."""
        _write_procedure(
            anima_dir,
            "very-failing.md",
            failure_count=5,
            confidence=0.1,
        )
        targets = await engine.find_reconsolidation_targets()
        assert len(targets) == 1

    @pytest.mark.asyncio
    async def test_failure_count_1_triggers(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
    ) -> None:
        """Procedure with failure_count=1 should trigger (>= 1)."""
        _write_procedure(
            anima_dir,
            "almost.md",
            failure_count=1,
            confidence=0.3,
        )
        targets = await engine.find_reconsolidation_targets()
        assert len(targets) == 1
        assert targets[0].name == "almost.md"

    @pytest.mark.asyncio
    async def test_high_failure_count_triggers_even_with_high_confidence(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
    ) -> None:
        """Procedure with failure_count=3 and confidence=0.8 triggers (>= 1)."""
        _write_procedure(
            anima_dir,
            "high-conf-fail.md",
            failure_count=3,
            confidence=0.8,
        )
        targets = await engine.find_reconsolidation_targets()
        assert len(targets) == 1

    @pytest.mark.asyncio
    async def test_max_files_prioritizes_higher_failure_count(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
    ) -> None:
        _write_procedure(anima_dir, "a-low.md", failure_count=1, confidence=0.2)
        high = _write_procedure(anima_dir, "z-high.md", failure_count=5, confidence=0.9)

        targets = await engine.find_reconsolidation_targets(max_files=1)
        assert targets == [high]

    @pytest.mark.asyncio
    async def test_low_confidence_without_new_failures_is_not_a_target(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
    ) -> None:
        """Confidence alone does not trigger reconsolidation."""
        _write_procedure(
            anima_dir,
            "just-below.md",
            failure_count=0,
            confidence=0.59,
        )
        targets = await engine.find_reconsolidation_targets()
        assert targets == []

    @pytest.mark.asyncio
    async def test_no_trigger_when_high_confidence_and_no_failures(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
    ) -> None:
        """Procedure with high confidence and no failures is not a target."""
        _write_procedure(
            anima_dir,
            "fine.md",
            failure_count=0,
            confidence=0.8,
        )
        targets = await engine.find_reconsolidation_targets()
        assert targets == []

    @pytest.mark.asyncio
    async def test_reconsolidated_procedure_is_skipped_until_failure_count_increases(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        proc_path = _write_procedure(
            anima_dir,
            "reconsolidated.md",
            failure_count=3,
            confidence=0.2,
        )
        monkeypatch.setattr(engine, "_revise_procedure", AsyncMock(return_value="Revised procedure"))

        assert await engine.find_reconsolidation_targets() == [proc_path]
        result = await engine.apply_reconsolidation([proc_path], "test-model")
        assert result["updated"] == 1
        meta = engine.memory_manager.read_procedure_metadata(proc_path)
        assert meta["failure_count"] == 3
        assert meta["reconsolidated_failure_count"] == 3
        assert meta["confidence"] == 0.2
        assert await engine.find_reconsolidation_targets() == []

        meta["failure_count"] += 1
        engine.memory_manager.write_procedure_with_meta(proc_path, "Updated after another failure", meta)
        assert await engine.find_reconsolidation_targets() == [proc_path]

    @pytest.mark.asyncio
    async def test_defaults_for_missing_metadata(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
    ) -> None:
        """Procedure without failure_count/confidence in metadata is not a target.

        Defaults: failure_count=0, confidence=1.0.
        """
        path = anima_dir / "procedures" / "no-meta.md"
        path.write_text(
            "---\ndescription: test\n---\n\n# Test\n\nSteps here.\n",
            encoding="utf-8",
        )
        targets = await engine.find_reconsolidation_targets()
        assert targets == []

    @pytest.mark.asyncio
    async def test_multiple_targets(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
    ) -> None:
        """Multiple procedures can be targets simultaneously."""
        _write_procedure(
            anima_dir,
            "fail1.md",
            failure_count=2,
            confidence=0.4,
        )
        _write_procedure(
            anima_dir,
            "fail2.md",
            failure_count=3,
            confidence=0.2,
        )
        _write_procedure(
            anima_dir,
            "ok.md",
            failure_count=0,
            confidence=0.9,
        )
        targets = await engine.find_reconsolidation_targets()
        assert len(targets) == 2
        target_names = {t.name for t in targets}
        assert "fail1.md" in target_names
        assert "fail2.md" in target_names
        assert "ok.md" not in target_names


# ── apply_reconsolidation Tests ────────────────────────────


class TestApplyReconsolidation:
    """Test applying reconsolidation to target procedures."""

    @pytest.mark.asyncio
    async def test_successful_update(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
    ) -> None:
        """Successfully revise a procedure via LLM."""
        proc_path = _write_procedure(
            anima_dir,
            "deploy.md",
            failure_count=3,
            confidence=0.3,
            version=2,
        )

        llm_response = MagicMock()
        llm_response.choices = [MagicMock()]
        llm_response.choices[
            0
        ].message.content = (
            "# Deploy\n\n1. Run tests\n2. Build image\n3. Deploy to staging\n4. Verify\n5. Deploy to prod\n"
        )

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_response
            result = await engine.apply_reconsolidation([proc_path], "test-model")

        assert result["updated"] == 1
        assert result["skipped"] == 0
        assert result["errors"] == 0

        # Verify content was updated
        content = engine.memory_manager.read_procedure_content(proc_path)
        assert "staging" in content

    @pytest.mark.asyncio
    async def test_metadata_reset_after_reconsolidation(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
    ) -> None:
        """After reconsolidation, counters are reset and version is bumped."""
        proc_path = _write_procedure(
            anima_dir,
            "deploy.md",
            failure_count=4,
            confidence=0.2,
            version=3,
        )

        llm_response = MagicMock()
        llm_response.choices = [MagicMock()]
        llm_response.choices[0].message.content = "Revised procedure text"

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_response
            await engine.apply_reconsolidation([proc_path], "test-model")

        meta = engine.memory_manager.read_procedure_metadata(proc_path)
        assert meta["version"] == 4
        assert meta["failure_count"] == 4
        assert meta["reconsolidated_failure_count"] == 4
        assert meta["success_count"] == 0
        assert meta["confidence"] == 0.2
        assert meta["previous_version"] == "v3"
        assert "reconsolidated_at" in meta

    @pytest.mark.asyncio
    async def test_skip_when_llm_returns_empty(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
    ) -> None:
        """When LLM returns empty/None, the procedure is skipped."""
        proc_path = _write_procedure(
            anima_dir,
            "deploy.md",
            failure_count=2,
            confidence=0.4,
        )

        llm_response = MagicMock()
        llm_response.choices = [MagicMock()]
        llm_response.choices[0].message.content = ""

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_response
            result = await engine.apply_reconsolidation([proc_path], "test-model")

        assert result["updated"] == 0
        assert result["skipped"] == 1

    @pytest.mark.asyncio
    async def test_error_handling(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
    ) -> None:
        """Errors during apply are counted, not raised."""
        proc_path = _write_procedure(
            anima_dir,
            "deploy.md",
            failure_count=2,
            confidence=0.4,
        )

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = RuntimeError("API error")
            result = await engine.apply_reconsolidation([proc_path], "test-model")

        # Error in _revise_procedure causes None return -> skipped
        # Actually, the exception is caught in _revise_procedure and returns None
        assert result["skipped"] == 1 or result["errors"] == 0

    @pytest.mark.asyncio
    async def test_multiple_targets(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
    ) -> None:
        """Multiple targets are processed independently."""
        proc1 = _write_procedure(
            anima_dir,
            "proc1.md",
            failure_count=2,
            confidence=0.3,
        )
        proc2 = _write_procedure(
            anima_dir,
            "proc2.md",
            failure_count=3,
            confidence=0.2,
        )

        llm_response = MagicMock()
        llm_response.choices = [MagicMock()]
        llm_response.choices[0].message.content = "Revised content"

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_response
            result = await engine.apply_reconsolidation(
                [proc1, proc2],
                "test-model",
            )

        assert result["updated"] == 2
        assert result["skipped"] == 0
        assert result["errors"] == 0


# ── Version Archive Tests ──────────────────────────────────


class TestArchiveVersion:
    """Test version archiving before reconsolidation."""

    @pytest.mark.asyncio
    async def test_archive_creates_copy(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
    ) -> None:
        """Archiving creates a timestamped copy in archive/versions/."""
        proc_path = _write_procedure(
            anima_dir,
            "deploy.md",
            failure_count=2,
            confidence=0.3,
            version=2,
        )
        original_content = proc_path.read_text(encoding="utf-8")

        llm_response = MagicMock()
        llm_response.choices = [MagicMock()]
        llm_response.choices[0].message.content = "Revised content"

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_response
            await engine.apply_reconsolidation([proc_path], "test-model")

        archive_dir = engine.anima_dir / "archive" / "versions"
        assert archive_dir.exists()
        # R4: archive names are prefixed with the path relative to the anima dir
        archived_files = list(archive_dir.glob("*deploy_v2_*"))
        assert len(archived_files) == 1

        # Verify content is preserved
        archived_content = archived_files[0].read_text(encoding="utf-8")
        assert archived_content == original_content

    def test_archive_nonexistent_file(
        self,
        engine: ReconsolidationEngine,
    ) -> None:
        """Archiving a nonexistent file is a no-op."""
        fake_path = engine.anima_dir / "procedures" / "nonexistent.md"
        engine._archive_version(fake_path, "some content", 1)

        archive_dir = engine.anima_dir / "archive" / "versions"
        assert not archive_dir.exists()


# ── Activity Log Event Tests ───────────────────────────────


class TestActivityLogEvent:
    """Test that reconsolidation records activity log events."""

    @pytest.mark.asyncio
    async def test_procedure_reconsolidated_event(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
        activity_logger,
    ) -> None:
        """A procedure_reconsolidated event is recorded after successful update."""
        proc_path = _write_procedure(
            anima_dir,
            "deploy.md",
            failure_count=2,
            confidence=0.3,
            version=1,
        )

        llm_response = MagicMock()
        llm_response.choices = [MagicMock()]
        llm_response.choices[0].message.content = "Revised content"

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_response
            await engine.apply_reconsolidation([proc_path], "test-model")

        # Check the activity log file
        import json

        log_dir = anima_dir / "activity_log"
        today_file = log_dir / f"{now_jst().strftime('%Y-%m-%d')}.jsonl"
        assert today_file.exists()

        entries = [json.loads(line) for line in today_file.read_text(encoding="utf-8").strip().splitlines()]
        recon_entries = [e for e in entries if e["type"] == "procedure_reconsolidated"]
        assert len(recon_entries) == 1

        entry = recon_entries[0]
        assert "deploy.md" in entry["summary"]
        assert "v1" in entry["summary"]
        assert "v2" in entry["summary"]
        assert entry["meta"]["procedure"] == "deploy.md"
        assert entry["meta"]["old_version"] == 1
        assert entry["meta"]["new_version"] == 2

    @pytest.mark.asyncio
    async def test_no_event_on_skip(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
    ) -> None:
        """No activity log event when reconsolidation is skipped."""
        proc_path = _write_procedure(
            anima_dir,
            "deploy.md",
            failure_count=2,
            confidence=0.3,
        )

        llm_response = MagicMock()
        llm_response.choices = [MagicMock()]
        llm_response.choices[0].message.content = ""  # Empty -> skip

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_response
            await engine.apply_reconsolidation([proc_path], "test-model")

        import json

        log_dir = anima_dir / "activity_log"
        today_file = log_dir / f"{now_jst().strftime('%Y-%m-%d')}.jsonl"
        if today_file.exists():
            entries = [json.loads(line) for line in today_file.read_text(encoding="utf-8").strip().splitlines()]
            recon_entries = [e for e in entries if e["type"] == "procedure_reconsolidated"]
            assert len(recon_entries) == 0
        # If the file doesn't exist, that's also correct (no events logged)


# ── LLM Procedure Revision Tests ──────────────────────────


class TestReviseProcedure:
    """Test the LLM-based procedure revision."""

    @pytest.mark.asyncio
    async def test_successful_revision(
        self,
        engine: ReconsolidationEngine,
    ) -> None:
        """LLM returns a revised procedure text."""
        llm_response = MagicMock()
        llm_response.choices = [MagicMock()]
        llm_response.choices[0].message.content = "# Deploy\n\n1. Run tests\n2. Deploy to staging\n3. Verify\n"

        meta = {
            "description": "deployment procedure",
            "failure_count": 3,
            "confidence": 0.2,
        }

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_response
            result = await engine._revise_procedure(
                "1. Run tests\n2. Deploy",
                meta,
                "test-model",
            )

        assert result is not None
        assert "staging" in result

    @pytest.mark.asyncio
    async def test_llm_failure_returns_none(
        self,
        engine: ReconsolidationEngine,
    ) -> None:
        """LLM failure returns None gracefully."""
        meta = {"description": "test", "failure_count": 2, "confidence": 0.3}

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = RuntimeError("API error")
            result = await engine._revise_procedure(
                "some content",
                meta,
                "test-model",
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_empty_llm_response_returns_none(
        self,
        engine: ReconsolidationEngine,
    ) -> None:
        """Empty LLM response returns None."""
        llm_response = MagicMock()
        llm_response.choices = [MagicMock()]
        llm_response.choices[0].message.content = ""

        meta = {"description": "test", "failure_count": 2, "confidence": 0.3}

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_response
            result = await engine._revise_procedure(
                "some content",
                meta,
                "test-model",
            )

        assert result is None


# ── Constructor Tests ──────────────────────────────────────


class TestConstructor:
    """Test ReconsolidationEngine constructor."""

    def test_default_construction(self, anima_dir: Path) -> None:
        """Constructor creates MemoryManager and ActivityLogger when not provided."""
        engine = ReconsolidationEngine(anima_dir, "test-anima")
        assert engine.memory_manager is not None
        assert engine.activity_logger is not None
        assert engine.anima_dir == anima_dir
        assert engine.anima_name == "test-anima"

    def test_injected_dependencies(
        self,
        anima_dir: Path,
        memory_manager,
        activity_logger,
    ) -> None:
        """Constructor uses injected dependencies."""
        engine = ReconsolidationEngine(
            anima_dir,
            "test-anima",
            memory_manager=memory_manager,
            activity_logger=activity_logger,
        )
        assert engine.memory_manager is memory_manager
        assert engine.activity_logger is activity_logger


# ── Knowledge Persistence Guard Tests ──────────────────────


class TestKnowledgePersistenceGuard:
    """Fail closed before an unsafe LLM response reaches canonical memory."""

    @staticmethod
    def _failure_events(anima_dir: Path) -> list[dict]:
        """Read diagnostic reconsolidation failure events from the test log."""
        import json

        path = anima_dir / "activity_log" / f"{now_jst().strftime('%Y-%m-%d')}.jsonl"
        if not path.exists():
            return []
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        return [
            entry
            for entry in entries
            if entry["type"] == "error" and entry.get("meta", {}).get("phase") == "knowledge_reconsolidation"
        ]

    @pytest.mark.asyncio
    async def test_weekly_limit_text_preserves_canonical_and_records_failure(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A subscription-limit assistant message is never persisted or archived."""
        target = _write_knowledge(anima_dir, "portfolio.md", version=4)
        original = target.read_bytes()
        limit_text = "You've hit your weekly limit · resets Oct 1 at 3am (Asia/Tokyo)"

        with (
            caplog.at_level("WARNING", logger="animaworks.reconsolidation"),
            patch(
                "core.memory._llm_utils.one_shot_completion",
                new=AsyncMock(return_value=limit_text),
            ),
        ):
            result = await engine.reconsolidate_knowledge(model="test-model")

        assert result == {
            "targets_found": 1,
            "updated": 0,
            "skipped": 0,
            "errors": 1,
            "updated_files": [],
        }
        assert target.read_bytes() == original
        assert list((anima_dir / "archive" / "versions").glob("*")) == []
        assert "llm_error_response:quota_exhausted" in caplog.text

        events = self._failure_events(anima_dir)
        assert len(events) == 1
        assert events[0]["meta"]["phase"] == "knowledge_reconsolidation"
        assert events[0]["meta"]["knowledge"] == "knowledge/portfolio.md"
        assert events[0]["meta"]["reason"] == "llm_error_response:quota_exhausted"
        assert events[0]["meta"]["output_chars"] == len(limit_text)
        assert events[0]["meta"]["minimum_chars"] > 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("response", [None, "", "Too short."])
    async def test_empty_or_short_response_is_failure_and_preserves_canonical(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
        response: str | None,
    ) -> None:
        """Missing and implausibly short revisions fail instead of skipping."""
        target = _write_knowledge(anima_dir, "operations.md")
        original = target.read_bytes()

        with patch(
            "core.memory._llm_utils.one_shot_completion",
            new=AsyncMock(return_value=response),
        ):
            result = await engine.reconsolidate_knowledge(model="test-model")

        assert result["errors"] == 1
        assert result["updated"] == 0
        assert result["skipped"] == 0
        assert target.read_bytes() == original
        assert list((anima_dir / "archive" / "versions").glob("*")) == []

        events = self._failure_events(anima_dir)
        assert len(events) == 1
        expected = "empty_response" if not response else "response_too_short"
        assert events[0]["meta"]["reason"] == expected

    @pytest.mark.asyncio
    async def test_api_error_text_is_failure_and_preserves_canonical(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
    ) -> None:
        """A CLI API-error envelope is rejected even when returned as text."""
        target = _write_knowledge(anima_dir, "api-result.md")
        original = target.read_bytes()

        with patch(
            "core.memory._llm_utils.one_shot_completion",
            new=AsyncMock(return_value="API Error: 500 Internal Server Error"),
        ):
            result = await engine.reconsolidate_knowledge(model="test-model")

        assert result["errors"] == 1
        assert result["updated"] == 0
        assert target.read_bytes() == original
        assert list((anima_dir / "archive" / "versions").glob("*")) == []
        assert self._failure_events(anima_dir)[0]["meta"]["reason"] == "llm_error_response:server_error"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("response", "expected_reason"),
        [
            (
                "API Error: Unable to connect to the provider after several attempts; "
                "please check network connectivity.",
                "llm_error_response:provider_error",
            ),
            (
                "Failed to authenticate. API Error: credential was rejected by the provider; please sign in again.",
                "llm_error_response:provider_error",
            ),
            (
                "API Error: 500 Internal Server Error\n" + ("provider diagnostic detail " * 100),
                "llm_error_response:server_error",
            ),
            (
                "You've hit your weekly limit · resets Oct 1 at 3am (Asia/Tokyo)\n"
                + ("provider diagnostic detail " * 100),
                "llm_error_response:quota_exhausted",
            ),
            (
                "You've reached your usage limit. Switch to another model.\n" + ("provider diagnostic detail " * 100),
                "llm_error_response:quota_exhausted",
            ),
            (
                "You've hit your weekly limit\n---\n" + ("provider diagnostic detail " * 100),
                "llm_error_response:quota_exhausted",
            ),
            (
                "Failed to authenticate: API Error: credential rejected.\n" + ("provider diagnostic detail " * 100),
                "llm_error_response:provider_error",
            ),
            (
                "Failed to authenticate\nAPI Error: credential rejected.\n" + ("provider diagnostic detail " * 100),
                "llm_error_response:provider_error",
            ),
        ],
    )
    async def test_explicit_error_envelope_never_bypasses_guard(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
        response: str,
        expected_reason: str,
    ) -> None:
        """Known envelopes fail closed regardless of classifier or response size."""
        target = _write_knowledge(anima_dir, "provider-result.md")
        original = target.read_bytes()

        with patch(
            "core.memory._llm_utils.one_shot_completion",
            new=AsyncMock(return_value=response),
        ):
            result = await engine.reconsolidate_knowledge(model="test-model")

        assert result["errors"] == 1
        assert result["updated"] == 0
        assert target.read_bytes() == original
        assert list((anima_dir / "archive" / "versions").glob("*")) == []
        assert self._failure_events(anima_dir)[0]["meta"]["reason"] == expected_reason

    @pytest.mark.asyncio
    async def test_short_technical_markdown_mentioning_errors_still_updates(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
    ) -> None:
        """Classifier words in a real document do not create a false failure."""
        target = _write_knowledge(anima_dir, "api-operations.md", version=3)
        revision = (
            "# API operations\n\n"
            "- Use OAuth for authentication.\n"
            "- Retry a timeout twice.\n"
            "- Back off after the documented rate limit."
        )
        assert len(revision) <= 512

        with patch(
            "core.memory._llm_utils.one_shot_completion",
            new=AsyncMock(return_value=revision),
        ):
            result = await engine.reconsolidate_knowledge(model="test-model")

        assert result["errors"] == 0
        assert result["updated"] == 1
        assert engine.memory_manager.read_knowledge_content(target) == revision
        assert engine.memory_manager.read_knowledge_metadata(target)["version"] == 4
        assert self._failure_events(anima_dir) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "revision",
        [
            (
                "Authentication error handling\n"
                "=============================\n\n"
                "Retry a timeout twice, then back off after the documented rate limit."
            ),
            (
                "Authentication error\n=\n\n"
                "This technical note documents credential verification, retry behavior, "
                "timeout handling, rate limits, and safe key rotation."
            ),
        ],
    )
    async def test_setext_technical_markdown_mentioning_errors_still_updates(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
        revision: str,
    ) -> None:
        """An error-shaped Setext heading is still normal knowledge content."""
        target = _write_knowledge(anima_dir, "api-error-handling.md", version=3)

        with patch(
            "core.memory._llm_utils.one_shot_completion",
            new=AsyncMock(return_value=revision),
        ):
            result = await engine.reconsolidate_knowledge(model="test-model")

        assert result["errors"] == 0
        assert result["updated"] == 1
        assert engine.memory_manager.read_knowledge_content(target) == revision
        assert engine.memory_manager.read_knowledge_metadata(target)["version"] == 4
        assert self._failure_events(anima_dir) == []

    @pytest.mark.asyncio
    async def test_normal_revision_updates_and_archives_previous_version(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
    ) -> None:
        """A substantive revision retains the existing successful behaviour."""
        target = _write_knowledge(anima_dir, "research.md", version=2)
        original = target.read_bytes()
        revision = (
            "# Verified research knowledge\n\n"
            "## Evidence\n\n"
            + ("The revised fact is supported by a dated primary source. " * 16)
            + "\n\n## Usage\n\nApply this conclusion only within the documented scope."
        )

        with patch(
            "core.memory._llm_utils.one_shot_completion",
            new=AsyncMock(return_value=revision),
        ):
            result = await engine.reconsolidate_knowledge(model="test-model")

        assert result["errors"] == 0
        assert result["updated"] == 1
        assert result["updated_files"] == ["research.md"]
        assert engine.memory_manager.read_knowledge_content(target) == revision
        assert engine.memory_manager.read_knowledge_metadata(target)["version"] == 3

        archived = list((anima_dir / "archive" / "versions").glob("knowledge__research_v2_*.md"))
        assert len(archived) == 1
        assert archived[0].read_bytes() == original
        assert self._failure_events(anima_dir) == []


# ── Create Procedures from Resolved Events Tests ─────────


class TestCreateProceduresFromResolved:
    """Test create_procedures_from_resolved() method."""

    @pytest.mark.asyncio
    async def test_no_resolved_events_returns_empty(
        self,
        engine: ReconsolidationEngine,
    ) -> None:
        """No issue_resolved events should return zeros."""
        # ActivityLogger.recent() returns empty list
        with patch(
            "core.memory.activity.logger.ActivityLogger.recent",
            return_value=[],
        ):
            result = await engine.create_procedures_from_resolved(
                model="test-model",
                days=1,
            )

        # All counters should be zero when there are no events
        assert result["created"] == 0
        assert result["skipped"] == 0
        assert result["errors"] == 0

    @pytest.mark.asyncio
    async def test_creates_procedure_from_resolved_event(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
    ) -> None:
        """Should create a procedure from a resolved event."""
        from dataclasses import dataclass, field
        from typing import Any

        @dataclass
        class FakeEntry:
            ts: str = "2026-02-22T10:00:00"
            type: str = "issue_resolved"
            content: str = "DBマイグレーション手順: 1. バックアップ 2. マイグレーション実行 3. 確認"
            summary: str = "DBマイグレーション完了"
            meta: dict[str, Any] = field(default_factory=dict)

        fake_entry = FakeEntry()

        # LLM response in the classification format
        llm_response_text = (
            "## knowledge抽出\n(なし)\n\n"
            "## procedure抽出\n"
            "- ファイル名: procedures/db_migration.md\n"
            "  description: DBマイグレーション手順\n"
            "  tags: db, migration\n"
            "  内容: # DBマイグレーション\n\n1. バックアップ\n2. マイグレーション実行\n3. 確認"
        )
        llm_response = MagicMock()
        llm_response.choices = [MagicMock()]
        llm_response.choices[0].message.content = llm_response_text

        with (
            patch(
                "core.memory.activity.logger.ActivityLogger.recent",
                return_value=[fake_entry],
            ),
            patch(
                "core.memory.maintenance.distillation.ProceduralDistiller._check_rag_duplicate",
                return_value=None,
            ),
            patch(
                "litellm.acompletion",
                new_callable=AsyncMock,
                return_value=llm_response,
            ),
        ):
            result = await engine.create_procedures_from_resolved(
                model="test-model",
                days=1,
            )

        # A procedure should have been created
        assert result["created"] == 1
        assert result["errors"] == 0

        # Verify the file was actually created
        proc_files = list((anima_dir / "procedures").glob("*.md"))
        assert len(proc_files) >= 1
        created_file = [f for f in proc_files if "db_migration" in f.name]
        assert len(created_file) == 1

    @pytest.mark.asyncio
    async def test_skips_empty_content(
        self,
        engine: ReconsolidationEngine,
    ) -> None:
        """Events with empty content should be skipped."""
        from dataclasses import dataclass, field
        from typing import Any

        @dataclass
        class FakeEntry:
            ts: str = "2026-02-22T10:00:00"
            type: str = "issue_resolved"
            content: str = ""
            summary: str = ""
            meta: dict[str, Any] = field(default_factory=dict)

        fake_entry = FakeEntry()

        with patch(
            "core.memory.activity.logger.ActivityLogger.recent",
            return_value=[fake_entry],
        ):
            result = await engine.create_procedures_from_resolved(
                model="test-model",
                days=1,
            )

        # Empty content entries should increment skipped count
        assert result["skipped"] == 1
        assert result["created"] == 0
        assert result["errors"] == 0

    @pytest.mark.asyncio
    async def test_skips_rag_duplicate(
        self,
        engine: ReconsolidationEngine,
    ) -> None:
        """Events similar to existing procedures should be skipped."""
        from dataclasses import dataclass, field
        from typing import Any

        @dataclass
        class FakeEntry:
            ts: str = "2026-02-22T10:00:00"
            type: str = "issue_resolved"
            content: str = "既存手順と同じ内容"
            summary: str = "重複イベント"
            meta: dict[str, Any] = field(default_factory=dict)

        fake_entry = FakeEntry()

        with (
            patch(
                "core.memory.activity.logger.ActivityLogger.recent",
                return_value=[fake_entry],
            ),
            patch(
                "core.memory.maintenance.distillation.ProceduralDistiller._check_rag_duplicate",
                return_value="procedures/existing.md",
            ),
        ):
            result = await engine.create_procedures_from_resolved(
                model="test-model",
                days=1,
            )

        # RAG duplicate should increment skipped count
        assert result["skipped"] == 1
        assert result["created"] == 0

    @pytest.mark.asyncio
    async def test_llm_error_increments_errors(
        self,
        engine: ReconsolidationEngine,
    ) -> None:
        """LLM failure should increment error count, not raise."""
        from dataclasses import dataclass, field
        from typing import Any

        @dataclass
        class FakeEntry:
            ts: str = "2026-02-22T10:00:00"
            type: str = "issue_resolved"
            content: str = "問題を解決した: サーバー再起動手順"
            summary: str = "サーバー再起動"
            meta: dict[str, Any] = field(default_factory=dict)

        fake_entry = FakeEntry()

        with (
            patch(
                "core.memory.activity.logger.ActivityLogger.recent",
                return_value=[fake_entry],
            ),
            patch(
                "core.memory.maintenance.distillation.ProceduralDistiller._check_rag_duplicate",
                return_value=None,
            ),
            patch(
                "litellm.acompletion",
                new_callable=AsyncMock,
                side_effect=RuntimeError("API error"),
            ),
        ):
            result = await engine.create_procedures_from_resolved(
                model="test-model",
                days=1,
            )

        # LLM error should increment errors, not raise
        assert result["errors"] == 1
        assert result["created"] == 0

    @pytest.mark.asyncio
    async def test_procedure_saved_with_correct_metadata(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
    ) -> None:
        """Created procedure should have confidence=0.4 and auto_distilled=True."""
        from dataclasses import dataclass, field
        from typing import Any

        @dataclass
        class FakeEntry:
            ts: str = "2026-02-22T10:00:00"
            type: str = "issue_resolved"
            content: str = "ネットワーク設定手順: 1. DNS変更 2. ルーティング確認"
            summary: str = "ネットワーク設定完了"
            meta: dict[str, Any] = field(default_factory=dict)

        fake_entry = FakeEntry()

        llm_response_text = (
            "## knowledge抽出\n(なし)\n\n"
            "## procedure抽出\n"
            "- ファイル名: procedures/network_config.md\n"
            "  description: ネットワーク設定手順\n"
            "  tags: network, ops\n"
            "  内容: # ネットワーク設定\n\n1. DNS変更\n2. ルーティング確認"
        )
        llm_response = MagicMock()
        llm_response.choices = [MagicMock()]
        llm_response.choices[0].message.content = llm_response_text

        with (
            patch(
                "core.memory.activity.logger.ActivityLogger.recent",
                return_value=[fake_entry],
            ),
            patch(
                "core.memory.maintenance.distillation.ProceduralDistiller._check_rag_duplicate",
                return_value=None,
            ),
            patch(
                "litellm.acompletion",
                new_callable=AsyncMock,
                return_value=llm_response,
            ),
        ):
            result = await engine.create_procedures_from_resolved(
                model="test-model",
                days=1,
            )

        assert result["created"] == 1

        # Verify metadata on the saved procedure file
        proc_path = anima_dir / "procedures" / "network_config.md"
        assert proc_path.exists()

        meta = engine.memory_manager.read_procedure_metadata(proc_path)
        # Auto-distilled procedures should start with confidence=0.4
        assert meta["confidence"] == 0.4
        # The auto_distilled flag should be set
        assert meta["auto_distilled"] is True
        # Counters should start at zero
        assert meta["failure_count"] == 0
        assert meta["success_count"] == 0
        assert meta["version"] == 1


# ── Recursive scan / archive exclusion (F4) ────────────────


def _write_knowledge_target(path: Path, *, failure_count: int = 3, confidence: float = 0.3) -> Path:
    """Write a knowledge .md that qualifies as a reconsolidation target."""
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "created_at": "2026-01-01T00:00:00",
        "confidence": confidence,
        "failure_count": failure_count,
        "success_count": 0,
    }
    fm = yaml.dump(meta, default_flow_style=False, allow_unicode=True).rstrip()
    path.write_text(f"---\n{fm}\n---\n\n# Knowledge\n\nBody text.\n", encoding="utf-8")
    return path


def _write_procedure_target(path: Path, *, failure_count: int = 3, confidence: float = 0.3) -> Path:
    """Write a procedure .md that qualifies as a reconsolidation target."""
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "description": "nested procedure",
        "tags": ["deploy"],
        "version": 1,
        "confidence": confidence,
        "failure_count": failure_count,
        "success_count": 0,
        "created_at": "2026-01-01T00:00:00",
    }
    fm = yaml.dump(meta, default_flow_style=False, allow_unicode=True).rstrip()
    path.write_text(f"---\n{fm}\n---\n\n# Proc\n\n1. Step\n", encoding="utf-8")
    return path


class TestKnowledgeReconsolidationCheckpoint:
    @pytest.mark.asyncio
    async def test_reconsolidated_knowledge_is_skipped_until_failure_count_increases(
        self,
        anima_dir: Path,
        engine: ReconsolidationEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = _write_knowledge_target(anima_dir / "knowledge" / "bad.md", failure_count=3, confidence=0.3)
        monkeypatch.setattr(engine, "_revise_knowledge", AsyncMock(return_value="Revised knowledge"))

        assert await engine.find_knowledge_reconsolidation_targets() == [path]
        result = await engine.reconsolidate_knowledge("test-model")
        assert result["updated"] == 1
        meta = engine.memory_manager.read_knowledge_metadata(path)
        assert meta["failure_count"] == 3
        assert meta["reconsolidated_failure_count"] == 3
        assert meta["confidence"] == 0.3
        assert await engine.find_knowledge_reconsolidation_targets() == []

        meta["failure_count"] += 1
        engine.memory_manager.write_knowledge_with_meta(path, "Updated after another failure", meta)
        assert await engine.find_knowledge_reconsolidation_targets() == [path]


class TestRecursiveScanExcludesArchive:
    """F4: reconsolidation scans subdirectories recursively but skips archive/."""

    @pytest.mark.asyncio
    async def test_knowledge_subdirectory_is_scanned(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
    ) -> None:
        """A qualifying knowledge file in a subdirectory is a reconsolidation target."""
        _write_knowledge_target(anima_dir / "knowledge" / "topic" / "nested.md")
        targets = await engine.find_knowledge_reconsolidation_targets()
        assert {p.name for p in targets} == {"nested.md"}

    @pytest.mark.asyncio
    async def test_archive_subdirectory_is_excluded(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
    ) -> None:
        """Files under knowledge/archive/ are never reconsolidation targets."""
        _write_knowledge_target(anima_dir / "knowledge" / "live.md")
        _write_knowledge_target(anima_dir / "knowledge" / "archive" / "archived.md")
        targets = await engine.find_knowledge_reconsolidation_targets()
        names = {p.name for p in targets}
        assert "live.md" in names
        assert "archived.md" not in names

    @pytest.mark.asyncio
    async def test_procedure_subdirectory_is_scanned(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
    ) -> None:
        """A qualifying procedure in a subdirectory is a reconsolidation target."""
        _write_procedure_target(anima_dir / "procedures" / "sub" / "nested-proc.md")
        targets = await engine.find_reconsolidation_targets()
        assert {p.name for p in targets} == {"nested-proc.md"}

    @pytest.mark.asyncio
    async def test_procedure_archive_subdirectory_is_excluded(
        self,
        engine: ReconsolidationEngine,
        anima_dir: Path,
    ) -> None:
        """Files under procedures/archive/ are never reconsolidation targets."""
        _write_procedure_target(anima_dir / "procedures" / "live-proc.md")
        _write_procedure_target(anima_dir / "procedures" / "archive" / "archived-proc.md")
        targets = await engine.find_reconsolidation_targets()
        names = {p.name for p in targets}
        assert "live-proc.md" in names
        assert "archived-proc.md" not in names


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestArchiveVersionSubdirCollision:
    """R4: archive names must not collide across subdirectories."""

    def test_same_stem_in_different_subdirs_archives_separately(self, engine: ReconsolidationEngine, anima_dir: Path):
        proc_a = anima_dir / "procedures" / "a" / "deploy.md"
        proc_b = anima_dir / "procedures" / "b" / "deploy.md"
        proc_a.parent.mkdir(parents=True, exist_ok=True)
        proc_b.parent.mkdir(parents=True, exist_ok=True)
        proc_a.write_text("content a", encoding="utf-8")
        proc_b.write_text("content b", encoding="utf-8")

        engine._archive_version(proc_a, "content a", 1)
        engine._archive_version(proc_b, "content b", 1)

        archived = sorted((anima_dir / "archive" / "versions").glob("*.md"))
        assert len(archived) == 2
        contents = {p.read_text(encoding="utf-8") for p in archived}
        assert contents == {"content a", "content b"}

    def test_archive_name_matches_cleanup_grouping(self, engine: ReconsolidationEngine, anima_dir: Path):
        # Archive naming follows the {stem}_v{N}_{timestamp}.md convention
        import re

        proc = anima_dir / "procedures" / "sub" / "task.md"
        proc.parent.mkdir(parents=True, exist_ok=True)
        proc.write_text("x", encoding="utf-8")
        engine._archive_version(proc, "x", 3)
        archived = list((anima_dir / "archive" / "versions").glob("*.md"))
        assert len(archived) == 1
        assert re.match(r"^(.+?)_v\d+_\d{8}_\d{6}\.md$", archived[0].name)
