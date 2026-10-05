"""The pomodoro's looks, kept as scenes in its room: one per part of the day for focus, and one
for each kind of break, with a dimmer, warmer version for night."""

import math
from dataclasses import dataclass
from typing import Any

from hue_mcp.color import GAMUT_C, clamp_to_gamut, hex_to_xy, kelvin_to_mirek
from hue_mcp.daylight import Band
from hue_mcp.home import Light


@dataclass(frozen=True)
class Look:
    """`task_white` (kelvin, brightness %) lights the task light, a white to read by; the
    accents (hex, brightness %) go to the other lights in name order, repeating as needed."""

    scene_name: str
    accents: tuple[tuple[str, float], ...]
    task_white: tuple[int, float] | None = None


FOCUS_LOOKS: dict[Band, Look] = {
    "morning": Look(
        "Pomodoro morning", (("#ffc890", 40), ("#5ab4f0", 50), ("#7fd8e0", 50)), (5000, 45)
    ),
    "midday": Look(
        "Pomodoro midday", (("#ffe9a0", 60), ("#ffd84a", 65), ("#fff2c2", 65)), (4500, 50)
    ),
    "golden": Look(
        "Pomodoro golden", (("#ffa040", 50), ("#ff8c3a", 55), ("#ffb878", 55)), (3500, 45)
    ),
    "evening": Look(
        "Pomodoro evening", (("#c8406a", 35), ("#e0507a", 40), ("#ff6f61", 40)), (2700, 40)
    ),
    "night": Look(
        "Pomodoro night", (("#ff7a2a", 15), ("#ff4a1a", 20), ("#e8301c", 20)), (2200, 20)
    ),
}
SHORT_BREAK = Look(
    "Pomodoro short break", (("#c8f5d8", 40), ("#33dd88", 40), ("#9ee04a", 45), ("#19c9b4", 45))
)
SHORT_BREAK_NIGHT = Look(
    "Pomodoro short break night",
    (("#d8e8a0", 8), ("#9cc83c", 8), ("#c8d040", 10), ("#7fb83a", 10)),
)
LONG_BREAK = Look(
    "Pomodoro long break", (("#c79ae8", 40), ("#9a6ae0", 40), ("#7a4fd0", 45), ("#b04fa8", 45))
)
LONG_BREAK_NIGHT = Look(
    "Pomodoro long break night",
    (("#c06080", 8), ("#a04070", 8), ("#8a3a78", 10), ("#b03a60", 10)),
)
ALL_LOOKS = (*FOCUS_LOOKS.values(), SHORT_BREAK, SHORT_BREAK_NIGHT, LONG_BREAK, LONG_BREAK_NIGHT)
LOOKS_BY_SCENE = {look.scene_name: look for look in ALL_LOOKS}

# The nudges: grouped_light commands that fade there and back to the look's scene.
RED_PULSE_HEX = "#ff2000"
RED_PULSE_BRIGHTNESS = 25
PULSE_FADE_MS = 2000
BREATH_DELTA = 30  # Brightness points up for "take your break", and down for the heads-up dip.
DIP_FADE_MS = 400
DIP_HOLD_S = 1.2

# How far a light may read from what it was sent and still count as showing it: the bridge
# rounds brightness and color, and bulbs clamp colors to their gamut.
BRIGHTNESS_TOLERANCE = 3
XY_TOLERANCE = 0.015
MIREK_TOLERANCE = 5
RED_XY_TOLERANCE = 0.04


def focus_look(band: Band) -> Look:
    return FOCUS_LOOKS[band]


def break_look(long: bool, band: Band) -> Look:
    if long:
        return LONG_BREAK_NIGHT if band == "night" else LONG_BREAK
    return SHORT_BREAK_NIGHT if band == "night" else SHORT_BREAK


def scene_actions(
    look: Look, lights: list[Light], task_light_id: str | None
) -> list[dict[str, Any]]:
    """The scene actions that put `lights` in `look`. A break look has no task light."""
    white = look.task_white
    task = next((light for light in lights if light.id == task_light_id), None) if white else None
    others = sorted((light for light in lights if light is not task), key=_name_order)
    actions = [
        _accent(light, *look.accents[i % len(look.accents)]) for i, light in enumerate(others)
    ]
    if task is not None and white is not None:
        actions.append(_white(task, *white))
    return actions


def current_look(light: Light) -> dict[str, Any]:
    """A scene action that brings back what the light shows now."""
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


def shows(resource: dict[str, Any], action: dict[str, Any]) -> bool:
    """Whether a light's state is what a scene action sets, within the bridge's rounding."""
    if resource["on"]["on"] != action.get("on", {}).get("on", True):
        return False
    if not resource["on"]["on"]:
        return True
    wanted_brightness = action.get("dimming", {}).get("brightness")
    brightness = resource.get("dimming", {}).get("brightness")
    if (
        wanted_brightness is not None
        and brightness is not None
        and abs(brightness - wanted_brightness) > BRIGHTNESS_TOLERANCE
    ):
        return False
    if "color_temperature" in action:
        color_temperature = resource.get("color_temperature") or {}
        mirek = color_temperature.get("mirek")
        return (
            bool(color_temperature.get("mirek_valid"))
            and mirek is not None
            and abs(mirek - action["color_temperature"]["mirek"]) <= MIREK_TOLERANCE
        )
    if "color" in action:
        xy = resource.get("color", {}).get("xy")
        wanted = action["color"]["xy"]
        if xy is None:
            return False
        return math.dist((xy["x"], xy["y"]), (wanted["x"], wanted["y"])) <= XY_TOLERANCE
    return True


def shows_red_pulse(light: Light) -> bool:
    """Lights without color can't turn red: the pulse only dims them."""
    resource = light.resource
    if not resource["on"]["on"]:
        return False
    if not light.supports_color:
        brightness = resource.get("dimming", {}).get("brightness")
        return brightness is not None and (
            abs(brightness - RED_PULSE_BRIGHTNESS) <= BRIGHTNESS_TOLERANCE
        )
    xy = resource["color"].get("xy")
    if xy is None:
        return False
    red = clamp_to_gamut(hex_to_xy(RED_PULSE_HEX), light.gamut)
    return math.dist((xy["x"], xy["y"]), red) <= RED_XY_TOLERANCE


def red_pulse_body() -> dict[str, Any]:
    x, y = clamp_to_gamut(hex_to_xy(RED_PULSE_HEX), GAMUT_C)
    return {
        "color": {"xy": {"x": round(x, 4), "y": round(y, 4)}},
        "dimming": {"brightness": RED_PULSE_BRIGHTNESS},
        "dynamics": {"duration": PULSE_FADE_MS},
    }


def _accent(light: Light, hex_color: str, brightness: float) -> dict[str, Any]:
    action: dict[str, Any] = {"on": {"on": True}}
    if "dimming" in light.resource:
        action["dimming"] = {"brightness": brightness}
    if light.supports_color:
        x, y = clamp_to_gamut(hex_to_xy(hex_color), light.gamut)
        action["color"] = {"xy": {"x": round(x, 4), "y": round(y, 4)}}
    return {"target": {"rid": light.id, "rtype": "light"}, "action": action}


def _white(light: Light, kelvin: int, brightness: float) -> dict[str, Any]:
    action: dict[str, Any] = {"on": {"on": True}}
    if "dimming" in light.resource:
        action["dimming"] = {"brightness": brightness}
    if light.supports_color_temperature:
        action["color_temperature"] = {"mirek": kelvin_to_mirek(kelvin, *light.mirek_range)}
    return {"target": {"rid": light.id, "rtype": "light"}, "action": action}


def _name_order(light: Light) -> tuple[str, str]:
    return light.name.casefold(), light.id
