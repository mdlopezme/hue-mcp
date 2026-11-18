"""Light shows: colors for each light, 50 times a second, from the music, the screen, both, the
processor's load or a race kart's state."""

import colorsys
import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from itertools import pairwise
from typing import Literal

import anyio

from hue_mcp.audio import SILENCE, Listener, Sound
from hue_mcp.bridge import HueBridge
from hue_mcp.cpu import CpuMeter
from hue_mcp.entertainment import Area, Stream
from hue_mcp.errors import HueError
from hue_mcp.huestream import RGB
from hue_mcp.race import Look, Race
from hue_mcp.screen import Region, Screen

Mode = Literal["music", "screen", "both", "cpu", "race"]
MODES: tuple[Mode, ...] = ("music", "screen", "both", "cpu", "race")
MONITORS: tuple[Mode, ...] = ("cpu", "race")  # Meant to stay on while nobody's there.

FRAME_S = 0.02  # The bridge renders 25 frames a second; sending twice that covers lost ones.
CHECK_EVERY_S = 5.0  # How often to ask the bridge whether the stream is still ours.
SHARE_TIMEOUT_S = 150.0  # For the user to answer the desktop's screen-sharing question.
SMOOTHING_S = 0.15  # Screen colors are followed this smoothly, so cuts don't flicker.
CPU_SMOOTHING_S = 0.6  # Core loads, sampled twice a second, glide rather than jump.
RACE_SMOOTHING_S = 0.3  # A new flag shows within a second, without a hard cut.
RACE_START_TIMEOUT_S = 30.0  # For the race helper to load ROS and subscribe.
# A thermal scale, as on weather maps: blue for an idle machine, through cyan, green, yellow
# and orange, to red for a flat-out one.
LOAD_SCALE: tuple[tuple[float, RGB], ...] = (
    (0.0, (0.1, 0.25, 1.0)),
    (0.2, (0.0, 0.8, 1.0)),
    (0.4, (0.1, 1.0, 0.3)),
    (0.6, (1.0, 0.85, 0.0)),
    (0.8, (1.0, 0.4, 0.0)),
    (1.0, (1.0, 0.0, 0.0)),
)
PUNCH_DECAY_S = 0.25  # A beat's flash fades over about this long.
BEAT_HUE_STEP = 0.12  # Turns of the color wheel per beat.
SPREAD = 0.22  # Color wheel turns between neighboring lights, in music mode.
NO_HUE = (1.0, 0.75, 0.5)  # A warm white, for music over a black or grey screen.
FLASH_STEP = 0.25  # A brightness rise this big counts as a flash...
FLASH_GAP_S = 1 / 3  # ...and flashes come at most three a second (photosensitivity).


@dataclass(frozen=True)
class Placement:
    """The part of the screen a light follows: a region of one monitor (numbered from 1 at the
    left), or of all of them averaged when `screen` is None. And how bright it may get: a
    light that shines on the user, say, kept dimmer."""

    region: Region = "whole"
    screen: int | None = None
    max_brightness: float = 1.0


class Renderer:
    """Turns sound and screen colors into one color per light. It keeps a little state:
    smoothing, the music's color wheel, and the flash limiter."""

    def __init__(self, mode: Mode, lights: dict[str, Placement]):
        self.mode = mode
        self.lights = lights
        self._last: float | None = None
        self._hue = 0.0
        self._punch = 0.0
        self._smoothed: dict[str, RGB] = {light: (0.0, 0.0, 0.0) for light in lights}
        self._recent: dict[str, deque[tuple[float, float]]] = {light: deque() for light in lights}
        self._flashed_at: dict[str, float] = dict.fromkeys(lights, -math.inf)

    def frame(
        self,
        now: float,
        sound: Sound,
        screens: dict[int, dict[Region, RGB]],
        pressure: tuple[float, float] | None = None,
        race: Look | None = None,
    ) -> dict[str, RGB]:
        """`pressure`, the machine's 1- and 5-minute load averages per core (0 to 1), drives
        the cpu mode: the quicker one colors the lights, the slower one sets how bright.
        `race`, the look of the kart's state, drives the race mode."""
        dt = 0.0 if self._last is None else max(0.0, now - self._last)
        self._last = now
        self._follow_music(dt, sound)
        colors = {}
        for index, (light, placement) in enumerate(self.lights.items()):
            if self.mode == "music":
                color = self._music_color(index, sound)
            elif self.mode == "cpu":
                target = _load_color(*(pressure or (0.0, 0.0)))
                color = self._smooth(light, target, dt, CPU_SMOOTHING_S)
            elif self.mode == "race":
                target = race.at(now) if race is not None else (0.0, 0.0, 0.0)
                color = self._smooth(light, target, dt, RACE_SMOOTHING_S)
            else:
                color = self._smooth(light, _screen_color(screens, placement), dt)
                if self.mode == "both" and sound is not SILENCE:
                    color = _with_brightness(_hue_of(color), self._music_brightness(sound))
            color = _with_brightness(color, min(max(color), placement.max_brightness))
            colors[light] = self._limit_flashes(light, color, now)
        return colors

    def _follow_music(self, dt: float, sound: Sound) -> None:
        self._punch *= math.exp(-dt / PUNCH_DECAY_S)
        self._hue = (self._hue + dt * (0.03 + 0.12 * sound.bass)) % 1.0
        if sound.beat:
            self._punch = 1.0
            self._hue = (self._hue + BEAT_HUE_STEP) % 1.0

    def _music_brightness(self, sound: Sound) -> float:
        return min(1.0, 0.12 + 0.5 * sound.level + 0.45 * self._punch)

    def _music_color(self, index: int, sound: Sound) -> RGB:
        hue = (self._hue + index * SPREAD) % 1.0
        return colorsys.hsv_to_rgb(hue, 1.0, self._music_brightness(sound))

    def _smooth(self, light: str, target: RGB, dt: float, over_s: float = SMOOTHING_S) -> RGB:
        keep = math.exp(-dt / over_s)
        previous = self._smoothed[light]
        smoothed = (
            previous[0] * keep + target[0] * (1 - keep),
            previous[1] * keep + target[1] * (1 - keep),
            previous[2] * keep + target[2] * (1 - keep),
        )
        self._smoothed[light] = smoothed
        return smoothed

    def _limit_flashes(self, light: str, color: RGB, now: float) -> RGB:
        """A rise of FLASH_STEP over the darkest of the last FLASH_GAP_S is a flash, however
        many frames it spans; one is allowed per FLASH_GAP_S, and between them the light is
        held just under one."""
        recent = self._recent[light]
        while recent and recent[0][0] <= now - FLASH_GAP_S:
            recent.popleft()
        wanted = max(color)
        low = min((brightness for _, brightness in recent), default=wanted)
        if wanted - low >= FLASH_STEP:
            if now - self._flashed_at[light] >= FLASH_GAP_S:
                self._flashed_at[light] = now
            else:
                wanted = low + FLASH_STEP * 0.99
        recent.append((now, wanted))
        return _with_brightness(color, wanted)


def _screen_color(screens: dict[int, dict[Region, RGB]], placement: Placement) -> RGB:
    if placement.screen is not None:
        return screens.get(placement.screen, {}).get(placement.region, (0.0, 0.0, 0.0))
    colors = [regions[placement.region] for regions in screens.values()]
    if not colors:
        return (0.0, 0.0, 0.0)
    return (
        sum(color[0] for color in colors) / len(colors),
        sum(color[1] for color in colors) / len(colors),
        sum(color[2] for color in colors) / len(colors),
    )


def _load_color(load: float, sustained: float) -> RGB:
    """`load`'s color on LOAD_SCALE, brighter as the `sustained` load rises."""
    load = min(max(load, 0.0), 1.0)
    for (low, low_color), (high, high_color) in pairwise(LOAD_SCALE):
        if load <= high:
            share = (load - low) / (high - low)
            color = tuple(a + (b - a) * share for a, b in zip(low_color, high_color, strict=True))
            break
    brightness = 0.2 + 0.65 * min(max(sustained, 0.0), 1.0)
    return _with_brightness((color[0], color[1], color[2]), brightness)


def in_order(area: Area, order: list[str] | None) -> list[str]:
    """The area's lights, those the user ordered first in that order, then the rest."""
    lights = list(area.channels.values())
    ranked = {light: rank for rank, light in enumerate(order or [])}
    return sorted(lights, key=lambda light: (ranked.get(light, len(ranked)), lights.index(light)))


def _hue_of(color: RGB) -> RGB:
    """The color at full brightness; warm white when it has no hue to speak of."""
    peak = max(color)
    if peak < 0.02 or peak - min(color) < 0.02:
        return NO_HUE
    return (color[0] / peak, color[1] / peak, color[2] / peak)


def _with_brightness(color: RGB, brightness: float) -> RGB:
    peak = max(color)
    if peak <= 0:
        return (0.0, 0.0, 0.0)
    scale = max(0.0, brightness) / peak
    return (color[0] * scale, color[1] * scale, color[2] * scale)


async def run_show(
    bridge: HueBridge,
    area: Area,
    mode: Mode,
    placements: dict[str, Placement],
    should_end: Callable[[], str | None],
    started: Callable[[], None],
    order: list[str] | None = None,
) -> str:
    """Play until `should_end` gives a reason, or the bridge takes the lights back; returns
    why it ended. Sources and the stream failing raise HueError. `order` lists light ids in
    the user's order, which music colors spread along and cpu cores split along."""
    lights = {light: placements.get(light, Placement()) for light in in_order(area, order)}
    renderer = Renderer(mode, lights)
    listener = Listener() if mode in ("music", "both") else None
    screen = Screen() if mode in ("screen", "both") else None
    meter = CpuMeter() if mode == "cpu" else None
    race = Race() if mode == "race" else None
    lost: list[str] = []  # Set when the bridge takes the lights back.
    try:
        async with anyio.create_task_group() as sources:
            if listener is not None:
                sources.start_soon(listener.run)
            if meter is not None:
                sources.start_soon(meter.run)
            if race is not None:
                sources.start_soon(race.run)
                with anyio.move_on_after(RACE_START_TIMEOUT_S):
                    await race.ready.wait()
                if not race.ready.is_set():
                    raise HueError("The race helper didn't subscribe to the stack in time.")
            if screen is not None:
                sources.start_soon(screen.run)
                with anyio.move_on_after(SHARE_TIMEOUT_S):
                    await screen.sharing.wait()
                if not screen.sharing.is_set():
                    raise HueError("The screen wasn't shared in time.")
            async with Stream(bridge, area) as stream:
                started()
                sources.start_soon(_watch_ownership, stream, lost)
                reason = await _play(
                    stream, renderer, listener, screen, meter, race, should_end, lost
                )
            sources.cancel_scope.cancel()
    except* HueError as failures:
        raise failures.exceptions[0] from None
    except* Exception as failures:  # Not a group of one: the cause itself.
        raise failures.exceptions[0] from None
    return reason


async def _watch_ownership(stream: Stream, lost: list[str]) -> None:
    """Off the frame loop: a slow bridge must not hold up the lights."""
    while True:
        await anyio.sleep(CHECK_EVERY_S)
        if not await stream.still_ours():
            lost.append("Another app took over the lights, so the light show ended.")
            return


async def _play(
    stream: Stream,
    renderer: Renderer,
    listener: Listener | None,
    screen: Screen | None,
    meter: CpuMeter | None,
    race: Race | None,
    should_end: Callable[[], str | None],
    lost: list[str],
) -> str:
    next_frame = anyio.current_time()
    beats_seen = 0
    while True:
        now = anyio.current_time()
        if reason := should_end() or (lost[0] if lost else None):
            return reason
        sound = SILENCE
        if listener is not None:  # Each beat reaches exactly one frame.
            sound = replace(listener.sound, beat=listener.beats != beats_seen)
            beats_seen = listener.beats
        screens = screen.colors if screen is not None else {}
        pressure = meter.pressure if meter is not None else None
        look = race.look(now) if race is not None else None
        stream.send(renderer.frame(now, sound, screens, pressure, look))
        next_frame = max(next_frame + FRAME_S, anyio.current_time())
        await anyio.sleep_until(next_frame)
