"""A pomodoro kept by the bridge: timers switch a room between its focus look and a break look."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from hue_mcp.color import GAMUT_C, clamp_to_gamut, hex_to_xy

DESCRIPTION_PREFIX = "pomodoro:"  # Marks the timers of a pomodoro among this server's timers.
FOCUS_SCENE = "Pomodoro focus"
BREAK_COLOR = "#33dd88"  # A soft green: unmistakably not the working look.
BREAK_BRIGHTNESS = 40
SWITCH_FADE_DECISECONDS = 30  # A 3 s fade marks each switch.


@dataclass(frozen=True)
class Phase:
    """A switch of the lights, `after` the start, to the break look or back to the focus look."""

    after: timedelta
    is_break: bool
    description: str


def plan(focus_minutes: int, break_minutes: int, cycles: int, room: str) -> list[Phase]:
    """Focus starts at once; each focus is followed by a break, and the last break stays on."""
    phases = []
    round_minutes = focus_minutes + break_minutes
    for cycle in range(1, cycles + 1):
        focus_ends = timedelta(minutes=(cycle - 1) * round_minutes + focus_minutes)
        if cycle == cycles:
            phases.append(Phase(focus_ends, True, f"{DESCRIPTION_PREFIX} done, take a long break"))
        else:
            label = f"{DESCRIPTION_PREFIX} break {cycle} of {cycles}"
            phases.append(Phase(focus_ends, True, label))
            phases.append(
                Phase(
                    focus_ends + timedelta(minutes=break_minutes),
                    False,
                    f"{DESCRIPTION_PREFIX} focus {cycle + 1} of {cycles}",
                )
            )
    return [Phase(p.after, p.is_break, f"{p.description} in {room}") for p in phases]


def break_look() -> dict[str, Any]:
    """The break look as a v1 group action, which is what a bridge timer can send."""
    x, y = clamp_to_gamut(hex_to_xy(BREAK_COLOR), GAMUT_C)
    return {
        "on": True,
        "bri": round(BREAK_BRIGHTNESS * 254 / 100),  # v1 brightness runs from 1 to 254.
        "xy": [round(x, 4), round(y, 4)],
        "transitiontime": SWITCH_FADE_DECISECONDS,
    }


def is_pomodoro(schedule: dict[str, Any]) -> bool:
    return str(schedule.get("description", "")).startswith(DESCRIPTION_PREFIX)
