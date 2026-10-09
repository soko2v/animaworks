# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
"""E2E: a chat PDF reaches every execution engine in a readable form.

The real DigitalAnima message pipeline runs; only the engine call
(``AgentCore.run_cycle_streaming`` / ``run_cycle``) is replaced by a fake
that records what the engine would receive:

* Claude Agent SDK (mode ``s``): the PDF as a native ``document`` block
  built by the real ``_build_sdk_query_input``.
* Every other engine (e.g. Codex/GPT, mode ``c``): the extracted text and
  the saved path inside the prompt text.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core import chat_attachments as ca
from core.execution.engines.claude._sdk_session import (
    _build_sdk_query_input,
    _clear_session_id,
    _load_session_id,
    _save_session_id,
)
from core.schemas import CycleResult
from tests.helpers.pdf_fixtures import make_pdf

pytestmark = pytest.mark.skipif(
    ca.find_poppler_tool("pdftotext") is None or ca.find_poppler_tool("pdfinfo") is None,
    reason="poppler (pdftotext/pdfinfo) not installed",
)

PDF = make_pdf(["Quarterly memo: the code word is ALPHA-42", "Page two says BLUE-7"])


def _store(anima_dir: Path, name: str = "memo.pdf") -> str:
    """Store a PDF exactly like the chat route does (analysis + AttachmentStore)."""
    inspection, text, status = ca.analyse_pdf_bytes(PDF)
    with ca.AttachmentStore(anima_dir) as store:
        names = ca.store_chat_pdf(
            store,
            data=PDF,
            display_name=name,
            timestamp="20261009_110000",
            inspection=inspection,
            text=text,
            status=status,
        )
    return f"attachments/{names[0]}"


async def _sdk_payload(prompt: str, images: Any) -> Any:
    """Materialise what ClaudeSDKClient.query() would receive."""
    query_input = _build_sdk_query_input(prompt, images)
    if isinstance(query_input, str):
        return query_input
    return [message async for message in query_input]


def _remember_sdk_session(dp: Any, mode: str, thread_id: str) -> None:
    """Like the real Claude Agent SDK: the first turn of a thread starts a session whose id is kept."""
    if mode == "s" and _load_session_id(dp.anima_dir, "chat", thread_id) is None:
        _save_session_id(dp.anima_dir, f"sess-{thread_id}", "chat", thread_id)


def _install_fake_engine(dp: Any, mode: str, seen: list[dict[str, Any]]) -> None:
    dp.agent._resolve_execution_mode = lambda config=None: mode

    async def fake_stream(prompt: str, **kwargs: Any):
        seen.append(
            {
                "prompt": prompt,
                "sdk": await _sdk_payload(prompt, kwargs.get("images")),
                "documents": ca.chat_sdk_document_blocks(),
            }
        )
        _remember_sdk_session(dp, mode, kwargs.get("thread_id", "default"))
        yield {"type": "text_delta", "text": "ok"}
        yield {
            "type": "cycle_done",
            "cycle_result": {
                "trigger": kwargs.get("trigger", ""),
                "action": "responded",
                "summary": "ok",
                "thread_id": kwargs.get("thread_id", "default"),
            },
        }

    async def fake_cycle(prompt: str, **kwargs: Any) -> CycleResult:
        seen.append({"prompt": prompt, "sdk": await _sdk_payload(prompt, kwargs.get("images"))})
        _remember_sdk_session(dp, mode, kwargs.get("thread_id", "default"))
        return CycleResult(trigger=kwargs.get("trigger", ""), action="responded", summary="ok")

    dp.agent.run_cycle_streaming = fake_stream
    dp.agent.run_cycle = fake_cycle


async def _send(dp: Any, text: str, paths: list[str] | None, thread_id: str = "default") -> list[dict]:
    return [
        chunk
        async for chunk in dp.process_message_stream(
            text, from_person="human", attachment_paths=paths, thread_id=thread_id
        )
    ]


class TestClaudeSdkDocument:
    async def test_pdf_sent_as_document_block(self, make_digital_anima) -> None:
        dp = make_digital_anima(name="pdf-claude", model="claude-sonnet-4-6")
        seen: list[dict[str, Any]] = []
        _install_fake_engine(dp, "s", seen)
        rel = _store(dp.anima_dir)

        chunks = await _send(dp, "What is the code word?", [rel])

        assert any(c.get("type") == "cycle_done" for c in chunks)
        payload = seen[0]["sdk"]
        assert isinstance(payload, list) and len(payload) == 1
        blocks = payload[0]["message"]["content"]
        assert blocks[0]["type"] == "document"
        assert blocks[0]["source"]["media_type"] == "application/pdf"
        assert blocks[0]["title"] == "memo.pdf"
        assert blocks[-1]["type"] == "text"
        assert blocks[-1]["text"].startswith("What is the code word?")
        assert str((dp.anima_dir / rel).resolve()) in blocks[-1]["text"]
        assert "ALPHA-42" not in blocks[-1]["text"]  # body travels as the document
        # published only for this turn
        assert ca.chat_sdk_document_blocks() == []

    async def test_images_and_pdf_together(self, make_digital_anima) -> None:
        dp = make_digital_anima(name="pdf-claude-img", model="claude-sonnet-4-6")
        seen: list[dict[str, Any]] = []
        _install_fake_engine(dp, "s", seen)
        rel = _store(dp.anima_dir)
        image = {"data": "iVBORw0KGgo=", "media_type": "image/png"}

        async for _ in dp.process_message_stream("both", from_person="human", images=[image], attachment_paths=[rel]):
            pass

        types = [b["type"] for b in seen[0]["sdk"][0]["message"]["content"]]
        assert types == ["document", "image", "text"]

    async def test_blocking_process_message(self, make_digital_anima) -> None:
        dp = make_digital_anima(name="pdf-claude-blocking", model="claude-sonnet-4-6")
        seen: list[dict[str, Any]] = []
        _install_fake_engine(dp, "s", seen)
        rel = _store(dp.anima_dir)

        await dp.process_message("read it", from_person="human", attachment_paths=[rel])

        assert seen[0]["sdk"][0]["message"]["content"][0]["type"] == "document"
        assert ca.chat_sdk_document_blocks() == []


class TestTextEngines:
    async def test_codex_mode_gets_extracted_text_and_path(self, make_digital_anima) -> None:
        dp = make_digital_anima(name="pdf-codex", model="claude-sonnet-4-6")
        seen: list[dict[str, Any]] = []
        _install_fake_engine(dp, "c", seen)
        rel = _store(dp.anima_dir)

        await _send(dp, "What is the code word?", [rel])

        prompt = seen[0]["prompt"]
        assert isinstance(seen[0]["sdk"], str)  # no document blocks outside mode s
        assert seen[0]["documents"] == []
        assert "ALPHA-42" in prompt and "BLUE-7" in prompt
        assert str((dp.anima_dir / rel).resolve()) in prompt
        assert "What is the code word?" in prompt

    async def test_pdf_only_message(self, make_digital_anima) -> None:
        dp = make_digital_anima(name="pdf-only", model="claude-sonnet-4-6")
        seen: list[dict[str, Any]] = []
        _install_fake_engine(dp, "c", seen)
        rel = _store(dp.anima_dir)

        await _send(dp, "", [rel])

        assert "ALPHA-42" in seen[0]["prompt"]


class TestHistoryAndFollowUp:
    async def test_history_and_follow_up(self, make_digital_anima) -> None:
        dp = make_digital_anima(name="pdf-history", model="claude-sonnet-4-6")
        seen: list[dict[str, Any]] = []
        _install_fake_engine(dp, "c", seen)
        rel = _store(dp.anima_dir, "契約書.pdf")

        await _send(dp, "", [rel], thread_id="t1")
        await _send(dp, "And page two?", None, thread_id="t1")

        # follow-up turn still knows where the PDF is
        follow_up = seen[1]["prompt"]
        assert "契約書.pdf" in follow_up
        assert str(ca.text_sidecar_path((dp.anima_dir / rel).resolve())) in follow_up

        # the stored human turn keeps the user's text, not the injected context
        conv = json.loads((dp.anima_dir / "state" / "conversations" / "t1.json").read_text(encoding="utf-8"))
        human_turns = [t for t in conv["turns"] if t["role"] == "human"]
        assert human_turns[0]["content"] == "" and human_turns[0]["attachments"] == [rel]
        assert "ALPHA-42" not in json.dumps(conv, ensure_ascii=False)

        # activity-log history (what the chat UI renders) carries the file chip data
        view = dp._activity.get_conversation_view(limit=20, thread_id="t1")
        humans = [m for s in view["sessions"] for m in s["messages"] if m["role"] == "human"]
        assert humans[0]["attachments"] == [
            {"path": rel, "name": "契約書.pdf", "media_type": "application/pdf", "size": len(PDF), "pages": 2}
        ]
        assert "attachments" not in humans[1]

    async def test_broken_attachment_does_not_break_chat(self, make_digital_anima) -> None:
        dp = make_digital_anima(name="pdf-broken", model="claude-sonnet-4-6")
        seen: list[dict[str, Any]] = []
        _install_fake_engine(dp, "s", seen)

        chunks = await _send(dp, "hello", ["attachments/../../etc/passwd.pdf", "attachments/missing.pdf"])

        assert any(c.get("type") == "cycle_done" for c in chunks)
        assert seen[0]["sdk"] == "hello"


class TestSessionBudget:
    async def test_budget_exhausted_falls_back_to_text(self, make_digital_anima, monkeypatch) -> None:
        dp = make_digital_anima(name="pdf-claude-budget", model="claude-sonnet-4-6")
        seen: list[dict[str, Any]] = []
        _install_fake_engine(dp, "s", seen)
        monkeypatch.setattr(ca, "SDK_SESSION_DOCUMENT_BUDGET_BYTES", len(PDF) + 10)  # room for one PDF
        first = _store(dp.anima_dir, "one.pdf")
        second = _store(dp.anima_dir, "two.pdf")

        await _send(dp, "first", [first], thread_id="b1")
        await _send(dp, "second", [second], thread_id="b1")

        assert seen[0]["sdk"][0]["message"]["content"][0]["type"] == "document"
        assert isinstance(seen[1]["sdk"], str)  # no native document this time
        assert "ALPHA-42" in seen[1]["prompt"]  # the extracted text is delivered instead

    async def test_budget_survives_history_loss_and_resets_with_a_new_session(
        self, make_digital_anima, monkeypatch
    ) -> None:
        """The ledger follows the SDK session, not conversation memory or stored files."""
        dp = make_digital_anima(name="pdf-claude-ledger", model="claude-sonnet-4-6")
        seen: list[dict[str, Any]] = []
        _install_fake_engine(dp, "s", seen)
        monkeypatch.setattr(ca, "SDK_SESSION_DOCUMENT_BUDGET_BYTES", len(PDF) + 10)  # room for one PDF
        first = _store(dp.anima_dir, "one.pdf")
        await _send(dp, "first", [first], thread_id="b2")

        # Conversation memory forgets the turn and the PDF file is deleted:
        # the document is still inside the resumed SDK session.
        (dp.anima_dir / "state" / "conversations" / "b2.json").unlink()
        (dp.anima_dir / first).unlink()
        second = _store(dp.anima_dir, "two.pdf")
        await _send(dp, "second", [second], thread_id="b2")
        assert isinstance(seen[1]["sdk"], str)

        # A new SDK session (e.g. after compaction) carries nothing over.
        _clear_session_id(dp.anima_dir, "chat", "b2")
        third = _store(dp.anima_dir, "three.pdf")
        await _send(dp, "third", [third], thread_id="b2")
        assert seen[2]["sdk"][0]["message"]["content"][0]["type"] == "document"


def _replace_session_during_execution(dp: Any, *, fail: bool = False) -> None:
    """A failed resume: the SDK retries in a fresh session that gets the same documents."""
    stream, cycle = dp.agent.run_cycle_streaming, dp.agent.run_cycle

    async def replacing_stream(prompt: str, **kwargs: Any):
        _save_session_id(dp.anima_dir, "sess-fresh", "chat", kwargs.get("thread_id", "default"))
        if fail:
            raise RuntimeError("interrupted after the documents were sent")
        async for chunk in stream(prompt, **kwargs):
            yield chunk

    async def replacing_cycle(prompt: str, **kwargs: Any) -> CycleResult:
        _save_session_id(dp.anima_dir, "sess-fresh", "chat", kwargs.get("thread_id", "default"))
        if fail:
            raise RuntimeError("interrupted after the documents were sent")
        return await cycle(prompt, **kwargs)

    dp.agent.run_cycle_streaming = replacing_stream
    dp.agent.run_cycle = replacing_cycle


class TestReplacementSession:
    """PDFs delivered into a replacement session (failed resume) count against that session."""

    @pytest.mark.parametrize("blocking", [False, True])
    async def test_documents_count_in_the_replacement_session(
        self, make_digital_anima, monkeypatch, blocking: bool
    ) -> None:
        dp = make_digital_anima(name=f"pdf-claude-replace-{int(blocking)}", model="claude-sonnet-4-6")
        seen: list[dict[str, Any]] = []
        _install_fake_engine(dp, "s", seen)
        monkeypatch.setattr(ca, "SDK_SESSION_DOCUMENT_BUDGET_BYTES", len(PDF) + 10)  # room for one PDF
        _save_session_id(dp.anima_dir, "sess-old", "chat", "r1")  # the session that will fail to resume
        original = (dp.agent.run_cycle_streaming, dp.agent.run_cycle)
        _replace_session_during_execution(dp)
        first = _store(dp.anima_dir, "one.pdf")
        if blocking:
            await dp.process_message("first", from_person="human", attachment_paths=[first], thread_id="r1")
        else:
            await _send(dp, "first", [first], thread_id="r1")
        assert seen[0]["sdk"][0]["message"]["content"][0]["type"] == "document"
        assert ca.native_usage(dp.anima_dir, "r1", "sess-fresh")[0] == len(PDF)

        # The next turn resumes the replacement session: its budget is already used.
        dp.agent.run_cycle_streaming, dp.agent.run_cycle = original
        second = _store(dp.anima_dir, "two.pdf")
        await _send(dp, "second", [second], thread_id="r1")
        assert isinstance(seen[1]["sdk"], str)
        assert "BLUE-7" in seen[1]["prompt"]  # delivered as extracted text instead

    async def test_interrupted_replacement_still_counts(self, make_digital_anima, monkeypatch) -> None:
        dp = make_digital_anima(name="pdf-claude-replace-fail", model="claude-sonnet-4-6")
        seen: list[dict[str, Any]] = []
        _install_fake_engine(dp, "s", seen)
        _save_session_id(dp.anima_dir, "sess-old", "chat", "r2")
        _replace_session_during_execution(dp, fail=True)
        first = _store(dp.anima_dir, "one.pdf")
        chunks = await _send(dp, "first", [first], thread_id="r2")
        assert any(c.get("type") == "error" for c in chunks)
        assert ca.native_usage(dp.anima_dir, "r2", "sess-fresh")[0] == len(PDF)

    async def test_same_session_is_not_counted_twice(self, make_digital_anima) -> None:
        dp = make_digital_anima(name="pdf-claude-same", model="claude-sonnet-4-6")
        seen: list[dict[str, Any]] = []
        _install_fake_engine(dp, "s", seen)
        _save_session_id(dp.anima_dir, "sess-keep", "chat", "r3")
        first = _store(dp.anima_dir, "one.pdf")
        await _send(dp, "first", [first], thread_id="r3")
        assert ca.native_usage(dp.anima_dir, "r3", "sess-keep")[0] == len(PDF)

    async def test_text_engine_records_nothing(self, make_digital_anima) -> None:
        dp = make_digital_anima(name="pdf-codex-replace", model="claude-sonnet-4-6")
        seen: list[dict[str, Any]] = []
        _install_fake_engine(dp, "c", seen)
        _save_session_id(dp.anima_dir, "sess-old", "chat", "r4")
        _replace_session_during_execution(dp)
        first = _store(dp.anima_dir, "one.pdf")
        await _send(dp, "first", [first], thread_id="r4")
        assert ca.native_usage(dp.anima_dir, "r4", "sess-fresh") == (0, 0)
