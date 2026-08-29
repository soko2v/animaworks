#!/usr/bin/env python3
"""Fail-safe, single-attempt launchd service cutover helper."""

from __future__ import annotations

import argparse
import os
import plistlib
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

RunCommand = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class CutoverConfig:
    """Inputs required for one service cutover attempt."""

    domain: str
    service_label: str
    temporary_label: str
    service_plist: Path
    candidate_plist: Path
    rollback_plist: Path
    attempt_marker: Path
    temporary_plist: Path
    expected_program_fragment: str
    timeout_seconds: float = 120.0
    poll_seconds: float = 1.0
    cleanup_timeout_seconds: float = 5.0


def run_command(args: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Run one command without invoking a shell."""
    return subprocess.run(args, check=False, capture_output=True, text=True, timeout=timeout)


def make_launchd_plist(*, label: str, program_arguments: list[str], output_path: Path) -> None:
    """Write a launchd job that launchd can never automatically restart."""
    payload = {
        "Label": label,
        "ProgramArguments": program_arguments,
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Background",
    }
    with tempfile.SpooledTemporaryFile() as file_handle:
        plistlib.dump(payload, file_handle, sort_keys=False)
        file_handle.seek(0)
        _atomic_replace(output_path, file_handle.read())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_replace(path: Path, content: bytes) -> None:
    """Durably replace a file without exposing a partial destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as file_handle:
            file_handle.write(content)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        temporary_path.unlink(missing_ok=True)


def _claim_attempt(marker: Path) -> bool:
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as file_handle:
        file_handle.write(f"pid={os.getpid()}\n")
        file_handle.flush()
        os.fsync(file_handle.fileno())
    _fsync_directory(marker.parent)
    return True


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("cutover deadline exceeded")
    return remaining


def _command_ok(command: RunCommand, args: Sequence[str], deadline: float) -> bool:
    return command(args, _remaining(deadline)).returncode == 0


def _candidate_is_running(config: CutoverConfig, command: RunCommand, deadline: float) -> bool:
    state = command(
        ["launchctl", "print", f"{config.domain}/{config.service_label}"], _remaining(deadline)
    )
    if state.returncode != 0:
        return False
    match = re.search(r"^\s*pid\s*=\s*(\d+)\s*$", state.stdout, re.MULTILINE)
    if match is None:
        return False
    pid = int(match.group(1))
    if pid == os.getpid():
        return False
    process = command(["ps", "-p", str(pid), "-o", "command="], _remaining(deadline))
    return process.returncode == 0 and config.expected_program_fragment in process.stdout


def _rollback_once(config: CutoverConfig, command: RunCommand, deadline: float) -> None:
    _atomic_replace(config.service_plist, config.rollback_plist.read_bytes())
    _command_ok(command, ["launchctl", "bootout", f"{config.domain}/{config.service_label}"], deadline)
    if not _command_ok(command, ["launchctl", "bootstrap", config.domain, str(config.service_plist)], deadline):
        raise RuntimeError("rollback bootstrap failed")


def _cleanup(config: CutoverConfig, command: RunCommand) -> None:
    """Prevent respawn before asking launchd to terminate the temporary job."""
    disabled_plist = config.temporary_plist.with_name(
        f".{config.temporary_plist.name}.disabled.{os.getpid()}"
    )
    if config.temporary_plist.exists():
        os.replace(config.temporary_plist, disabled_plist)
        _fsync_directory(config.temporary_plist.parent)
    try:
        command(
            ["launchctl", "bootout", f"{config.domain}/{config.temporary_label}"],
            config.cleanup_timeout_seconds,
        )
    finally:
        disabled_plist.unlink(missing_ok=True)


def _best_effort_cleanup(config: CutoverConfig, command: RunCommand) -> None:
    try:
        _cleanup(config, command)
    except (OSError, subprocess.TimeoutExpired, TimeoutError):
        pass


def cutover(config: CutoverConfig, command: RunCommand = run_command) -> int:
    """Perform at most one destructive cutover and one rollback."""
    started = time.monotonic()
    deadline = started + config.timeout_seconds
    operation_deadline = started + (config.timeout_seconds * 0.8)
    try:
        already_running = _candidate_is_running(config, command, deadline)
    except (OSError, subprocess.TimeoutExpired, TimeoutError):
        _best_effort_cleanup(config, command)
        return 1
    if already_running:
        _best_effort_cleanup(config, command)
        return 0
    if not _claim_attempt(config.attempt_marker):
        _best_effort_cleanup(config, command)
        return 0

    exit_code = 1
    try:
        _atomic_replace(config.service_plist, config.candidate_plist.read_bytes())
        _command_ok(command, ["launchctl", "bootout", f"{config.domain}/{config.service_label}"], operation_deadline)
        if not _command_ok(
            command, ["launchctl", "bootstrap", config.domain, str(config.service_plist)], operation_deadline
        ):
            raise RuntimeError("candidate bootstrap failed")
        while not _candidate_is_running(config, command, operation_deadline):
            time.sleep(min(config.poll_seconds, _remaining(operation_deadline)))
        exit_code = 0
    except (OSError, RuntimeError, subprocess.TimeoutExpired, TimeoutError):
        try:
            _rollback_once(config, command, deadline)
        except (OSError, RuntimeError, subprocess.TimeoutExpired, TimeoutError):
            pass
    finally:
        _best_effort_cleanup(config, command)
    return exit_code


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--service-label", required=True)
    parser.add_argument("--temporary-label", required=True)
    parser.add_argument("--service-plist", required=True, type=Path)
    parser.add_argument("--candidate-plist", required=True, type=Path)
    parser.add_argument("--rollback-plist", required=True, type=Path)
    parser.add_argument("--attempt-marker", required=True, type=Path)
    parser.add_argument("--temporary-plist", required=True, type=Path)
    parser.add_argument("--expected-program-fragment", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    return parser.parse_args()


def main() -> int:
    """CLI entry point."""
    args = _parse_args()
    return cutover(CutoverConfig(**vars(args)))


if __name__ == "__main__":
    raise SystemExit(main())
