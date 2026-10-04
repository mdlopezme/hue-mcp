import logging
import ssl
from dataclasses import replace
from importlib.resources import files
from typing import Any

import anyio
import httpx2
from anyio import to_thread

from hue_mcp import discovery
from hue_mcp.config import BridgeConfig, save_config
from hue_mcp.errors import HueError

# The bridge answers 429 with more than 3 requests in flight, and 503 when it is overloaded.
MAX_CONNECTIONS = 3
RETRY_STATUS_CODES = {429, 503}
RETRY_DELAYS_S = (0.5, 1.0)

# httpx2 logs each request URL at INFO, and v1 URLs carry the app key.
logging.getLogger("httpx2").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

V1_UNAUTHORIZED_ERROR_TYPE = 1
V1_NOT_AVAILABLE_ERROR_TYPE = 3
RE_PAIR_HINT = "The bridge no longer accepts this app's key; run `hue-mcp setup` again."


def bridge_ssl_context(check_hostname: bool = True) -> ssl.SSLContext:
    """Trust only Signify's bridge root CAs. Certificates name the bridge id, not its IP."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = check_hostname
    context.load_verify_locations(cadata=(files("hue_mcp") / "hue_roots.pem").read_text())
    return context


class HueBridge:
    """Client for a paired bridge: the CLIP v2 API, plus v1 schedules (v2 has no timers)."""

    def __init__(self, config: BridgeConfig, transport: httpx2.AsyncBaseTransport | None = None):
        self.config = config
        self._client = httpx2.AsyncClient(
            verify=bridge_ssl_context(),
            transport=transport,
            timeout=10,
            limits=httpx2.Limits(max_connections=MAX_CONNECTIONS),
            trust_env=False,  # The bridge is on the LAN: proxy settings must not apply.
        )
        self._relocation_lock = anyio.Lock()

    async def get_resources(self) -> list[dict[str, Any]]:
        resources, _ = await self._v2("GET", "resource")
        return resources

    async def update(self, rtype: str, rid: str, body: dict[str, Any]) -> list[str]:
        """Returns the bridge's warnings, e.g. a light in the group that didn't respond."""
        _, warnings = await self._v2("PUT", f"resource/{rtype}/{rid}", body)
        return warnings

    async def create(self, rtype: str, body: dict[str, Any]) -> str:
        created, warnings = await self._v2("POST", f"resource/{rtype}", body)
        if not created:
            raise HueError("The bridge created nothing: " + ("; ".join(warnings) or "no reason"))
        rid: str = created[0]["rid"]
        return rid

    async def delete(self, rtype: str, rid: str) -> None:
        await self._v2("DELETE", f"resource/{rtype}/{rid}")

    async def get_schedules(self) -> dict[str, dict[str, Any]]:
        schedules: dict[str, dict[str, Any]] = await self._v1("GET", "schedules")
        return schedules

    async def create_schedule(self, schedule: dict[str, Any]) -> str:
        result = await self._v1("POST", "schedules", schedule)
        schedule_id: str = result[0]["success"]["id"]
        return schedule_id

    async def delete_schedule(self, schedule_id: str) -> None:
        """A timer that is already gone, e.g. it just fired, counts as deleted."""
        await self._v1("DELETE", f"schedules/{schedule_id}", gone_is_fine=True)

    async def _v2(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """The response data, plus warnings when the bridge only partly carried out a command.

        A 2xx answer can still list errors (HTTP 207), e.g. when one light of a group is
        unpowered; the command reached the others, so that is a warning, not a failure.
        """
        response = await self._send(method, f"/clip/v2/{path}", body)
        if response.status_code in (401, 403):
            raise HueError(RE_PAIR_HINT)
        try:
            payload = response.json()
        except ValueError:
            raise HueError(f"The bridge answered HTTP {response.status_code}.") from None
        problems = [self._scrub(error["description"]) for error in payload.get("errors", [])]
        if response.is_error:
            reason = "; ".join(problems) or f"HTTP {response.status_code}"
            raise HueError(f"The bridge refused: {reason}")
        data: list[dict[str, Any]] = payload.get("data", [])
        return data, problems

    async def _v1(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        gone_is_fine: bool = False,
    ) -> Any:
        response = await self._send(method, f"/api/{self.config.app_key}/{path}", body)
        try:
            payload = response.json()
        except ValueError:
            raise HueError(f"The bridge answered HTTP {response.status_code}.") from None
        errors = []
        if isinstance(payload, list):  # Writes answer with a list of successes and errors.
            errors = [item["error"] for item in payload if "error" in item]
        if gone_is_fine:
            errors = [e for e in errors if e.get("type") != V1_NOT_AVAILABLE_ERROR_TYPE]
        if any(error.get("type") == V1_UNAUTHORIZED_ERROR_TYPE for error in errors):
            raise HueError(RE_PAIR_HINT)
        if errors:
            reason = "; ".join(self._scrub(error["description"]) for error in errors)
            raise HueError(f"The bridge refused: {reason}")
        if response.is_error:
            raise HueError(f"The bridge answered HTTP {response.status_code}.")
        return payload

    async def _send(self, method: str, path: str, body: dict[str, Any] | None) -> httpx2.Response:
        attempted = self.config
        try:
            try:
                return await self._send_with_retries(attempted, method, path, body)
            except (httpx2.ConnectError, httpx2.ConnectTimeout):
                # Held until the new address answers, so concurrent calls reuse what one found.
                async with self._relocation_lock:
                    if self.config.ip == attempted.ip:
                        moved = await self._find_moved_bridge(attempted)
                        if moved is None:
                            raise
                        attempted = moved
                        response = await self._send_with_retries(moved, method, path, body)
                        # Adopted only now: an answer over TLS proves this is the bridge.
                        self.config = moved
                        self._save(moved)
                        return response
                attempted = self.config  # Another call already followed the bridge.
                return await self._send_with_retries(attempted, method, path, body)
        except (httpx2.ReadTimeout, httpx2.WriteTimeout) as error:
            raise HueError(
                f"The Hue Bridge at {attempted.ip} didn't answer in time; the command may still "
                "have taken effect."
            ) from error
        except httpx2.TransportError as error:
            reason = str(error) or type(error).__name__
            message = f"Can't reach the Hue Bridge at {attempted.ip}: {reason}"
            raise HueError(self._scrub(message)) from error

    async def _send_with_retries(
        self, config: BridgeConfig, method: str, path: str, body: dict[str, Any] | None
    ) -> httpx2.Response:
        response = await self._send_once(config, method, path, body)
        for delay_s in RETRY_DELAYS_S:
            if response.status_code not in RETRY_STATUS_CODES:
                break
            await anyio.sleep(delay_s)
            response = await self._send_once(config, method, path, body)
        return response

    async def _send_once(
        self, config: BridgeConfig, method: str, path: str, body: dict[str, Any] | None
    ) -> httpx2.Response:
        return await self._client.request(
            method,
            f"https://{config.ip}{path}",
            json=body,
            headers={"hue-application-key": config.app_key},
            extensions={"sni_hostname": config.bridge_id},
        )

    async def _find_moved_bridge(self, unreachable: BridgeConfig) -> BridgeConfig | None:
        """The bridge at the new IP DHCP gave it, or None if it hasn't moved."""
        bridges = await to_thread.run_sync(discovery.discover_lan_bridges)
        new_ip = next((b.ip for b in bridges if b.bridge_id == unreachable.bridge_id), None)
        if new_ip is None or new_ip == unreachable.ip:
            return None
        return replace(unreachable, ip=new_ip)

    def _save(self, config: BridgeConfig) -> None:
        try:
            save_config(config)
        except OSError as error:  # The command went through; failing it now would mislead.
            logger.warning("Couldn't save the bridge's new address %s: %s", config.ip, error)

    def _scrub(self, text: str) -> str:
        """Bridge messages can echo a request's address, and v1 addresses carry the app key."""
        return text.replace(self.config.app_key, "<app-key>")
