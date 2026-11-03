"""Status reporting for persistent RAG repair state."""

from __future__ import annotations

import argparse
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from core.memory.rag.repair.state import parse_dt, read_state, utc_now
from core.memory.rag.store_status import read_store_status

_HEARTBEAT_STALE_AFTER = timedelta(minutes=30)
_SIGNAL_WINDOW = timedelta(hours=24)


def setup_rag_repair_status_command(subparsers: argparse._SubParsersAction) -> None:
    """Register the top-level ``rag-repair-status`` command."""
    parser = subparsers.add_parser(
        "rag-repair-status",
        help="Show RAG repair state for all animas",
    )
    parser.add_argument("--json", action="store_true", dest="json_output", help="Print structured JSON")
    parser.set_defaults(func=rag_repair_status_command)


def collect_rag_repair_status(
    animas_dir: Path,
    *,
    now: Any | None = None,
) -> list[dict[str, Any]]:
    """Return repair state summaries for every non-hidden anima directory."""
    current_time = now or utc_now()
    signal_cutoff = current_time - _SIGNAL_WINDOW
    heartbeat_cutoff = current_time - _HEARTBEAT_STALE_AFTER
    if not animas_dir.is_dir():
        return []

    rows: list[dict[str, Any]] = []
    for anima_dir in sorted(
        (entry for entry in animas_dir.iterdir() if entry.is_dir() and not entry.name.startswith(".")),
        key=lambda entry: entry.name,
    ):
        store_status = read_store_status(anima_dir) or {}
        store_fields = {
            "store_unavailable_since": store_status.get("unavailable_since"),
            "store_last_error": store_status.get("last_error"),
        }
        state_file = anima_dir / "state" / "rag_repair.json"
        if not state_file.is_file():
            rows.append(
                {
                    "name": anima_dir.name,
                    "status": "no-signal",
                    "last_success_at": None,
                    "consecutive_failures": 0,
                    "recent_signals_24h": 0,
                    "last_signal_reason": None,
                    "store_init_failed_24h": False,
                    "stale_repairing": False,
                    **store_fields,
                }
            )
            continue

        state = read_state(anima_dir.name, animas_dir=animas_dir)
        signals = state.get("recent_signals")
        recent_signals = (
            [
                signal
                for signal in signals
                if isinstance(signal, dict)
                and (at := parse_dt(signal.get("at"))) is not None
                and signal_cutoff <= at <= current_time
            ]
            if isinstance(signals, list)
            else []
        )
        recent_signal_count = len(recent_signals)
        last_signal = max(
            recent_signals,
            key=lambda signal: parse_dt(signal.get("at")),
            default=None,
        )
        last_signal_reason = str(last_signal.get("reason")) if last_signal and last_signal.get("reason") else None
        store_init_failed_24h = any(signal.get("reason") == "store_init_failed" for signal in recent_signals)
        status = str(state.get("status") or "unknown")
        heartbeat_at = parse_dt(state.get("heartbeat_at"))
        stale_repairing = status == "repairing" and (heartbeat_at is None or heartbeat_at <= heartbeat_cutoff)
        try:
            consecutive_failures = int(state.get("consecutive_failures") or 0)
        except (TypeError, ValueError):
            consecutive_failures = 0
        rows.append(
            {
                "name": anima_dir.name,
                "status": status,
                "last_success_at": state.get("last_success_at"),
                "consecutive_failures": consecutive_failures,
                "recent_signals_24h": recent_signal_count,
                "last_signal_reason": last_signal_reason,
                "store_init_failed_24h": store_init_failed_24h,
                "stale_repairing": stale_repairing,
                **store_fields,
            }
        )
    return rows


def rag_repair_status_command(args: argparse.Namespace) -> None:
    """Print current repair status and signal unhealthy state through exit code."""
    from core.paths import get_animas_dir

    rows = collect_rag_repair_status(get_animas_dir())
    if getattr(args, "json_output", False):
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        _print_status_table(rows)

    if any(
        row["status"] in {"requested", "failed"}
        or row["stale_repairing"]
        or row["recent_signals_24h"] >= 2
        or row["store_init_failed_24h"]
        or row.get("store_unavailable_since")
        for row in rows
    ):
        raise SystemExit(1)


def _print_status_table(rows: list[dict[str, Any]]) -> None:
    headers = (
        "ANIMA",
        "STATUS",
        "LAST SUCCESS",
        "FAILURES",
        "SIGNALS (24H)",
        "LAST SIGNAL",
        "STORE UNAVAILABLE SINCE",
    )
    values = [
        (
            str(row["name"]),
            str(row["status"]),
            str(row["last_success_at"] or "-"),
            str(row["consecutive_failures"]),
            str(row["recent_signals_24h"]),
            str(row["last_signal_reason"] or "-"),
            str(row.get("store_unavailable_since") or "-"),
        )
        for row in rows
    ]
    widths = [len(header) for header in headers]
    for value_row in values:
        for index, value in enumerate(value_row):
            widths[index] = max(widths[index], len(value))

    def render(value_row: tuple[str, ...]) -> str:
        return "  ".join(value.ljust(widths[index]) for index, value in enumerate(value_row)).rstrip()

    print(render(headers))
    print(render(tuple("-" * width for width in widths)))
    for value_row in values:
        print(render(value_row))
