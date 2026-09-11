"""Regression tests for one-shot authentication errors at persistence boundaries."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core.memory import _llm_utils as llm

ERRORS = [
    "Invalid API key · Fix external API key",
    "Fix external API key",
    "Failed to authenticate",
    "API Error: 401 unauthorized",
    "Not logged in · Please run /login",
    "invalid x-api-key",
]


@pytest.mark.parametrize("text", [*ERRORS, "OK"])
async def test_compression_rejects_bad_output(tmp_path: Path, text: str) -> None:
    from core.memory.conversation import ConversationMemory
    from core.schemas import ModelConfig

    conv = ConversationMemory(tmp_path, ModelConfig(model="claude-sonnet-4-6", max_tokens=1024))
    for _ in range(45):
        conv.append_turn("human", "Original user context " * 200)
        conv.append_turn("assistant", "Original assistant context " * 200)
    with (
        patch.object(llm, "one_shot_completion", AsyncMock(return_value=text)),
        patch.object(llm, "one_shot_completion_with_model_config", AsyncMock(return_value=text)),
    ):
        result = await conv.compress_if_needed_detailed()
    assert result.status == "deterministic_fallback"
    assert text not in conv.load().compressed_summary


@pytest.mark.parametrize("auth", [None, "max", "api"])
async def test_oneshot_sdk_filters_inherited_auth(monkeypatch, auth) -> None:
    from tests.helpers.mocks import MockClaudeSDKClient, patch_agent_sdk

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-inherited-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-inherited-token")
    cfg = SimpleNamespace(
        anima_defaults=SimpleNamespace(mode_s_auth=auth),
        credentials={"anthropic": SimpleNamespace(api_key="test-config-key", keys={}, base_url="")},
    )
    child: dict[str, str] = {}

    class Client(MockClaudeSDKClient):
        def __init__(self, *, options):
            super().__init__(messages=[])
            self.options = options

        async def __aenter__(self):
            nonlocal child
            import os

            child = {**os.environ, **self.options.env}
            return await super().__aenter__()

    with (
        patch_agent_sdk(),
        patch("claude_agent_sdk.ClaudeSDKClient", Client),
        patch("core.config.load_config", return_value=cfg),
        patch("core.execution._sdk_options._resolve_sdk_cli_path", return_value=None),
    ):
        await llm._try_agent_sdk("test", system_prompt="", model="claude-test", max_tokens=10)
    assert child
    if auth == "api":
        assert child["ANTHROPIC_API_KEY"] == "test-config-key"
    else:
        assert "ANTHROPIC_API_KEY" not in child
        assert "ANTHROPIC_AUTH_TOKEN" not in child


@pytest.mark.parametrize("text", [*ERRORS, "", "OK", "x"])
def test_error_detection(text: str) -> None:
    assert llm.looks_like_cli_error(text)


@pytest.mark.parametrize(
    "text",
    [
        '# Authentication guide\n\nIf you see "Invalid API key", check configuration.',
        'The CLI returned "Failed to authenticate" during testing.',
        "A portrait of a woman with brown eyes in natural light.",
        "1girl, brown_hair, smile",
        "NO_APPEARANCE_DATA",
    ],
)
def test_normal_text_is_preserved(text: str) -> None:
    assert not llm.looks_like_cli_error(text)


@pytest.mark.parametrize("backend", ["litellm", "agent_sdk", "codex_sdk"])
@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("text", ERRORS)
async def test_all_oneshot_backends_reject_auth_errors(backend: str, active: bool, text: str) -> None:
    model = "codex/test" if backend == "codex_sdk" else "anthropic/claude-test"
    with (
        patch.object(llm, "get_llm_kwargs_for_model", return_value={"model": model}),
        patch.object(llm, "get_llm_kwargs_for_model_config", return_value={"model": model}),
        patch.object(
            llm,
            "_litellm_stage_with_guard",
            AsyncMock(
                return_value=(
                    "success" if backend == "litellm" else "fallback",
                    text if backend == "litellm" else None,
                )
            ),
        ),
        patch.object(llm, "_sdk_stage_guarded", return_value=False),
        patch.object(llm, "_mode_s_realm", return_value="max"),
        patch.object(llm, "_try_agent_sdk", AsyncMock(return_value=text)),
        patch.object(llm, "_try_codex_sdk", AsyncMock(return_value=text)),
    ):
        if active:
            result = await llm.one_shot_completion_with_model_config("test", model_config=SimpleNamespace())
        else:
            result = await llm.one_shot_completion("test")
    assert result is None


@pytest.mark.parametrize("style", ["realistic", "anime"])
@pytest.mark.parametrize("existing", [True, False])
@pytest.mark.parametrize("text", [*ERRORS, "OK"])
async def test_error_prompt_not_saved(tmp_path: Path, style: str, existing: bool, text: str) -> None:
    from core.asset_reconciler import _synthesize_prompt_via_llm

    path = tmp_path / "assets" / ("prompt_realistic.txt" if style == "realistic" else "prompt.txt")
    if existing:
        path.parent.mkdir()
        path.write_text("Original valid image prompt", encoding="utf-8")
    with (
        patch.object(llm, "one_shot_completion", AsyncMock(return_value=text)),
        patch("core.asset_reconciler.load_prompt", return_value="test"),
        patch("core.asset_reconciler._resolve_prompt_synthesis_model", return_value=(None, "")),
    ):
        assert await _synthesize_prompt_via_llm(tmp_path, "character", style=style) is None
    assert path.read_text() == "Original valid image prompt" if existing else not path.exists()


@pytest.mark.parametrize("text", [*ERRORS, "OK"])
async def test_error_revision_preserves_knowledge(tmp_path: Path, text: str) -> None:
    from core.memory.manager import MemoryManager
    from core.memory.reconsolidation import ReconsolidationEngine

    path = tmp_path / "knowledge" / "existing.md"
    path.parent.mkdir()
    manager = MemoryManager(tmp_path)
    manager.write_knowledge_with_meta(
        path,
        "# Original knowledge\n\nKeep this content.",
        {
            "version": 3,
            "failure_count": 2,
            "confidence": 0.3,
        },
    )
    original = path.read_bytes()
    engine = ReconsolidationEngine(tmp_path, "test", memory_manager=manager)
    with (
        patch.object(llm, "one_shot_completion", AsyncMock(return_value=text)),
        patch.object(engine, "find_knowledge_reconsolidation_targets", AsyncMock(return_value=[path])),
        patch("core.memory.reconsolidation.load_prompt", return_value="test"),
    ):
        result = await engine.reconsolidate_knowledge(model="test")
        assert await engine._revise_procedure("original", {}, "test") is None
    assert result["updated"] == 0
    assert result["skipped"] == 1
    assert path.read_bytes() == original
    assert not (path.parent / "archive").exists()
