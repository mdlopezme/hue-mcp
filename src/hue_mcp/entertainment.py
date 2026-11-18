"""Entertainment areas, and streaming colors to one: start it, hold a DTLS stream, stop it."""

from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

import anyio

from hue_mcp.bridge import HueBridge
from hue_mcp.dtls import DtlsClient
from hue_mcp.errors import HueError
from hue_mcp.huestream import RGB, encode

STREAM_PORT = 2100
NO_CLIENT_KEY = (
    "Light shows need a streaming key, which this pairing lacks. Ask the user to run "
    "`hue-mcp setup` once more and press the bridge's link button."
)


@dataclass(frozen=True)
class Area:
    """An entertainment area, with the light each streaming channel drives."""

    id: str
    name: str
    channels: dict[int, str]  # Channel id: light id.


def areas(resources: list[dict[str, Any]]) -> list[Area]:
    """Each area's channels mapped to lights, through the entertainment service's device."""
    by_id = {resource["id"]: resource for resource in resources}
    light_of_device = {
        resource["owner"]["rid"]: resource["id"]
        for resource in resources
        if resource["type"] == "light"
    }
    found = []
    for config in (r for r in resources if r["type"] == "entertainment_configuration"):
        channels = {}
        for channel in config.get("channels", []):
            for member in channel.get("members", []):
                service = by_id.get(member["service"]["rid"], {})
                light = light_of_device.get(service.get("owner", {}).get("rid"))
                if light is not None:
                    channels[channel["channel_id"]] = light
        found.append(Area(config["id"], config["metadata"]["name"], channels))
    return found


def area_for(resources: list[dict[str, Any]], light_ids: set[str]) -> Area:
    """The area with all these lights, preferring the one with the fewest others."""
    covering = [area for area in areas(resources) if light_ids <= set(area.channels.values())]
    if not covering:
        raise HueError(
            "No entertainment area holds all of those lights. Ask the user to make one in the "
            "Hue app (Settings, Entertainment areas)."
        )
    return min(covering, key=lambda area: (len(area.channels), area.name))


class Stream:
    """Streams colors to an area's lights. Entering starts the area and connects; leaving
    always stops it, so the bridge hands the lights back."""

    def __init__(self, bridge: HueBridge, area: Area):
        self.bridge = bridge
        self.area = area
        self.application_id = ""
        self._client: DtlsClient | None = None
        self._sequence = 0
        self._lost = False  # Another app has the area: stopping it would stop theirs.

    async def __aenter__(self) -> Self:
        client_key = self.bridge.config.client_key
        if not client_key:
            raise HueError(NO_CLIENT_KEY)
        self.application_id = await self.bridge.application_id()
        config = await self.bridge.get_resource("entertainment_configuration", self.area.id)
        streamer = (config or {}).get("active_streamer", {}).get("rid")
        if (config or {}).get("status") == "active" and streamer != self.application_id:
            raise HueError(f"Another app is streaming to {self.area.name}; stop it first.")
        try:
            await self._set_action("start")  # The bridge only takes a handshake once started.
            self._client = DtlsClient(
                self.bridge.config.ip, STREAM_PORT, self.application_id, bytes.fromhex(client_key)
            )
            await self._client.handshake()
        except BaseException:
            with anyio.CancelScope(shield=True):  # Even when cancelled: hand the lights back.
                await self._stop()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        with anyio.CancelScope(shield=True):  # A cancelled show must still hand the lights back.
            await self._stop()

    def send(self, colors: dict[str, RGB]) -> None:
        """Colors by light id; lights outside the area are ignored."""
        if self._client is None:
            raise HueError("The stream isn't open.")
        by_channel = {
            channel: colors[light]
            for channel, light in self.area.channels.items()
            if light in colors
        }
        self._client.send(encode(self.area.id, self._sequence, by_channel))
        self._sequence += 1

    async def still_ours(self) -> bool:
        """Whether the bridge still streams this area from us: it ends a stream that goes
        quiet for 10 s, and another app can take over."""
        config = await self.bridge.get_resource("entertainment_configuration", self.area.id)
        if config is None:
            return False
        streamer = config.get("active_streamer", {}).get("rid")
        ours = bool(config.get("status") == "active" and streamer == self.application_id)
        self._lost = not ours
        return ours

    async def _stop(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        if not self._lost:
            await self._set_action("stop")

    async def _set_action(self, action: str) -> None:
        await self.bridge.update("entertainment_configuration", self.area.id, {"action": action})
