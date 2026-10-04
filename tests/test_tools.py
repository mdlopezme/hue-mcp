import time
from datetime import UTC, datetime

import anyio
import httpx2
import pytest
from mcp import Client

from hue_mcp import server as server_module
from hue_mcp.bridge import HueBridge
from hue_mcp.errors import HueError
from hue_mcp.server import MAX_ACTIVE_TIMERS, build_server

from conftest import APP_KEY, CONFIG, call, call_failing, our_timer, resource

pytestmark = pytest.mark.anyio

FLAKY_LIGHT = (
    "device (grouped_light) has communication issues, command (.on.on) may not have effect"
)


@pytest.fixture
def non_utc_timezone(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TZ", "XST+05:30")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


async def test_get_home_describes_rooms_zones_and_loose_lights(client):
    home = await call(client, "get_home")
    bedroom, living, downstairs = home["rooms_and_zones"]
    assert [group["name"] for group in (bedroom, living, downstairs)] == [
        "Bedroom",
        "Living room",
        "Downstairs",
    ]
    assert living["type"] == "room"
    assert living["any_on"] is True
    assert living["brightness"] == 72
    assert living["lights"][0] == {
        "name": "Floor lamp",
        "kind": "color",
        "on": True,
        "estimated_watts": 7.3,
        "brightness": 80,
        "color_temperature_kelvin": 2700,
    }
    assert living["scenes"] == ["Movie", "Relax"]
    assert bedroom["any_on"] is False
    assert "brightness" not in bedroom
    assert bedroom["lights"] == [{"name": "Bedside", "kind": "color", "on": False}]
    assert downstairs["type"] == "zone"
    assert downstairs["lights"] == ["Floor lamp", "Ceiling"]
    assert home["lights_not_in_a_room"] == [
        {"name": "Desk lamp", "kind": "white", "reachable": False, "on": False}
    ]


async def test_lights_that_are_on_show_their_color_and_effect(client, resources):
    bedside = next(r for r in resources if r["id"] == "light-bedside")
    bedside["on"]["on"] = True
    home = await call(client, "get_home", room="Bedroom")
    [state] = home["rooms_and_zones"][0]["lights"]
    assert state["effect"] == "candle"
    assert state["color_hex"].startswith("#")


async def test_get_home_for_one_zone_describes_its_lights(client):
    home = await call(client, "get_home", room="downstairs")
    [zone] = home["rooms_and_zones"]
    assert [light["name"] for light in zone["lights"]] == ["Floor lamp", "Ceiling"]


async def test_get_home_survives_odd_states(client, resources):
    by_id = {resource["id"]: resource for resource in resources}
    by_id["light-bedside"]["on"]["on"] = True
    by_id["light-bedside"]["color"]["xy"] = {"x": 0.3, "y": 0.0}
    del by_id["gl-bedroom"]["on"]
    [bedroom] = (await call(client, "get_home", room="Bedroom"))["rooms_and_zones"]
    assert bedroom["any_on"] is False
    assert "color_hex" not in bedroom["lights"][0]


async def test_room_changes_go_to_its_grouped_light_and_turn_it_on(client, fake_bridge):
    result = await call(
        client, "set_lights", target="living room", brightness=40, color_temperature_kelvin=2700
    )
    assert fake_bridge.writes == [
        (
            "PUT",
            "/clip/v2/resource/grouped_light/gl-living",
            {
                "on": {"on": True},
                "dimming": {"brightness": 40},
                "color_temperature": {"mirek": 370},
            },
        )
    ]
    assert result == {
        "target": "Living room (room)",
        "applied": {"on": True, "brightness": 40, "color_temperature_kelvin": 2700},
    }


async def test_colors_are_clamped_to_what_the_light_can_show(client, fake_bridge):
    result = await call(client, "set_lights", target="Bedside", color_hex="#00ff00")
    [(_, path, body)] = fake_bridge.writes
    assert path == "/clip/v2/resource/light/light-bedside"
    assert body["color"]["xy"] == {"x": 0.409, "y": 0.518}  # Gamut B's green corner.
    assert result["applied"]["color_hex"] != "#00ff00"


async def test_white_tones_are_clamped_to_what_the_light_can_show(client, fake_bridge):
    result = await call(client, "set_lights", target="Ceiling", color_temperature_kelvin=2000)
    assert fake_bridge.writes[0][2]["color_temperature"] == {"mirek": 454}  # Its warmest.
    assert result["applied"]["color_temperature_kelvin"] == 2200


async def test_fade_off(client, fake_bridge):
    await call(client, "set_lights", target="all", on=False, transition_seconds=120)
    assert fake_bridge.writes == [
        (
            "PUT",
            "/clip/v2/resource/grouped_light/gl-home",
            {"on": {"on": False}, "dynamics": {"duration": 120000}},
        )
    ]


async def test_relative_dimming_leaves_on_off_alone(client, fake_bridge):
    await call(client, "set_lights", target="Floor lamp", brightness_change=-20)
    assert fake_bridge.writes[0][2] == {
        "dimming_delta": {"action": "down", "brightness_delta": 20}
    }


async def test_partial_success_is_reported_as_a_warning(client, fake_bridge):
    fake_bridge.write_responses["/clip/v2/resource/grouped_light/gl-home"] = httpx2.Response(
        207,
        json={
            "data": [{"rid": "gl-home", "rtype": "grouped_light"}],
            "errors": [{"description": FLAKY_LIGHT}],
        },
    )
    result = await call(client, "set_lights", target="all", on=False)
    assert result["warnings"] == [FLAKY_LIGHT]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"target": "Ceiling", "color_hex": "#ff0000"}, "can't show colors"),
        ({"target": "Desk lamp", "color_temperature_kelvin": 3000}, "can't show white tones"),
        ({"target": "Ceiling"}, "Nothing to change"),
        (
            {"target": "Floor", "color_hex": "#ff0000", "color_temperature_kelvin": 3000},
            "not both",
        ),
        ({"target": "Celing", "on": False}, "Did you mean: Ceiling (light in Living room)?"),
        ({"target": "hall", "on": False}, "No light, room or zone named 'hall'"),
    ],
)
async def test_set_lights_refuses_what_it_cannot_do(client, fake_bridge, arguments, message):
    assert message in await call_failing(client, "set_lights", **arguments)
    assert fake_bridge.writes == []


async def test_activate_scene_in_a_room(client, fake_bridge):
    result = await call(client, "activate_scene", scene="relax", room="bedroom")
    assert fake_bridge.writes == [
        ("PUT", "/clip/v2/resource/scene/scene-relax-bedroom", {"recall": {"action": "active"}})
    ]
    assert result == {"activated": "Relax", "room": "Bedroom"}


async def test_ambiguous_scene_names_list_names_that_work(client, fake_bridge):
    message = await call_failing(client, "activate_scene", scene="Relax")
    assert "Relax (in Bedroom), Relax (in Living room)" in message
    await call(client, "activate_scene", scene="Relax (in Living room)")
    assert fake_bridge.writes[0][1] == "/clip/v2/resource/scene/scene-relax-living"


async def test_create_scene_snapshots_the_current_look(client, fake_bridge):
    result = await call(client, "create_scene", name="Evening", room="Living room")
    assert fake_bridge.writes == [
        (
            "POST",
            "/clip/v2/resource/scene",
            {
                "type": "scene",
                "metadata": {"name": "Evening"},
                "group": {"rid": "room-living", "rtype": "room"},
                "actions": [
                    {
                        "target": {"rid": "light-floor", "rtype": "light"},
                        "action": {
                            "on": {"on": True},
                            "dimming": {"brightness": 80.0},
                            "color_temperature": {"mirek": 370},
                        },
                    },
                    {
                        "target": {"rid": "light-ceiling", "rtype": "light"},
                        "action": {
                            "on": {"on": True},
                            "dimming": {"brightness": 64.8},
                            "color_temperature": {"mirek": 233},
                        },
                    },
                ],
            },
        )
    ]
    assert result == {"created": "Evening", "room": "Living room", "lights": 2}


@pytest.mark.parametrize(("name", "message"), [("relax", "already has"), ("   ", "needs a name")])
async def test_create_scene_refuses_bad_names(client, fake_bridge, name, message):
    assert message in await call_failing(client, "create_scene", name=name, room="Living room")
    assert fake_bridge.writes == []


async def test_effects_apply_per_light_and_skip_unsupported_ones(client, fake_bridge):
    result = await call(client, "set_effect", target="Living room", effect="candle", speed=0.3)
    assert fake_bridge.writes == [
        (
            "PUT",
            "/clip/v2/resource/light/light-floor",
            {
                "on": {"on": True},
                "effects_v2": {"action": {"effect": "candle", "parameters": {"speed": 0.3}}},
            },
        )
    ]
    assert result == {
        "effect": "candle",
        "applied_to": ["Floor lamp"],
        "skipped_unsupported": ["Ceiling"],
    }


async def test_a_failing_light_reports_where_the_effect_got_to(client, fake_bridge):
    fake_bridge.write_responses["/clip/v2/resource/light/light-floor"] = httpx2.Response(
        400, json={"data": [], "errors": [{"description": "invalid effect"}]}
    )
    message = await call_failing(client, "set_effect", target="all", effect="candle")
    assert "invalid effect (at Floor lamp (light in Living room); applied to Bedside)" in message


async def test_lights_without_effects_v2_use_the_older_effects_field(client, fake_bridge):
    await call(client, "set_effect", target="Bedroom", effect="none")
    assert fake_bridge.writes == [
        ("PUT", "/clip/v2/resource/light/light-bedside", {"effects": {"effect": "no_effect"}})
    ]


async def test_sunrise_runs_for_the_given_duration(client, fake_bridge):
    assert "needs duration_minutes" in await call_failing(
        client, "set_effect", target="Floor lamp", effect="sunrise"
    )
    await call(client, "set_effect", target="Floor lamp", effect="sunrise", duration_minutes=30)
    assert fake_bridge.writes == [
        (
            "PUT",
            "/clip/v2/resource/light/light-floor",
            {"timed_effects": {"effect": "sunrise", "duration": 1800000}},
        )
    ]


async def test_unsupported_effects_report_what_is_supported(client):
    message = await call_failing(client, "set_effect", target="Bedroom", effect="fire")
    assert "Bedside: candle" in message


async def test_timer_turns_a_room_off_from_the_bridge(client, fake_bridge):
    result = await call(client, "set_timer", target="bedroom", minutes=30)
    assert fake_bridge.writes == [
        (
            "POST",
            f"/api/{APP_KEY}/schedules",
            {
                "name": "hue-mcp",
                "description": "turn Bedroom off",
                "command": {
                    "address": f"/api/{APP_KEY}/groups/2/action",
                    "method": "PUT",
                    "body": {"on": False},
                },
                "localtime": "PT00:30:00",
                "autodelete": True,
            },
        )
    ]
    assert result["timer_id"] == "7"
    assert result["does"] == "turn Bedroom off"


async def test_timer_can_activate_a_scene(client, fake_bridge):
    await call(client, "set_timer", target="Living room", minutes=90.5, action="movie")
    [(_, _, schedule)] = fake_bridge.writes
    assert schedule["command"]["body"] == {"scene": "MoViE123456789a"}
    assert schedule["localtime"] == "PT01:30:30"
    assert schedule["description"] == "activate Movie in Living room"


async def test_timer_on_a_single_light(client, fake_bridge):
    await call(client, "set_timer", target="Desk lamp", minutes=5, action="on")
    [(_, _, schedule)] = fake_bridge.writes
    assert schedule["command"]["address"] == f"/api/{APP_KEY}/lights/4/state"
    assert schedule["command"]["body"] == {"on": True}
    assert "only turn it on or off" in await call_failing(
        client, "set_timer", target="Desk lamp", minutes=5, action="Relax"
    )


@pytest.mark.parametrize("minutes", [0.001, 1439.995])
async def test_timer_delays_must_fit_the_bridge(client, fake_bridge, minutes):
    message = await call_failing(client, "set_timer", target="Bedroom", minutes=minutes)
    assert "1 second to just under 24 hours" in message
    assert fake_bridge.writes == []


async def test_timers_are_capped_to_leave_room_for_other_apps(client, fake_bridge):
    fake_bridge.schedules = {str(i): our_timer() for i in range(MAX_ACTIVE_TIMERS)}
    message = await call_failing(client, "set_timer", target="Bedroom", minutes=5)
    assert f"{MAX_ACTIVE_TIMERS} timers are already set" in message
    assert fake_bridge.writes == []


@pytest.mark.usefixtures("non_utc_timezone")
async def test_list_and_cancel_only_our_timers(client, fake_bridge):
    fake_bridge.schedules = {
        "7": our_timer(),
        "3": our_timer(name="Wake up"),
        "4": our_timer(command={"address": "/api/another-apps-key/groups/2/action"}),
    }

    timers = (await call(client, "list_timers"))["timers"]
    assert [timer["timer_id"] for timer in timers] == ["7"]
    assert timers[0]["minutes_left"] == pytest.approx(20, abs=0.1)
    assert timers[0]["fires_at"].endswith("-05:30")  # The bridge's start time is UTC.

    for not_ours in ("3", "4"):
        assert "No timer with id" in await call_failing(client, "cancel_timer", timer_id=not_ours)
    assert await call(client, "cancel_timer", timer_id="7") == {"cancelled": "turn Bedroom off"}
    assert fake_bridge.writes == [("DELETE", f"/api/{APP_KEY}/schedules/7", None)]


async def test_tools_explain_how_to_pair_when_not_paired():
    def not_paired():
        raise HueError("Not paired with a Hue Bridge yet.")

    async with Client(build_server(not_paired)) as client:
        assert "Not paired" in await call_failing(client, "get_home")


@pytest.mark.parametrize(
    "change",
    [{"brightness": 30}, {"color_hex": "#ff8800"}, {"color_temperature_kelvin": 3000}],
)
async def test_setting_a_look_turns_the_light_on(client, fake_bridge, change):
    await call(client, "set_lights", target="Floor lamp", **change)
    assert fake_bridge.writes[0][2]["on"] == {"on": True}


async def test_a_group_reports_the_lights_that_cannot_follow(client, fake_bridge):
    result = await call(client, "set_lights", target="Living room", color_hex="#ff0000")
    assert result["warnings"] == ["Ceiling (light in Living room) can't show colors."]


async def test_a_group_where_no_light_can_follow_is_refused(client, fake_bridge, resources):
    only_ceiling = [{"rid": "light-ceiling", "rtype": "light"}]
    resource(resources, "zone-downstairs")["children"] = only_ceiling
    message = await call_failing(client, "set_lights", target="Downstairs", color_hex="#0000ff")
    assert "No light in Downstairs (zone) can show colors." in message
    assert fake_bridge.writes == []


async def test_lights_that_cannot_dim_are_not_given_a_brightness(client, fake_bridge, resources):
    del resource(resources, "light-desk")["dimming"]
    message = await call_failing(client, "set_lights", target="Desk lamp", brightness=50)
    assert "Desk lamp can't be dimmed (white light)." in message
    assert fake_bridge.writes == []


@pytest.mark.parametrize("target", ["Bedside", "Bedroom"])
async def test_relative_dimming_of_lights_that_are_off_is_refused(client, fake_bridge, target):
    message = await call_failing(client, "set_lights", target=target, brightness_change=20)
    assert "is off. Set brightness instead" in message
    assert fake_bridge.writes == []


async def test_relative_dimming_works_when_also_turning_on(client, fake_bridge):
    await call(client, "set_lights", target="Bedside", on=True, brightness_change=20)
    assert fake_bridge.writes[0][2]["on"] == {"on": True}


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"brightness": 30, "brightness_change": 10}, "not both"),
        ({"transition_seconds": 6001, "on": False}, "less than or equal to 6000"),
        ({"brightness": 0}, "greater than or equal to 1"),
    ],
)
async def test_set_lights_checks_its_arguments(client, fake_bridge, arguments, message):
    assert message in await call_failing(client, "set_lights", target="Floor lamp", **arguments)
    assert fake_bridge.writes == []


async def test_an_instant_change_says_so_explicitly(client, fake_bridge):
    await call(client, "set_lights", target="Floor lamp", on=False, transition_seconds=0)
    assert fake_bridge.writes[0][2]["dynamics"] == {"duration": 0}


async def test_lights_on_at_their_lowest_level_show_1_percent(client, resources):
    resource(resources, "light-floor")["dimming"]["brightness"] = 0.39
    resource(resources, "gl-living")["dimming"]["brightness"] = 0.2
    [living] = (await call(client, "get_home", room="Living room"))["rooms_and_zones"]
    assert living["brightness"] == 1
    assert living["lights"][0]["brightness"] == 1


async def test_a_running_sunrise_shows_as_the_lights_effect(client, resources):
    resource(resources, "light-floor")["timed_effects"]["status"] = "sunrise"
    [living] = (await call(client, "get_home", room="Living room"))["rooms_and_zones"]
    assert living["lights"][0]["effect"] == "sunrise"


async def test_scenes_save_lights_that_are_off_as_off_and_colors_as_colors(
    client, fake_bridge, resources
):
    await call(client, "create_scene", name="Night", room="Bedroom")
    [action] = fake_bridge.writes[0][2]["actions"]
    assert action["action"] == {"on": {"on": False}}

    resource(resources, "light-bedside")["on"]["on"] = True
    await call(client, "create_scene", name="Night light", room="Bedroom")
    [action] = fake_bridge.writes[1][2]["actions"]
    assert action["action"]["color"] == {"xy": {"x": 0.2, "y": 0.1}}


async def test_scene_names_only_need_to_be_unique_within_their_room(client, fake_bridge):
    await call(client, "create_scene", name="Relax", room="Downstairs")
    assert fake_bridge.writes[0][2]["metadata"] == {"name": "Relax"}


async def test_scene_transitions_and_color_cycling(client, fake_bridge):
    await call(client, "activate_scene", scene="Movie", transition_seconds=2.5, dynamic=True)
    assert fake_bridge.writes[0][2] == {"recall": {"action": "dynamic_palette", "duration": 2500}}


async def test_scene_and_effect_warnings_are_passed_on_and_say_which_light(client, fake_bridge):
    partial = httpx2.Response(
        207, json={"data": [{"rid": "x", "rtype": "light"}], "errors": [{"description": "slow"}]}
    )
    fake_bridge.write_responses["/clip/v2/resource/scene/scene-movie-living"] = partial
    fake_bridge.write_responses["/clip/v2/resource/light/light-floor"] = partial
    assert (await call(client, "activate_scene", scene="Movie"))["warnings"] == ["slow"]
    result = await call(client, "set_effect", target="Floor lamp", effect="candle")
    assert result["warnings"] == ["Floor lamp (light in Living room): slow"]


async def test_stopping_effects_clears_looping_and_timed_ones(client, fake_bridge):
    await call(client, "set_effect", target="Floor lamp", effect="none")
    assert fake_bridge.writes[0][2] == {
        "effects_v2": {"action": {"effect": "no_effect"}},
        "timed_effects": {"effect": "no_effect"},
    }


async def test_stopping_effects_where_there_are_none_is_not_an_error(client, fake_bridge):
    result = await call(client, "set_effect", target="Ceiling", effect="none")
    assert result["applied_to"] == []
    assert fake_bridge.writes == []


async def test_sunrise_skips_lights_without_timed_effects(client, fake_bridge):
    result = await call(
        client, "set_effect", target="Living room", effect="sunrise", duration_minutes=20
    )
    assert result["applied_to"] == ["Floor lamp"]
    assert result["skipped_unsupported"] == ["Ceiling"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"effect": "candle", "duration_minutes": 30}, "only for sunrise and sunset"),
        ({"effect": "sunrise", "duration_minutes": 30, "speed": 0.5}, "only for looping"),
        ({"effect": "none", "speed": 0.5}, "only for looping"),
        ({"effect": "sunrise", "duration_minutes": 361}, "less than or equal to 360"),
    ],
)
async def test_effect_options_that_would_be_ignored_are_refused(
    client, fake_bridge, arguments, message
):
    assert message in await call_failing(client, "set_effect", target="Floor lamp", **arguments)
    assert fake_bridge.writes == []


async def test_speed_on_a_light_with_the_older_effects_api_is_flagged(client, fake_bridge):
    result = await call(client, "set_effect", target="Bedside", effect="candle", speed=0.8)
    assert result["warnings"] == ["Bedside (light in Bedroom) can't change an effect's speed."]
    assert fake_bridge.writes[0][2] == {"on": {"on": True}, "effects": {"effect": "candle"}}


async def test_effects_on_several_lights_are_spaced_out(client, fake_bridge, monkeypatch):
    waits: list[float] = []

    async def record(delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr(anyio, "sleep", record)
    result = await call(client, "set_effect", target="all", effect="candle")
    assert result["applied_to"] == ["Bedside", "Floor lamp"]
    assert waits == [0.1]


@pytest.mark.parametrize(
    ("target", "action", "address", "body"),
    [
        ("Desk lamp", "off", f"/api/{APP_KEY}/lights/4/state", {"on": False}),
        ("Bedroom", "on", f"/api/{APP_KEY}/groups/2/action", {"on": True}),
        ("Bedroom", "OFF", f"/api/{APP_KEY}/groups/2/action", {"on": False}),
    ],
)
async def test_timers_do_what_they_were_asked(client, fake_bridge, target, action, address, body):
    await call(client, "set_timer", target=target, minutes=5, action=action)
    command = fake_bridge.writes[0][2]["command"]
    assert command["address"] == address
    assert command["body"] == body


async def test_a_timer_scene_must_be_in_the_target_room(client, fake_bridge):
    message = await call_failing(client, "set_timer", target="Bedroom", minutes=5, action="Movie")
    assert "No scene in Bedroom (room) named 'Movie'" in message


async def test_timer_descriptions_fit_the_bridges_64_characters(client, fake_bridge, resources):
    resource(resources, "room-living")["metadata"]["name"] = "L" * 32
    resource(resources, "scene-movie-living")["metadata"]["name"] = "M" * 32
    result = await call(client, "set_timer", target="L" * 32, minutes=5, action="M" * 32)
    assert len(fake_bridge.writes[0][2]["description"]) == 64
    assert result["does"] == f"activate {'M' * 32} in {'L' * 32}"


async def test_a_one_second_timer_is_allowed(client, fake_bridge):
    await call(client, "set_timer", target="Bedroom", minutes=1 / 60)
    assert fake_bridge.writes[0][2]["localtime"] == "PT00:00:01"


async def test_lights_without_a_v1_id_cannot_get_timers(client, fake_bridge, resources):
    del resource(resources, "light-desk")["id_v1"]
    message = await call_failing(client, "set_timer", target="Desk lamp", minutes=5)
    assert "no v1 id" in message


async def test_the_timer_cap_counts_only_our_pending_timers(client, fake_bridge):
    fake_bridge.schedules = {str(i): our_timer(name="Wake up") for i in range(MAX_ACTIVE_TIMERS)}
    fake_bridge.schedules |= {
        f"d{i}": our_timer(status="disabled") for i in range(MAX_ACTIVE_TIMERS)
    }
    await call(client, "set_timer", target="Bedroom", minutes=5)
    assert fake_bridge.writes[0][0] == "POST"


async def test_overdue_and_hand_edited_timers_are_listed_sensibly(client, fake_bridge):
    hand_edited = our_timer()
    del hand_edited["starttime"]
    fake_bridge.schedules = {"1": our_timer(minutes_ago=45), "2": hand_edited}
    overdue, edited = (await call(client, "list_timers"))["timers"]
    assert overdue["minutes_left"] == 0
    assert edited == {"timer_id": "2", "does": "turn Bedroom off"}


@pytest.fixture
def us_eastern_time(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TZ", "EST5EDT,M3.2.0,M11.1.0")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.usefixtures("us_eastern_time")
async def test_a_timer_across_a_clock_change_reports_the_right_local_time(
    client, fake_bridge, monkeypatch
):
    class JustBeforeDaylightSaving(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = datetime(2026, 3, 8, 6, 30, tzinfo=UTC)  # 01:30 EST.
            return moment if tz else moment.astimezone()

    monkeypatch.setattr(server_module, "datetime", JustBeforeDaylightSaving)
    result = await call(client, "set_timer", target="Bedroom", minutes=60)
    assert result["fires_at"] == "2026-03-08T03:30-04:00"  # 02:30 EST doesn't exist.


async def test_brightness_change_on_a_light_that_cannot_dim_is_refused(
    client, fake_bridge, resources
):
    desk = resource(resources, "light-desk")
    desk["on"]["on"] = True
    del desk["dimming"]
    message = await call_failing(client, "set_lights", target="Desk lamp", brightness_change=10)
    assert "Desk lamp can't be dimmed" in message
    assert fake_bridge.writes == []


async def test_a_room_white_tone_names_lights_that_cannot_reach_it(client, fake_bridge):
    result = await call(client, "set_lights", target="Living room", color_temperature_kelvin=2000)
    assert result["warnings"] == ["Ceiling (light in Living room) can only go to 2200 K."]


async def test_concurrent_timers_still_respect_the_cap(fake_bridge):
    fake_bridge.schedules = {str(i): our_timer() for i in range(MAX_ACTIVE_TIMERS - 1)}

    async def slow_bridge(request: httpx2.Request) -> httpx2.Response:
        await anyio.sleep(0.01)  # Lets the other call run in between.
        return fake_bridge.handle(request)

    bridge = HueBridge(CONFIG, transport=httpx2.MockTransport(slow_bridge))
    refused: list[bool] = []
    async with Client(build_server(lambda: bridge)) as client:

        async def set_timer() -> None:
            result = await client.call_tool("set_timer", {"target": "Bedroom", "minutes": 5})
            refused.append(result.is_error)

        async with anyio.create_task_group() as calls:
            calls.start_soon(set_timer)
            calls.start_soon(set_timer)
    assert sorted(refused) == [False, True]


async def test_a_white_tone_for_everything_names_each_light_that_cannot_follow(
    client, fake_bridge
):
    result = await call(client, "set_lights", target="all", color_temperature_kelvin=2000)
    assert result["warnings"] == [
        "Desk lamp (light) can't show white tones.",
        "Ceiling (light in Living room) can only go to 2200 K.",
    ]


async def test_get_home_estimates_watts_per_light_room_and_home(client):
    home = await call(client, "get_home")
    bedroom, living, downstairs = home["rooms_and_zones"]
    assert living["estimated_watts"] == 13.3  # 7.3 W + 6.0 W.
    assert bedroom["estimated_watts"] == 0.5  # Off, on standby.
    assert downstairs["estimated_watts"] == 13.3
    assert "estimated_watts" not in bedroom["lights"][0]
    assert home["estimated_watts"] == 14.3  # Every light once, standby included.


async def test_set_power_gives_every_light_one_brightness_for_the_budget(client, fake_bridge):
    result = await call(client, "set_power", watts=10, target="Living room")
    assert fake_bridge.writes == [
        (
            "PUT",
            "/clip/v2/resource/grouped_light/gl-living",
            {"on": {"on": True}, "dimming": {"brightness": 52.9}},
        )
    ]
    assert result == {"target": "Living room (room)", "brightness": 52.9, "estimated_watts": 10.0}


async def test_set_power_on_one_light_uses_its_rating(client, fake_bridge, resources):
    resource(resources, "dev-floor")["product_data"]["model_id"] = "LCA007"
    result = await call(client, "set_power", watts=5.5, target="Floor lamp", transition_seconds=4)
    assert fake_bridge.writes == [
        (
            "PUT",
            "/clip/v2/resource/light/light-floor",
            {"on": {"on": True}, "dimming": {"brightness": 50.0}, "dynamics": {"duration": 4000}},
        )
    ]
    assert result["estimated_watts"] == 5.5


async def test_a_budget_above_what_the_lights_can_draw_means_full_brightness(client, fake_bridge):
    result = await call(client, "set_power", watts=100, target="Living room")
    assert result["brightness"] == 100
    assert result["warnings"] == ["At full brightness these lights draw only about 18.0 W."]


async def test_a_budget_too_small_to_keep_the_lights_on_is_refused(client, fake_bridge):
    message = await call_failing(client, "set_power", watts=1, target="Living room")
    assert "1 W can't keep 2 lights on: at their dimmest they draw about 1.2 W" in message
    assert fake_bridge.writes == []


async def test_set_power_needs_lights_that_dim(client, fake_bridge, resources):
    del resource(resources, "light-desk")["dimming"]
    message = await call_failing(client, "set_power", watts=3, target="Desk lamp")
    assert "set_power needs lights that dim; Desk lamp (light) can't." in message
    assert fake_bridge.writes == []


@pytest.mark.parametrize("watts", [0, 1001])
async def test_set_power_checks_the_budget(client, fake_bridge, watts):
    await call_failing(client, "set_power", watts=watts)
    assert fake_bridge.writes == []
