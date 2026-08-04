from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from cli.commands.auth_cmd import cmd_auth_claude_login, cmd_auth_claude_status
from core.config.schemas import AnimaWorksConfig, CredentialConfig


def test_status_reports_only_profile_wiring(tmp_path, capsys) -> None:
    profile = tmp_path / "central-claude"
    profile.mkdir()
    config = AnimaWorksConfig(
        credentials={"anthropic": CredentialConfig(type="claude_code_login", keys={"claude_home": str(profile)})}
    )
    completed = Mock(returncode=0, stdout='{"loggedIn":true,"authMethod":"claude.ai"}', stderr="")
    with (
        patch("core.config.load_config", return_value=config),
        patch("core.platform.claude_code.get_claude_executable", return_value="/usr/bin/claude"),
        patch("cli.commands.auth_cmd.subprocess.run", return_value=completed) as run,
    ):
        cmd_auth_claude_status(SimpleNamespace())
    output = capsys.readouterr().out
    assert str(profile) in output
    assert '"loggedIn":true' in output
    assert run.call_args.args[0] == ["/usr/bin/claude", "auth", "status"]


def test_status_requires_configured_central_profile(capsys) -> None:
    config = AnimaWorksConfig(credentials={"anthropic": CredentialConfig(type="claude_code_login")})
    with patch("core.config.load_config", return_value=config), pytest.raises(SystemExit) as exc:
        cmd_auth_claude_status(SimpleNamespace())
    assert exc.value.code == 2
    assert "Central Claude profile is not configured" in capsys.readouterr().err


def test_login_uses_central_profile_and_removes_inherited_api_credentials(tmp_path) -> None:
    profile = tmp_path / "central-claude"
    config = AnimaWorksConfig(
        credentials={"anthropic": CredentialConfig(type="claude_code_login", keys={"claude_home": str(profile)})}
    )
    completed = Mock(returncode=0)
    with (
        patch("core.config.load_config", return_value=config),
        patch("core.platform.claude_code.get_claude_executable", return_value="/usr/bin/claude"),
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "inherited", "ANTHROPIC_AUTH_TOKEN": "inherited"}),
        patch("cli.commands.auth_cmd.subprocess.run", return_value=completed) as run,
    ):
        cmd_auth_claude_login(SimpleNamespace())

    assert run.call_args.args[0] == ["/usr/bin/claude", "auth", "login", "--claudeai"]
    env = run.call_args.kwargs["env"]
    assert env["CLAUDE_HOME"] == str(profile)
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
