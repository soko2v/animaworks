"""Tests for shortterm (session handoff) budget protection.

Verifies that shortterm content is NOT trimmed by the framework target,
only by the hard ceiling. Also checks that dropping shortterm items
emits a WARNING-level log.
"""
from __future__ import annotations

import logging

from core.prompt.assembler import PromptBudget, SectionEntry, _allocate_sections


def _by_id(allocated: list[SectionEntry], section_id: str) -> SectionEntry | None:
    return next((section for section in allocated if section.id == section_id), None)


class TestShorttermBudgetProtection:
    """shortterm must survive framework target trim but yield to ceiling."""

    def test_shortterm_survives_target_trim(self) -> None:
        """shortterm items are NOT dropped when total exceeds target but
        stays within ceiling."""
        identity_content = "identity " * 10  # ~10 tokens
        shortterm_content = "handoff " * 500  # ~500 tokens, well above target=100

        sections = [
            SectionEntry("identity", 1, "rigid", identity_content),
            SectionEntry(
                "shortterm",
                3,
                "elastic",
                shortterm_content,
                budget_group="shortterm",
            ),
        ]

        allocated = _allocate_sections(
            sections,
            PromptBudget(target=100, ceiling=5000),
        )

        shortterm = _by_id(allocated, "shortterm")
        assert shortterm is not None, "shortterm must NOT be dropped by target trim"
        assert shortterm.content == shortterm_content, (
            "shortterm content must be fully preserved when within ceiling"
        )

    def test_shortterm_dropped_at_ceiling(self) -> None:
        """shortterm items ARE dropped when total exceeds the hard ceiling."""
        identity_content = "identity " * 10
        shortterm_content = "handoff " * 500

        sections = [
            SectionEntry("identity", 1, "rigid", identity_content),
            SectionEntry(
                "shortterm",
                3,
                "elastic",
                shortterm_content,
                budget_group="shortterm",
            ),
        ]

        # ceiling=100 is too small to hold both identity + shortterm
        allocated = _allocate_sections(
            sections,
            PromptBudget(target=100, ceiling=100),
        )

        shortterm = _by_id(allocated, "shortterm")
        # Should be dropped or severely trimmed
        assert shortterm is None or len(shortterm.content) < len(shortterm_content), (
            "shortterm must be trimmed/dropped when exceeding ceiling"
        )

    def test_framework_elastic_still_trimmed_by_target(self) -> None:
        """Regular framework elastic items are still trimmed by target
        (regression guard: shortterm protection must not break framework trim)."""
        identity_content = "identity"
        framework_content = "framework " * 200
        shortterm_content = "handoff " * 50

        sections = [
            SectionEntry("identity", 1, "rigid", identity_content),
            SectionEntry(
                "optional_framework",
                3,
                "elastic",
                framework_content,
                budget_group="framework",
            ),
            SectionEntry(
                "shortterm",
                3,
                "elastic",
                shortterm_content,
                budget_group="shortterm",
            ),
        ]

        allocated = _allocate_sections(
            sections,
            PromptBudget(target=50, ceiling=5000),
        )

        framework = _by_id(allocated, "optional_framework")
        shortterm = _by_id(allocated, "shortterm")
        # framework elastic should be trimmed by target
        assert framework is None or len(framework.content) < len(framework_content), (
            "framework elastic must still be trimmed by target"
        )
        # shortterm should survive
        assert shortterm is not None, (
            "shortterm must survive even when framework elastic is trimmed"
        )


class TestShorttermDropWarning:
    """Dropping shortterm items must log at WARNING level."""

    def test_warning_log_on_shortterm_drop(self, caplog) -> None:  # type: ignore[no-untyped-def]
        """When shortterm items are dropped by ceiling, the allocation log
        must be at WARNING level."""
        identity_content = "identity " * 10
        shortterm_content = "handoff " * 500

        sections = [
            SectionEntry("identity", 1, "rigid", identity_content),
            SectionEntry(
                "shortterm",
                3,
                "elastic",
                shortterm_content,
                budget_group="shortterm",
            ),
        ]

        with caplog.at_level(logging.DEBUG, logger="animaworks.prompt_builder"):
            _allocate_sections(
                sections,
                PromptBudget(target=100, ceiling=100),
            )

        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "shortterm" in r.getMessage()
        ]
        assert warning_records, (
            "Dropping shortterm must produce a WARNING log"
        )

    def test_info_log_when_non_shortterm_dropped(self, caplog) -> None:  # type: ignore[no-untyped-def]
        """When only non-shortterm items are dropped, log stays at INFO."""
        identity_content = "identity " * 10
        framework_content = "framework " * 500

        sections = [
            SectionEntry("identity", 1, "rigid", identity_content),
            SectionEntry(
                "optional",
                3,
                "elastic",
                framework_content,
                budget_group="framework",
            ),
        ]

        with caplog.at_level(logging.DEBUG, logger="animaworks.prompt_builder"):
            _allocate_sections(
                sections,
                PromptBudget(target=100, ceiling=5000),
            )

        allocation_records = [
            r for r in caplog.records
            if "Prompt allocation:" in r.getMessage()
        ]
        if allocation_records:
            # Should be INFO, not WARNING
            for rec in allocation_records:
                assert rec.levelno == logging.INFO, (
                    "Dropping non-shortterm items should log at INFO, not WARNING"
                )

    def test_shortterm_trim_from_tail_default(self) -> None:
        """shortterm sections should trim from tail (preserve the head which
        contains the original request)."""
        head = "## original request\n\n" + "original " * 50
        tail = "## later context\n\n" + "later " * 50
        content = head + "\n\n" + tail

        sections = [
            SectionEntry("identity", 1, "rigid", "identity " * 10),
            SectionEntry(
                "shortterm",
                3,
                "elastic",
                content,
                budget_group="shortterm",
            ),
        ]

        # ceiling is tight enough to force trim but not complete removal
        allocated = _allocate_sections(
            sections,
            PromptBudget(target=50, ceiling=120),
        )

        shortterm = _by_id(allocated, "shortterm")
        if shortterm is not None:
            # head (original request) should be preserved, tail dropped
            assert "original" in shortterm.content, (
                "shortterm must preserve the head (original request)"
            )
