from datetime import UTC, date, datetime, time, timedelta

import pytest

from hue_mcp import daylight
from hue_mcp.config import Location
from hue_mcp.daylight import band_at

# Bogotá: sunrise about 05:43 and sunset about 17:46 local (UTC-5) in October.
TROPICS = Location(latitude=4.71, longitude=-74.07, label="Bogotá, Colombia")
NORTH = Location(latitude=69.65, longitude=18.96, label="Tromsø, Norway")
LOCAL = timedelta(hours=-5)


def local(hour: int, minute: int = 0, day: int = 5) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC) - LOCAL


@pytest.mark.parametrize(
    ("hour", "minute", "band"),
    [
        (5, 30, "night"),  # Before sunrise.
        (6, 0, "morning"),
        (9, 30, "morning"),
        (10, 0, "midday"),  # Sunrise + 4 h.
        (14, 30, "midday"),
        (15, 0, "golden"),  # Sunset - 3 h.
        (17, 30, "golden"),
        (18, 0, "evening"),  # After sunset.
        (20, 0, "evening"),
        (20, 30, "night"),  # Sunset + 2.5 h.
        (23, 59, "night"),
    ],
)
def test_the_day_runs_through_five_bands(hour, minute, band):
    assert band_at(local(hour, minute), TROPICS) == band


def test_just_after_midnight_counts_from_the_previous_sunset():
    assert band_at(local(0, 30), TROPICS) == "night"


def test_an_evening_running_past_midnight_stays_evening():
    late_sunset = Location(latitude=60.17, longitude=24.94, label="Midsummer")
    midsummer_midnight = datetime(2026, 6, 21, 21, 0, tzinfo=UTC)  # Local midnight.
    assert band_at(midsummer_midnight, late_sunset) == "evening"


def test_a_short_winter_day_skips_midday():
    short_day = Location(latitude=64.15, longitude=-21.94, label="Short day")  # About 4 h.
    noon = datetime(2026, 12, 21, 13, 30, tzinfo=UTC)
    assert band_at(noon, short_day) == "golden"


def test_a_polar_night_is_night_and_a_polar_day_midday():
    assert band_at(datetime(2026, 12, 21, 12, 0, tzinfo=UTC), NORTH) == "night"
    assert band_at(datetime(2026, 6, 21, 0, 0, tzinfo=UTC), NORTH) == "midday"


@pytest.mark.parametrize("start", [date(2026, 5, 10), date(2026, 7, 18), date(2026, 11, 20)])
def test_the_weeks_around_a_polar_season_always_have_a_band(start):
    hours = [datetime.combine(start, time(0), UTC) + timedelta(hours=h) for h in range(14 * 24)]
    assert {band_at(hour, NORTH) for hour in hours} <= set(daylight.BANDS)


def test_a_day_whose_sunrise_falls_near_midnight_utc_still_has_one():
    # astral's own sunrise() skips this day at this longitude.
    kolkata = Location(latitude=22.57, longitude=88.36, label="Kolkata, India")
    assert band_at(datetime(2026, 4, 1, 3, 0, tzinfo=UTC), kolkata) == "morning"  # 08:30 local.
