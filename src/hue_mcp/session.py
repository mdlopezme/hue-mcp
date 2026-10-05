"""The adaptive pomodoro's timeline, free of I/O.

Given the time, the user's activity and the part of the day, a Session says which phase it is,
which look its room should show, and which nudge is due. Times are seconds on a clock that
counts suspend and never jumps (CLOCK_BOOTTIME), so a phase is never cut short or stretched.
"""

import math
from dataclasses import dataclass, field
from typing import Any, Literal, TypeGuard

from hue_mcp import looks
from hue_mcp.config import Location
from hue_mcp.daylight import BANDS, Band
from hue_mcp.errors import HueError

Phase = Literal["focus", "short_break", "long_break", "waiting"]
PHASES: tuple[Phase, ...] = ("focus", "short_break", "long_break", "waiting")
Nudge = Literal["red", "breath", "dip"]

IDLE_TIMEOUT_S = 5.0  # The compositor reports idle after this long without input.
RED_AFTER_S = 10.0  # Into waiting, before the first red pulse.
NUDGE_EVERY_S = 30.0
BREATH_AFTER_S = 60.0  # Into a break before "take your break", to let the user wrap up.
DIP_BEFORE_END_S = 60.0
UNNUDGED_BREAK_S = 120.0  # Breaks this short or shorter get no breath and no dip.
AWAY_LIMIT_S = 2 * 60 * 60  # Away this long while waiting, and the session ends.


@dataclass(frozen=True)
class Activity:
    """`last_input` is the clock time of the last input: now, while the user isn't idle.
    Unknown activity (the compositor can't be reached) never starts a round or ends a session."""

    known: bool
    idle: bool
    last_input: float


class ActivityTracker:
    """A new connection proves nothing until the compositor reports idle, or doesn't within
    IDLE_TIMEOUT_S (so there was input): a watcher restarting must not look like a return."""

    def __init__(self) -> None:
        self._connected_at: float | None = None
        self._reported = False
        self._idle = False
        self._last_input: float | None = None

    def connected(self, now: float) -> None:
        self._connected_at, self._reported, self._idle = now, False, False

    def idled(self, now: float) -> None:
        if self._reported or self._connected_at is None:
            last_input = now - IDLE_TIMEOUT_S
        else:  # Only proves no input since connecting; keep what was seen before, if older.
            last_input = min(self._connected_at, self._last_input or self._connected_at)
        self._reported, self._idle, self._last_input = True, True, last_input

    def resumed(self) -> None:
        self._reported, self._idle = True, False

    def lost(self) -> None:
        self._connected_at = None

    def at(self, now: float) -> Activity:
        unknown = Activity(known=False, idle=False, last_input=0.0)
        if self._connected_at is None:
            return unknown
        if not self._reported and now - self._connected_at <= IDLE_TIMEOUT_S + 1:
            return unknown
        if self._idle and self._last_input is not None:
            return Activity(True, True, self._last_input)
        return Activity(True, False, now)


@dataclass(frozen=True)
class Durations:
    focus_s: float
    short_break_s: float
    long_break_s: float
    rounds: int

    def __post_init__(self) -> None:
        lengths = (self.focus_s, self.short_break_s, self.long_break_s)
        if not all(is_positive_number(length) for length in lengths):
            raise HueError("Focus and break lengths must be positive numbers.")
        if not isinstance(self.rounds, int) or isinstance(self.rounds, bool) or self.rounds < 1:
            raise HueError("A pomodoro needs a whole number of rounds, at least one.")


@dataclass(frozen=True)
class PhaseChanged:
    phase: Phase
    round: int


@dataclass(frozen=True)
class RoundDone:
    round: int
    minutes: float
    band: Band


@dataclass(frozen=True)
class Ended:
    """The user has been away AWAY_LIMIT_S."""


Event = PhaseChanged | RoundDone | Ended


@dataclass
class Session:
    room_id: str
    room_name: str
    durations: Durations
    location: Location
    phase: Phase
    round: int
    phase_started: float
    band: Band  # The part of the day the current look was picked for.
    look: str  # The scene the room should show.
    last_nudge: float | None = field(default=None, compare=False)
    dipped: bool = field(default=False, compare=False)
    _changed: bool = field(default=False, compare=False, repr=False)

    @classmethod
    def start(
        cls,
        room_id: str,
        room_name: str,
        durations: Durations,
        location: Location,
        now: float,
        band: Band,
    ) -> "Session":
        session = cls(room_id, room_name, durations, location, "focus", 1, now, band, "")
        session._enter("focus", now, band)
        return session

    def deadline(self) -> float | None:
        """When the phase ends on its own; waiting lasts until the user is back."""
        length = {
            "focus": self.durations.focus_s,
            "short_break": self.durations.short_break_s,
            "long_break": self.durations.long_break_s,
        }.get(self.phase)
        return None if length is None else self.phase_started + length

    def advance(self, now: float, activity: Activity, band: Band) -> list[Event]:
        """Move through every phase that has ended by `now`: after a suspend, several may have."""
        events: list[Event] = []
        self._changed = False
        while True:
            deadline = self.deadline()
            if self.phase == "focus" and deadline is not None and now >= deadline:
                events.append(RoundDone(self.round, self.durations.focus_s / 60, self.band))
                last_round = self.round >= self.durations.rounds
                self._enter("long_break" if last_round else "short_break", deadline, band)
            elif deadline is not None and now >= deadline:  # A break is over.
                if activity.known and not activity.idle:
                    self._next_round(now, band)
                else:
                    self._enter("waiting", deadline, band)
            elif self.phase == "waiting" and activity.known:
                if activity.last_input > self.phase_started:
                    self._next_round(now, band)
                elif now - activity.last_input >= AWAY_LIMIT_S:
                    return [*events, Ended()]
                else:
                    break
            else:
                break
        if self._changed:
            events.append(PhaseChanged(self.phase, self.round))
        return events

    def due_nudge(self, now: float, activity: Activity) -> Nudge | None:
        """The nudge to play now, if one is due; a nudge returned counts as played."""
        if self.phase == "waiting":
            if now - self.phase_started < RED_AFTER_S:
                return None
            return self._every("red", now)
        deadline = self.deadline()
        if self.phase == "focus" or deadline is None:
            return None
        if deadline - self.phase_started <= UNNUDGED_BREAK_S:
            return None
        if now >= deadline - DIP_BEFORE_END_S:
            if self.dipped:
                return None
            self.dipped = True
            return "dip"
        working = activity.known and activity.last_input >= now - NUDGE_EVERY_S
        if now - self.phase_started >= BREATH_AFTER_S and working:
            return self._every("breath", now)
        return None

    def to_json(self, now: float, wall_now: float) -> dict[str, Any]:
        """For a restart: clock times become wall-clock times, since the clock resets at boot."""
        return {
            "room_id": self.room_id,
            "room_name": self.room_name,
            "durations": {
                "focus_s": self.durations.focus_s,
                "short_break_s": self.durations.short_break_s,
                "long_break_s": self.durations.long_break_s,
                "rounds": self.durations.rounds,
            },
            "location": {
                "latitude": self.location.latitude,
                "longitude": self.location.longitude,
                "label": self.location.label,
            },
            "phase": self.phase,
            "round": self.round,
            "phase_started_wall": wall_now - (now - self.phase_started),
            "band": self.band,
            "look": self.look,
            "saved_at_wall": wall_now,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any], now: float, wall_now: float) -> "Session":
        try:
            session = cls(
                room_id=str(data["room_id"]),
                room_name=str(data["room_name"]),
                durations=Durations(**data["durations"]),
                location=Location(**data["location"]),
                phase=data["phase"],
                round=int(data["round"]),
                phase_started=now - (wall_now - float(data["phase_started_wall"])),
                band=data["band"],
                look=str(data["look"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise HueError(f"The saved pomodoro is unreadable ({error}).") from error
        if session.phase not in PHASES or session.band not in BANDS:
            raise HueError("The saved pomodoro has an unknown phase or part of the day.")
        return session

    def _next_round(self, now: float, band: Band) -> None:
        """After the last round's long break, a new set of rounds starts."""
        self.round = 1 if self.round >= self.durations.rounds else self.round + 1
        self._enter("focus", now, band)

    def _enter(self, phase: Phase, started: float, band: Band) -> None:
        if phase == "focus":
            self.band, self.look = band, looks.focus_look(band).scene_name
        elif phase != "waiting":  # Waiting keeps the break's look.
            self.band, self.look = band, looks.break_look(phase == "long_break", band).scene_name
        self.phase, self.phase_started = phase, started
        self.last_nudge, self.dipped, self._changed = None, False, True

    def _every(self, nudge: Nudge, now: float) -> Nudge | None:
        if self.last_nudge is not None and now - self.last_nudge < NUDGE_EVERY_S:
            return None
        self.last_nudge = now
        return nudge


def is_positive_number(value: object) -> TypeGuard[int | float]:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )
