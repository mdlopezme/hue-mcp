import pytest

from hue_mcp.home import Home
from hue_mcp.power import (
    STANDBY_WATTS,
    TYPICAL_RATED_WATTS,
    current_watts,
    rated_watts,
    shared_brightness,
    watts_at,
)


@pytest.fixture
def home(resources) -> Home:
    next(r for r in resources if r["id"] == "dev-floor")["product_data"]["model_id"] = "LCA007"
    return Home(resources)


def test_known_models_use_their_rating_and_others_a_typical_one(home):
    assert rated_watts(home.find_target("Floor lamp")) == 10.5
    assert rated_watts(home.find_target("Ceiling")) == TYPICAL_RATED_WATTS


def test_draw_scales_from_standby_to_the_rating(home):
    floor = home.find_target("Floor lamp")
    assert watts_at(floor, 100) == pytest.approx(10.5)
    assert watts_at(floor, 50) == pytest.approx(5.5)
    assert watts_at(floor, 0) == pytest.approx(STANDBY_WATTS)


def test_lights_that_are_off_draw_standby_and_lights_that_are_on_their_level(home):
    assert current_watts(home.find_target("Bedside")) == STANDBY_WATTS
    assert current_watts(home.find_target("Floor lamp")) == pytest.approx(0.5 + 10 * 0.8)


def test_lights_without_dimming_draw_their_full_rating_when_on(resources):
    desk = next(r for r in resources if r["id"] == "light-desk")
    desk["on"]["on"] = True
    del desk["dimming"]
    assert current_watts(Home(resources).find_target("Desk lamp")) == TYPICAL_RATED_WATTS


def test_a_shared_brightness_splits_a_budget_by_each_lights_range(home):
    living = home.find_group("Living room").lights  # 10.5 W and 9 W bulbs.
    brightness = shared_brightness(living, 10)
    assert sum(watts_at(light, brightness) for light in living) == pytest.approx(10)
    assert brightness == pytest.approx((10 - 1.0) / (10.0 + 8.5) * 100)
