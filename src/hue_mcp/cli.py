import argparse
import shlex
import sys
from collections.abc import Callable
from pathlib import Path

import anyio

from hue_mcp.bridge import HueBridge
from hue_mcp.config import BridgeConfig, load_config
from hue_mcp.discovery import is_ipv4
from hue_mcp.errors import HueError
from hue_mcp.pairing import run_setup
from hue_mcp.server import build_server


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
    args = parser.parse_args()

    if args.command == "setup":
        try:
            anyio.run(run_setup, args.ip)
        except HueError as error:
            sys.exit(f"Setup failed: {error}")
        print("\nTo let Claude Code use it:")
        command = shlex.quote(str(Path(sys.argv[0]).absolute()))
        print(f"  claude mcp add --scope user hue -- {command}")
        return
    build_server(_paired_bridge()).run()


def _ipv4(text: str) -> str:
    if not is_ipv4(text):
        raise argparse.ArgumentTypeError(f"{text!r} is not an IPv4 address like 192.168.1.20")
    return text


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
