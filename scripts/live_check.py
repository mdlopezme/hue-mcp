"""Check every tool against the real, paired bridge, then put the lights back as they were.

Run it before releasing a change that touches what is sent to the bridge:

    .venv/bin/python scripts/live_check.py --light "Desk"
    .venv/bin/python scripts/live_check.py --all

The lights turn on, change color, play effects, are switched by timers and run a short
pomodoro (about six minutes in all). Room-level commands go through a temporary zone holding
just these lights. The lights are restored and the zone removed at the end, even when a check
fails.
"""

import argparse
import os
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

import anyio
from anyio import to_thread
from mcp import Client, StdioServerParameters
from mcp.types import TextContent

from hue_mcp import pomodoro, power
from hue_mcp.bridge import HueBridge
from hue_mcp.color import clamp_to_gamut, hex_to_xy
from hue_mcp.config import load_config
from hue_mcp.discovery import discover_lan_bridges
from hue_mcp.home import Home, Light
from hue_mcp.server import MAX_ACTIVE_TIMERS

SCENE_NAME = "hue-mcp live check"
ZONE_NAME = "hue-mcp live check"
# The bridge reports a light's state as the light catches up, so reads can be mid-change.
SETTLE_S = 6
# Hue bulbs sometimes switch themselves back on right after an off, mostly a faded or group one.
TURNED_BACK_ON = "Hue bulbs sometimes switch back on after an off; rerun to confirm"

State = dict[str, Any]  # A light resource, as the bridge reports it.


class CheckFailed(Exception):
    pass


async def main(light_names: list[str] | None) -> int:
    config = load_config()
    if config is None:
        print("Not paired; run `hue-mcp setup` first.")
        return 2
    bridge = HueBridge(config)
    home = Home(await bridge.get_resources())
    lights = home.lights if light_names is None else [_light(home, n) for n in light_names]
    originals = {light.id: light.resource for light in lights}
    tunable = [light for light in lights if light.supports_color_temperature]
    colorful = [light for light in lights if light.supports_color]
    flickering = [light for light in lights if "candle" in light.effects]
    waking = [light for light in lights if "sunrise" in light.timed_effects]
    failures: list[str] = []
    created_timers: list[str] = []
    print(f"Checking with {', '.join(light.name for light in lights)}.")

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

        async def zone_tool(tool_name: str, /, **arguments: Any) -> Any:
            return await tool(tool_name, target=ZONE_NAME, target_type="zone", **arguments)

        async def states() -> dict[str, State]:
            resources = await bridge.get_resources()
            return {r["id"]: r for r in resources if r["id"] in originals}

        async def settled(
            condition: Callable[[State], bool], among: list[Light] | None = None
        ) -> dict[str, State]:
            """The lights' states once each meets `condition`, or as they are after SETTLE_S."""
            among = lights if among is None else among
            deadline = anyio.current_time() + SETTLE_S
            while True:
                now = await states()
                done = all(condition(now[light.id]) for light in among)
                if done or anyio.current_time() > deadline:
                    return now
                await anyio.sleep(0.5)

        async def expect_each(
            condition: Callable[[State], bool], failure: str, among: list[Light] | None = None
        ) -> None:
            """Wait for every light to meet `condition`, and name the ones that don't."""
            among = lights if among is None else among
            now = await settled(condition, among)
            if missed := [light.name for light in among if not condition(now[light.id])]:
                raise CheckFailed(f"{failure}: {', '.join(missed)}")

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
            listed = await tool("get_home")
            rooms = [group for group in listed["rooms_and_zones"] if group["type"] == "room"]
            described = [lt for room in rooms for lt in room["lights"]]
            described += listed.get("lights_not_in_a_room", [])
            names = {lt["name"] for lt in described}
            missing = [light.name for light in lights if light.name not in names]
            expect(not missing, f"get_home doesn't list {missing}")

        async def white_tone() -> None:
            for light in tunable:  # One command each: the single-light path.
                await tool(
                    "set_lights", target=light.label, brightness=40, color_temperature_kelvin=2700
                )
            await expect_each(
                lambda s: (
                    abs(s["dimming"]["brightness"] - 40) < 1
                    and s["color_temperature"]["mirek"] == 370
                ),
                "not at 40% and 2700 K",
                tunable,
            )

        async def power_budget() -> None:
            watts = round(sum(power.watts_at(light, 50) for light in lights), 2)
            result = await zone_tool("set_power", watts=watts)
            expect(abs(result["brightness"] - 50) < 1, f"set_power chose {result['brightness']}%")
            await expect_each(
                lambda s: s["on"]["on"] and abs(s["dimming"]["brightness"] - 50) < 2,
                "not on at 50%",
            )

        async def color() -> None:
            wanted = {
                light.id: clamp_to_gamut(hex_to_xy("#ff8800"), light.gamut) for light in colorful
            }
            for light in colorful:
                await tool("set_lights", target=light.label, color_hex="#ff8800")
            await expect_each(
                lambda s: _shows(s, wanted[s["id"]]), "not showing #ff8800", colorful
            )

        async def looping_effect() -> None:
            await zone_tool("set_effect", effect="candle")
            await expect_each(lambda s: _effect_now(s) == "candle", "no candle", flickering)
            await zone_tool("set_effect", effect="none")
            await expect_each(
                lambda s: _effect_now(s) == "no_effect", "still flickering", flickering
            )

        async def sunrise() -> None:
            await zone_tool("set_lights", on=False)
            await expect_each(lambda s: not s["on"]["on"], "still on")
            await zone_tool("set_effect", effect="sunrise", duration_minutes=1)
            await expect_each(
                lambda s: s["timed_effects"]["status"] == "sunrise", "no sunrise", waking
            )
            await zone_tool("set_effect", effect="none")

        async def fade_off() -> None:
            await zone_tool("set_lights", brightness=60)
            await anyio.sleep(2)
            await zone_tool("set_lights", on=False, transition_seconds=3)
            await anyio.sleep(6)  # Long enough to catch a light that turns itself back on.
            now = await states()
            still_on = [light.name for light in lights if now[light.id]["on"]["on"]]
            names = ", ".join(still_on)
            expect(not still_on, f"still on after the fade: {names} ({TURNED_BACK_ON})")

        async def group_commands() -> None:
            await zone_tool("set_lights", brightness=35, color_temperature_kelvin=3000)
            await expect_each(
                lambda s: (
                    s["on"]["on"]
                    and abs(s["dimming"]["brightness"] - 35) < 1
                    and s["color_temperature"]["mirek"] == 333
                ),
                "not on at 35% and 3000 K",
                tunable,
            )

        async def scenes() -> None:
            rooms = sorted({light.room for light in lights if light.room})
            if not rooms:
                raise CheckFailed("none of these lights is in a room")
            in_rooms = [light for light in lights if light.room]
            await zone_tool("set_lights", brightness=30)
            await expect_each(lambda s: abs(s["dimming"]["brightness"] - 30) < 1, "not at 30%")
            try:
                for room in rooms:
                    await tool("create_scene", name=SCENE_NAME, room=room)
                    listed = (await tool("get_home", room=room))["rooms_and_zones"][0]["scenes"]
                    expect(SCENE_NAME in listed, f"scene missing from {room}: {listed}")
                await zone_tool("set_lights", on=False)
                await expect_each(lambda s: not s["on"]["on"], "still on")
                for room in rooms:
                    await tool("activate_scene", scene=SCENE_NAME, room=room)
                await expect_each(lambda s: s["on"]["on"], "left off by the scene", in_rooms)
            finally:
                for scene in Home(await bridge.get_resources()).scenes:
                    if scene.name == SCENE_NAME:
                        await bridge.delete("scene", scene.id)

        async def pending_timers() -> list[str]:
            return [t["timer_id"] for t in (await tool("list_timers"))["timers"]]

        async def timers() -> None:
            await zone_tool("set_lights", on=True)
            await expect_each(lambda s: s["on"]["on"], "still off")
            cancelled = await tool("set_timer", target=lights[0].label, minutes=5)
            created_timers.append(cancelled["timer_id"])
            expect(cancelled["timer_id"] in await pending_timers(), "timer not listed")
            await tool("cancel_timer", timer_id=cancelled["timer_id"])
            # Each light's own timer turns it off, then the zone's turns them all back on. The
            # bridge's timer slots are limited, so only the first few lights get their own.
            timed = lights[: MAX_ACTIVE_TIMERS - 2]
            light_timers = {}
            for light in timed:
                timer = await tool("set_timer", target=light.label, minutes=1)
                light_timers[light.id] = timer["timer_id"]
            zone_on = await zone_tool("set_timer", minutes=1.5, action="on")
            created_timers.extend([*light_timers.values(), zone_on["timer_id"]])
            # Each timer is checked twice: did the bridge fire it, and did the light obey?
            await anyio.sleep(65)
            pending = await pending_timers()
            unfired = [light.name for light in timed if light_timers[light.id] in pending]
            expect(not unfired, f"the bridge didn't fire the timers of {', '.join(unfired)}")
            now = await states()
            missed = [light.name for light in timed if now[light.id]["on"]["on"]]
            expect(not missed, f"timers fired, but these stayed on: {missed} ({TURNED_BACK_ON})")
            await anyio.sleep(30)
            expect(zone_on["timer_id"] not in await pending_timers(), "zone timer not fired")
            now = await states()
            missed = [light.name for light in lights if not now[light.id]["on"]["on"]]
            expect(not missed, f"zone timer fired, but these lights missed the command: {missed}")

        async def pomodoro_rounds() -> None:
            # One-minute rounds: focus, a break at 1:00, focus again at 2:00, then stop.
            await zone_tool("set_lights", brightness=40, color_temperature_kelvin=3000)

            def focus_look(s: State) -> bool:
                return bool(
                    abs(s["dimming"]["brightness"] - 40) < 1
                    and s["color_temperature"]["mirek"] == 333
                )

            await expect_each(focus_look, "focus look not set", tunable)
            await tool(
                "start_pomodoro", room=ZONE_NAME, focus_minutes=1, break_minutes=1, cycles=2
            )
            x, y = pomodoro.break_look()["xy"]
            await anyio.sleep(62)
            await expect_each(lambda s: _shows(s, (x, y)), "not green for the break", colorful)
            await anyio.sleep(60)
            await expect_each(focus_look, "focus look not back after the break", tunable)
            await tool("stop_pomodoro")

        async def restore() -> str:
            """Put the lights back, checking it took: a light can miss a single command."""
            unrestored = list(lights)
            for _ in range(2):
                for light in unrestored:
                    await bridge.update("light", light.id, _restoring(originals[light.id]))
                now = await settled(lambda s: _looks_like(s, originals[s["id"]]), unrestored)
                unrestored = [
                    light
                    for light in unrestored
                    if not _looks_like(now[light.id], originals[light.id])
                ]
                if not unrestored:
                    return "Restored the lights."
            return f"Couldn't restore {', '.join(light.name for light in unrestored)}."

        zone = {
            "type": "zone",
            "metadata": {"name": ZONE_NAME, "archetype": "other"},
            "children": [{"rid": light.id, "rtype": "light"} for light in lights],
        }
        zone_id = await bridge.create("zone", zone)
        try:
            await check("mDNS discovery finds the paired bridge", discovery)
            await check("get_home lists the lights", listing)
            await check("brightness and white tone, light by light", white_tone)
            await check("power budget", power_budget)
            if colorful:
                await check("color, light by light", color)
            if flickering:
                await check("candle effect, then none", looping_effect)
            if waking:
                await check("sunrise, then none", sunrise)
            await check("fade off", fade_off)
            await check("room-level white tone (through a zone)", group_commands)
            await check("create, list, recall and delete a scene", scenes)
            await check("timers: set, list, cancel, and fire on lights and on a zone", timers)
            await check("pomodoro: focus, break, focus again, stop", pomodoro_rounds)
        finally:
            with suppress(CheckFailed):  # Ends a pomodoro a failed check left running.
                await tool("stop_pomodoro")
            pending = await pending_timers()
            for timer_id in set(created_timers) & set(pending):
                await tool("cancel_timer", timer_id=timer_id)
            await bridge.delete("zone", zone_id)
            print(f"Removed the test zone. {await restore()}")

    print(f"\n{'All checks passed.' if not failures else f'FAILED: {failures}'}")
    return 1 if failures else 0


def _light(home: Home, name: str) -> Light:
    light = home.find_target(name, "light")
    if not isinstance(light, Light):
        raise TypeError(f"{name} is not a light")
    return light


def _shows(state: State, xy: tuple[float, float]) -> bool:
    shown_x: float = state["color"]["xy"]["x"]
    shown_y: float = state["color"]["xy"]["y"]
    return abs(shown_x - xy[0]) < 0.01 and abs(shown_y - xy[1]) < 0.01


def _effect_now(light: State) -> str | None:
    """The playing effect, from whichever of the two effect APIs the light has."""
    newer = light.get("effects_v2", {}).get("status", {}).get("effect")
    older = light.get("effects", {}).get("status")
    effect: str | None = newer or older
    return effect


def _looks_like(now: State, original: State) -> bool:
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
        xy = original["color"]["xy"]
        return _shows(now, (xy["x"], xy["y"]))
    return True


def _restoring(original: State) -> State:
    body: State = {"on": {"on": original["on"]["on"]}}
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
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--light", action="append", help="a light to check with (repeatable)")
    which.add_argument("--all", action="store_true", help="check with every light")
    arguments = parser.parse_args()
    sys.exit(anyio.run(main, None if arguments.all else arguments.light))
