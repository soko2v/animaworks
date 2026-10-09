# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
"""Fresh-session handover keeps the thread's summary, older turns and recent turns.

Regression (2026-10-08, hana chat thread cd41f711): after a context-threshold
reset the new session received only the tail of the thread history.
``_format_history`` cut it to the last 6000 chars, which dropped the compressed
summary (it listed "M&A事業計画書の作成" as an open topic) and the 17:50 turn
that started the share-sale discussion, so the Anima asked "which business
plan?" and then guessed the wrong one.
"""

from __future__ import annotations

from pathlib import Path

from core.memory.conversation.memory import ConversationMemory
from core.memory.conversation.models import ConversationState, ConversationTurn
from core.memory.conversation.prompt import (
    _HANDOVER_DETAIL_TURNS,
    _format_history,
    _format_turn_digest,
    format_handover_history,
    format_thread_context_note,
)
from core.memory.conversation.shortterm import build_thread_context_note
from core.schemas import ModelConfig

SUMMARY_OPEN_TOPIC = "主なトピック⑦：M&A事業計画書の作成（新規）"
SUMMARY_ACTION_ITEM = "| M&A事業計画書の作成 | hana+okoso | 開始直前 |"
FIRST_REQUEST = "ストライクと別で企業探していて、株式譲渡。Ballinごとかな。障害者雇用の納付金を払っている会社に売りたい"
LATEST_WORK = "手帳スキャン結果 9"


def _turn(role: str, content: str, ts: str) -> ConversationTurn:
    return ConversationTurn(role=role, content=content, timestamp=ts)


def _incident_state() -> ConversationState:
    """A thread shaped like hana/cd41f711 at 23:20 (current message excluded)."""
    summary = (
        "## 会話の要約（統合版）\n\n### 主なトピック①：在宅10分未満利用の調査\n"
        + "調査の詳細。" * 600
        + f"\n\n### {SUMMARY_OPEN_TOPIC}\n- 株式譲渡か事業譲渡かは未確定\n\n"
        + "### アクションアイテム\n| 項目 | 担当 | 状態 |\n|---|---|---|\n"
        + SUMMARY_ACTION_ITEM
    )
    turns = [
        _turn("human", FIRST_REQUEST, "2026-10-08T17:50:55+09:00"),
        _turn(
            "assistant", "いい着眼点です。これはM&Aの動機になる。" + "制度の説明。" * 400, "2026-10-08T17:53:21+09:00"
        ),
    ]
    # The long notebook-scan work that followed and crowded everything else out.
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
    return ConversationState(anima_name="hana", turns=turns, compressed_summary=summary, compressed_turn_count=111)


class TestFormatHandoverHistory:
    def test_tail_truncation_loses_summary_and_first_request(self) -> None:
        """Documents the root cause: the old tail cut keeps only the latest work."""
        old = _format_history(_incident_state(), max_chars=6000)
        assert SUMMARY_OPEN_TOPIC not in old
        assert "株式譲渡" not in old
        assert LATEST_WORK in old

    def test_keeps_summary_first_request_and_latest_work(self) -> None:
        result = format_handover_history(_incident_state(), 12000, summary_chars=5000, digest_chars=2500)

        assert SUMMARY_OPEN_TOPIC in result
        assert SUMMARY_ACTION_ITEM in result
        assert "株式譲渡" in result
        assert "Ballinごとかな" in result
        assert LATEST_WORK in result
        # Order: summary → digest of older turns → recent turns in detail.
        assert result.index(SUMMARY_OPEN_TOPIC) < result.index("Ballinごとかな") < result.index(LATEST_WORK)

    def test_stays_within_budget(self) -> None:
        result = format_handover_history(_incident_state(), 12000, summary_chars=5000, digest_chars=2500)
        # Headers and the omission marker are the only overhead.
        assert len(result) <= 12000 + 100

    def test_long_summary_keeps_its_tail(self) -> None:
        state = ConversationState(
            turns=[_turn("human", "hi", "2026-10-08T10:00:00+09:00")],
            compressed_summary="OLD-HEAD " + "x" * 20000 + " OPEN-ITEM",
            compressed_turn_count=80,
        )
        result = format_handover_history(state, 12000, summary_chars=3000, digest_chars=2500)
        assert "OPEN-ITEM" in result
        assert "OLD-HEAD" not in result
        assert "...(前半省略)..." in result or "...(earlier omitted)..." in result
        assert "hi" in result.rsplit("---", 1)[1]

    def test_every_older_human_request_survives_a_long_backlog(self) -> None:
        turns: list[ConversationTurn] = []
        for i in range(24):
            turns.append(_turn("human", f"REQ-{i:02d} " + "依頼の詳細。" * 100, f"2026-10-08T09:{i:02d}:00+09:00"))
            turns.append(_turn("assistant", f"ANS-{i:02d} " + "回答。" * 300, f"2026-10-08T09:{i:02d}:30+09:00"))
        state = ConversationState(turns=turns)

        result = format_handover_history(state, 12000, summary_chars=5000, digest_chars=2500)

        older_requests = 24 - _HANDOVER_DETAIL_TURNS // 2
        for i in range(older_requests):
            assert f"REQ-{i:02d}" in result
        assert "ANS-23" in result
        digest = _format_turn_digest(turns[:-_HANDOVER_DETAIL_TURNS], 2500)
        assert len(digest) <= 2500

    def test_small_thread_renders_like_format_history(self) -> None:
        turns = [
            _turn("human", "こんにちは", "2026-10-08T10:00:00+09:00"),
            _turn("assistant", "こんにちは！", "2026-10-08T10:00:05+09:00"),
            _turn("human", "今日の予定は？", "2026-10-08T10:01:00+09:00"),
        ]
        state = ConversationState(turns=turns)
        assert format_handover_history(state, 12000, summary_chars=5000, digest_chars=2500) == _format_history(state)

    def test_thread_that_fits_is_not_condensed(self) -> None:
        """Older turns are only digested when the full rendering does not fit."""
        turns = [
            _turn(
                "human" if i % 2 == 0 else "assistant",
                f"TURN-{i:02d} " + "本文。" * 150,
                f"2026-10-08T10:{i:02d}:00+09:00",
            )
            for i in range(12)
        ]
        state = ConversationState(turns=turns, compressed_summary="要約: 未完了の依頼あり", compressed_turn_count=40)
        full = _format_history(state)
        assert len(full) <= 12000

        result = format_handover_history(state, 12000, summary_chars=5000, digest_chars=2500)
        assert result == full
        assert "本文。" * 150 in result.split("TURN-00", 1)[1]

    def test_empty_state_renders_nothing(self) -> None:
        assert format_handover_history(ConversationState(), 12000, summary_chars=5000, digest_chars=2500) == ""


class TestThreadContextNote:
    def test_note_lists_earlier_topics_and_requests(self) -> None:
        note = format_thread_context_note(_incident_state())

        assert note.startswith("## ")
        assert SUMMARY_OPEN_TOPIC in note
        assert SUMMARY_ACTION_ITEM in note
        assert "株式譲渡" in note
        assert "手帳スキャンの確認 9" in note
        assert len(note) <= 4000

    def test_empty_thread_has_no_note(self) -> None:
        assert format_thread_context_note(ConversationState()) == ""

    def test_build_reads_the_thread_conversation(self, tmp_path: Path) -> None:
        anima_dir = tmp_path / "hana"
        conv = ConversationMemory(anima_dir, ModelConfig(), thread_id="cd41f711")
        conv._state = _incident_state()
        conv.save()

        note = build_thread_context_note(anima_dir, "cd41f711")
        assert SUMMARY_OPEN_TOPIC in note
        assert "株式譲渡" in note
        assert build_thread_context_note(anima_dir, "other-thread") == ""

    def test_build_never_raises_on_corrupt_state(self, tmp_path: Path) -> None:
        anima_dir = tmp_path / "hana"
        conv_dir = anima_dir / "state" / "conversations"
        conv_dir.mkdir(parents=True)
        (conv_dir / "cd41f711.json").write_text("{broken", encoding="utf-8")
        assert build_thread_context_note(anima_dir, "cd41f711") == ""
