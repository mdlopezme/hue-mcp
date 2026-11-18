import json
from typing import Any, ClassVar

import anyio
import httpx2
import pytest

from hue_mcp import entertainment
from hue_mcp.bridge import HueBridge
from hue_mcp.entertainment import Area, Stream, area_for, areas
from hue_mcp.errors import HueError

from conftest import AREA_ID, CONFIG, FakeBridge, resource, with_entertainment_area

pytestmark = pytest.mark.anyio

CLIENT_KEY = "00112233445566778899AABBCCDDEEFF"
APP_ID = "the-app-id"


class FakeClient:
    """Stands in for DtlsClient: records what it was given and sent."""

    made: ClassVar[list["FakeClient"]] = []
    fail_handshake: ClassVar[BaseException | None] = None

    def __init__(self, host: str, port: int, identity: str, psk: bytes):
        self.args = (host, port, identity, psk)
        self.sent: list[bytes] = []
        self.closed = False
        FakeClient.made.append(self)

    async def handshake(self) -> None:
        if FakeClient.fail_handshake is not None:
            raise FakeClient.fail_handshake

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def fake_dtls(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeClient.made, FakeClient.fail_handshake = [], None
    monkeypatch.setattr(entertainment, "DtlsClient", FakeClient)


class StreamingBridge(FakeBridge):
    """Answers /auth/v1 with an application id, and keeps the area's status."""

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/auth/v1":
            self.requests.append(("GET", "/auth/v1", None))
            return httpx2.Response(200, headers={"hue-application-id": APP_ID})
        response = super().handle(request)
        area = next((r for r in self.resources if r["id"] == AREA_ID), None)
        if request.method == "PUT" and area is not None and request.url.path.endswith(AREA_ID):
            action = json.loads(request.content)["action"]
            area["status"] = "active" if action == "start" else "inactive"
            area["active_streamer"] = {"rid": APP_ID, "rtype": "auth_v1"}
        if request.method == "GET" and request.url.path.endswith(AREA_ID):
            found = [area] if area is not None else []
            return httpx2.Response(200, json={"errors": [], "data": found})
        return response


@pytest.fixture
def bridge_and_fake(resources: list[dict[str, Any]]) -> tuple[HueBridge, StreamingBridge]:
    fake = StreamingBridge(with_entertainment_area(resources))
    config = CONFIG.__class__(CONFIG.bridge_id, CONFIG.ip, CONFIG.app_key, CLIENT_KEY)
    return HueBridge(config, transport=httpx2.MockTransport(fake.handle)), fake


def actions(fake: FakeBridge) -> list[str]:
    return [
        body["action"]
        for method, path, body in fake.writes
        if method == "PUT" and "entertainment_configuration" in path
    ]


def test_an_area_maps_its_channels_to_lights(resources):
    [area] = areas(with_entertainment_area(resources))
    assert area == Area(AREA_ID, "Movie area", {0: "light-floor", 1: "light-ceiling"})


def test_the_area_for_some_lights_must_hold_them_all(resources):
    full = with_entertainment_area(resources)
    assert area_for(full, {"light-floor"}).id == AREA_ID
    with pytest.raises(HueError, match="No entertainment area holds"):
        area_for(full, {"light-bedside"})


async def test_a_stream_starts_the_area_before_connecting_and_always_stops_it(bridge_and_fake):
    bridge, fake = bridge_and_fake
    [area] = areas(fake.resources)
    async with Stream(bridge, area) as stream:
        assert actions(fake) == ["start"]
        [client] = FakeClient.made
        assert client.args == (CONFIG.ip, 2100, APP_ID, bytes.fromhex(CLIENT_KEY))
        stream.send({"light-ceiling": (1.0, 0.0, 0.0), "light-bedside": (0.0, 1.0, 0.0)})
        assert await stream.still_ours()
    assert actions(fake) == ["start", "stop"]
    assert client.closed
    [frame] = client.sent
    assert frame[16:52] == AREA_ID.encode()
    assert frame[52:] == bytes([1, 0xFF, 0xFF, 0, 0, 0, 0])  # Channel 1 only: bedside isn't in.


async def test_a_failed_handshake_still_stops_the_area(bridge_and_fake):
    bridge, fake = bridge_and_fake
    FakeClient.fail_handshake = HueError("refused")
    with pytest.raises(HueError, match="refused"):
        async with Stream(bridge, areas(fake.resources)[0]):
            pass
    assert actions(fake) == ["start", "stop"]


async def test_a_cancelled_stream_still_stops_the_area(bridge_and_fake):
    bridge, fake = bridge_and_fake
    with anyio.CancelScope() as scope:
        async with Stream(bridge, areas(fake.resources)[0]):
            scope.cancel()
            await anyio.sleep(1)
    assert actions(fake) == ["start", "stop"]


async def test_a_pairing_without_a_streaming_key_asks_for_setup(resources):
    fake = StreamingBridge(with_entertainment_area(resources))
    bridge = HueBridge(CONFIG, transport=httpx2.MockTransport(fake.handle))
    with pytest.raises(HueError, match="hue-mcp setup"):
        async with Stream(bridge, areas(fake.resources)[0]):
            pass
    assert actions(fake) == []


async def test_a_stream_taken_over_or_gone_is_no_longer_ours(bridge_and_fake):
    bridge, fake = bridge_and_fake
    async with Stream(bridge, areas(fake.resources)[0]) as stream:
        resource(fake.resources, AREA_ID)["active_streamer"] = {"rid": "another-app"}
        assert not await stream.still_ours()
        fake.resources.remove(resource(fake.resources, AREA_ID))
        assert not await stream.still_ours()


async def test_sending_without_a_stream_is_refused(bridge_and_fake):
    bridge, fake = bridge_and_fake
    with pytest.raises(HueError, match="isn't open"):
        Stream(bridge, areas(fake.resources)[0]).send({})


async def test_another_apps_stream_is_left_alone(bridge_and_fake):
    bridge, fake = bridge_and_fake
    resource(fake.resources, AREA_ID).update(
        status="active", active_streamer={"rid": "hue-sync", "rtype": "auth_v1"}
    )
    with pytest.raises(HueError, match="Another app is streaming"):
        async with Stream(bridge, areas(fake.resources)[0]):
            pass
    assert actions(fake) == []


async def test_a_lost_stream_isnt_stopped_on_the_way_out(bridge_and_fake):
    bridge, fake = bridge_and_fake
    async with Stream(bridge, areas(fake.resources)[0]) as stream:
        resource(fake.resources, AREA_ID)["active_streamer"] = {"rid": "hue-sync"}
        assert not await stream.still_ours()
    assert actions(fake) == ["start"]  # Stopping now would stop Hue Sync.
