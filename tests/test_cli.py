import sys
from dataclasses import replace
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from hue_mcp import cli
from hue_mcp.config import save_config
from hue_mcp.errors import HueError

from conftest import CONFIG


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def test_bridge_is_created_once_from_the_config():
    get_bridge = cli._paired_bridge()
    with pytest.raises(HueError, match="Not paired"):
        get_bridge()
    save_config(CONFIG)
    bridge = get_bridge()
    assert bridge.config == CONFIG
    assert get_bridge() is bridge


def test_pairing_again_takes_effect_without_a_restart():
    save_config(CONFIG)
    get_bridge = cli._paired_bridge()
    bridge = get_bridge()
    paired_again = replace(CONFIG, app_key="new-app-key")
    save_config(paired_again)
    assert get_bridge() is bridge
    assert bridge.config == paired_again


def test_an_address_the_bridge_moved_to_is_kept_until_saved():
    save_config(CONFIG)
    get_bridge = cli._paired_bridge()
    bridge = get_bridge()
    bridge.config = replace(CONFIG, ip="192.168.1.51")  # Adopted, but not yet saved.
    assert get_bridge().config.ip == "192.168.1.51"


def test_setup_rejects_an_address_that_is_not_ipv4(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["hue-mcp", "setup", "--ip", "bridge.local/api"])
    with pytest.raises(SystemExit):
        cli.main()
    assert "is not an IPv4 address" in capsys.readouterr().err


def test_setup_prints_how_to_register_with_claude_code(monkeypatch, capsys):
    calls = []

    async def fake_setup(ip: str | None) -> None:
        calls.append(ip)

    monkeypatch.setattr(cli, "run_setup", fake_setup)
    monkeypatch.setattr(sys, "argv", ["/opt/hue-mcp/bin/hue-mcp", "setup", "--ip", CONFIG.ip])
    cli.main()
    assert calls == [CONFIG.ip]
    assert "claude mcp add --scope user hue -- /opt/hue-mcp/bin/hue-mcp" in capsys.readouterr().out


def test_setup_failure_exits_with_the_reason(monkeypatch):
    async def failing_setup(ip: str | None) -> None:
        raise HueError("No Hue Bridge found.")

    monkeypatch.setattr(cli, "run_setup", failing_setup)
    monkeypatch.setattr(sys, "argv", ["hue-mcp", "setup"])
    with pytest.raises(SystemExit, match=r"Setup failed: No Hue Bridge found\."):
        cli.main()


@pytest.mark.anyio
async def test_installed_command_serves_mcp_over_stdio(tmp_path):
    command = Path(sys.executable).parent / "hue-mcp"
    server = StdioServerParameters(command=str(command), env={"XDG_CONFIG_HOME": str(tmp_path)})
    async with Client(server) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        assert tools == {
            "get_home",
            "set_lights",
            "set_power",
            "activate_scene",
            "create_scene",
            "set_effect",
            "set_timer",
            "list_timers",
            "cancel_timer",
            "start_pomodoro",
            "save_pomodoro_look",
            "stop_pomodoro",
        }
        result = await client.call_tool("get_home", {})
        assert result.is_error
        assert "Not paired" in result.content[0].text


def test_the_printed_registration_command_survives_spaces_in_the_path(monkeypatch, capsys):
    async def fake_setup(ip: str | None) -> None:
        pass

    monkeypatch.setattr(cli, "run_setup", fake_setup)
    monkeypatch.setattr(sys, "argv", ["/home/me/my apps/hue-mcp/bin/hue-mcp", "setup"])
    cli.main()
    output = capsys.readouterr().out
    assert "hue -- '/home/me/my apps/hue-mcp/bin/hue-mcp'" in output
