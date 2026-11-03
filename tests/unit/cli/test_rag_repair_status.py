from __future__ import annotations

import argparse
import json
from datetime import timedelta
from pathlib import Path

import pytest

from cli.commands.rag_repair_status import (
    collect_rag_repair_status,
    rag_repair_status_command,
    setup_rag_repair_status_command,
)
from core.memory.rag.repair.state import iso, utc_now


def _write_state(animas_dir: Path, name: str, state: dict[str, object]) -> None:
    state_dir = animas_dir / name / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "rag_repair.json").write_text(json.dumps(state), encoding="utf-8")


def test_rag_repair_status_parser_accepts_json() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    setup_rag_repair_status_command(subparsers)

    args = parser.parse_args(["rag-repair-status", "--json"])

    assert args.command == "rag-repair-status"
    assert args.json_output is True


def test_collect_rag_repair_status_reports_all_animas_and_no_signal(tmp_path: Path) -> None:
    now = utc_now()
    animas_dir = tmp_path / "animas"
    (animas_dir / "no-state").mkdir(parents=True)
    _write_state(
        animas_dir,
        "healthy",
        {
            "status": "healthy",
            "last_success_at": iso(now - timedelta(minutes=5)),
            "consecutive_failures": 0,
            "recent_signals": [
                {"at": iso(now - timedelta(hours=1))},
                {"at": iso(now - timedelta(days=2))},
                {"at": iso(now + timedelta(hours=1))},
            ],
        },
    )

    rows = collect_rag_repair_status(animas_dir, now=now)

    assert rows == [
        {
            "name": "healthy",
            "status": "healthy",
            "last_success_at": iso(now - timedelta(minutes=5)),
            "consecutive_failures": 0,
            "recent_signals_24h": 1,
            "last_signal_reason": None,
            "store_init_failed_24h": False,
            "stale_repairing": False,
            "store_unavailable_since": None,
            "store_last_error": None,
        },
        {
            "name": "no-state",
            "status": "no-signal",
            "last_success_at": None,
            "consecutive_failures": 0,
            "recent_signals_24h": 0,
            "last_signal_reason": None,
            "store_init_failed_24h": False,
            "stale_repairing": False,
            "store_unavailable_since": None,
            "store_last_error": None,
        },
    ]


def test_rag_repair_status_healthy_table_returns_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = utc_now()
    animas_dir = tmp_path / "animas"
    _write_state(
        animas_dir,
        "healthy",
        {
            "status": "healthy",
            "last_success_at": iso(now),
            "consecutive_failures": 0,
            "recent_signals": [{"at": iso(now - timedelta(hours=1))}],
        },
    )
    monkeypatch.setattr("core.paths.get_animas_dir", lambda: animas_dir)

    assert rag_repair_status_command(argparse.Namespace(json_output=False)) is None

    output = capsys.readouterr().out
    assert "healthy" in output
    assert "SIGNALS (24H)" in output
    assert "LAST SIGNAL" in output


def test_rag_repair_status_json_exits_one_for_stale_repair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = utc_now()
    animas_dir = tmp_path / "animas"
    _write_state(
        animas_dir,
        "stuck",
        {
            "status": "repairing",
            "heartbeat_at": iso(now - timedelta(minutes=30)),
            "consecutive_failures": 3,
            "recent_signals": [],
        },
    )
    monkeypatch.setattr("core.paths.get_animas_dir", lambda: animas_dir)

    with pytest.raises(SystemExit) as exc:
        rag_repair_status_command(argparse.Namespace(json_output=True))

    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)[0]["stale_repairing"] is True


def test_rag_repair_status_treats_missing_repair_heartbeat_as_stale(tmp_path: Path) -> None:
    now = utc_now()
    animas_dir = tmp_path / "animas"
    _write_state(animas_dir, "stuck", {"status": "repairing", "recent_signals": []})

    rows = collect_rag_repair_status(animas_dir, now=now)

    assert rows[0]["stale_repairing"] is True


def test_rag_repair_status_table_exits_one_for_multiple_recent_signals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = utc_now()
    animas_dir = tmp_path / "animas"
    _write_state(
        animas_dir,
        "noisy",
        {
            "status": "healthy",
            "last_success_at": iso(now),
            "consecutive_failures": 1,
            "recent_signals": [{"at": iso(now - timedelta(hours=1))}, {"at": iso(now - timedelta(hours=2))}],
        },
    )
    monkeypatch.setattr("core.paths.get_animas_dir", lambda: animas_dir)

    with pytest.raises(SystemExit) as exc:
        rag_repair_status_command(argparse.Namespace(json_output=False))

    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "SIGNALS (24H)" in output
    assert "noisy" in output
    assert "2" in output


@pytest.mark.parametrize("status", ["requested", "failed"])
def test_rag_repair_status_exits_one_for_unhealthy_status(
    status: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    animas_dir = tmp_path / "animas"
    _write_state(
        animas_dir,
        "sora",
        {
            "status": status,
            "recent_signals": [],
        },
    )
    monkeypatch.setattr("core.paths.get_animas_dir", lambda: animas_dir)

    with pytest.raises(SystemExit) as exc:
        rag_repair_status_command(argparse.Namespace(json_output=True))

    assert exc.value.code == 1


def test_rag_repair_status_exits_one_for_recent_store_init_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = utc_now()
    animas_dir = tmp_path / "animas"
    _write_state(
        animas_dir,
        "sora",
        {
            "status": "healthy",
            "recent_signals": [
                {
                    "at": iso(now - timedelta(hours=1)),
                    "reason": "store_init_failed",
                }
            ],
        },
    )
    monkeypatch.setattr("core.paths.get_animas_dir", lambda: animas_dir)

    with pytest.raises(SystemExit) as exc:
        rag_repair_status_command(argparse.Namespace(json_output=False))

    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "LAST SIGNAL" in output
    assert "store_init_failed" in output


def test_rag_repair_status_exposes_store_unavailable_since_and_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from core.memory.rag.store_status import clear_store_unavailable, write_store_unavailable

    animas_dir = tmp_path / "animas"
    # rag_repair.json still says healthy: chroma_transient errors are not
    # recorded there, which is exactly why the sidecar is needed.
    _write_state(animas_dir, "luca", {"status": "healthy"})
    (animas_dir / "nostate").mkdir(parents=True)
    since = "2026-11-03T00:28:31+00:00"
    assert write_store_unavailable(
        animas_dir / "luca",
        unavailable_since=since,
        last_error="OperationalError: (code: 4874) disk I/O error",
        last_attempt_at=since,
        next_retry_at=None,
        free_bytes=0,
    )
    assert write_store_unavailable(
        animas_dir / "nostate",
        unavailable_since=since,
        last_error="boom",
        last_attempt_at=since,
        next_retry_at=None,
        free_bytes=None,
    )

    rows = {row["name"]: row for row in collect_rag_repair_status(animas_dir)}
    assert rows["luca"]["status"] == "healthy"
    assert rows["luca"]["store_unavailable_since"] == since
    assert "4874" in rows["luca"]["store_last_error"]
    assert rows["nostate"]["status"] == "no-signal"
    assert rows["nostate"]["store_unavailable_since"] == since

    monkeypatch.setattr("core.paths.get_animas_dir", lambda: animas_dir)
    with pytest.raises(SystemExit) as exc:
        rag_repair_status_command(argparse.Namespace(json_output=False))
    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "STORE UNAVAILABLE SINCE" in output
    assert since in output

    # Once the store reopens the marker is removed and status is clean again.
    assert clear_store_unavailable(animas_dir / "luca")
    assert clear_store_unavailable(animas_dir / "nostate")
    rows = {row["name"]: row for row in collect_rag_repair_status(animas_dir)}
    assert rows["luca"]["store_unavailable_since"] is None
    rag_repair_status_command(argparse.Namespace(json_output=False))
