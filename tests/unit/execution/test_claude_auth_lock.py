from __future__ import annotations

import asyncio

import pytest

from core.execution._claude_auth_lock import (
    ClaudeOAuthCircuitOpen,
    claude_auth_lock,
    claude_circuit_path,
    claude_execution_lock,
    trip_claude_oauth_circuit,
    trip_claude_oauth_circuit_from_result,
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


@pytest.mark.asyncio
async def test_waiter_timeout_finishes_while_lock_still_held(tmp_path):
    profile = tmp_path / "profile"
    env = {"CLAUDE_HOME": str(profile)}

    async def wait_with_timeout():
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.02):
                async with claude_execution_lock(env):
                    pytest.fail("holder still owns the lock")

    with claude_auth_lock(profile):
        waiter = asyncio.create_task(wait_with_timeout())
        done, _ = await asyncio.wait({waiter}, timeout=0.3)
    # Release even on regression, so a broken implementation cannot hang cleanup.
    await waiter
    assert waiter in done, "cancellation waited for the lock holder"
    async with claude_execution_lock(env):
        pass


_AUTH_TEXT = "API Error: 401 OAuth access token has been revoked"


@pytest.mark.parametrize(
    "shape,expected",
    [
        ("unflagged_result_only", True),
        ("unflagged_mirrored", False),
        ("unflagged_after_progress", False),
        ("flagged_assistant_error", True),
        ("mixed_quoted_then_flagged_unrelated", False),
        ("quoted_then_max_turns", False),
        ("quoted_success", False),
        ("quoted_result_prose", False),
        ("verbatim_success", False),
    ],
)
def test_result_circuit_never_trips_from_generated_text(tmp_path, shape, expected) -> None:
    from types import SimpleNamespace

    profile = tmp_path / "claude-profile"
    env = {"CLAUDE_HOME": str(profile)}
    quoted = f'The log quotes "{_AUTH_TEXT}" as an example.'
    if shape == "unflagged_mirrored":
        result, text = SimpleNamespace(subtype="success", result=_AUTH_TEXT), _AUTH_TEXT
    elif shape == "unflagged_after_progress":
        result, text = SimpleNamespace(subtype="success", result=_AUTH_TEXT), "Reading files...\nStill working."
    elif shape == "unflagged_result_only":
        result, text = SimpleNamespace(subtype="success", result=_AUTH_TEXT), ""
    elif shape == "flagged_assistant_error":
        result, text = SimpleNamespace(subtype="success", result=None), _AUTH_TEXT
        assert trip_claude_oauth_circuit_from_result(env, result, text, _AUTH_TEXT) is True
        assert claude_circuit_path(profile).exists()
        return
    elif shape == "mixed_quoted_then_flagged_unrelated":
        result, text = SimpleNamespace(subtype="success", result=_AUTH_TEXT), quoted
        assert trip_claude_oauth_circuit_from_result(env, result, text, "Provider request failed") is False
        assert not claude_circuit_path(profile).exists()
        return
    elif shape == "verbatim_success":
        result, text = SimpleNamespace(subtype="success", result=_AUTH_TEXT, is_error=False), _AUTH_TEXT + "\n\nThat is the exact line from the log."
    elif shape == "quoted_then_max_turns":
        result, text = SimpleNamespace(subtype="error_max_turns", is_error=True, result=None), quoted
    elif shape == "quoted_success":
        result, text = SimpleNamespace(subtype="success", result=quoted), quoted
    else:
        result, text = SimpleNamespace(subtype="success", result=quoted), ""
    assert trip_claude_oauth_circuit_from_result(env, result, text) is expected
    assert claude_circuit_path(profile).exists() is expected
