"""SDK startup environment filtering and restoration without live CLI calls."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from core.execution import _sdk_env


@pytest.fixture(autouse=True)
def isolated_startup_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_sdk_env, "_startup_lock", asyncio.Lock())


@pytest.mark.parametrize("failure", [None, RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("inherited", [True, False])
async def test_subscription_merge_and_restoration(monkeypatch, failure, inherited):
    for key in _sdk_env._AUTH_KEYS:
        if inherited:
            monkeypatch.setenv(key, "test-parent-secret")
        else:
            monkeypatch.delenv(key, raising=False)
    options = SimpleNamespace(env={"HOME": "/test", "ANTHROPIC_API_KEY": ""})

    class Client:
        def __init__(self, *, options):
            self.options = options

        async def __aenter__(self):
            # Reproduce the SDK's inherited + explicit subprocess merge.
            child_env = {**os.environ, **self.options.env}
            assert all(key not in child_env for key in _sdk_env._AUTH_KEYS)
            if failure:
                raise failure()
            return self

        async def __aexit__(self, *args):
            return False

    async def run():
        async with _sdk_env.sdk_client_context(Client, options):
            # Restored before the long-lived query starts.
            for key in _sdk_env._AUTH_KEYS:
                assert os.environ.get(key) == ("test-parent-secret" if inherited else None)

    if failure:
        with pytest.raises(failure):
            await run()
    else:
        await run()
    for key in _sdk_env._AUTH_KEYS:
        assert os.environ.get(key) == ("test-parent-secret" if inherited else None)


@pytest.mark.parametrize(
    "env",
    [
        {"ANTHROPIC_API_KEY": "test-explicit-api"},
        {"ANTHROPIC_API_KEY": "", "CLAUDE_CODE_USE_BEDROCK": "1"},
        {"ANTHROPIC_API_KEY": "", "CLAUDE_CODE_USE_VERTEX": "1"},
    ],
)
async def test_other_auth_modes_unchanged(monkeypatch, env):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-parent")
    original = env.copy()

    class Client:
        def __init__(self, *, options):
            self.options = options

        async def __aenter__(self):
            assert os.environ["ANTHROPIC_API_KEY"] == "test-parent"
            assert self.options.env == original
            return self

        async def __aexit__(self, *args):
            return False

    async with _sdk_env.sdk_client_context(Client, SimpleNamespace(env=env)):
        pass
    assert env == original


async def test_concurrent_api_start_waits_for_max_restoration(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-parent")
    max_connecting = asyncio.Event()
    release_max = asyncio.Event()
    api_connected = asyncio.Event()

    class Client:
        def __init__(self, *, options):
            self.options = options

        async def __aenter__(self):
            if self.options.env.get("ANTHROPIC_API_KEY"):
                assert os.environ["ANTHROPIC_API_KEY"] == "test-parent"
                api_connected.set()
            else:
                max_connecting.set()
                await release_max.wait()
                assert "ANTHROPIC_API_KEY" not in os.environ
            return self

        async def __aexit__(self, *args):
            return False

    async def start(env):
        async with _sdk_env.sdk_client_context(Client, SimpleNamespace(env=env)):
            pass

    async with asyncio.TaskGroup() as group:
        group.create_task(start({}))
        await max_connecting.wait()
        group.create_task(start({"ANTHROPIC_API_KEY": "test-api"}))
        await asyncio.sleep(0)
        assert not api_connected.is_set()
        release_max.set()
    assert api_connected.is_set()
    assert os.environ["ANTHROPIC_API_KEY"] == "test-parent"
