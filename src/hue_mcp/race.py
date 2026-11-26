"""The race light show's source: a race kart's health and race-control flags, read from its
ROS 2 stack by a helper that runs in the stack's workspace environment."""

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

import anyio

from hue_mcp.config import load_ros
from hue_mcp.errors import HueError
from hue_mcp.huestream import RGB

HELPER = Path(__file__).with_name("race_listener.py")
SHELL = "/bin/bash"  # A colcon workspace's setup script is for bash.
# The setup script's own output goes to stderr: stdout carries only the helper's lines.
SOURCE_THEN_RUN = 'source "$0" >&2 && exec python3 "$1"'
EXIT_MISSING = 6  # The helper's, which a test keeps equal to its own.
# What `hue-mcp set-ros` saves from the user's shell: setup scripts don't set these.
ROS_MIDDLEWARE = (
    "RMW_IMPLEMENTATION",
    "CYCLONEDDS_URI",
    "ROS_DOMAIN_ID",
    "ROS_AUTOMATIC_DISCOVERY_RANGE",
    "ROS_STATIC_PEERS",
)
FAULTS_STALE_S = 2.0  # The fault status comes 10 times a second, so silence is a lost link.
FLAGS_STALE_S = 5.0  # Race control sends the flags once a second.
BREATH_LOW = 0.35  # A breath dims to this share of the look's brightness and back.

# race_msgs' StopType.
SAFE, IMMEDIATE, EMERGENCY = 1, 2, 3
# Bits of race_msgs' flags bitfield (VehicleBitfields.msg).
RED_FLAG, FULL_COURSE_YELLOW, GREEN_FLAG, WAVED_GREEN, CHECKERED = 0, 1, 2, 3, 4
SPEED_LIMITED_GREENS = tuple(range(5, 22))  # G40 to G190.
ORANGE_FLAG, YELLOW_FLAG, DOUBLE_YELLOW, STOP, BLACK_FLAG, PURPLE_FLAG, BLUE_FLAG = range(22, 29)
EMERGENCY_KILL = 31

RED: RGB = (1.0, 0.0, 0.0)
ORANGE: RGB = (1.0, 0.3, 0.0)
AMBER: RGB = (1.0, 0.55, 0.0)
YELLOW: RGB = (1.0, 0.85, 0.0)
GREEN: RGB = (0.45, 1.0, 0.35)
BLUE: RGB = (0.1, 0.25, 1.0)
PURPLE: RGB = (0.6, 0.0, 1.0)
WHITE: RGB = (1.0, 1.0, 1.0)
WARM_WHITE: RGB = (1.0, 0.75, 0.5)


@dataclass(frozen=True)
class Look:
    """A color at full brightness, how bright to show it, and whether it breathes."""

    color: RGB
    brightness: float
    breathe_s: float | None = None  # One breath's period; None holds steady.

    def at(self, now: float) -> RGB:
        level = self.brightness
        if self.breathe_s is not None:
            swing = 0.5 + 0.5 * math.cos(2 * math.pi * now / self.breathe_s)
            level *= BREATH_LOW + (1 - BREATH_LOW) * swing
        return (self.color[0] * level, self.color[1] * level, self.color[2] * level)


# A problem with the kart breathes and beats any flag. Breaths are slow enough that the flash
# limiter never holds them back.
LINK_LOST = Look(BLUE, 0.4, breathe_s=5.0)
STOP_LOOKS = {
    SAFE: Look(AMBER, 0.8, breathe_s=4.0),
    IMMEDIATE: Look(ORANGE, 0.9, breathe_s=3.0),
    EMERGENCY: Look(RED, 1.0, breathe_s=3.0),
}
# Flags hold steady; the first that is up wins.
FLAG_LOOKS: tuple[tuple[tuple[int, ...], Look], ...] = (
    ((EMERGENCY_KILL, STOP, RED_FLAG), Look(RED, 1.0)),
    ((BLACK_FLAG,), Look(WHITE, 0.15)),
    ((PURPLE_FLAG,), Look(PURPLE, 0.8)),
    ((ORANGE_FLAG,), Look(ORANGE, 0.8)),
    ((DOUBLE_YELLOW, YELLOW_FLAG, FULL_COURSE_YELLOW), Look(YELLOW, 0.8)),
    ((BLUE_FLAG,), Look(BLUE, 0.8)),
    ((CHECKERED,), Look(WHITE, 0.8)),
    # Up most of a race day, so soft enough to work under.
    ((GREEN_FLAG, WAVED_GREEN, *SPEED_LIMITED_GREENS), Look(GREEN, 0.35)),
)
NO_FLAG = Look(WARM_WHITE, 0.1)


@dataclass(frozen=True)
class Reading:
    value: int
    at: float


def race_look(now: float, stop: Reading | None, flags: Reading | None) -> Look:
    """What the room shows: a lost link, then the stop the kart asks for, then the flag."""
    if stop is None or now - stop.at > FAULTS_STALE_S:
        return LINK_LOST
    if stop.value > 0:  # An unknown stop type is taken for the worst.
        return STOP_LOOKS.get(stop.value, STOP_LOOKS[EMERGENCY])
    if flags is None or now - flags.at > FLAGS_STALE_S:
        return NO_FLAG
    for bits, look in FLAG_LOOKS:
        if any(flags.value & (1 << bit) for bit in bits):
            return look
    return NO_FLAG


class Race:
    """Keeps the kart's latest stop request and flags while `run` reads the helper."""

    def __init__(self) -> None:
        ros = load_ros()
        if ros is None:
            raise HueError(
                "The race mode doesn't know the stack's ROS 2 workspace yet. Ask the user to "
                "run `hue-mcp set-ros <workspace>/install/setup.bash` in a shell set up for "
                "the stack."
            )
        self.ros = ros
        self.stop: Reading | None = None
        self.flags: Reading | None = None
        self.ready = anyio.Event()  # Set once the helper has subscribed.

    def look(self, now: float) -> Look:
        return race_look(now, self.stop, self.flags)

    async def run(self) -> None:
        command = [SHELL, "-c", SOURCE_THEN_RUN, self.ros.setup, str(HELPER)]
        environment = {**os.environ, **self.ros.environment}
        async with await anyio.open_process(command, env=environment, stderr=None) as process:
            if process.stdout is None:
                raise HueError("The race helper gave no output to read.")
            pending = b""
            async for chunk in process.stdout:
                *lines, pending = (pending + chunk).split(b"\n")
                for line in lines:
                    self._read(line, anyio.current_time())
            code = await process.wait()
        if code == EXIT_MISSING:
            raise HueError(
                f"{self.ros.setup} doesn't provide rclpy and race_msgs; build the workspace, "
                "or run `hue-mcp set-ros` with the right setup script."
            )
        raise HueError(f"The race helper stopped (code {code}).")

    def _read(self, line: bytes, now: float) -> None:
        try:
            message = json.loads(line)
            if "stop" in message:
                self.stop = Reading(int(message["stop"]), now)
            elif "flags" in message:
                self.flags = Reading(int(message["flags"]), now)
            elif message.get("ready"):
                self.ready.set()
        except (ValueError, TypeError, AttributeError) as error:
            raise HueError(f"The race helper said something unreadable: {line!r}") from error
