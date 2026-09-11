from __future__ import annotations

"""Real SSE response canary and anomaly notification helpers."""

import json
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class CanaryResult:
    ok: bool
    classification: str
    text: str = ""


def classify_sse(lines: list[str]) -> CanaryResult:
    """Require both a non-empty text_delta and a terminal done event."""
    event = ""
    text_parts: list[str] = []
    done = False
    for line in lines:
        if line.startswith("event:"):
            event = line[6:].strip()
            continue
        if not line.startswith("data:"):
            continue
        try:
            payload = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        if event == "error" and payload.get("code") == "IPC_TIMEOUT":
            return CanaryResult(False, "ipc_timeout")
        if event == "text_delta" and str(payload.get("text") or "").strip():
            text_parts.append(str(payload["text"]))
        elif event == "done":
            done = True
    text = "".join(text_parts)
    if not text.strip():
        return CanaryResult(False, "empty_body")
    if not done:
        return CanaryResult(False, "done_missing", text)
    return CanaryResult(True, "ok", text)


async def run_response_canary(
    base_url: str,
    anima_name: str,
    *,
    timeout_seconds: float = 90.0,
    client: httpx.AsyncClient | None = None,
) -> CanaryResult:
    """POST a harmless chat probe and classify the returned SSE stream."""
    if not base_url.startswith(("http://", "https://")) or not anima_name.strip():
        raise ValueError("valid base_url and anima_name are required")
    own_client = client is None
    http = client or httpx.AsyncClient(timeout=timeout_seconds)
    lines: list[str] = []
    try:
        async with http.stream(
            "POST",
            f"{base_url.rstrip('/')}/api/animas/{anima_name}/chat/stream",
            json={"message": "Reply with OK only.", "from_person": "health-canary", "thread_id": "health-canary"},
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                lines.append(line)
        return classify_sse(lines)
    except httpx.TimeoutException:
        return CanaryResult(False, "ipc_timeout")
    finally:
        if own_client:
            await http.aclose()


async def notify_health_anomaly(subject: str, body: str) -> list[str]:
    """Use the configured notification fan-out; callers should pass sanitized text."""
    from core.config import load_config
    from core.notification.notifier import HumanNotifier

    config = load_config().human_notification
    if not config.enabled:
        return []
    notifier = HumanNotifier.from_config(config)
    return await notifier.notify(subject[:120], body[:2000], "high", anima_name="system")
