import re
import ssl

import anyio
import httpx2
import pytest

from hue_mcp import bridge as bridge_module
from hue_mcp import discovery
from hue_mcp.bridge import HueBridge
from hue_mcp.config import load_config
from hue_mcp.discovery import DiscoveredBridge
from hue_mcp.errors import HueError

from conftest import CONFIG

pytestmark = pytest.mark.anyio

NEW_IP = "192.168.1.51"


def bridge_answering(*responses: httpx2.Response) -> tuple[HueBridge, list[httpx2.Request]]:
    requests: list[httpx2.Request] = []
    remaining = list(responses)

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return remaining.pop(0)

    return HueBridge(CONFIG, transport=httpx2.MockTransport(handle)), requests


@pytest.fixture
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


@pytest.fixture
def bridge_moved_to_new_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        discovery,
        "discover_lan_bridges",
        lambda: [
            DiscoveredBridge("001788fffe654321", "192.168.1.9"),
            DiscoveredBridge(CONFIG.bridge_id, NEW_IP),
        ],
    )


def unreachable_at_old_ip(failure: Exception):
    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == CONFIG.ip:
            raise failure
        return httpx2.Response(200, json={"errors": [], "data": []})

    return handle


async def test_requests_are_addressed_to_the_bridge_id_with_the_app_key():
    bridge, requests = bridge_answering(httpx2.Response(200, json={"errors": [], "data": []}))
    await bridge.get_resources()
    [request] = requests
    assert str(request.url) == f"https://{CONFIG.ip}/clip/v2/resource"
    assert request.headers["hue-application-key"] == CONFIG.app_key
    assert request.extensions["sni_hostname"] == CONFIG.bridge_id


@pytest.fixture
def recorded_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []

    async def record(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(anyio, "sleep", record)
    return sleeps


async def test_busy_bridge_is_retried_with_backoff(recorded_sleeps):
    bridge, requests = bridge_answering(
        httpx2.Response(429),
        httpx2.Response(503),
        httpx2.Response(200, json={"errors": [], "data": [{"id": "x"}]}),
    )
    assert await bridge.get_resources() == [{"id": "x"}]
    assert len(requests) == 3
    assert recorded_sleeps == [0.5, 1.0]


async def test_a_bridge_that_stays_busy_is_reported(recorded_sleeps):
    bridge, requests = bridge_answering(*[httpx2.Response(503)] * 3)
    with pytest.raises(HueError, match="answered HTTP 503"):
        await bridge.get_resources()
    assert len(requests) == 3


async def test_the_client_verifies_the_bridge_and_ignores_proxies(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:9")  # Would need socksio if honoured.
    created: list[dict] = []
    real_client = httpx2.AsyncClient

    def recording_client(**options):
        created.append(options)
        return real_client(**options)

    monkeypatch.setattr(httpx2, "AsyncClient", recording_client)
    HueBridge(CONFIG)
    [options] = created
    assert options["verify"].verify_mode == ssl.CERT_REQUIRED
    assert options["verify"].check_hostname
    assert options["trust_env"] is False
    assert options["timeout"] == 10
    assert options["limits"].max_connections == bridge_module.MAX_CONNECTIONS


async def test_bridge_errors_reach_the_model():
    bridge, _ = bridge_answering(
        httpx2.Response(400, json={"errors": [{"description": "invalid body"}], "data": []})
    )
    with pytest.raises(HueError, match="invalid body"):
        await bridge.update("light", "x", {})


async def test_partial_success_comes_back_as_warnings():
    bridge, _ = bridge_answering(
        httpx2.Response(
            207,
            json={
                "data": [{"rid": "x", "rtype": "grouped_light"}],
                "errors": [{"description": "device has communication issues"}],
            },
        )
    )
    assert await bridge.update("grouped_light", "x", {}) == ["device has communication issues"]


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx2.Response(500, json={}), "refused: HTTP 500"),
        (httpx2.Response(502, text="<html>Bad gateway</html>"), "answered HTTP 502"),
    ],
)
async def test_unexplained_v2_failures_are_reported(response, message):
    bridge, _ = bridge_answering(response)
    with pytest.raises(HueError, match=message):
        await bridge.update("light", "x", {})


async def test_creating_nothing_is_an_error():
    bridge, _ = bridge_answering(
        httpx2.Response(200, json={"errors": [{"description": "scene limit reached"}], "data": []})
    )
    with pytest.raises(HueError, match="created nothing: scene limit reached"):
        await bridge.create("scene", {})


async def test_non_json_timer_answers_are_reported(monkeypatch):
    monkeypatch.setattr(bridge_module, "RETRY_DELAYS_S", ())
    bridge, _ = bridge_answering(httpx2.Response(503, text="<html>busy</html>"))
    with pytest.raises(HueError, match="answered HTTP 503"):
        await bridge.get_schedules()


async def test_failed_timer_writes_are_reported():
    bridge, _ = bridge_answering(httpx2.Response(500, json={}))
    with pytest.raises(HueError, match="answered HTTP 500"):
        await bridge.delete_schedule("7")


async def test_the_app_key_never_appears_in_errors():
    echoed_address = f"/api/{CONFIG.app_key}/groups/9/action"
    bridge, _ = bridge_answering(
        httpx2.Response(
            200,
            json=[
                {
                    "error": {
                        "type": 7,
                        "address": "/schedules",
                        "description": f"invalid value, {echoed_address}, for parameter, address",
                    }
                }
            ],
        )
    )
    with pytest.raises(HueError) as raised:
        await bridge.create_schedule({})
    assert CONFIG.app_key not in str(raised.value)
    assert "/api/<app-key>/groups/9/action" in str(raised.value)


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(401, json={"errors": [{"description": "Unauthorized"}], "data": []}),
        httpx2.Response(403, json={"errors": [{"description": "Forbidden"}], "data": []}),
        httpx2.Response(200, json=[{"error": {"type": 1, "description": "unauthorized user"}}]),
    ],
)
async def test_revoked_app_key_asks_to_pair_again(response):
    bridge, _ = bridge_answering(response)
    call = bridge.get_schedules() if response.status_code == 200 else bridge.get_resources()
    with pytest.raises(HueError, match="run `hue-mcp setup` again"):
        await call


@pytest.mark.usefixtures("isolated_config", "bridge_moved_to_new_ip")
@pytest.mark.parametrize(
    "failure",
    [httpx2.ConnectError("no route to host"), httpx2.ConnectTimeout("timed out")],
)
async def test_bridge_that_moved_ip_is_followed(failure):
    bridge = HueBridge(CONFIG, transport=httpx2.MockTransport(unreachable_at_old_ip(failure)))
    assert await bridge.get_resources() == []
    assert bridge.config.ip == NEW_IP
    assert load_config().ip == NEW_IP


@pytest.mark.usefixtures("isolated_config", "bridge_moved_to_new_ip")
async def test_concurrent_requests_all_follow_a_moved_bridge():
    failure = httpx2.ConnectError("no route to host")
    bridge = HueBridge(CONFIG, transport=httpx2.MockTransport(unreachable_at_old_ip(failure)))
    results = []

    async def fetch() -> None:
        results.append(await bridge.get_resources())

    async with anyio.create_task_group() as requests:
        for _ in range(3):
            requests.start_soon(fetch)
    assert results == [[], [], []]
    assert bridge.config.ip == NEW_IP


@pytest.mark.usefixtures("isolated_config", "bridge_moved_to_new_ip")
async def test_a_new_address_that_fails_is_neither_adopted_nor_saved():
    def handle(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("certificate verify failed")

    bridge = HueBridge(CONFIG, transport=httpx2.MockTransport(handle))
    # The error names the address that was tried last: the new one, which failed TLS.
    with pytest.raises(HueError, match=re.escape(f"Can't reach the Hue Bridge at {NEW_IP}")):
        await bridge.get_resources()
    assert bridge.config == CONFIG
    assert load_config() is None


async def test_unreachable_bridge_that_did_not_move_is_reported(monkeypatch):
    monkeypatch.setattr(
        discovery, "discover_lan_bridges", lambda: [DiscoveredBridge(CONFIG.bridge_id, CONFIG.ip)]
    )
    attempts = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        attempts.append(request)
        raise httpx2.ConnectError("certificate verify failed")

    bridge = HueBridge(CONFIG, transport=httpx2.MockTransport(handle))
    with pytest.raises(HueError, match=re.escape(f"Can't reach the Hue Bridge at {CONFIG.ip}")):
        await bridge.get_resources()
    assert len(attempts) == 1  # Found at the same address: no pointless second try.


async def test_a_slow_answer_is_not_mistaken_for_a_moved_bridge(monkeypatch):
    def no_search():
        raise AssertionError("searched for a moved bridge")

    monkeypatch.setattr(discovery, "discover_lan_bridges", no_search)

    def handle(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("")

    bridge = HueBridge(CONFIG, transport=httpx2.MockTransport(handle))
    with pytest.raises(HueError, match="didn't answer in time; the command may still"):
        await bridge.update("light", "x", {"on": {"on": False}})


async def test_errors_without_a_message_still_say_what_happened():
    def handle(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.RemoteProtocolError("")

    bridge = HueBridge(CONFIG, transport=httpx2.MockTransport(handle))
    with pytest.raises(HueError, match=f"{re.escape(CONFIG.ip)}: RemoteProtocolError"):
        await bridge.get_resources()


@pytest.mark.usefixtures("isolated_config")
async def test_concurrent_calls_share_one_search_for_a_moved_bridge(monkeypatch):
    searches = []

    def discover():
        searches.append(1)
        return [DiscoveredBridge(CONFIG.bridge_id, NEW_IP)]

    monkeypatch.setattr(discovery, "discover_lan_bridges", discover)

    async def handle(request: httpx2.Request) -> httpx2.Response:
        await anyio.sleep(0.01)  # Lets the other calls run in between.
        if request.url.host == CONFIG.ip:
            raise httpx2.ConnectError("no route to host")
        return httpx2.Response(200, json={"errors": [], "data": []})

    bridge = HueBridge(CONFIG, transport=httpx2.MockTransport(handle))
    results = []

    async def fetch() -> None:
        results.append(await bridge.get_resources())

    async with anyio.create_task_group() as calls:
        for _ in range(3):
            calls.start_soon(fetch)
    assert results == [[], [], []]
    assert searches == [1]


@pytest.mark.usefixtures("bridge_moved_to_new_ip")
async def test_a_command_that_reached_a_moved_bridge_succeeds_even_if_saving_fails(
    monkeypatch, caplog
):
    def failing_save(config):
        raise OSError("read-only file system")

    monkeypatch.setattr(bridge_module, "save_config", failing_save)
    failure = httpx2.ConnectError("no route to host")
    bridge = HueBridge(CONFIG, transport=httpx2.MockTransport(unreachable_at_old_ip(failure)))
    assert await bridge.get_resources() == []
    assert bridge.config.ip == NEW_IP
    assert "read-only file system" in caplog.text


async def test_a_write_timeout_may_still_have_taken_effect():
    def handle(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.WriteTimeout("")

    bridge = HueBridge(CONFIG, transport=httpx2.MockTransport(handle))
    with pytest.raises(HueError, match="may still have taken effect"):
        await bridge.update("light", "x", {"on": {"on": True}})


async def test_a_connect_timeout_means_the_command_never_arrived(monkeypatch):
    monkeypatch.setattr(
        discovery, "discover_lan_bridges", lambda: [DiscoveredBridge(CONFIG.bridge_id, CONFIG.ip)]
    )

    def handle(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectTimeout("timed out")

    bridge = HueBridge(CONFIG, transport=httpx2.MockTransport(handle))
    with pytest.raises(HueError) as raised:
        await bridge.update("light", "x", {"on": {"on": True}})
    assert "Can't reach the Hue Bridge" in str(raised.value)
    assert "may still have taken effect" not in str(raised.value)


async def test_deleting_a_timer_that_already_fired_is_not_an_error():
    gone = {"type": 3, "address": "/schedules/7", "description": "resource not available"}
    bridge, _ = bridge_answering(httpx2.Response(200, json=[{"error": gone}]))
    await bridge.delete_schedule("7")


async def test_other_delete_errors_still_count():
    refused = {"type": 7, "address": "/schedules/7", "description": "invalid value"}
    bridge, _ = bridge_answering(httpx2.Response(200, json=[{"error": refused}]))
    with pytest.raises(HueError, match="invalid value"):
        await bridge.delete_schedule("7")
