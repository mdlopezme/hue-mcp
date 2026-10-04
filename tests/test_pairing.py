import json
import socket
import ssl
import stat
from typing import Any

import anyio
import httpx2
import pytest

from hue_mcp import pairing
from hue_mcp.config import BridgeConfig, config_path, load_config
from hue_mcp.discovery import CLOUD_DISCOVERY_URL, DiscoveredBridge, discover_cloud_bridges
from hue_mcp.errors import HueError

from conftest import CONFIG

pytestmark = pytest.mark.anyio

NEW_APP_KEY = "freshly-issued-key"
# Read before the fixtures below patch it, so the shipped value is what gets checked.
REAL_LINK_BUTTON_POLL_S = pairing.LINK_BUTTON_POLL_S
LINK_BUTTON_NOT_PRESSED = [{"error": {"type": 101, "description": "link button not pressed"}}]


class FakePairingBridge:
    """A bridge whose link button gets pressed after `presses_after` pairing attempts."""

    def __init__(self, resources: list[dict[str, Any]], presses_after: int = 2):
        self.resources = resources
        self.presses_after = presses_after
        self.pairing_requests: list[httpx2.Request] = []
        self.cloud_bridges: list[dict[str, Any]] = []
        self.cloud_status = 200

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        if str(request.url) == CLOUD_DISCOVERY_URL:
            return httpx2.Response(self.cloud_status, json=self.cloud_bridges)
        if request.url.path == "/api/config":
            return httpx2.Response(200, json={"bridgeid": CONFIG.bridge_id.upper()})
        if request.url.path == "/api" and request.method == "POST":
            self.pairing_requests.append(request)
            if len(self.pairing_requests) <= self.presses_after:
                return httpx2.Response(200, json=LINK_BUTTON_NOT_PRESSED)
            return httpx2.Response(200, json=[{"success": {"username": NEW_APP_KEY}}])
        if request.url.path == "/clip/v2/resource":
            assert request.headers["hue-application-key"] == NEW_APP_KEY
            return httpx2.Response(200, json={"errors": [], "data": self.resources})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")


@pytest.fixture
def fake(resources: list[dict[str, Any]]) -> FakePairingBridge:
    return FakePairingBridge(resources)


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(pairing, "LINK_BUTTON_POLL_S", 0)
    monkeypatch.setattr(pairing, "discover_lan_bridges", lambda: [])


async def test_setup_with_ip_pairs_and_saves_a_private_config(fake, capsys):
    await pairing.run_setup(CONFIG.ip, httpx2.MockTransport(fake.handle))

    assert load_config() == BridgeConfig(CONFIG.bridge_id, CONFIG.ip, NEW_APP_KEY)
    assert stat.S_IMODE(config_path().stat().st_mode) == 0o600
    assert len(fake.pairing_requests) == 3
    first_attempt = fake.pairing_requests[0]
    assert first_attempt.extensions["sni_hostname"] == CONFIG.bridge_id
    devicetype = json.loads(first_attempt.content)["devicetype"]
    assert devicetype.startswith("hue-mcp#")
    assert len(devicetype) <= 40  # The bridge's limit.
    output = capsys.readouterr().out
    assert "Press the round link button" in output
    assert "4 lights in 2 rooms: Bedroom, Living room." in output


async def test_setup_finds_the_bridge_over_mdns(fake, monkeypatch):
    monkeypatch.setattr(
        pairing,
        "discover_lan_bridges",
        lambda: [DiscoveredBridge(CONFIG.bridge_id, CONFIG.ip)],
    )
    await pairing.run_setup(None, httpx2.MockTransport(fake.handle))
    assert load_config().ip == CONFIG.ip


async def test_setup_falls_back_to_cloud_discovery(fake):
    fake.cloud_bridges = [{"id": CONFIG.bridge_id.upper(), "internalipaddress": CONFIG.ip}]
    await pairing.run_setup(None, httpx2.MockTransport(fake.handle))
    assert load_config().bridge_id == CONFIG.bridge_id


async def test_setup_without_any_bridge_says_how_to_continue(fake):
    with pytest.raises(HueError, match="No Hue Bridge found"):
        await pairing.run_setup(None, httpx2.MockTransport(fake.handle))


async def test_setup_with_several_bridges_asks_to_choose(fake):
    fake.cloud_bridges = [
        {"id": "001788fffe000001", "internalipaddress": "192.168.1.2"},
        {"id": "001788fffe000002", "internalipaddress": "192.168.1.3"},
    ]
    with pytest.raises(HueError, match=r"several bridges.*choose one with --ip"):
        await pairing.run_setup(None, httpx2.MockTransport(fake.handle))


async def test_unpressed_link_button_times_out(fake, monkeypatch):
    monkeypatch.setattr(pairing, "LINK_BUTTON_ATTEMPTS", 2)
    with pytest.raises(HueError, match="wasn't pressed in time"):
        await pairing.run_setup(CONFIG.ip, httpx2.MockTransport(fake.handle))
    assert load_config() is None


async def test_other_pairing_errors_are_reported(fake):
    def refuse(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api":
            return httpx2.Response(
                200, json=[{"error": {"type": 7, "description": "invalid value for devicetype"}}]
            )
        return fake.handle(request)

    with pytest.raises(HueError, match="refused pairing: invalid value for devicetype"):
        await pairing.run_setup(CONFIG.ip, httpx2.MockTransport(refuse))


async def test_unreachable_ip_is_reported():
    def unreachable(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route to host")

    with pytest.raises(HueError, match="No Hue Bridge answered at"):
        await pairing.run_setup(CONFIG.ip, httpx2.MockTransport(unreachable))


async def test_lost_contact_during_pairing_is_reported(fake):
    def drops_pairing(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api":
            raise httpx2.ReadTimeout("timed out")
        return fake.handle(request)

    with pytest.raises(HueError, match="Lost contact with the bridge"):
        await pairing.run_setup(CONFIG.ip, httpx2.MockTransport(drops_pairing))


@pytest.mark.parametrize(
    ("status", "message"),
    [(429, "rate-limited"), (500, "HTTP 500")],
)
async def test_cloud_discovery_failures(fake, status, message):
    fake.cloud_status = status
    with pytest.raises(HueError, match=message):
        await discover_cloud_bridges(httpx2.MockTransport(fake.handle))


async def test_cloud_entries_without_a_valid_address_are_ignored(fake):
    fake.cloud_bridges = [
        {"id": "001788fffe000001", "internalipaddress": "evil.example/api"},
        {"internalipaddress": "192.168.1.3"},
        {"id": CONFIG.bridge_id.upper(), "internalipaddress": CONFIG.ip},
    ]
    found = await discover_cloud_bridges(httpx2.MockTransport(fake.handle))
    assert found == [DiscoveredBridge(CONFIG.bridge_id, CONFIG.ip)]


async def test_cloud_discovery_that_is_not_json_is_reported():
    def html(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text="<html>maintenance</html>")

    with pytest.raises(HueError, match="other than JSON"):
        await discover_cloud_bridges(httpx2.MockTransport(html))


async def test_unreachable_cloud_discovery_is_reported():
    def offline(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no internet")

    with pytest.raises(HueError, match="Cloud discovery failed: no internet"):
        await discover_cloud_bridges(httpx2.MockTransport(offline))


async def test_every_connection_verifies_the_bridge_and_ignores_proxies(fake, monkeypatch):
    created: list[dict[str, Any]] = []
    real_client = httpx2.AsyncClient

    def recording_client(**options):
        created.append(options)
        return real_client(**options)

    monkeypatch.setattr(httpx2, "AsyncClient", recording_client)
    await pairing.run_setup(CONFIG.ip, httpx2.MockTransport(fake.handle))
    learn_id, pair, summary = created
    assert all(options["trust_env"] is False for options in created)
    assert all(options["verify"].verify_mode == ssl.CERT_REQUIRED for options in created)
    assert not learn_id["verify"].check_hostname  # The bridge id isn't known yet.
    assert pair["verify"].check_hostname
    assert summary["verify"].check_hostname


async def test_waiting_for_the_button_asks_about_once_a_second_for_a_minute(fake, monkeypatch):
    waits: list[float] = []

    async def record(delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr(anyio, "sleep", record)
    monkeypatch.setattr(pairing, "LINK_BUTTON_POLL_S", REAL_LINK_BUTTON_POLL_S)
    await pairing.run_setup(CONFIG.ip, httpx2.MockTransport(fake.handle))
    assert waits == [1.0, 1.0]
    assert pairing.LINK_BUTTON_ATTEMPTS * REAL_LINK_BUTTON_POLL_S == 60


async def test_a_busy_bridge_is_asked_again(fake):
    answers = iter([httpx2.Response(503, text="busy"), httpx2.Response(429)])

    def busy_at_first(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api":
            busy = next(answers, None)
            if busy is not None:
                return busy
        return fake.handle(request)

    await pairing.run_setup(CONFIG.ip, httpx2.MockTransport(busy_at_first))
    assert load_config().app_key == NEW_APP_KEY


@pytest.mark.parametrize(
    "answer",
    [
        httpx2.Response(401, json={"errors": [{"description": "unauthorized"}]}),
        httpx2.Response(200, json=[]),
        httpx2.Response(200, json=[1]),
        httpx2.Response(500, text="<html>error</html>"),
    ],
)
async def test_unexpected_pairing_answers_are_reported(fake, answer):
    def odd(request: httpx2.Request) -> httpx2.Response:
        return answer if request.url.path == "/api" else fake.handle(request)

    with pytest.raises(HueError, match="answered pairing unexpectedly"):
        await pairing.run_setup(CONFIG.ip, httpx2.MockTransport(odd))


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (httpx2.Response(503, text="busy"), "answered HTTP 503"),
        (httpx2.Response(200, json={"name": "Some router"}), "not like a Hue Bridge"),
        (httpx2.Response(200, json={"bridgeid": "\x1b[8mhidden"}), "not like a Hue Bridge"),
        (httpx2.Response(200, json={"bridgeid": "001788fffe123456-x"}), "not like a Hue Bridge"),
        (httpx2.Response(200, json=["001788fffe123456"]), "not like a Hue Bridge"),
        (httpx2.Response(200, text="<html>"), "not like a Hue Bridge"),
    ],
)
async def test_an_address_that_is_not_a_bridge_is_reported(answer, message):
    def handle(request: httpx2.Request) -> httpx2.Response:
        return answer

    with pytest.raises(HueError, match=message):
        await pairing.run_setup(CONFIG.ip, httpx2.MockTransport(handle))


async def test_long_hostnames_are_cut_to_the_bridges_limit(fake, monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "a-very-long-workstation-hostname-indeed")
    await pairing.run_setup(CONFIG.ip, httpx2.MockTransport(fake.handle))
    devicetype = json.loads(fake.pairing_requests[0].content)["devicetype"]
    assert devicetype == "hue-mcp#a-very-long-worksta"


async def test_setup_succeeds_even_if_listing_the_lights_afterwards_fails(fake, capsys):
    def no_listing(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/clip/v2/resource":
            return httpx2.Response(500, json={})
        return fake.handle(request)

    await pairing.run_setup(CONFIG.ip, httpx2.MockTransport(no_listing))
    assert load_config().app_key == NEW_APP_KEY
    assert "Couldn't list the lights yet" in capsys.readouterr().out


async def test_a_key_that_cannot_be_saved_is_reported(fake, monkeypatch):
    def failing_save(config) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(pairing, "save_config", failing_save)
    with pytest.raises(HueError, match="Paired, but couldn't save"):
        await pairing.run_setup(CONFIG.ip, httpx2.MockTransport(fake.handle))


async def test_a_bridge_busy_for_the_whole_minute_says_so(monkeypatch):
    monkeypatch.setattr(pairing, "LINK_BUTTON_ATTEMPTS", 3)

    def busy(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/config":
            return httpx2.Response(200, json={"bridgeid": CONFIG.bridge_id})
        return httpx2.Response(503, text="busy")

    with pytest.raises(HueError, match="stayed too busy"):
        await pairing.run_setup(CONFIG.ip, httpx2.MockTransport(busy))


async def test_cloud_entries_must_be_well_formed_bridges(fake):
    fake.cloud_bridges = [
        "not a bridge",
        {"id": 1234567890123456, "internalipaddress": "192.168.1.2"},
        {"id": "001788fffe123456-x", "internalipaddress": "192.168.1.3"},
        {"id": CONFIG.bridge_id, "internalipaddress": 7},
        {"id": CONFIG.bridge_id, "internalipaddress": CONFIG.ip},
    ]
    found = await discover_cloud_bridges(httpx2.MockTransport(fake.handle))
    assert found == [DiscoveredBridge(CONFIG.bridge_id, CONFIG.ip)]


async def test_cloud_discovery_that_is_not_a_list_is_reported():
    def odd(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"error": "maintenance"})

    with pytest.raises(HueError, match="other than a list of bridges"):
        await discover_cloud_bridges(httpx2.MockTransport(odd))


@pytest.mark.parametrize(
    ("proxy", "cause"),
    [("socks5://127.0.0.1:9", "socksio"), ("gopher://127.0.0.1:9", "Unknown scheme")],
)
async def test_proxy_settings_cloud_discovery_cannot_use_are_reported(monkeypatch, proxy, cause):
    # Refused while the client is built, before any request goes out.
    monkeypatch.setenv("ALL_PROXY", proxy)
    with pytest.raises(HueError, match=f"Cloud discovery failed: .*{cause}"):
        await discover_cloud_bridges()
