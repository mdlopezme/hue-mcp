import ipaddress
import re
import time
from dataclasses import dataclass

import httpx2
from zeroconf import IPVersion, ServiceBrowser, ServiceListener, Zeroconf

from hue_mcp.errors import HueError

HUE_MDNS_SERVICE = "_hue._tcp.local."
CLOUD_DISCOVERY_URL = "https://discovery.meethue.com"
# Bridge ids are 16 hex digits. Anything else, from any LAN host, is not a Hue bridge.
BRIDGE_ID = re.compile("[0-9a-fA-F]{16}")


@dataclass(frozen=True)
class DiscoveredBridge:
    bridge_id: str  # lowercase, as in the CN of the bridge's TLS certificate
    ip: str


def is_ipv4(text: str) -> bool:
    try:
        ipaddress.IPv4Address(text)
    except ValueError:
        return False
    return True


def discover_lan_bridges(listen_s: float = 3.0) -> list[DiscoveredBridge]:
    """Browse mDNS for Hue bridges. Blocks for `listen_s`."""
    found: dict[str, DiscoveredBridge] = {}

    class Listener(ServiceListener):
        def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            info = zc.get_service_info(type_, name)
            if info is None:
                return
            bridge = bridge_from_mdns(info.properties, info.parsed_addresses(IPVersion.V4Only))
            if bridge is not None:
                found[bridge.bridge_id] = bridge

        update_service = add_service

        def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            pass

    zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
    try:
        ServiceBrowser(zeroconf, HUE_MDNS_SERVICE, Listener())
        time.sleep(listen_s)
    finally:
        zeroconf.close()
    return list(found.values())


def bridge_from_mdns(
    properties: dict[bytes, bytes | None], addresses: list[str]
) -> DiscoveredBridge | None:
    """A bridge from its mDNS TXT record (bridgeid) and its IPv4 addresses."""
    bridge_id = (properties.get(b"bridgeid") or b"").decode("ascii", errors="replace")
    if not BRIDGE_ID.fullmatch(bridge_id) or not addresses:
        return None
    return DiscoveredBridge(bridge_id.lower(), addresses[0])


async def discover_cloud_bridges(
    transport: httpx2.AsyncBaseTransport | None = None,
) -> list[DiscoveredBridge]:
    """Ask Signify's discovery service, which allows about one call per 15 minutes."""
    try:
        async with httpx2.AsyncClient(transport=transport, timeout=10) as client:
            response = await client.get(CLOUD_DISCOVERY_URL)
    # This internet service goes through proxy settings, which the client refuses when it
    # can't use them (ImportError for SOCKS without socksio, ValueError for unknown schemes).
    except (httpx2.HTTPError, ImportError, ValueError) as error:
        raise HueError(f"Cloud discovery failed: {error}") from error
    if response.status_code == 429:
        raise HueError("Cloud discovery is rate-limited; wait 15 minutes or pass --ip.")
    if response.is_error:
        raise HueError(f"Cloud discovery failed with HTTP {response.status_code}.")
    try:
        entries = response.json()
    except ValueError:
        raise HueError("Cloud discovery answered with something other than JSON.") from None
    if not isinstance(entries, list):
        raise HueError("Cloud discovery answered with something other than a list of bridges.")
    return [
        DiscoveredBridge(entry["id"].lower(), entry["internalipaddress"])
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("id"), str)
        and BRIDGE_ID.fullmatch(entry["id"])
        and isinstance(entry.get("internalipaddress"), str)
        and is_ipv4(entry["internalipaddress"])
    ]
