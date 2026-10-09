# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
"""Session-handover memos carry the thread's earlier topics, not only the latest work.

Regression (2026-10-08, hana chat thread cd41f711): the memo saved at the 22:29
context-threshold reset held only "不明ってなんで？" and the notebook-scan
answer.  The open "M&A business plan" topic of the same thread was absent, and
the next session treated the latest work as the whole conversation.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.agent.session_compactor import _compact_mode_s_shared
from core.execution.base import ExecutionResult
from core.execution.engines.claude._sdk_session import _load_session_id
from core.memory.conversation.memory import ConversationMemory
from core.memory.conversation.models import ConversationState, ConversationTurn
from core.memory.conversation.shortterm import ShortTermMemory
from core.prompt.builder import BuildResult
from core.schemas import ModelConfig
from tests.unit.core.test_agent import _make_agent

THREAD = "cd41f711"
SUMMARY_OPEN_TOPIC = "主なトピック⑦：M&A事業計画書の作成（新規）"
LAST_REQUEST = "不明ってなんで？"
LAST_ANSWER = "手帳スキャンの種別・等級を全23人分読み取りました"


def _save_thread(anima_dir: Path) -> None:
    """Store a thread shaped like hana/cd41f711 at the 22:29 reset."""
    summary = (
        "## 会話の要約（統合版）\n\n### 主なトピック①：在宅10分未満利用の調査\n"
        + "調査の詳細。" * 400
        + f"\n\n### {SUMMARY_OPEN_TOPIC}\n- 対象法人・株式譲渡か事業譲渡かは未確定\n\n"
        + "### アクションアイテム\n| M&A事業計画書の作成 | hana+okoso | 開始直前 |"
    )
    turns = [
        ConversationTurn(
            "human", "ストライクと別で企業探していて、株式譲渡。Ballinごとかな。", "2026-10-08T17:50:55+09:00"
        ),
        ConversationTurn("assistant", "納付金を払っている企業にはM&Aの動機になる。", "2026-10-08T17:53:21+09:00"),
        ConversationTurn("human", "Googleドライブの手帳スキャンを確認して", "2026-10-08T18:06:01+09:00"),
        ConversationTurn("assistant", "手帳スキャンを照合します。" + "照合表。" * 500, "2026-10-08T22:01:02+09:00"),
        ConversationTurn("human", LAST_REQUEST, "2026-10-08T22:16:11+09:00"),
    ]
    conv = ConversationMemory(anima_dir, ModelConfig(), thread_id=THREAD)
    conv._state = ConversationState(
        anima_name="hana", turns=turns, compressed_summary=summary, compressed_turn_count=111
    )
    conv.save()


def _assert_memo_carries_thread_context(anima_dir: Path) -> None:
    memo = ShortTermMemory(anima_dir, session_type="chat", thread_id=THREAD)
    state = memo.load()
    assert state is not None
    assert SUMMARY_OPEN_TOPIC in state.notes
    assert "Ballinごとかな" in state.notes
    rendered = memo.render_for_injection()
    # The latest work is still the memo's main body ...
    assert LAST_ANSWER in rendered
    # ... but the thread's earlier open topic is injected with it.
    assert SUMMARY_OPEN_TOPIC in rendered
    assert rendered.index(LAST_ANSWER) < rendered.index(SUMMARY_OPEN_TOPIC)


@pytest.mark.asyncio
async def test_idle_compaction_memo_carries_thread_context(tmp_path: Path) -> None:
    from core.time_utils import now_local

    anima_dir = tmp_path / "animas" / "hana"
    (anima_dir / "state").mkdir(parents=True)
    _save_thread(anima_dir)
    log_dir = anima_dir / "activity_log"
    log_dir.mkdir()
    entries = [
        {
            "ts": "2026-10-08T22:16:11+09:00",
            "type": "message_received",
            "content": LAST_REQUEST,
            "meta": {"from_type": "human", "thread_id": THREAD},
        },
        {
            "ts": "2026-10-08T22:29:18+09:00",
            "type": "response_sent",
            "content": LAST_ANSWER,
            "meta": {"thread_id": THREAD},
        },
    ]
    (log_dir / f"{now_local().date().isoformat()}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries), encoding="utf-8"
    )
    (anima_dir / "state" / f"current_session_chat_{THREAD}.json").write_text(
        json.dumps({"session_id": "ff2e9f0d", "timestamp": "2026-10-08T13:29:18Z"}), encoding="utf-8"
    )

    assert await _compact_mode_s_shared(anima_dir, "hana", THREAD) is True

    _assert_memo_carries_thread_context(anima_dir)
    state = ShortTermMemory(anima_dir, session_type="chat", thread_id=THREAD).load()
    assert state is not None
    # The extraction note is kept in front of the thread context.
    assert state.notes.startswith("Auto-extracted from activity_log")
    assert _load_session_id(anima_dir, "chat", THREAD) is None


def _mode_s_agent(anima_dir: Path, mode: str = "s"):
    agent = _make_agent(anima_dir, model="claude-sonnet-4-6")
    agent._sdk_available = True
    agent._resolve_execution_mode = MagicMock(return_value=mode)
    agent._run_priming = AsyncMock(return_value=("", ""))
    agent._preflight_size_check = AsyncMock(side_effect=lambda system_prompt, prompt, *a, **k: (system_prompt, prompt))
    return agent


# Mode C (Codex) has its own blocking threshold branch; the memo it saves must
# carry the thread context too (independently of the history that
# ``build_chat_prompt`` injects upstream for Mode C).
@pytest.mark.parametrize("mode", ["s", "c"])
@pytest.mark.asyncio
async def test_blocking_threshold_reset_memo_carries_thread_context(tmp_path: Path, mode: str) -> None:
    _save_thread(tmp_path)
    agent = _mode_s_agent(tmp_path, mode)
    agent._executor.execute = AsyncMock(
        return_value=ExecutionResult(
            text=LAST_ANSWER,
            result_message=SimpleNamespace(num_turns=63, session_id="ff2e9f0d", usage=None),
        )
    )

    with (
        patch("core.agent.cycle.build_system_prompt", return_value=BuildResult(system_prompt="sysprompt")),
        patch("core.agent.cycle.ContextTracker") as tracker_cls,
    ):
        tracker_cls.return_value.threshold_exceeded = True
        tracker_cls.return_value.usage_ratio = 0.656
        await agent.run_cycle(LAST_REQUEST, trigger="message:okoso", thread_id=THREAD)

    _assert_memo_carries_thread_context(tmp_path)


@pytest.mark.asyncio
async def test_stream_threshold_reset_memo_carries_thread_context(tmp_path: Path) -> None:
    _save_thread(tmp_path)
    agent = _mode_s_agent(tmp_path)
    agent._load_stream_retry_config = lambda: {"checkpoint_enabled": False, "retry_max": 0, "retry_delay_s": 0}
    agent._executor.supports_streaming = True

    async def stream(*args, **kwargs):
        yield {"type": "text_delta", "text": LAST_ANSWER}
        yield {
            "type": "done",
            "full_text": LAST_ANSWER,
            "result_message": SimpleNamespace(num_turns=63, session_id="ff2e9f0d", usage=None),
            "usage": {"input_tokens": 10},
        }

    agent._executor.execute_streaming = stream
    with (
        patch("core.agent.cycle.build_system_prompt", return_value=BuildResult(system_prompt="system")),
        patch("core.agent.cycle._save_prompt_log"),
        patch("core.agent.cycle._save_prompt_log_end"),
        patch("core.agent.cycle._log_session_token_usage"),
        patch("core.agent.cycle.ContextTracker") as tracker_cls,
    ):
        tracker_cls.return_value.threshold_exceeded = True
        tracker_cls.return_value.usage_ratio = 0.656
        events = [
            e async for e in agent._run_cycle_streaming_inner(LAST_REQUEST, trigger="message:okoso", thread_id=THREAD)
        ]

    assert events[-1]["type"] == "cycle_done"
    _assert_memo_carries_thread_context(tmp_path)
