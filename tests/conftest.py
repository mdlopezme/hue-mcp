import json
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
            return httpx2.Response(
                200, json={"errors": [], "data": [{"rid": rid or f"new-{rtype}", "rtype": rtype}]}
            )
        if path == f"/api/{APP_KEY}/schedules" and request.method == "GET":
            return httpx2.Response(200, json=self.schedules)
        if path == f"/api/{APP_KEY}/schedules" and request.method == "POST":
            return httpx2.Response(200, json=[{"success": {"id": "7"}}])
        if path.startswith(f"/api/{APP_KEY}/schedules/") and request.method == "DELETE":
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
