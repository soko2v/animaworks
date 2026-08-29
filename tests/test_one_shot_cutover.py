from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.one_shot_cutover import (
    CutoverConfig,
    _atomic_replace,
    _candidate_is_running,
    _claim_attempt,
    cutover,
    make_launchd_plist,
    run_command,
)


class FakeCommand:
    def __init__(self, results: list[int]) -> None:
        self.results = iter(results)
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        returncode = next(self.results, 0)
        stdout = ""
        if args[:2] == ["launchctl", "print"] and returncode == 0:
            stdout = "state = running\n\tpid = 4242\n"
        elif args[:2] == ["ps", "-p"] and returncode == 0:
            stdout = "/candidate/animaworks serve\n"
        return subprocess.CompletedProcess(args, returncode, stdout, "")


@pytest.fixture
def config(tmp_path: Path) -> CutoverConfig:
    paths = {name: tmp_path / name for name in ("service", "candidate", "rollback", "marker", "temporary")}
    paths["service"].write_text("old", encoding="utf-8")
    paths["candidate"].write_text("new", encoding="utf-8")
    paths["rollback"].write_text("old", encoding="utf-8")
    paths["temporary"].write_text("job", encoding="utf-8")
    return CutoverConfig(
        domain="gui/501",
        service_label="com.animaworks.serve",
        temporary_label="com.animaworks.cutover",
        service_plist=paths["service"],
        candidate_plist=paths["candidate"],
        rollback_plist=paths["rollback"],
        attempt_marker=paths["marker"],
        temporary_plist=paths["temporary"],
        expected_program_fragment="/candidate/animaworks serve",
        timeout_seconds=1,
        poll_seconds=0,
    )


def test_bootstrap_failure_then_respawn_does_not_bootout_twice(config: CutoverConfig) -> None:
    first = FakeCommand([1, 0, 5, 0, 0, 0])
    assert cutover(config, first) == 1
    second = FakeCommand([1])
    assert cutover(config, second) == 0
    calls = first.calls + second.calls
    service_bootouts = [
        call for call in calls if call == ["launchctl", "bootout", f"{config.domain}/{config.service_label}"]
    ]
    temporary_bootouts = [
        call for call in calls if call == ["launchctl", "bootout", f"{config.domain}/{config.temporary_label}"]
    ]
    assert len(service_bootouts) == 2  # cutover + rollback only
    assert len(temporary_bootouts) == 2  # one cleanup per execution
    assert all(call != ["launchctl", "bootout", f"{config.domain}/{config.service_label}"] for call in second.calls)


def test_already_running_is_noop(config: CutoverConfig) -> None:
    fake = FakeCommand([0, 0, 0])
    assert cutover(config, fake) == 0
    assert fake.calls == [
        ["launchctl", "print", f"{config.domain}/{config.service_label}"],
        ["ps", "-p", "4242", "-o", "command="],
        ["launchctl", "bootout", f"{config.domain}/{config.temporary_label}"],
    ]
    assert config.service_plist.read_text() == "old"


def test_successful_cutover_cannot_run_again(config: CutoverConfig) -> None:
    first = FakeCommand([1, 0, 0, 0, 0, 0])
    assert cutover(config, first) == 0
    second = FakeCommand([1])
    assert cutover(config, second) == 0
    assert all(call != ["launchctl", "bootout", f"{config.domain}/{config.service_label}"] for call in second.calls)


def test_timeout_rolls_back_once(config: CutoverConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCommand([0, 0, 0, 0])
    checks = iter([False, TimeoutError()])

    def candidate_check(*_args: object) -> bool:
        result = next(checks)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("scripts.one_shot_cutover._candidate_is_running", candidate_check)
    assert cutover(config, fake) == 1
    assert config.service_plist.read_text() == "old"
    assert sum(call[:2] == ["launchctl", "bootstrap"] for call in fake.calls) == 2


def test_rollback_happens_only_once(config: CutoverConfig) -> None:
    fake = FakeCommand([1, 0, 5, 0, 5])
    assert cutover(config, fake) == 1
    assert sum(
        call == ["launchctl", "bootout", f"{config.domain}/{config.service_label}"] for call in fake.calls
    ) == 2


def test_temporary_bootout_failure_is_fail_safe(config: CutoverConfig) -> None:
    # Candidate bootstrap fails; rollback succeeds; the temporary-job bootout fails.
    fake = FakeCommand([1, 0, 5, 0, 0, 1])
    assert cutover(config, fake) == 1
    respawn = FakeCommand([1, 0])
    assert cutover(config, respawn) == 0
    assert all(call != ["launchctl", "bootout", f"{config.domain}/{config.service_label}"] for call in respawn.calls)


def test_candidate_check_uses_launchd_pid_and_real_process(config: CutoverConfig, tmp_path: Path) -> None:
    marker = tmp_path / "candidate-identity-token"
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", str(marker)])
    try:
        def command(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            if args[:2] == ["launchctl", "print"]:
                return subprocess.CompletedProcess(args, 0, f"state = running\n pid = {process.pid}\n", "")
            return run_command(args, timeout)

        candidate = CutoverConfig(**{**config.__dict__, "expected_program_fragment": str(marker)})
        assert _candidate_is_running(candidate, command, time.monotonic() + 2)

        unrelated = CutoverConfig(**{**config.__dict__, "expected_program_fragment": "not-in-the-process"})
        assert not _candidate_is_running(unrelated, command, time.monotonic() + 2)

        def self_command(args: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 0, f"state = running\n pid = {os.getpid()}\n", "")

        assert not _candidate_is_running(candidate, self_command, time.monotonic() + 2)
    finally:
        process.terminate()
        process.wait(timeout=2)


def test_cleanup_disables_plist_before_bootout_after_deadline(
    config: CutoverConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "scripts.one_shot_cutover._candidate_is_running",
        lambda *_args: (_ for _ in ()).throw(TimeoutError("deadline consumed")),
    )
    observed: list[tuple[list[str], float]] = []

    def command(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        assert not config.temporary_plist.exists()
        observed.append((list(args), timeout))
        return subprocess.CompletedProcess(args, 0, "", "")

    assert cutover(config, command) == 1
    assert observed == [
        (["launchctl", "bootout", f"{config.domain}/{config.temporary_label}"], config.cleanup_timeout_seconds)
    ]


def test_atomic_replace_failure_preserves_live_file(
    config: CutoverConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_replace = os.replace

    def fail_before_replace(source: Path, destination: Path) -> None:
        if destination == config.service_plist:
            raise OSError("injected replace failure")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_before_replace)
    with pytest.raises(OSError, match="injected"):
        _atomic_replace(config.service_plist, b"partial candidate")
    assert config.service_plist.read_text(encoding="utf-8") == "old"


def test_temporary_plist_generation_failure_preserves_live_file(
    config: CutoverConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_replace = os.replace

    def fail_before_replace(source: Path, destination: Path) -> None:
        if destination == config.temporary_plist:
            raise OSError("injected replace failure")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_before_replace)
    with pytest.raises(OSError, match="injected"):
        make_launchd_plist(
            label=config.temporary_label,
            program_arguments=["python3", "helper.py"],
            output_path=config.temporary_plist,
        )
    assert config.temporary_plist.read_text(encoding="utf-8") == "job"


def test_attempt_marker_fsyncs_file_and_parent(config: CutoverConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda descriptor: calls.append(descriptor))
    assert _claim_attempt(config.attempt_marker)
    assert len(calls) == 2
    monkeypatch.setattr(os, "fsync", real_fsync)


def test_command_timeouts_decrease_and_stay_within_operation_budget(
    config: CutoverConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [100.0]
    monkeypatch.setattr("scripts.one_shot_cutover.time.monotonic", lambda: now[0])
    timeouts: list[float] = []
    results = iter([1, 0, 0, 0, 0, 0])

    def command(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        timeouts.append(timeout)
        now[0] += 0.05
        returncode = next(results, 0)
        stdout = "pid = 4242\n" if args[:2] == ["launchctl", "print"] else ""
        if args[:2] == ["ps", "-p"]:
            stdout = config.expected_program_fragment
        return subprocess.CompletedProcess(args, returncode, stdout, "")

    assert cutover(config, command) == 0
    main_timeouts = timeouts[:-1]
    assert main_timeouts == sorted(main_timeouts, reverse=True)
    assert now[0] - 100.0 <= config.timeout_seconds
    assert timeouts[-1] == config.cleanup_timeout_seconds


def test_initial_candidate_timeout_is_fail_safe(config: CutoverConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "scripts.one_shot_cutover._candidate_is_running",
        lambda *_args: (_ for _ in ()).throw(TimeoutError("pgrep timeout")),
    )
    fake = FakeCommand([0])
    assert cutover(config, fake) == 1
    assert fake.calls == [["launchctl", "bootout", f"{config.domain}/{config.temporary_label}"]]


def test_generated_plist_has_non_restart_contract(tmp_path: Path) -> None:
    output = tmp_path / "cutover.plist"
    make_launchd_plist(label="com.animaworks.cutover", program_arguments=["python3", "helper.py"], output_path=output)
    with output.open("rb") as file_handle:
        plist = plistlib.load(file_handle)
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is False
    assert "StartInterval" not in plist
    assert "StartCalendarInterval" not in plist
