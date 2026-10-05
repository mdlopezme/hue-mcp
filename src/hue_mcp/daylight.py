"""The part of the day, from the sun where the lights are, that the pomodoro's looks follow."""

from datetime import date, datetime, time, timedelta, timezone
from typing import Literal

from astral import Observer
from astral.sun import elevation

from hue_mcp.config import Location

Band = Literal["morning", "midday", "golden", "evening", "night"]
BANDS: tuple[Band, ...] = ("morning", "midday", "golden", "evening", "night")

MORNING = timedelta(hours=4)  # From sunrise.
GOLDEN = timedelta(hours=3)  # Up to sunset.
EVENING = timedelta(hours=2.5)  # From sunset.
HORIZON_DEGREES = -0.833  # The sun's upper edge on the horizon, refraction included.
PRECISION = timedelta(seconds=1)


def band_at(when: datetime, location: Location) -> Band:
    """`when` must be timezone-aware. In a polar day it's always midday; in a polar night,
    always night."""
    # Mean solar time keeps each day's sunrise and sunset on one date, wherever the lights are.
    solar = timezone(timedelta(minutes=round(location.longitude * 4)))
    observer = Observer(latitude=location.latitude, longitude=location.longitude)
    day = when.astimezone(solar).date()
    rises, sets = _sun_times(observer, day, solar)
    if rises is None or sets is None:
        return "midday" if _above_horizon(observer, _noon(day, solar)) else "night"
    if when < rises:
        _, last_sets = _sun_times(observer, day - timedelta(days=1), solar)
        if last_sets is not None and when < last_sets + EVENING:
            return "evening"
        return "night"
    morning_ends = max(rises, min(rises + MORNING, sets - GOLDEN))
    if when < morning_ends:
        return "morning"
    if when < sets - GOLDEN:
        return "midday"
    if when < sets:
        return "golden"
    return "evening" if when < sets + EVENING else "night"


def _sun_times(
    observer: Observer, day: date, solar: timezone
) -> tuple[datetime | None, datetime | None]:
    """Sunrise and sunset on a solar day, found where the sun's elevation crosses the horizon
    (astral's own sunrise skips a day a year at some longitudes); None for none that day."""
    noon = _noon(day, solar)
    midnight, next_midnight = noon - timedelta(hours=12), noon + timedelta(hours=12)
    return (
        _crossing(observer, midnight, noon),
        _crossing(observer, noon, next_midnight),
    )


def _crossing(observer: Observer, start: datetime, end: datetime) -> datetime | None:
    """Where the elevation crosses the horizon between `start` and `end`, if it does: it only
    rises from solar midnight to noon, and only sets from noon to midnight."""
    start_above = _above_horizon(observer, start)
    if start_above == _above_horizon(observer, end):
        return None
    while end - start > PRECISION:
        middle = start + (end - start) / 2
        if _above_horizon(observer, middle) == start_above:
            start = middle
        else:
            end = middle
    return end


def _above_horizon(observer: Observer, when: datetime) -> bool:
    return bool(elevation(observer, when, with_refraction=False) > HORIZON_DEGREES)


def _noon(day: date, solar: timezone) -> datetime:
    return datetime.combine(day, time(12), solar)
