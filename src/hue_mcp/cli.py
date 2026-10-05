import argparse
import logging
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import anyio

from hue_mcp.bridge import HueBridge
from hue_mcp.config import BridgeConfig, Location, config_dir, load_config, save_location
from hue_mcp.discovery import is_ipv4
from hue_mcp.errors import HueError
from hue_mcp.pairing import run_setup
from hue_mcp.places import find_places
from hue_mcp.server import build_server
from hue_mcp.session import IDLE_TIMEOUT_S
from hue_mcp.watcher import Watcher
from hue_mcp.wayland_idle import WaylandError, idle_changes

SERVICE_NAME = "hue-mcp-watch.service"
FAILURES = {"setup": "Setup failed", "set-location": "Couldn't set the location"}


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="hue-mcp",
        description="MCP server for Philips Hue lights. With no command, serves MCP over stdio.",
    )
    commands = parser.add_subparsers(dest="command")
    setup = commands.add_parser("setup", help="find the Hue Bridge and pair with it")
    setup.add_argument(
        "--ip", type=_ipv4, help="the bridge's IP address, if discovery doesn't find it"
    )
    set_location = commands.add_parser(
        "set-location", help="where the lights are, for the pomodoro's sun times"
    )
    set_location.add_argument("city", help='a city name, like "Lisbon"')
    watch = commands.add_parser("watch", help="run the pomodoro watcher (a background service)")
    watch.add_argument(
        "--print-activity",
        action="store_true",
        help="only print when you go idle and come back, to check activity detection",
    )
    commands.add_parser(
        "install-watcher", help="run the pomodoro watcher now and at every login (systemd)"
    )
    args = parser.parse_args()

    try:
        if args.command == "setup":
            anyio.run(run_setup, args.ip)
            print("\nTo let Claude Code use it:")
            print(f"  claude mcp add --scope user hue -- {shlex.quote(str(_executable()))}")
        elif args.command == "set-location":
            _set_location(args.city)
        elif args.command == "watch" and args.print_activity:
            anyio.run(_print_activity)
        elif args.command == "watch":
            logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
            anyio.run(Watcher(_paired_bridge()).run)
        elif args.command == "install-watcher":
            _install_watcher()
        else:
            build_server(_paired_bridge()).run()
    except HueError as error:
        sys.exit(f"{FAILURES.get(args.command, 'Failed')}: {error}")
    except (KeyboardInterrupt, EOFError):  # Ctrl+C, or Ctrl+D at a question.
        sys.exit(130)


def _ipv4(text: str) -> str:
    if not is_ipv4(text):
        raise argparse.ArgumentTypeError(f"{text!r} is not an IPv4 address like 192.168.1.20")
    return text


def _executable() -> Path:
    return Path(sys.argv[0]).absolute()


def _paired_bridge() -> Callable[[], HueBridge]:
    """Reads the config on every call, so pairing again takes effect without a restart."""
    bridge: HueBridge | None = None
    last_read: BridgeConfig | None = None

    def get_bridge() -> HueBridge:
        nonlocal bridge, last_read
        config = load_config()
        if config is None:
            raise HueError(
                "Not paired with a Hue Bridge yet. Ask the user to run `hue-mcp setup` in a "
                "terminal and press the bridge's link button when it asks."
            )
        if bridge is None:
            bridge = HueBridge(config)
        elif config != last_read:  # Paired again, or the bridge moved and was saved.
            bridge.config = config
        last_read = config
        return bridge

    return get_bridge


def _set_location(city: str) -> None:
    places = anyio.run(find_places, city)
    if not places:
        raise HueError(f"No place called {city!r} turned up; try a nearby larger city.")
    place = places[0] if len(places) == 1 else _choose(places)
    save_location(place)
    print(f"Saved {place.label} ({place.latitude:.2f}, {place.longitude:.2f}).")
    print("The pomodoro's looks now follow the sun there.")


def _choose(places: list[Location]) -> Location:
    for number, place in enumerate(places, start=1):
        print(f"  {number}. {place.label} ({place.latitude:.2f}, {place.longitude:.2f})")
    while True:
        answer = input(f"Which one? [1-{len(places)}] ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(places):
            return places[int(answer) - 1]
        print(f"Type a number from 1 to {len(places)}.")


async def _print_activity() -> None:
    print(f"Idle means no input for {IDLE_TIMEOUT_S:g} s. Ctrl+C stops.", flush=True)
    try:
        async for idle in idle_changes(round(IDLE_TIMEOUT_S * 1000)):
            print(f"{time.strftime('%H:%M:%S')} {'idle' if idle else 'active'}", flush=True)
    except (OSError, WaylandError) as error:
        raise HueError(f"Can't watch activity: {error}") from error


def _install_watcher() -> None:
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        raise HueError("systemctl isn't here; run `hue-mcp watch` some other way at login.")
    unit_path = config_dir().parent / "systemd" / "user" / SERVICE_NAME
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    # The service must read the same pairing and state as this shell, wherever they are.
    environment = "".join(
        f"Environment={_systemd_quote(f'{name}={os.environ[name]}')}\n"
        for name in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")
        if os.environ.get(name)
    )
    unit_path.write_text(
        "[Unit]\n"
        "Description=hue-mcp pomodoro watcher\n"
        "PartOf=graphical-session.target\n"
        "After=graphical-session.target\n"
        "\n"
        "[Service]\n"
        f"ExecStart={_systemd_quote(str(_executable()))} watch\n"
        f"{environment}"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=graphical-session.target\n"
    )
    # A restart, so installing again after an update runs the new code.
    steps = (["daemon-reload"], ["enable", SERVICE_NAME], ["restart", SERVICE_NAME])
    for arguments in steps:
        try:
            anyio.run(anyio.run_process, [systemctl, "--user", *arguments])
        except subprocess.CalledProcessError as error:
            stderr = error.stderr.decode(errors="replace").strip()
            raise HueError(f"systemctl --user {' '.join(arguments)} failed: {stderr}") from error
    print(f"Installed {unit_path}; the watcher runs now and at every login.")
    print(f"  Status: systemctl --user status {SERVICE_NAME}")
    print(f"  Logs:   journalctl --user -u {SERVICE_NAME}")


def _systemd_quote(text: str) -> str:
    """systemd's own quoting: double quotes with backslash escapes, and % doubled."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'
