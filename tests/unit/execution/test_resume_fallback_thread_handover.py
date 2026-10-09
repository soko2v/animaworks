# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
"""The first prompt of a fresh Mode S chat session carries the whole thread.

Regression (2026-10-08, hana chat thread cd41f711): the context-threshold reset
at 22:29 cleared the SDK session; the next message at 23:20 started a fresh
session whose first prompt held only the last 6000 chars of the thread (the
notebook-scan work).  The compressed summary with the open "M&A business plan"
topic and the 17:50 share-sale request were cut off, so "事業計画どう書いたら
いいかな" was answered with "which business plan?".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.execution.engines.claude.agent_sdk import _RESUME_FALLBACK_HISTORY_CHARS, _with_resume_fallback_history
from core.memory.conversation.memory import ConversationMemory
from core.memory.conversation.models import ConversationState, ConversationTurn
from core.schemas import ModelConfig

THREAD = "cd41f711"
CURRENT_MESSAGE = "一旦これはおいといて、事業計画どう書いたらいいかな"
SUMMARY_OPEN_TOPIC = "主なトピック⑦：M&A事業計画書の作成（新規）"
LATEST_MARKER = "[最新メッセージ]"


def _turn(role: str, content: str, ts: str) -> ConversationTurn:
    return ConversationTurn(role=role, content=content, timestamp=ts)


@pytest.fixture
def anima_dir(tmp_path: Path) -> Path:
    """Hana's thread as stored at 23:20, the current message already appended."""
    d = tmp_path / "animas" / "hana"
    summary = (
        "## 会話の要約（統合版）\n\n### 主なトピック①：在宅10分未満利用の調査\n"
        + "調査の詳細。" * 600
        + f"\n\n### {SUMMARY_OPEN_TOPIC}\n- 対象法人・株式譲渡か事業譲渡かは未確定\n\n"
        + "### アクションアイテム\n| M&A事業計画書の作成 | hana+okoso | 開始直前 |"
    )
    turns = [
        _turn("human", "ストライクと別で企業探していて、株式譲渡。Ballinごとかな。", "2026-10-08T17:50:55+09:00"),
        _turn(
            "assistant",
            "障害者雇用の納付金を払っている企業にはM&Aの動機になる。" + "説明。" * 800,
            "2026-10-08T17:53:21+09:00",
        ),
    ]
    for i in range(10):
        hour = 18 + i // 2
        turns.append(_turn("human", f"手帳スキャンの確認 {i}", f"2026-10-08T{hour:02d}:{(i % 2) * 30:02d}:00+09:00"))
        turns.append(
            _turn(
                "assistant",
                f"手帳スキャン結果 {i}: " + "照合表。" * 700,
                f"2026-10-08T{hour:02d}:{(i % 2) * 30 + 5:02d}:00+09:00",
            )
        )
    turns.append(_turn("human", CURRENT_MESSAGE, "2026-10-08T23:20:26+09:00"))
    conv = ConversationMemory(d, ModelConfig(), thread_id=THREAD)
    conv._state = ConversationState(
        anima_name="hana", turns=turns, compressed_summary=summary, compressed_turn_count=111
    )
    conv.save()
    return d


@pytest.mark.parametrize("reason", ["no_session", "resume_failed"])
def test_fresh_session_prompt_keeps_thread_topics_and_recent_work(anima_dir: Path, reason: str) -> None:
    prompt = _with_resume_fallback_history(
        anima_dir, ModelConfig(model="claude-opus-4-6"), CURRENT_MESSAGE, "chat", THREAD, reason=reason
    )
    history, _, latest = prompt.partition(LATEST_MARKER)

    # Main topic and open request from the compressed summary.
    assert SUMMARY_OPEN_TOPIC in history
    assert "M&A事業計画書の作成 | hana+okoso" in history
    # The 17:50 turn that started the topic, beyond the last 15 turns.
    assert "Ballinごとかな" in history
    # The latest work is still there.
    assert "手帳スキャン結果 9" in history
    # The current message is the prompt itself, not part of the history.
    assert CURRENT_MESSAGE not in history
    assert latest.strip() == CURRENT_MESSAGE


def test_fresh_session_history_is_bounded(anima_dir: Path) -> None:
    prompt = _with_resume_fallback_history(
        anima_dir, ModelConfig(model="claude-opus-4-6"), CURRENT_MESSAGE, "chat", THREAD, reason="no_session"
    )
    # Lead text, separators and section headers are the only overhead.
    assert len(prompt) <= _RESUME_FALLBACK_HISTORY_CHARS + len(CURRENT_MESSAGE) + 400
