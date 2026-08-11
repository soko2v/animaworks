from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.response_canary import classify_sse, notify_health_anomaly


def test_canary_requires_text_and_done() -> None:
    assert classify_sse(['event: text_delta', 'data: {"text":"OK"}', 'event: done', 'data: {}']).ok
    assert classify_sse(['event: done', 'data: {}']).classification == "empty_body"
    assert classify_sse(['event: text_delta', 'data: {"text":"OK"}']).classification == "done_missing"


def test_canary_classifies_ipc_timeout() -> None:
    result = classify_sse(['event: error', 'data: {"code":"IPC_TIMEOUT"}'])
    assert not result.ok
    assert result.classification == "ipc_timeout"


@pytest.mark.asyncio
async def test_notification_uses_mocked_external_notifier(monkeypatch: pytest.MonkeyPatch) -> None:
    config = MagicMock(enabled=True)
    notifier = MagicMock()
    notifier.notify = AsyncMock(return_value=["mocked"])
    monkeypatch.setattr("core.config.load_config", lambda: MagicMock(human_notification=config))
    monkeypatch.setattr("core.notification.notifier.HumanNotifier.from_config", lambda _config: notifier)

    assert await notify_health_anomaly("RAG anomaly", "quick_check requested repair") == ["mocked"]
    notifier.notify.assert_awaited_once()
