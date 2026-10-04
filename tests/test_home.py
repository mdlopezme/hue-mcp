import pytest

from hue_mcp.color import GAMUT_C
from hue_mcp.errors import HueError
from hue_mcp.home import ALL_LIGHTS, Home, Light


@pytest.fixture
def home(resources) -> Home:
    return Home(resources)


def names(items) -> list[str]:
    return [item.name for item in items]


def test_light_names_come_from_their_devices(home):
    assert names(home.lights) == ["Bedside", "Ceiling", "Desk lamp", "Floor lamp"]


def test_unreachable_lights_are_flagged(home):
    reachability = {light.name: light.reachable for light in home.lights}
    assert reachability == {
        "Bedside": True,
        "Ceiling": True,
        "Desk lamp": False,
        "Floor lamp": True,
    }


def test_room_lights_resolve_through_devices_and_zone_lights_directly(home):
    assert names(home.find_group("Living room").lights) == ["Floor lamp", "Ceiling"]
    assert names(home.find_group("Downstairs").lights) == ["Floor lamp", "Ceiling"]


def test_lights_know_their_room(home):
    rooms = {light.name: light.room for light in home.lights}
    assert rooms == {
        "Bedside": "Bedroom",
        "Ceiling": "Living room",
        "Desk lamp": None,
        "Floor lamp": "Living room",
    }


def test_rooms_without_lights_are_left_out(home):
    assert "Office" not in names(home.groups)


def test_all_targets_every_light(home):
    everything = home.find_target(ALL_LIGHTS.upper())
    assert everything is home.everything
    assert everything.kind == "home"
    assert names(everything.lights) == names(home.lights)


def test_names_match_exactly_or_by_a_unique_part(home):
    assert home.find_target("living ROOM").name == "Living room"
    assert home.find_target("floor").name == "Floor lamp"
    assert home.find_target("Downstairs (zone)").name == "Downstairs"


def test_misspellings_are_suggested_never_acted_on(home):
    with pytest.raises(HueError, match=r"Did you mean: Ceiling \(light in Living room\)\?"):
        home.find_target("Celing")


def test_all_must_be_spelled_out(home):
    with pytest.raises(HueError, match="No light, room or zone named 'hall'"):
        home.find_target("hall")


def test_unknown_names_list_what_exists(home):
    with pytest.raises(HueError, match=r"Known: .*Bedroom \(room\)"):
        home.find_target("garage")


def test_empty_names_are_refused(home):
    with pytest.raises(HueError, match="Name the light, room or zone"):
        home.find_target("  ")


def test_scene_names_repeat_across_rooms_so_the_room_disambiguates(home):
    with pytest.raises(HueError, match=r"Relax \(in Bedroom\), Relax \(in Living room\)"):
        home.find_scene("relax")
    bedroom = home.find_group("Bedroom")
    assert home.find_scene("relax", bedroom).id == "scene-relax-bedroom"


def test_duplicate_light_names_are_told_apart_by_label(resources):
    next(r for r in resources if r["id"] == "dev-bedside")["metadata"]["name"] = "Ceiling"
    home = Home(resources)
    with pytest.raises(
        HueError, match=r"Ceiling \(light in Bedroom\), Ceiling \(light in Living room\)"
    ):
        home.find_target("ceiling")
    assert home.find_target("Ceiling (light in Bedroom)").id == "light-bedside"


def test_target_type_narrows_the_search(home):
    with pytest.raises(HueError):
        home.find_target("Floor lamp", target_type="room")
    assert isinstance(home.find_target("Floor lamp", target_type="light"), Light)


def test_light_capabilities(home):
    floor, ceiling, bedside = (home.find_target(n) for n in ("Floor lamp", "Ceiling", "Bedside"))
    assert floor.gamut == GAMUT_C
    assert "candle" in floor.effects
    assert "no_effect" not in floor.effects
    assert floor.timed_effects == ["sunrise", "sunset"]
    assert not ceiling.supports_color
    assert ceiling.supports_color_temperature
    assert ceiling.mirek_range == (153, 454)
    assert ceiling.effects == []
    assert bedside.effects == ["candle"]


def test_unusual_bridge_data_does_not_break_the_snapshot(resources):
    by_id = {resource["id"]: resource for resource in resources}
    del by_id["zc-dev-ceiling"]["status"]
    by_id["light-floor"]["effects_v2"] = {}
    by_id["scene-movie-living"]["metadata"] = {}
    by_id["zone-downstairs"]["children"].append({"rid": "light-gone", "rtype": "light"})
    resources.remove(by_id["dev-desk"])  # A light whose device the snapshot doesn't have.

    home = Home(resources)
    assert home.find_target("Ceiling").reachable
    assert home.find_target("Floor lamp").effects == []
    assert "Movie" not in names(home.scenes)
    assert names(home.find_group("Downstairs").lights) == ["Floor lamp", "Ceiling"]
    assert "Hue light" in names(home.lights)  # Named by the light itself.


def rename(resources, resource_id: str, name: str) -> None:
    next(r for r in resources if r["id"] == resource_id)["metadata"]["name"] = name


def test_an_exact_name_beats_a_longer_name_containing_it(resources):
    rename(resources, "dev-desk", "Ceiling lamp")
    assert Home(resources).find_target("Ceiling").id == "light-ceiling"


def test_partial_names_match_anywhere_in_a_name_but_not_in_labels(home):
    assert home.find_target("oor lamp").name == "Floor lamp"
    assert home.find_target("living").name == "Living room"


@pytest.mark.parametrize("name", ["all", "ALL", "all (every light)"])
def test_all_and_its_label_mean_every_light(home, name):
    assert home.find_target(name) is home.everything


def test_words_starting_with_all_are_not_everything(home):
    with pytest.raises(HueError, match="No light, room or zone named 'alley'"):
        home.find_target("alley")


def test_all_is_never_one_room_even_one_whose_name_contains_it(resources):
    rename(resources, "room-bedroom", "Hallway")
    home = Home(resources)
    with pytest.raises(HueError, match="means every light; leave out target_type"):
        home.find_target("all", target_type="room")
    with pytest.raises(HueError, match="means every light, not one room or zone"):
        home.find_group("all")


def test_errors_offer_all_for_every_light(home):
    with pytest.raises(HueError, match=r"Known: .*all \(every light\)"):
        home.find_target("garage")


def test_narrowed_lookups_say_what_was_searched(home):
    with pytest.raises(HueError, match="No room named 'Downstairs'"):
        home.find_target("Downstairs", target_type="room")
    with pytest.raises(HueError, match=r"No scene in Bedroom \(room\) named 'Movie'"):
        home.find_scene("Movie", home.find_group("Bedroom"))


def test_rooms_without_lights_say_so(home):
    for find in (home.find_target, home.find_group):
        with pytest.raises(HueError, match="Office has no lights in it"):
            find("office")


def test_same_named_lights_in_one_room_are_told_apart_by_id(resources):
    rename(resources, "dev-ceiling", "Lamp")
    rename(resources, "dev-floor", "Lamp")
    home = Home(resources)
    with pytest.raises(HueError, match=r"\[id light-ceiling\].*\[id light-floor\].*ids"):
        home.find_target("lamp")
    assert home.find_target("light-floor").id == "light-floor"


def test_a_device_with_several_lights_names_each_light(resources):
    floor_device = next(r for r in resources if r["id"] == "dev-floor")
    floor_device["services"].append({"rid": "light-ceiling", "rtype": "light"})
    next(r for r in resources if r["id"] == "light-floor")["metadata"]["name"] = "Floor up"
    home = Home(resources)
    assert "Floor up" in names(home.lights)
    assert "Floor lamp" not in names(home.lights)


def test_scenes_of_unknown_groups_are_left_out(resources):
    scene = next(r for r in resources if r["id"] == "scene-movie-living")
    scene["group"]["rid"] = "room-gone"
    assert "Movie" not in names(Home(resources).scenes)


def test_lights_that_report_no_gamut_or_white_range_get_the_standard_ones(resources):
    floor = next(r for r in resources if r["id"] == "light-floor")
    del floor["color"]["gamut"]
    del floor["color_temperature"]["mirek_schema"]
    light = Home(resources).find_target("Floor lamp")
    assert light.gamut == GAMUT_C
    assert light.mirek_range == (153, 500)


def test_a_lightless_room_does_not_hide_a_zone_with_its_name(resources):
    rename(resources, "zone-downstairs", "Office")  # The fixture's Office room has no lights.
    assert Home(resources).find_target("Office").kind == "zone"


@pytest.mark.parametrize("name", ["ll", "al"])
def test_parts_of_all_never_mean_every_light(home, name):
    with pytest.raises(HueError, match=f"No light, room or zone named '{name}'"):
        home.find_target(name)


def test_padded_all_with_a_target_type_is_refused(resources):
    rename(resources, "room-bedroom", "Hallway")
    with pytest.raises(HueError, match="means every light; leave out target_type"):
        Home(resources).find_target("  all  ", target_type="room")


def test_padded_names_of_lightless_rooms_say_so(home):
    with pytest.raises(HueError, match="Office has no lights in it"):
        home.find_target("  office ")


def test_asking_for_a_light_is_not_answered_with_an_empty_room(home):
    with pytest.raises(HueError, match="No light named 'office'"):
        home.find_target("office", target_type="light")


def test_without_lights_all_has_nothing_to_act_on(resources):
    for resource_id in ("home", "gl-home"):
        resources.remove(next(r for r in resources if r["id"] == resource_id))
    rename(resources, "room-bedroom", "Hallway")
    with pytest.raises(HueError, match="no lights to control"):
        Home(resources).find_target("all")


def test_a_room_whose_lights_were_all_removed_counts_as_empty(resources):
    next(r for r in resources if r["id"] == "room-bedroom")["children"] = []
    home = Home(resources)
    assert "Bedroom" not in names(home.groups)
    with pytest.raises(HueError, match="Bedroom has no lights in it"):
        home.find_group("Bedroom")
