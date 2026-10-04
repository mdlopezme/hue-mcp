import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp import Client

from hue_mcp.bridge import HueBridge
from hue_mcp.config import BridgeConfig
from hue_mcp.server import build_server

APP_KEY = "test-app-key"
CONFIG = BridgeConfig(bridge_id="001788fffe123456", ip="192.168.1.50", app_key=APP_KEY)


class FakeBridge:
    """Answers like a Hue Bridge from the fixture, and records every request."""

    def __init__(self, resources: list[dict[str, Any]]):
        self.resources = resources
        self.schedules: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, Any]] = []
        self.write_responses: dict[str, httpx2.Response] = {}  # Overrides, by request path.
        self.next_schedule_id = 7

    @property
    def writes(self) -> list[tuple[str, str, Any]]:
        return [request for request in self.requests if request[0] != "GET"]

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        self.requests.append((request.method, path, body))
        if request.method != "GET" and path in self.write_responses:
            return self.write_responses[path]
        if path == "/clip/v2/resource":
            return httpx2.Response(200, json={"errors": [], "data": self.resources})
        if path.startswith("/clip/v2/resource/"):
            rtype, _, rid = path.removeprefix("/clip/v2/resource/").partition("/")
            if request.method == "POST":
                rid = f"new-{rtype}-{len(self.resources)}"
                self.resources.append({**body, "id": rid, "id_v1": f"/{rtype}s/V1{rid}"})
            created = [{"rid": rid, "rtype": rtype}]
            return httpx2.Response(200, json={"errors": [], "data": created})
        if path == f"/api/{APP_KEY}/schedules" and request.method == "GET":
            return httpx2.Response(200, json=self.schedules)
        if path == f"/api/{APP_KEY}/schedules" and request.method == "POST":
            while str(self.next_schedule_id) in self.schedules:  # Like the bridge: ids in use
                self.next_schedule_id += 1  # are never handed out again.
            schedule_id = str(self.next_schedule_id)
            self.next_schedule_id += 1
            started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
            self.schedules[schedule_id] = {**body, "status": "enabled", "starttime": started}
            return httpx2.Response(200, json=[{"success": {"id": schedule_id}}])
        if path.startswith(f"/api/{APP_KEY}/schedules/") and request.method == "DELETE":
            self.schedules.pop(path.rsplit("/", 1)[1], None)
            return httpx2.Response(200, json=[{"success": f"{path} deleted"}])
        raise AssertionError(f"unexpected request: {request.method} {path}")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def resources() -> list[dict[str, Any]]:
    return json.loads((Path(__file__).parent / "fixtures" / "resource.json").read_text())


@pytest.fixture
def fake_bridge(resources: list[dict[str, Any]]) -> FakeBridge:
    return FakeBridge(resources)


@pytest.fixture
async def client(fake_bridge: FakeBridge):
    bridge = HueBridge(CONFIG, transport=httpx2.MockTransport(fake_bridge.handle))
    async with Client(build_server(lambda: bridge)) as client:
        yield client


async def call(client: Client, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, result.content[0].text
    return result.structured_content


async def call_failing(client: Client, tool: str, **arguments: Any) -> str:
    result = await client.call_tool(tool, arguments)
    assert result.is_error
    return result.content[0].text


def resource(resources: list[dict[str, Any]], resource_id: str) -> dict[str, Any]:
    return next(r for r in resources if r["id"] == resource_id)


def our_timer(minutes_ago: float = 10, **changes: Any) -> dict[str, Any]:
    started = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return {
        "name": "hue-mcp",
        "description": "turn Bedroom off",
        "command": {"address": f"/api/{APP_KEY}/groups/2/action", "method": "PUT"},
        "localtime": "PT00:30:00",
        "starttime": started.strftime("%Y-%m-%dT%H:%M:%S"),
        "status": "enabled",
        **changes,
    }
