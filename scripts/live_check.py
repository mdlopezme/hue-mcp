"""Check every tool against the real, paired bridge using one light, then restore that light.

Run it before releasing a change that touches what is sent to the bridge:

    .venv/bin/python scripts/live_check.py --light "Desk"

The light turns on, changes color, plays effects and is switched by timers (about three
minutes in all). Room-level commands are checked through a temporary zone holding only this
light. The light's original state is restored and the zone removed at the end, even when a
check fails.
"""

import argparse
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
from anyio import to_thread
from mcp import Client, StdioServerParameters
from mcp.types import TextContent

from hue_mcp import power
from hue_mcp.bridge import HueBridge
from hue_mcp.color import clamp_to_gamut, hex_to_xy
from hue_mcp.config import load_config
from hue_mcp.discovery import discover_lan_bridges
from hue_mcp.home import Home, Light

SCENE_NAME = "hue-mcp live check"
ZONE_NAME = "hue-mcp live check"
# The bridge reports a light's state as the light catches up, so reads can be mid-change.
SETTLE_S = 6


class CheckFailed(Exception):
    pass


async def main(light_name: str) -> int:
    config = load_config()
    if config is None:
        print("Not paired; run `hue-mcp setup` first.")
        return 2
    bridge = HueBridge(config)
    light = Home(await bridge.get_resources()).find_target(light_name, "light")
    if not isinstance(light, Light):
        raise TypeError(f"{light_name} is not a light")
    original = light.resource
    failures: list[str] = []
    created_timers: list[str] = []

    # The server installed next to this Python, i.e. the code under test, reading the same
    # pairing as this script.
    command = str(Path(sys.executable).parent / "hue-mcp")
    config_home = os.environ.get("XDG_CONFIG_HOME")
    env = {"XDG_CONFIG_HOME": config_home} if config_home else None
    async with Client(StdioServerParameters(command=command, env=env)) as client:

        async def tool(tool_name: str, /, **arguments: Any) -> Any:
            result = await client.call_tool(tool_name, arguments)
            if result.is_error:
                message = " ".join(c.text for c in result.content if isinstance(c, TextContent))
                raise CheckFailed(f"{tool_name} {arguments} failed: {message}")
            return result.structured_content

        async def state() -> dict[str, Any]:
            resources = await bridge.get_resources()
            return next(r for r in resources if r["id"] == light.id)

        async def settled(condition: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
            """The light's state once `condition` holds, or as it is after SETTLE_S."""
            deadline = anyio.current_time() + SETTLE_S
            while True:
                now = await state()
                if condition(now) or anyio.current_time() > deadline:
                    return now
                await anyio.sleep(0.5)

        async def restore() -> str:
            """Put the light back, checking it took: a light can miss a single command."""
            for _ in range(2):
                await bridge.update("light", light.id, _restoring(original))
                if _looks_like(await settled(lambda s: _looks_like(s, original)), original):
                    return f"Restored {light.name}."
            return f"Couldn't restore {light.name}; it was: {_restoring(original)}"

        async def check(description: str, steps: Callable[[], Awaitable[None]]) -> None:
            try:
                await steps()
                print(f"PASS  {description}")
            except CheckFailed as failure:
                failures.append(description)
                print(f"FAIL  {description}: {failure}")

        def expect(condition: bool, message: str) -> None:
            if not condition:
                raise CheckFailed(message)

        async def discovery() -> None:
            found = await to_thread.run_sync(discover_lan_bridges)
            expect(any(b.bridge_id == config.bridge_id for b in found), f"mDNS found {found}")

        async def listing() -> None:
            home = await tool("get_home")
            rooms = [group for group in home["rooms_and_zones"] if group["type"] == "room"]
            described = [lt for room in rooms for lt in room["lights"]]
            described += home.get("lights_not_in_a_room", [])
            names = [lt["name"] for lt in described]
            expect(light.name in names, f"{light.name} not in {names}")

        async def power_budget() -> None:
            watts = round(power.watts_at(light, 50), 2)
            result = await tool("set_power", target=light.label, watts=watts)
            expect(abs(result["brightness"] - 50) < 1, f"set_power chose {result['brightness']}%")
            now = await settled(
                lambda s: s["on"]["on"] and abs(s["dimming"]["brightness"] - 50) < 2
            )
            expect(now["on"]["on"], "light is off")
            expect(abs(now["dimming"]["brightness"] - 50) < 2, f"brightness {now['dimming']}")

        async def white_tone() -> None:
            await tool(
                "set_lights", target=light.label, brightness=40, color_temperature_kelvin=2700
            )
            now = await settled(
                lambda s: (
                    abs(s["dimming"]["brightness"] - 40) < 1
                    and s["color_temperature"]["mirek"] == 370
                )
            )
            expect(now["on"]["on"], "light is off")
            expect(abs(now["dimming"]["brightness"] - 40) < 1, f"brightness {now['dimming']}")
            expect(now["color_temperature"]["mirek"] == 370, f"ct {now['color_temperature']}")

        async def color() -> None:
            await tool("set_lights", target=light.label, color_hex="#ff8800")
            x, y = clamp_to_gamut(hex_to_xy("#ff8800"), light.gamut)

            def shows_color(s: dict[str, Any]) -> bool:
                shown_x: float = s["color"]["xy"]["x"]
                shown_y: float = s["color"]["xy"]["y"]
                return abs(shown_x - x) < 0.01 and abs(shown_y - y) < 0.01

            now = await settled(shows_color)
            expect(shows_color(now), f"xy {now['color']['xy']}")

        async def looping_effect() -> None:
            await tool("set_effect", target=light.label, effect="candle")
            effect = _effect_now(await settled(lambda s: _effect_now(s) == "candle"))
            expect(effect == "candle", f"effect {effect}")
            await tool("set_effect", target=light.label, effect="none")
            effect = _effect_now(await settled(lambda s: _effect_now(s) == "no_effect"))
            expect(effect == "no_effect", f"effect after none: {effect}")

        async def sunrise() -> None:
            await tool("set_lights", target=light.label, on=False)
            await anyio.sleep(1.5)
            await tool("set_effect", target=light.label, effect="sunrise", duration_minutes=1)
            now = await settled(lambda s: s["timed_effects"]["status"] == "sunrise")
            status = now["timed_effects"]["status"]
            expect(status == "sunrise", f"timed effect {status}")
            await tool("set_effect", target=light.label, effect="none")

        async def fade_off() -> None:
            await tool("set_lights", target=light.label, brightness=60)
            await anyio.sleep(2)
            await tool("set_lights", target=light.label, on=False, transition_seconds=3)
            await anyio.sleep(6)
            expect(not (await state())["on"]["on"], "still on after the fade (bridge quirk?)")

        async def scenes() -> None:
            room = light.room
            if room is None:
                raise CheckFailed(f"{light.name} is in no room")
            await tool("set_lights", target=light.label, brightness=30, color_hex="#ff8800")
            await anyio.sleep(1.5)
            await tool("create_scene", name=SCENE_NAME, room=room)
            try:
                scenes = (await tool("get_home", room=room))["rooms_and_zones"][0]["scenes"]
                expect(SCENE_NAME in scenes, f"scene missing from {scenes}")
                await tool("set_lights", target=light.label, on=False)
                await settled(lambda s: not s["on"]["on"])
                await tool("activate_scene", scene=SCENE_NAME, room=room)
                now = await settled(lambda s: s["on"]["on"])
                expect(now["on"]["on"], "scene recall left the light off")
            finally:
                for scene in Home(await bridge.get_resources()).scenes:
                    if scene.name == SCENE_NAME:
                        await bridge.delete("scene", scene.id)

        async def group_commands() -> None:
            await tool(
                "set_lights",
                target=ZONE_NAME,
                target_type="zone",
                brightness=35,
                color_temperature_kelvin=3000,
            )
            now = await settled(
                lambda s: (
                    s["on"]["on"]
                    and abs(s["dimming"]["brightness"] - 35) < 1
                    and s["color_temperature"]["mirek"] == 333
                )
            )
            expect(now["on"]["on"], "light is off")
            expect(abs(now["dimming"]["brightness"] - 35) < 1, f"brightness {now['dimming']}")
            expect(now["color_temperature"]["mirek"] == 333, f"ct {now['color_temperature']}")

        async def pending_timers() -> list[str]:
            return [t["timer_id"] for t in (await tool("list_timers"))["timers"]]

        async def timers() -> None:
            await tool("set_lights", target=light.label, on=True)
            await anyio.sleep(1.5)
            cancelled = await tool("set_timer", target=light.label, minutes=5)
            created_timers.append(cancelled["timer_id"])
            expect(cancelled["timer_id"] in await pending_timers(), "timer not listed")
            await tool("cancel_timer", timer_id=cancelled["timer_id"])
            # One timer per path: the light's turns it off, then the zone's turns it back on.
            light_off = await tool("set_timer", target=light.label, minutes=1)
            zone_on = await tool(
                "set_timer", target=ZONE_NAME, target_type="zone", minutes=1.5, action="on"
            )
            created_timers.extend([light_off["timer_id"], zone_on["timer_id"]])
            # Each timer is checked twice: did the bridge fire it, and did the light obey?
            await anyio.sleep(65)
            fired = light_off["timer_id"] not in await pending_timers()
            expect(fired, "the bridge didn't fire the light's timer")
            missed = "the bridge fired the {} timer, but the light missed its command"
            expect(not (await state())["on"]["on"], missed.format("light's"))
            await anyio.sleep(30)
            fired = zone_on["timer_id"] not in await pending_timers()
            expect(fired, "the bridge didn't fire the zone's timer")
            expect((await state())["on"]["on"], missed.format("zone's"))

        zone = {
            "type": "zone",
            "metadata": {"name": ZONE_NAME, "archetype": "other"},
            "children": [{"rid": light.id, "rtype": "light"}],
        }
        zone_id = await bridge.create("zone", zone)
        try:
            await check("mDNS discovery finds the paired bridge", discovery)
            await check("get_home lists the light", listing)
            await check("brightness and white tone", white_tone)
            await check("power budget", power_budget)
            if light.supports_color:
                await check("color", color)
            if "candle" in light.effects:
                await check("candle effect, then none", looping_effect)
            if "sunrise" in light.timed_effects:
                await check("sunrise, then none", sunrise)
            await check("fade off", fade_off)
            await check("room-level commands (through a zone)", group_commands)
            await check("create, list, recall and delete a scene", scenes)
            await check("timers: set, list, cancel, and fire on a light and on a zone", timers)
        finally:
            pending = await pending_timers()
            for timer_id in set(created_timers) & set(pending):
                await tool("cancel_timer", timer_id=timer_id)
            await bridge.delete("zone", zone_id)
            print(f"Removed the test zone. {await restore()}")

    print(f"\n{'All checks passed.' if not failures else f'FAILED: {failures}'}")
    return 1 if failures else 0


def _effect_now(light: dict[str, Any]) -> str | None:
    """The playing effect, from whichever of the two effect APIs the light has."""
    newer = light.get("effects_v2", {}).get("status", {}).get("effect")
    older = light.get("effects", {}).get("status")
    effect: str | None = newer or older
    return effect


def _looks_like(now: dict[str, Any], original: dict[str, Any]) -> bool:
    if now["on"]["on"] != original["on"]["on"]:
        return False
    if (
        "dimming" in original
        and abs(now["dimming"]["brightness"] - original["dimming"]["brightness"]) > 1
    ):
        return False
    color_temperature = original.get("color_temperature")
    if color_temperature and color_temperature["mirek_valid"]:
        return bool(now["color_temperature"]["mirek"] == color_temperature["mirek"])
    if "color" in original:
        x, y = original["color"]["xy"]["x"], original["color"]["xy"]["y"]
        shown = now["color"]["xy"]
        return bool(abs(shown["x"] - x) < 0.01 and abs(shown["y"] - y) < 0.01)
    return True


def _restoring(original: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"on": {"on": original["on"]["on"]}}
    if "dimming" in original:
        body["dimming"] = {"brightness": original["dimming"]["brightness"]}
    color_temperature = original.get("color_temperature")
    if color_temperature and color_temperature["mirek_valid"]:
        body["color_temperature"] = {"mirek": color_temperature["mirek"]}
    elif "color" in original:
        body["color"] = {"xy": original["color"]["xy"]}
    return body


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--light", required=True, help="name of the light to test with")
    sys.exit(anyio.run(main, parser.parse_args().light))
