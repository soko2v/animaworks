from __future__ import annotations

import asyncio

import pytest

from core.execution._claude_auth_lock import claude_auth_lock, claude_execution_lock


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


def test_login_lock_is_nonblocking_while_profile_is_in_use(tmp_path) -> None:
    profile = tmp_path / "claude-profile"
    with (
        claude_auth_lock(profile),
        pytest.raises(BlockingIOError),
        claude_auth_lock(profile, nonblocking=True),
    ):
        pass
