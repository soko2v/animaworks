"""Centralized, serialized authentication commands."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from core.execution._claude_auth_lock import claude_auth_lock, clear_claude_oauth_circuit


def _configured_claude_home() -> Path:
    """Return the sole Claude Code profile configured for AnimaWorks.

    ``claude_home`` is deliberately stored as a non-secret credential setting
    so every Mode-S subprocess and the only supported login command use the
    exact same OAuth token store.
    """
    from core.config import load_config

    credential = load_config().credentials.get("anthropic")
    raw_path = (credential.keys or {}).get("claude_home") if credential else None
    if not raw_path:
        raise ValueError("Central Claude profile is not configured. Set credentials.anthropic.keys.claude_home first.")
    profile = Path(raw_path).expanduser()
    if not profile.is_absolute():
        raise ValueError("credentials.anthropic.keys.claude_home must be an absolute path.")
    return profile


def cmd_auth_claude_login(_args) -> None:
    """Start the only supported interactive Claude subscription re-login."""
    from core.platform.claude_code import get_claude_executable

    try:
        profile = _configured_claude_home()
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    cli = get_claude_executable()
    if not cli:
        print("Error: Claude Code CLI is not installed or unavailable.", file=sys.stderr)
        raise SystemExit(1)

    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = os.environ.copy()
    env["CLAUDE_HOME"] = str(profile)
    # An inherited API credential takes precedence over Claude.ai OAuth and
    # makes a successful browser login look like an invalid API-key failure.
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    try:
        with claude_auth_lock(profile, nonblocking=True):
            print("Starting the centralized Claude re-authentication flow. Do not run `claude login` elsewhere.")
            result = subprocess.run([cli, "auth", "login", "--claudeai"], env=env, check=False)
    except BlockingIOError as exc:
        print("Error: Claude OAuth is currently in use. Stop Claude Animas before re-authentication.", file=sys.stderr)
        raise SystemExit(1) from exc

    if result.returncode:
        raise SystemExit(result.returncode)
    clear_claude_oauth_circuit(profile)


def cmd_auth_claude_status(_args) -> None:
    """Report central-profile wiring and Claude CLI's non-secret auth state."""
    try:
        profile = _configured_claude_home()
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    print(f"Central Claude profile: {profile}")
    from core.platform.claude_code import get_claude_executable

    cli = get_claude_executable()
    if not cli:
        print("Claude CLI status: unavailable")
        return
    env = os.environ.copy()
    env["CLAUDE_HOME"] = str(profile)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    result = subprocess.run([cli, "auth", "status"], env=env, check=False, text=True, capture_output=True)
    print("Claude CLI status:")
    print((result.stdout or result.stderr).strip() or f"unavailable (exit {result.returncode})")
    print("All Mode-S Max-plan Animas inherit this profile. Use `animaworks auth claude login` to refresh it.")


def register_auth_command(subparsers) -> None:
    """Register ``animaworks auth claude {login,status}``."""
    parser = subparsers.add_parser("auth", help="Manage centralized provider authentication")
    providers = parser.add_subparsers(dest="auth_provider", required=True)
    claude = providers.add_parser("claude", help="Central Claude subscription authentication")
    commands = claude.add_subparsers(dest="auth_command", required=True)
    login = commands.add_parser("login", help="Run the single serialized Claude login flow")
    login.set_defaults(func=cmd_auth_claude_login)
    status = commands.add_parser("status", help="Show central Claude profile wiring")
    status.set_defaults(func=cmd_auth_claude_status)
