from __future__ import annotations

import asyncio

import pytest

from core.execution._claude_auth_lock import (
    ClaudeOAuthCircuitOpen,
    claude_auth_lock,
    claude_circuit_path,
    claude_execution_lock,
    claude_sync_execution_lock,
    trip_claude_oauth_circuit,
)


@pytest.mark.asyncio
async def test_execution_lock_serializes_same_profile(tmp_path) -> None:
    profile = tmp_path / "claude-profile"
    env = {"CLAUDE_HOME": str(profile)}
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first() -> None:
        async with claude_execution_lock(env):
            first_entered.set()
            await release_first.wait()

    async def second() -> None:
        await first_entered.wait()
        async with claude_execution_lock(env):
            second_entered.set()

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await first_entered.wait()
    await asyncio.sleep(0.05)
    assert not second_entered.is_set()
    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert second_entered.is_set()


@pytest.mark.asyncio
async def test_execution_lock_skips_non_oauth_env() -> None:
    async with claude_execution_lock({"ANTHROPIC_API_KEY": "not-a-real-key"}):
        pass


@pytest.mark.asyncio
async def test_execution_lock_releases_after_exception_and_timeout(tmp_path) -> None:
    env = {"CLAUDE_HOME": str(tmp_path / "claude-profile")}
    with pytest.raises(RuntimeError):
        async with claude_execution_lock(env):
            raise RuntimeError("mock failure")
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            async with claude_execution_lock(env):
                await asyncio.sleep(1)
    async with claude_execution_lock(env):
        pass


@pytest.mark.asyncio
async def test_execution_lock_releases_after_waiter_cancel(tmp_path) -> None:
    env = {"CLAUDE_HOME": str(tmp_path / "claude-profile")}
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with claude_execution_lock(env):
            entered.set()
            await release.wait()

    holder_task = asyncio.create_task(holder())
    await entered.wait()
    waiter = asyncio.create_task(claude_execution_lock(env).__aenter__())
    await asyncio.sleep(0.01)
    waiter.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await holder_task
    async with claude_execution_lock(env):
        pass


@pytest.mark.asyncio
async def test_revoked_401_opens_fleet_circuit_before_next_spawn(tmp_path) -> None:
    profile = tmp_path / "claude-profile"
    env = {"CLAUDE_HOME": str(profile)}
    assert trip_claude_oauth_circuit(env, "API Error: 401 OAuth access token has been revoked")
    assert claude_circuit_path(profile).exists()
    with pytest.raises(ClaudeOAuthCircuitOpen):
        async with claude_execution_lock(env):
            pytest.fail("circuit must fail before SDK construction")


def test_login_lock_is_nonblocking_while_profile_is_in_use(tmp_path) -> None:
    profile = tmp_path / "claude-profile"
    with (
        claude_auth_lock(profile),
        pytest.raises(BlockingIOError),
        claude_auth_lock(profile, nonblocking=True),
    ):
        pass


def test_sync_execution_lock_serializes_with_login_lock(tmp_path) -> None:
    profile = tmp_path / "claude-profile"
    env = {"CLAUDE_HOME": str(profile)}
    with (
        claude_sync_execution_lock(env),
        pytest.raises(BlockingIOError),
        claude_auth_lock(profile, nonblocking=True),
    ):
        pass


def test_sync_execution_lock_honors_revoked_circuit(tmp_path) -> None:
    profile = tmp_path / "claude-profile"
    env = {"CLAUDE_HOME": str(profile)}
    assert trip_claude_oauth_circuit(env, "API Error: 401 OAuth access token has been revoked")
    with pytest.raises(ClaudeOAuthCircuitOpen), claude_sync_execution_lock(env):
        pytest.fail("circuit must fail before raw Claude CLI construction")


def test_sync_execution_lock_times_out_while_profile_is_in_use(tmp_path) -> None:
    profile = tmp_path / "claude-profile"
    env = {"CLAUDE_HOME": str(profile)}
    with claude_auth_lock(profile), pytest.raises(TimeoutError), claude_sync_execution_lock(env, timeout=0.01):
        pass
