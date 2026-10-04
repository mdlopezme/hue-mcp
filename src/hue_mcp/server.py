"""The MCP tools Claude uses to control the lights."""

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from hue_mcp.bridge import HueBridge
from hue_mcp.color import (
    GAMUT_C,
    clamp_to_gamut,
    hex_to_xy,
    kelvin_to_mirek,
    mirek_to_kelvin,
    xy_to_hex,
)
from hue_mcp.errors import HueError
from hue_mcp.home import NO_EFFECT, Group, Home, Light

INSTRUCTIONS = """\
Controls the user's Philips Hue lights through their Hue Bridge.
Call get_home first to learn the names of rooms, zones, lights and scenes. When an error
lists several matches, repeat the call with one of them exactly as written (or its id).
Brightness is a percentage. For a gradual change ("fade off over 20 minutes") use
transition_seconds; for a delayed one ("turn off in 30 minutes") use set_timer."""

TIMER_SCHEDULE_NAME = "hue-mcp"  # Marks the bridge schedules this server created.
MAX_ACTIVE_TIMERS = 10  # The bridge has about 100 schedule slots, shared with other apps.
MAX_TRANSITION_S = 6000  # Signify's API caps transitions at 6,000,000 ms.
MAX_TIMED_EFFECT_MIN = 360  # And sunrise/sunset at 21,600,000 ms.
LIGHT_COMMAND_SPACING_S = 0.1  # The bridge handles about 10 light commands per second.

TargetType = Literal["light", "room", "zone"]
Effect = Literal[
    "candle",
    "fire",
    "prism",
    "sparkle",
    "opal",
    "glisten",
    "underwater",
    "cosmos",
    "sunbeam",
    "enchant",
    "sunrise",
    "sunset",
    "none",
]
TIMED_EFFECTS = ("sunrise", "sunset")

# What a light needs in its resource to take part in a change, and how to say it can't.
ABILITIES = {
    "dimming": "be dimmed",
    "color": "show colors",
    "color_temperature": "show white tones",
}

Target = Annotated[str, Field(description='A light, room or zone name, or "all" for every light.')]
TargetTypeArg = Annotated[
    TargetType | None, Field(description="Only needed when a light and a room share a name.")
]
TransitionSeconds = Annotated[
    float | None,
    Field(
        ge=0,
        le=MAX_TRANSITION_S,
        description=(
            "Fade to the new state over this many seconds (at most 100 minutes; "
            "set_effect sunset dims to off more slowly)."
        ),
    ),
]

READ_ONLY = ToolAnnotations(read_only_hint=True)


def build_server(get_bridge: Callable[[], HueBridge]) -> MCPServer:
    server = MCPServer("hue", instructions=INSTRUCTIONS)
    # Keeps concurrent set_timer calls from all passing the MAX_ACTIVE_TIMERS check.
    timer_lock = anyio.Lock()

    async def load_home() -> tuple[HueBridge, Home]:
        bridge = get_bridge()
        return bridge, Home(await bridge.get_resources())

    @server.tool(annotations=READ_ONLY)
    async def get_home(
        room: Annotated[str | None, Field(description="Only this room or zone.")] = None,
    ) -> dict[str, Any]:
        """Rooms and zones, with their lights' current state and their scenes."""
        _, home = await load_home()
        if room is not None:
            group = home.find_group(room)
            return {"rooms_and_zones": [_describe_group(home, group, light_states=True)]}
        result: dict[str, Any] = {
            "rooms_and_zones": [
                # A zone's lights also belong to rooms, which describe their state.
                _describe_group(home, group, light_states=group.kind == "room")
                for group in home.groups
            ]
        }
        if loose := [_describe_light(light) for light in home.lights if light.room is None]:
            result["lights_not_in_a_room"] = loose
        return result

    @server.tool()
    async def set_lights(
        target: Target,
        target_type: TargetTypeArg = None,
        on: bool | None = None,
        brightness: Annotated[
            float | None,
            Field(ge=1, le=100, description="Percent. Use on=false, not 0, to turn off."),
        ] = None,
        brightness_change: Annotated[
            float | None,
            Field(
                ge=-100,
                le=100,
                description="Relative change in percentage points, for lights that are on.",
            ),
        ] = None,
        color_hex: Annotated[
            str | None,
            Field(description="A color like #ff8800. Convert color names to hex yourself."),
        ] = None,
        color_temperature_kelvin: Annotated[
            int | None,
            Field(
                ge=2000,
                le=6500,
                description="White tone: 2200 candle, 2700 warm, 4000 neutral, 6500 daylight.",
            ),
        ] = None,
        transition_seconds: TransitionSeconds = None,
    ) -> dict[str, Any]:
        """Turn lights on or off, or change their brightness, color or white tone.

        Setting brightness, a color or a white tone also turns the lights on.
        """
        if color_hex is not None and color_temperature_kelvin is not None:
            raise HueError("Pass color_hex or color_temperature_kelvin, not both.")
        if brightness is not None and brightness_change is not None:
            raise HueError("Pass brightness or brightness_change, not both.")
        bridge, home = await load_home()
        target_item = home.find_target(target, target_type)

        body: dict[str, Any] = {}
        applied: dict[str, Any] = {}
        notes: list[str] = []
        sets_look = any(
            value is not None for value in (brightness, color_hex, color_temperature_kelvin)
        )
        if on is not None or sets_look:
            applied["on"] = on if on is not None else True
            body["on"] = {"on": applied["on"]}
        if brightness is not None:
            notes += _check_ability(target_item, "dimming")
            applied["brightness"] = brightness
            body["dimming"] = {"brightness": brightness}
        if brightness_change:
            notes += _check_ability(target_item, "dimming")
            if on is None and not _is_on(target_item):
                raise HueError(
                    f"{target_item.label} is off. Set brightness instead, which turns it on."
                )
            applied["brightness_change"] = brightness_change
            body["dimming_delta"] = {
                "action": "up" if brightness_change > 0 else "down",
                "brightness_delta": abs(brightness_change),
            }
        if color_hex is not None:
            notes += _check_ability(target_item, "color")
            gamut = target_item.gamut if isinstance(target_item, Light) else GAMUT_C
            x, y = clamp_to_gamut(hex_to_xy(color_hex), gamut)
            applied["color_hex"] = xy_to_hex((x, y))
            body["color"] = {"xy": {"x": round(x, 4), "y": round(y, 4)}}
        if color_temperature_kelvin is not None:
            notes += _check_ability(target_item, "color_temperature")
            if isinstance(target_item, Light):
                mirek = kelvin_to_mirek(color_temperature_kelvin, *target_item.mirek_range)
            else:
                mirek = kelvin_to_mirek(color_temperature_kelvin)
                notes += _white_tone_limits(target_item, color_temperature_kelvin, mirek)
            applied["color_temperature_kelvin"] = mirek_to_kelvin(mirek)
            body["color_temperature"] = {"mirek": mirek}
        if not body:
            raise HueError(
                "Nothing to change: pass on, brightness, brightness_change, color_hex or "
                "color_temperature_kelvin."
            )
        if transition_seconds is not None:
            applied["transition_seconds"] = transition_seconds
            body["dynamics"] = {"duration": round(transition_seconds * 1000)}

        if isinstance(target_item, Light):
            warnings = await bridge.update("light", target_item.id, body)
        else:
            warnings = await bridge.update("grouped_light", target_item.grouped_light["id"], body)
        return _with_warnings({"target": target_item.label, "applied": applied}, notes + warnings)

    @server.tool()
    async def activate_scene(
        scene: str,
        room: Annotated[
            str | None, Field(description="The scene's room or zone, when names repeat.")
        ] = None,
        transition_seconds: TransitionSeconds = None,
        dynamic: Annotated[
            bool, Field(description="Slowly cycle through the scene's colors.")
        ] = False,
    ) -> dict[str, Any]:
        """Activate a saved scene."""
        bridge, home = await load_home()
        found = home.find_scene(scene, home.find_group(room) if room else None)
        recall: dict[str, Any] = {"action": "dynamic_palette" if dynamic else "active"}
        if transition_seconds is not None:
            recall["duration"] = round(transition_seconds * 1000)
        warnings = await bridge.update("scene", found.id, {"recall": recall})
        return _with_warnings({"activated": found.name, "room": found.group.name}, warnings)

    @server.tool()
    async def create_scene(
        name: Annotated[str, Field(min_length=1, max_length=32)],
        room: Annotated[str, Field(description="The room or zone to save.")],
    ) -> dict[str, Any]:
        """Save the current look of a room or zone's lights as a new scene."""
        name = name.strip()
        if not name:
            raise HueError("The scene needs a name.")
        bridge, home = await load_home()
        group = home.find_group(room)
        if any(s.group is group and s.name.casefold() == name.casefold() for s in home.scenes):
            raise HueError(f"{group.name} already has a scene named {name!r}.")
        actions = [
            {"target": {"rid": light.id, "rtype": "light"}, "action": _current_look(light)}
            for light in group.lights
        ]
        await bridge.create(
            "scene",
            {
                "type": "scene",
                "metadata": {"name": name},
                "group": {"rid": group.id, "rtype": group.kind},
                "actions": actions,
            },
        )
        return {"created": name, "room": group.name, "lights": len(actions)}

    @server.tool()
    async def set_effect(
        target: Target,
        effect: Effect,
        target_type: TargetTypeArg = None,
        duration_minutes: Annotated[
            float | None,
            Field(
                gt=0, le=MAX_TIMED_EFFECT_MIN, description="Required for sunrise and sunset only."
            ),
        ] = None,
        speed: Annotated[
            float | None,
            Field(ge=0, le=1, description="Looping effects only: 0 slowest to 1 fastest."),
        ] = None,
    ) -> dict[str, Any]:
        """Start a light effect, or stop effects with "none".

        candle, fire, prism, sparkle, opal, glisten, underwater, cosmos, sunbeam and enchant
        loop until stopped. sunrise and sunset brighten or dim gradually over duration_minutes
        (wake-up light, falling asleep). Lights that don't support the effect are skipped.
        """
        if effect in TIMED_EFFECTS and duration_minutes is None:
            raise HueError(f"{effect} needs duration_minutes.")
        if effect not in TIMED_EFFECTS and duration_minutes is not None:
            raise HueError(
                "duration_minutes is only for sunrise and sunset; other effects run until "
                "stopped. Use set_timer to turn the lights off later."
            )
        if speed is not None and effect in (*TIMED_EFFECTS, "none"):
            raise HueError("speed is only for looping effects such as candle.")
        duration_ms = None if duration_minutes is None else round(duration_minutes * 60_000)
        bridge, home = await load_home()
        target_item = home.find_target(target, target_type)
        lights = [target_item] if isinstance(target_item, Light) else target_item.lights

        applied: list[str] = []
        skipped: list[Light] = []
        warnings: list[str] = []
        for light in lights:
            body = _effect_body(light, effect, duration_ms, speed)
            if body is None:
                skipped.append(light)
                continue
            if speed is not None and "effects_v2" not in light.resource:
                warnings.append(f"{light.label} can't change an effect's speed.")
            if applied:
                await anyio.sleep(LIGHT_COMMAND_SPACING_S)
            try:
                light_warnings = await bridge.update("light", light.id, body)
            except HueError as error:
                done = ", ".join(applied) or "no light"
                raise HueError(f"{error} (at {light.label}; applied to {done})") from error
            warnings += [f"{light.label}: {warning}" for warning in light_warnings]
            applied.append(light.name)
        if not applied and effect == "none":
            note = "None of these lights has effects."
            return {"effect": effect, "applied_to": [], "note": note}
        if not applied:
            supported = "; ".join(
                f"{light.name}: {', '.join(light.effects + light.timed_effects) or 'none'}"
                for light in skipped
            )
            raise HueError(f"No light there supports {effect}. Supported effects: {supported}.")
        result: dict[str, Any] = {"effect": effect, "applied_to": applied}
        if skipped:
            result["skipped_unsupported"] = [light.name for light in skipped]
        return _with_warnings(result, warnings)

    @server.tool()
    async def set_timer(
        target: Target,
        minutes: Annotated[float, Field(gt=0, lt=24 * 60)],
        action: Annotated[
            str, Field(description='"off", "on", or a scene name in the target room or zone.')
        ] = "off",
        target_type: TargetTypeArg = None,
    ) -> dict[str, Any]:
        """Do something after a delay, e.g. turn the bedroom off in 30 minutes.

        The timer runs on the bridge, so it fires even after this conversation ends.
        """
        delay = timedelta(seconds=round(minutes * 60))
        if not timedelta(seconds=1) <= delay < timedelta(days=1):
            raise HueError("A timer can run from 1 second to just under 24 hours.")
        bridge, home = await load_home()
        target_item = home.find_target(target, target_type)
        address, command_body, description = _timer_command(home, target_item, action.strip())
        async with timer_lock:
            active = _our_timers(await bridge.get_schedules(), bridge.config.app_key)
            if len(active) >= MAX_ACTIVE_TIMERS:
                raise HueError(f"{len(active)} timers are already set; cancel one first.")
            timer_id = await bridge.create_schedule(
                {
                    "name": TIMER_SCHEDULE_NAME,
                    "description": description[:64],
                    "command": {
                        "address": f"/api/{bridge.config.app_key}{address}",
                        "method": "PUT",
                        "body": command_body,
                    },
                    "localtime": f"PT{_hours_minutes_seconds(delay)}",
                    "autodelete": True,
                }
            )
        fires_at = (datetime.now(UTC) + delay).astimezone()
        return {
            "timer_id": timer_id,
            "does": description,
            "fires_at": fires_at.isoformat(timespec="minutes"),
        }

    @server.tool(annotations=READ_ONLY)
    async def list_timers() -> dict[str, Any]:
        """Timers set with set_timer that haven't fired yet."""
        bridge = get_bridge()
        timers = _our_timers(await bridge.get_schedules(), bridge.config.app_key)
        return {"timers": [_describe_timer(timer_id, timer) for timer_id, timer in timers.items()]}

    @server.tool()
    async def cancel_timer(timer_id: str) -> dict[str, Any]:
        """Cancel a timer from list_timers."""
        bridge = get_bridge()
        timer = _our_timers(await bridge.get_schedules(), bridge.config.app_key).get(timer_id)
        if timer is None:
            raise HueError(f"No timer with id {timer_id!r}; list_timers shows the active ones.")
        await bridge.delete_schedule(timer_id)
        return {"cancelled": timer.get("description", timer_id)}

    return server


def _with_warnings(result: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    if warnings:
        result["warnings"] = warnings
    return result


def _check_ability(target: Light | Group, ability: str) -> list[str]:
    """Refuse a change no light in the target can make; name the lights that will ignore it."""
    lights = [target] if isinstance(target, Light) else target.lights
    unable = [light for light in lights if ability not in light.resource]
    if len(unable) < len(lights):
        return [f"{light.label} can't {ABILITIES[ability]}." for light in unable]
    if isinstance(target, Light):
        raise HueError(f"{target.name} can't {ABILITIES[ability]} ({_light_kind(target)} light).")
    raise HueError(f"No light in {target.label} can {ABILITIES[ability]}.")


def _white_tone_limits(group: Group, kelvin: int, sent_mirek: int) -> list[str]:
    """Name the lights in a group whose range stops short of the requested white tone."""
    notes = []
    for light in group.lights:
        if not light.supports_color_temperature:
            continue
        reachable = kelvin_to_mirek(kelvin, *light.mirek_range)
        if reachable != sent_mirek:
            notes.append(f"{light.label} can only go to {mirek_to_kelvin(reachable)} K.")
    return notes


def _is_on(target: Light | Group) -> bool:
    if isinstance(target, Light):
        return bool(target.resource["on"]["on"])
    return bool(target.grouped_light.get("on", {}).get("on", False))


def _light_kind(light: Light) -> str:
    if light.supports_color:
        return "color"
    if light.supports_color_temperature:
        return "white ambiance"
    return "white"


def _percent(brightness: float) -> int:
    """Whole percent, but never 0 for a light that is on at its lowest level."""
    return max(1, round(brightness))


def _describe_light(light: Light) -> dict[str, Any]:
    resource = light.resource
    state: dict[str, Any] = {"name": light.name, "kind": _light_kind(light)}
    if not light.reachable:
        state["reachable"] = False
    state["on"] = resource["on"]["on"]
    if not state["on"]:
        return state
    brightness = resource.get("dimming", {}).get("brightness")
    if brightness is not None:
        state["brightness"] = _percent(brightness)
    color_temperature = resource.get("color_temperature") or {}
    xy = resource.get("color", {}).get("xy")
    if color_temperature.get("mirek_valid") and color_temperature.get("mirek"):
        state["color_temperature_kelvin"] = mirek_to_kelvin(color_temperature["mirek"])
    elif xy and xy.get("y", 0) > 0:  # A y of 0 has no RGB equivalent.
        state["color_hex"] = xy_to_hex((xy["x"], xy["y"]))
    if effect := _active_effect(light):
        state["effect"] = effect
    return state


def _describe_group(home: Home, group: Group, light_states: bool) -> dict[str, Any]:
    any_on = group.grouped_light.get("on", {}).get("on", False)
    description: dict[str, Any] = {"name": group.name, "type": group.kind, "any_on": any_on}
    brightness = group.grouped_light.get("dimming", {}).get("brightness")
    if any_on and brightness is not None:
        description["brightness"] = _percent(brightness)
    if light_states:
        description["lights"] = [_describe_light(light) for light in group.lights]
    else:
        description["lights"] = [light.name for light in group.lights]
    description["scenes"] = [scene.name for scene in home.scenes if scene.group is group]
    return description


def _active_effect(light: Light) -> str | None:
    resource = light.resource
    statuses = [
        resource.get("effects_v2", {}).get("status", {}).get("effect"),
        resource.get("effects", {}).get("status"),
        resource.get("timed_effects", {}).get("status"),
    ]
    return next((s for s in statuses if s and s != NO_EFFECT), None)


def _current_look(light: Light) -> dict[str, Any]:
    resource = light.resource
    if not resource["on"]["on"]:
        return {"on": {"on": False}}
    look: dict[str, Any] = {"on": {"on": True}}
    brightness = resource.get("dimming", {}).get("brightness")
    if brightness is not None:
        look["dimming"] = {"brightness": brightness}
    color_temperature = resource.get("color_temperature") or {}
    xy = resource.get("color", {}).get("xy")
    if color_temperature.get("mirek_valid") and color_temperature.get("mirek"):
        look["color_temperature"] = {"mirek": color_temperature["mirek"]}
    elif xy:
        look["color"] = {"xy": xy}
    return look


def _effect_body(
    light: Light, effect: str, duration_ms: int | None, speed: float | None
) -> dict[str, Any] | None:
    """The light update that applies `effect`, or None if the light doesn't support it."""
    if effect == "none":
        body: dict[str, Any] = {}
        if light.effects:
            body.update(_looping_effect(light, NO_EFFECT, speed=None))
        if light.timed_effects:
            body["timed_effects"] = {"effect": NO_EFFECT}
        return body or None
    if effect in TIMED_EFFECTS:
        if effect not in light.timed_effects:
            return None
        return {"timed_effects": {"effect": effect, "duration": duration_ms}}
    if effect not in light.effects:
        return None
    return {"on": {"on": True}, **_looping_effect(light, effect, speed)}


def _looping_effect(light: Light, effect: str, speed: float | None) -> dict[str, Any]:
    if "effects_v2" not in light.resource:
        return {"effects": {"effect": effect}}
    action: dict[str, Any] = {"effect": effect}
    if speed is not None:
        action["parameters"] = {"speed": speed}
    return {"effects_v2": {"action": action}}


def _timer_command(
    home: Home, target: Light | Group, action: str
) -> tuple[str, dict[str, Any], str]:
    """The v1 address, body and description of a timer. v1 ids come from each id_v1."""
    turn_on_or_off = action.casefold() in ("on", "off")
    if isinstance(target, Light):
        if not turn_on_or_off:
            raise HueError("A timer on one light can only turn it on or off.")
        address = f"{_v1_path(target.resource)}/state"
        return address, {"on": action.casefold() == "on"}, f"turn {target.name} {action}"
    address = f"{_v1_path(target.grouped_light)}/action"
    if turn_on_or_off:
        return address, {"on": action.casefold() == "on"}, f"turn {target.name} {action}"
    scene = home.find_scene(action, target)
    scene_v1_id = _v1_path(scene.resource).removeprefix("/scenes/")
    return address, {"scene": scene_v1_id}, f"activate {scene.name} in {target.name}"


def _v1_path(resource: dict[str, Any]) -> str:
    if "id_v1" not in resource:
        raise HueError("This light or group has no v1 id, so the bridge can't run a timer on it.")
    path: str = resource["id_v1"]
    return path


def _our_timers(schedules: dict[str, dict[str, Any]], app_key: str) -> dict[str, dict[str, Any]]:
    """Pending timers this server set: its name, and a command that carries its app key."""
    return {
        timer_id: schedule
        for timer_id, schedule in schedules.items()
        if schedule.get("name") == TIMER_SCHEDULE_NAME
        and schedule.get("status") == "enabled"
        and schedule.get("command", {}).get("address", "").startswith(f"/api/{app_key}/")
    }


def _hours_minutes_seconds(delay: timedelta) -> str:
    minutes, seconds = divmod(int(delay.total_seconds()), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


_TIMER_LOCALTIME = re.compile(r"PT(\d{2}):(\d{2}):(\d{2})")


def _describe_timer(timer_id: str, schedule: dict[str, Any]) -> dict[str, Any]:
    timer: dict[str, Any] = {"timer_id": timer_id, "does": schedule.get("description", "")}
    delay = _TIMER_LOCALTIME.fullmatch(schedule.get("localtime", ""))
    if delay is None or "starttime" not in schedule:  # Edited outside hue-mcp.
        return timer
    hours, minutes, seconds = map(int, delay.groups())
    started = datetime.fromisoformat(schedule["starttime"]).replace(tzinfo=UTC)
    fires_at = started + timedelta(hours=hours, minutes=minutes, seconds=seconds)
    minutes_left = max(0.0, (fires_at - datetime.now(UTC)).total_seconds() / 60)
    timer["fires_at"] = fires_at.astimezone().isoformat(timespec="minutes")
    timer["minutes_left"] = round(minutes_left, 1)
    return timer
