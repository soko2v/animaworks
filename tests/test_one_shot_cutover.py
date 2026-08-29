from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from scripts.one_shot_cutover import CutoverConfig, cutover, make_launchd_plist


class FakeCommand:
    def __init__(self, results: list[int]) -> None:
        self.results = iter(results)
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        return subprocess.CompletedProcess(args, next(self.results, 0), "", "")


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
    first = FakeCommand([1, 0, 5, 0, 0])
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
    fake = FakeCommand([0])
    assert cutover(config, fake) == 0
    assert fake.calls == [
        ["pgrep", "-f", config.expected_program_fragment],
        ["launchctl", "bootout", f"{config.domain}/{config.temporary_label}"],
    ]
    assert config.service_plist.read_text() == "old"


def test_successful_cutover_cannot_run_again(config: CutoverConfig) -> None:
    first = FakeCommand([1, 0, 0, 0])
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
