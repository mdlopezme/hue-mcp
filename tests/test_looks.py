import copy
from typing import Any

import pytest

from hue_mcp import looks
from hue_mcp.color import clamp_to_gamut, hex_to_xy, kelvin_to_mirek
from hue_mcp.home import Home, Light
from hue_mcp.looks import Look

from conftest import resource


def lights(resources: list[dict[str, Any]], *ids: str) -> list[Light]:
    home = Home(resources)
    return [next(light for light in home.lights if light.id == light_id) for light_id in ids]


def test_every_look_has_a_scene_name_the_bridge_accepts():
    names = [look.scene_name for look in looks.ALL_LOOKS]
    assert len(set(names)) == len(names) == 9
    assert all(len(name) <= 32 for name in names)


def test_the_task_light_gets_the_reading_white_and_the_others_the_palette(resources):
    floor, ceiling = lights(resources, "light-floor", "light-ceiling")
    look = Look("Test", accents=(("#ff0000", 30),), task_white=(5000, 45))
    actions = looks.scene_actions(look, [floor, ceiling], task_light_id="light-floor")
    by_light = {action["target"]["rid"]: action["action"] for action in actions}
    assert by_light["light-floor"] == {
        "on": {"on": True},
        "dimming": {"brightness": 45},
        "color_temperature": {"mirek": kelvin_to_mirek(5000, *floor.mirek_range)},
    }
    # The ceiling light only has white tones, so it can't take the red: it keeps the brightness.
    assert by_light["light-ceiling"] == {"on": {"on": True}, "dimming": {"brightness": 30}}


def test_accents_go_out_in_name_order_and_repeat(resources):
    floor, bedside = lights(resources, "light-floor", "light-bedside")
    floor.name, bedside.name = "B lamp", "A lamp"
    look = Look("Test", accents=(("#ff0000", 10),))
    actions = looks.scene_actions(look, [floor, bedside], task_light_id=None)
    assert [a["target"]["rid"] for a in actions] == ["light-bedside", "light-floor"]
    assert [a["action"]["dimming"]["brightness"] for a in actions] == [10, 10]


def test_break_looks_have_no_task_light(resources):
    [floor] = lights(resources, "light-floor")
    [action] = looks.scene_actions(looks.SHORT_BREAK, [floor], task_light_id="light-floor")
    assert "color" in action["action"]
    assert "color_temperature" not in action["action"]


def test_colors_are_clamped_to_each_light(resources):
    [floor] = lights(resources, "light-floor")
    look = Look("Test", accents=(("#0000ff", 50),))
    [action] = looks.scene_actions(look, [floor], task_light_id=None)
    x, y = clamp_to_gamut(hex_to_xy("#0000ff"), floor.gamut)
    assert action["action"]["color"]["xy"] == {"x": round(x, 4), "y": round(y, 4)}


def test_a_light_that_cant_dim_is_only_turned_on(resources):
    [desk] = lights(resources, "light-desk")
    del desk.resource["dimming"]
    [action] = looks.scene_actions(looks.SHORT_BREAK, [desk], task_light_id=None)
    assert action["action"] == {"on": {"on": True}}


@pytest.mark.parametrize(
    ("band", "long", "night_variant"),
    [("midday", False, False), ("evening", True, False), ("night", False, True)],
)
def test_breaks_turn_to_their_night_versions_only_at_night(band, long, night_variant):
    name = looks.break_look(long, band).scene_name
    assert name.endswith("night") == night_variant
    assert ("long" in name) == long


def floor_showing(resources, **state: Any) -> dict[str, Any]:
    light = copy.deepcopy(resource(resources, "light-floor"))
    light.update(state)
    return light


ACTION_XY = {
    "on": {"on": True},
    "dimming": {"brightness": 50},
    "color": {"xy": {"x": 0.4, "y": 0.4}},
}


def test_a_light_shows_an_action_within_the_bridges_rounding(resources):
    close = floor_showing(
        resources,
        dimming={"brightness": 52.4},
        color={"xy": {"x": 0.41, "y": 0.395}},
        color_temperature={"mirek": None, "mirek_valid": False},
    )
    assert looks.shows(close, ACTION_XY)


@pytest.mark.parametrize(
    "state",
    [
        {"on": {"on": False}},
        {"dimming": {"brightness": 60}},
        {"color": {"xy": {"x": 0.5, "y": 0.4}}},
    ],
)
def test_a_light_showing_something_else_doesnt_match(resources, state):
    assert not looks.shows(floor_showing(resources, **state), ACTION_XY)


def test_white_tones_compare_by_mirek(resources):
    white = {"on": {"on": True}, "color_temperature": {"mirek": 250}}
    tone = {"mirek": 253, "mirek_valid": True}
    assert looks.shows(floor_showing(resources, color_temperature=tone), white)
    colored = {"mirek": 250, "mirek_valid": False}
    assert not looks.shows(floor_showing(resources, color_temperature=colored), white)
    assert not looks.shows(floor_showing(resources, color_temperature=None), white)


def test_off_matches_off_whatever_the_rest(resources):
    off = floor_showing(resources, on={"on": False}, dimming={"brightness": 3})
    assert looks.shows(off, {"on": {"on": False}})
    assert not looks.shows(floor_showing(resources), {"on": {"on": False}})


def test_a_missing_color_reading_doesnt_match_a_color(resources):
    assert not looks.shows(floor_showing(resources, color={}), ACTION_XY)


def test_the_red_pulse_is_recognized(resources):
    [floor] = lights(resources, "light-floor")
    xy = looks.red_pulse_body()["color"]["xy"]
    floor.resource.update(on={"on": True}, color={**floor.resource["color"], "xy": xy})
    assert looks.shows_red_pulse(floor)
    floor.resource["on"] = {"on": False}
    assert not looks.shows_red_pulse(floor)


def test_the_current_look_captures_white_tones_and_colors(resources):
    [floor, bedside] = lights(resources, "light-floor", "light-bedside")
    assert looks.current_look(bedside) == {"on": {"on": False}}
    floor.resource["color_temperature"] = {"mirek": 300, "mirek_valid": True}
    look = looks.current_look(floor)
    assert look["color_temperature"] == {"mirek": 300}
    floor.resource["color_temperature"] = {"mirek": None, "mirek_valid": False}
    assert "color" in looks.current_look(floor)


def test_the_red_pulse_on_a_light_without_color_is_a_dim(resources):
    [ceiling] = lights(resources, "light-ceiling")
    ceiling.resource["dimming"] = {"brightness": looks.RED_PULSE_BRIGHTNESS}
    assert looks.shows_red_pulse(ceiling)
    ceiling.resource["dimming"] = {"brightness": 80}
    assert not looks.shows_red_pulse(ceiling)
    del ceiling.resource["dimming"]
    assert not looks.shows_red_pulse(ceiling)


def test_a_color_light_reporting_no_color_isnt_red(resources):
    [floor] = lights(resources, "light-floor")
    floor.resource["color"] = {"gamut_type": "C"}
    assert not looks.shows_red_pulse(floor)


def test_a_plain_light_keeps_only_what_it_has(resources):
    [desk] = lights(resources, "light-desk")
    desk.resource["on"] = {"on": True}
    del desk.resource["dimming"]
    assert looks.current_look(desk) == {"on": {"on": True}}
    [action] = looks.scene_actions(looks.focus_look("night"), [desk], task_light_id="light-desk")
    assert action["action"] == {"on": {"on": True}}
