"""`hue-mcp setup`: find the bridge and get an app key by having the user press its link button."""

import socket
from typing import Any

import anyio
import httpx2
from anyio import to_thread

from hue_mcp.bridge import RETRY_STATUS_CODES, HueBridge, bridge_ssl_context
from hue_mcp.config import BridgeConfig, config_path, save_config
from hue_mcp.discovery import (
    BRIDGE_ID,
    DiscoveredBridge,
    discover_cloud_bridges,
    discover_lan_bridges,
)
from hue_mcp.errors import HueError
from hue_mcp.home import Home

APP_NAME = "hue-mcp"
LINK_BUTTON_NOT_PRESSED = 101
LINK_BUTTON_ATTEMPTS = 60
LINK_BUTTON_POLL_S = 1.0

Transport = httpx2.AsyncBaseTransport | None


async def run_setup(ip: str | None, transport: Transport = None) -> None:
    bridge = await _locate_bridge(ip, transport)
    print(f"Found Hue Bridge {bridge.bridge_id} at {bridge.ip}.")
    print("Press the round link button on top of the bridge (waiting about a minute)...")
    app_key = await _wait_for_link_button(bridge, transport)
    config = BridgeConfig(bridge_id=bridge.bridge_id, ip=bridge.ip, app_key=app_key)
    try:
        save_config(config)
    except OSError as error:
        raise HueError(f"Paired, but couldn't save {config_path()}: {error}") from error
    print(f"Paired, and saved the app key to {config_path()}.")

    try:
        home = Home(await HueBridge(config, transport).get_resources())
    except HueError as error:  # Pairing worked; only this summary didn't.
        print(f"Couldn't list the lights yet: {error}")
        return
    rooms = [group.name for group in home.groups if group.kind == "room"]
    print(f"{len(home.lights)} lights in {len(rooms)} rooms: {', '.join(rooms)}.")


async def _locate_bridge(ip: str | None, transport: Transport) -> DiscoveredBridge:
    if ip is not None:
        return DiscoveredBridge(await _read_bridge_id(ip, transport), ip)
    bridges = await to_thread.run_sync(discover_lan_bridges)
    if not bridges:
        print("No bridge answered on the local network; asking Signify's discovery service...")
        bridges = await discover_cloud_bridges(transport)
    if not bridges:
        raise HueError("No Hue Bridge found. Check it is on this network, or pass --ip.")
    if len(bridges) > 1:
        found = ", ".join(f"{b.bridge_id} at {b.ip}" for b in bridges)
        raise HueError(f"Found several bridges ({found}); choose one with --ip.")
    return bridges[0]


def _client(transport: Transport, check_hostname: bool = True) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        verify=bridge_ssl_context(check_hostname),
        transport=transport,
        timeout=10,
        trust_env=False,  # The bridge is on the LAN: proxy settings must not apply.
    )


async def _read_bridge_id(ip: str, transport: Transport) -> str:
    # The certificate must chain to a Hue root CA, but the name it carries (the bridge id) is
    # what we're asking for, so it can't be checked yet.
    try:
        async with _client(transport, check_hostname=False) as client:
            response = await client.get(f"https://{ip}/api/config")
    except httpx2.HTTPError as error:
        raise HueError(f"No Hue Bridge answered at {ip}: {error}") from error
    if response.is_error:
        raise HueError(f"The bridge at {ip} answered HTTP {response.status_code}; try again.")
    try:
        bridge_id = str(response.json()["bridgeid"])
    except (ValueError, KeyError, TypeError):
        bridge_id = ""
    if not BRIDGE_ID.fullmatch(bridge_id):
        raise HueError(f"{ip} answered, but not like a Hue Bridge.")
    return bridge_id.lower()


async def _wait_for_link_button(bridge: DiscoveredBridge, transport: Transport) -> str:
    # The bridge allows at most 19 characters for the device part of the name.
    devicetype = f"{APP_NAME}#{socket.gethostname()[:19]}"
    busy = False
    try:
        async with _client(transport) as client:
            for _ in range(LINK_BUTTON_ATTEMPTS):
                response = await client.post(
                    f"https://{bridge.ip}/api",
                    json={"devicetype": devicetype},
                    extensions={"sni_hostname": bridge.bridge_id},
                )
                busy = response.status_code in RETRY_STATUS_CODES
                if not busy:  # A busy bridge is just asked again.
                    result = _pairing_result(response)
                    if "success" in result:
                        app_key: str = result["success"]["username"]
                        return app_key
                    error = result.get("error", {})
                    if error.get("type") != LINK_BUTTON_NOT_PRESSED:
                        refusal = error.get("description", result)
                        raise HueError(f"The bridge refused pairing: {refusal}")
                await anyio.sleep(LINK_BUTTON_POLL_S)
    except httpx2.HTTPError as error:
        raise HueError(f"Lost contact with the bridge at {bridge.ip}: {error}") from error
    if busy:
        raise HueError("The bridge stayed too busy to answer; wait a minute and run setup again.")
    raise HueError("The link button wasn't pressed in time; run setup again.")


def _pairing_result(response: httpx2.Response) -> dict[str, Any]:
    """The bridge answers a pairing request with [{"success": ...}] or [{"error": ...}]."""
    try:
        results = response.json()
    except ValueError:
        results = None
    if not (isinstance(results, list) and len(results) == 1 and isinstance(results[0], dict)):
        raise HueError(f"The bridge answered pairing unexpectedly (HTTP {response.status_code}).")
    result: dict[str, Any] = results[0]
    return result
