"""Cross-process serialization for shared Claude.ai OAuth credentials."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import TextIO


class ClaudeOAuthCircuitOpen(RuntimeError):
    """Raised before spawning Claude when shared OAuth was revoked."""


def claude_lock_path(profile: Path) -> Path:
    """Return the shared lock path for a configured Claude Code profile."""
    return profile.parent / ".animaworks-auth-locks" / "claude.lock"


def claude_circuit_path(profile: Path) -> Path:
    """Return the fleet-wide revoked-OAuth circuit marker path."""
    return claude_lock_path(profile).with_name("claude.revoked")


def is_revoked_oauth_error(text: str) -> bool:
    """Return whether *text* identifies the shared OAuth revoked 401."""
    folded = (text or "").casefold()
    return "401" in folded and "oauth access token has been revoked" in folded


def trip_claude_oauth_circuit(env: dict[str, str] | None, error_text: str) -> bool:
    """Atomically stop later shared-profile SDK starts after a revoked 401."""
    raw_profile = (env or {}).get("CLAUDE_HOME")
    if not raw_profile or not is_revoked_oauth_error(error_text):
        return False
    profile = Path(raw_profile).expanduser()
    if not profile.is_absolute():
        return False
    marker = claude_circuit_path(profile)
    marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker.touch(mode=0o600, exist_ok=True)
    return True


def trip_claude_oauth_circuit_from_result(
    env: dict[str, str] | None,
    result: object,
    text: str,
    assistant_error: str | None = None,
) -> bool:
    """Inspect SDK failure results, never ordinary generated assistant text."""
    subtype = getattr(result, "subtype", "")
    is_error = (
        getattr(result, "is_error", False) is True
        or (isinstance(subtype, str) and subtype.startswith("error_"))
        or bool(assistant_error)
    )
    result_text = getattr(result, "result", None)
    if not is_error:
        # Some SDK versions return only an auth error result without marking
        # it as an error. Any assistant content rules out this fallback.
        from core.execution.error_classifier import detect_cli_error_envelope

        if text.strip() or not isinstance(result_text, str) or not detect_cli_error_envelope(result_text):
            return False
    errors = getattr(result, "errors", None)
    details = [item for item in errors if isinstance(item, str)] if isinstance(errors, list) else []
    if isinstance(result_text, str):
        details.append(result_text)
    if is_error:
        details.extend([text, assistant_error or ""])
    return trip_claude_oauth_circuit(env, "\n".join(details))


def clear_claude_oauth_circuit(profile: Path) -> None:
    """Reset the circuit after a successful centralized re-login."""
    claude_circuit_path(profile).unlink(missing_ok=True)


def _acquire(profile: Path, *, nonblocking: bool) -> TextIO:
    import fcntl

    lock_path = claude_lock_path(profile)
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_file = lock_path.open("a+", encoding="utf-8")
    operation = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
    try:
        fcntl.flock(lock_file.fileno(), operation)
    except BaseException:
        lock_file.close()
        raise
    return lock_file


def _release(lock_file: TextIO) -> None:
    import fcntl

    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


@contextmanager
def claude_auth_lock(profile: Path, *, nonblocking: bool = False) -> Iterator[None]:
    """Serialize a synchronous Claude login or credential-using operation."""
    lock_file = _acquire(profile, nonblocking=nonblocking)
    try:
        yield
    finally:
        _release(lock_file)


@asynccontextmanager
async def claude_execution_lock(env: dict[str, str] | None) -> AsyncIterator[None]:
    """Serialize SDK processes that share a Claude Code OAuth profile.

    API, Bedrock, and Vertex executions do not set ``CLAUDE_HOME`` and are
    intentionally left unconstrained.  ``flock`` makes this effective across
    all AnimaWorks worker processes on the host, not merely one event loop.
    """
    raw_profile = (env or {}).get("CLAUDE_HOME")
    if not raw_profile:
        yield
        return

    profile = Path(raw_profile).expanduser()
    if not profile.is_absolute():
        raise ValueError("CLAUDE_HOME must be absolute before acquiring the Claude execution lock")

    while True:
        try:
            lock_file = _acquire(profile, nonblocking=True)
            break
        except BlockingIOError:
            await asyncio.sleep(0.05)
    try:
        if claude_circuit_path(profile).exists():
            raise ClaudeOAuthCircuitOpen(
                "Claude OAuth circuit is open after a revoked 401; centralized re-login is required"
            )
        try:
            yield
        except BaseException as exc:
            # Trip before releasing the mutex so a queued process cannot start
            # in the gap between observing the revoked 401 and writing marker.
            trip_claude_oauth_circuit(env, str(exc))
            raise
    finally:
        _release(lock_file)
