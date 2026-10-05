import sys
from dataclasses import replace
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from hue_mcp import cli
from hue_mcp.config import Location, load_location, save_config
from hue_mcp.errors import HueError
from hue_mcp.wayland_idle import WaylandError

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
            "get_pomodoro",
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


LISBON = Location(38.72, -9.14, "Lisbon, Lisbon, Portugal")
LISBON_MAINE = Location(44.03, -70.1, "Lisbon, Maine, United States")


def finding(*places: Location):
    async def find_places(city: str) -> list[Location]:
        return list(places)

    return find_places


def test_set_location_saves_the_only_match(monkeypatch, capsys):
    monkeypatch.setattr(cli, "find_places", finding(LISBON))
    monkeypatch.setattr(sys, "argv", ["hue-mcp", "set-location", "Lisbon"])
    cli.main()
    assert load_location() == LISBON
    assert "Saved Lisbon, Lisbon, Portugal (38.72, -9.14)" in capsys.readouterr().out


def test_set_location_asks_which_match_until_it_gets_a_number(monkeypatch, capsys):
    monkeypatch.setattr(cli, "find_places", finding(LISBON, LISBON_MAINE))
    answers = iter(["", "3", "2"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(sys, "argv", ["hue-mcp", "set-location", "Lisbon"])
    cli.main()
    assert load_location() == LISBON_MAINE
    output = capsys.readouterr().out
    assert "2. Lisbon, Maine, United States" in output
    assert output.count("Type a number from 1 to 2.") == 2


def test_set_location_with_no_match_says_so(monkeypatch):
    monkeypatch.setattr(cli, "find_places", finding())
    monkeypatch.setattr(sys, "argv", ["hue-mcp", "set-location", "Xyzzy"])
    with pytest.raises(SystemExit, match="Couldn't set the location: No place called 'Xyzzy'"):
        cli.main()
    assert load_location() is None


def test_closing_the_question_exits_quietly(monkeypatch):
    def closed(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr(cli, "find_places", finding(LISBON, LISBON_MAINE))
    monkeypatch.setattr("builtins.input", closed)
    monkeypatch.setattr(sys, "argv", ["hue-mcp", "set-location", "Lisbon"])
    with pytest.raises(SystemExit) as exit_:
        cli.main()
    assert exit_.value.code == 130
    assert load_location() is None


def test_watch_runs_the_watcher(monkeypatch):
    ran = []

    async def run(self) -> None:
        ran.append(self)

    monkeypatch.setattr(cli.Watcher, "run", run)
    monkeypatch.setattr(sys, "argv", ["hue-mcp", "watch"])
    cli.main()
    assert len(ran) == 1


def test_watch_print_activity_prints_each_change(monkeypatch, capsys):
    async def changes(timeout_ms: int):
        yield False
        yield True
        raise WaylandError("compositor gone")

    monkeypatch.setattr(cli, "idle_changes", changes)
    monkeypatch.setattr(sys, "argv", ["hue-mcp", "watch", "--print-activity"])
    with pytest.raises(SystemExit, match="Can't watch activity: compositor gone"):
        cli.main()
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].endswith(" active")
    assert lines[2].endswith(" idle")


def test_ctrl_c_stops_watching_without_a_traceback(monkeypatch):
    async def interrupted(timeout_ms: int):
        yield False
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "idle_changes", interrupted)
    monkeypatch.setattr(sys, "argv", ["hue-mcp", "watch", "--print-activity"])
    with pytest.raises(SystemExit) as exit_:
        cli.main()
    assert exit_.value.code == 130


@pytest.fixture
def fake_systemctl(tmp_path, monkeypatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "systemctl"
    script.write_text(
        f'#!/bin/sh\necho "$@" >> {tmp_path / "systemctl.log"}\n'
        'if [ -n "$FAIL" ]; then echo "unit broken" >&2; exit 1; fi\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    return tmp_path / "systemctl.log"


def test_install_watcher_writes_the_unit_and_restarts_it(monkeypatch, tmp_path, fake_systemctl):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(sys, "argv", ["/opt/my 100% apps/bin/hue-mcp", "install-watcher"])
    cli.main()
    unit = (tmp_path / "systemd" / "user" / "hue-mcp-watch.service").read_text()
    assert 'ExecStart="/opt/my 100%% apps/bin/hue-mcp" watch' in unit
    assert f'Environment="XDG_CONFIG_HOME={tmp_path}"' in unit
    assert "XDG_STATE_HOME" not in unit
    assert "WantedBy=graphical-session.target" in unit
    assert fake_systemctl.read_text().splitlines() == [
        "--user daemon-reload",
        "--user enable hue-mcp-watch.service",
        "--user restart hue-mcp-watch.service",  # Picks up a new version.
    ]


def test_install_watcher_reports_a_systemctl_failure(monkeypatch, fake_systemctl):
    monkeypatch.setenv("FAIL", "1")
    monkeypatch.setattr(sys, "argv", ["hue-mcp", "install-watcher"])
    with pytest.raises(SystemExit, match="daemon-reload failed: unit broken"):
        cli.main()


def test_install_watcher_needs_systemd(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["hue-mcp", "install-watcher"])
    with pytest.raises(SystemExit, match="systemctl isn't here"):
        cli.main()
