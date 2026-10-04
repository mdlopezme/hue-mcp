import re
from datetime import timedelta

import anyio
import httpx2
import pytest
from mcp import Client

from hue_mcp import server as server_module
from hue_mcp.bridge import HueBridge
from hue_mcp.color import GAMUT_C, _inside_triangle
from hue_mcp.pomodoro import break_look, is_pomodoro, plan
from hue_mcp.server import build_server

from conftest import APP_KEY, CONFIG, call, call_failing, our_timer, resource

pytestmark = pytest.mark.anyio

LIVING_ROOM = f"/api/{APP_KEY}/groups/1/action"


@pytest.fixture(autouse=True)
def no_waiting_for_fades(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_module, "SETTLE_INTERVAL_S", 0)


def scheduled(fake_bridge) -> list[dict]:
    return [body for _, path, body in fake_bridge.writes if path == f"/api/{APP_KEY}/schedules"]


def focus_scene(fake_bridge) -> dict:
    return next(
        r for r in fake_bridge.resources if r.get("metadata", {}).get("name") == "Pomodoro focus"
    )


def test_four_rounds_alternate_and_end_on_a_long_break():
    phases = plan(25, 5, 4, "Office")
    minutes = (25, 30, 55, 60, 85, 90, 115)
    assert [p.after for p in phases] == [timedelta(minutes=m) for m in minutes]
    assert [p.is_break for p in phases] == [True, False, True, False, True, False, True]
    assert phases[0].description == "pomodoro: break 1 of 4 in Office"
    assert phases[1].description == "pomodoro: focus 2 of 4 in Office"
    assert phases[-1].description == "pomodoro: done, take a long break in Office"


def test_one_round_is_one_focus_then_the_long_break():
    [phase] = plan(50, 10, 1, "Office")
    assert phase.after == timedelta(minutes=50)
    assert phase.is_break


def test_breaks_fade_to_a_soft_green_the_bridge_can_send():
    look = break_look()
    assert look["on"] is True
    assert look["bri"] == 102  # 40% on the v1 scale of 1-254.
    assert look["transitiontime"] == 30
    x, y = look["xy"]
    assert y > x  # Green.
    assert _inside_triangle((x, y), GAMUT_C)


def test_pomodoro_timers_are_recognised_by_their_description():
    assert is_pomodoro({"description": "pomodoro: break 1 of 4 in Office"})
    assert not is_pomodoro({"description": "turn Office off"})
    assert not is_pomodoro({})


async def test_start_saves_the_focus_look_and_sets_every_switch_on_the_bridge(client, fake_bridge):
    result = await call(client, "start_pomodoro", room="Living room")

    scene = focus_scene(fake_bridge)
    assert scene["group"] == {"rid": "room-living", "rtype": "room"}
    floor_look = next(a["action"] for a in scene["actions"] if a["target"]["rid"] == "light-floor")
    assert floor_look == {
        "on": {"on": True},
        "dimming": {"brightness": 80.0},
        "color_temperature": {"mirek": 370},
    }
    timers = scheduled(fake_bridge)
    assert [t["localtime"] for t in timers] == [
        "PT00:25:00",
        "PT00:30:00",
        "PT00:55:00",
        "PT01:00:00",
        "PT01:25:00",
        "PT01:30:00",
        "PT01:55:00",
    ]
    assert all(t["command"]["address"] == LIVING_ROOM for t in timers)
    assert timers[0]["command"]["body"] == break_look()
    scene_v1_id = scene["id_v1"].removeprefix("/scenes/")
    assert timers[1]["command"]["body"] == {"scene": scene_v1_id}
    assert result["room"] == "Living room"
    assert [step["then"] for step in result["schedule"][:2]] == [
        "break 1 of 4 in Living room",
        "focus 2 of 4 in Living room",
    ]


async def test_an_existing_focus_scene_is_updated_rather_than_duplicated(
    client, fake_bridge, resources
):
    resources.append(
        {
            "type": "scene",
            "id": "scene-focus",
            "id_v1": "/scenes/FoCuS",
            "metadata": {"name": "Pomodoro focus"},
            "group": {"rid": "room-living", "rtype": "room"},
            "actions": [],
        }
    )
    await call(client, "start_pomodoro", room="Living room", cycles=1)
    updates = [body for method, path, body in fake_bridge.writes if path.endswith("/scene-focus")]
    assert len(updates[0]["actions"]) == 2
    assert not [path for method, path, _ in fake_bridge.writes if path.endswith("/scene")]
    assert scheduled(fake_bridge)[0]["command"]["address"] == LIVING_ROOM


async def test_a_room_whose_lights_are_off_has_no_look_to_focus_with(client, fake_bridge):
    message = await call_failing(client, "start_pomodoro", room="Bedroom")
    assert "The lights in Bedroom (room) are off" in message
    assert fake_bridge.writes == []


async def test_a_new_pomodoro_replaces_the_running_one_and_keeps_other_timers(client, fake_bridge):
    fake_bridge.schedules = {
        "3": our_timer(description="pomodoro: break 1 of 4 in Living room"),
        "4": our_timer(),
    }
    result = await call(client, "start_pomodoro", room="Living room", cycles=1)
    assert result["replaced"] == "the pomodoro that was running"
    assert "3" not in fake_bridge.schedules
    assert "4" in fake_bridge.schedules


async def test_a_pomodoro_needs_room_among_the_other_timers(client, fake_bridge):
    fake_bridge.schedules = {f"other{i}": our_timer() for i in range(4)}
    message = await call_failing(client, "start_pomodoro", room="Living room")
    assert "4 rounds need 7 timers, but 4 other timers are set" in message
    assert fake_bridge.writes == []


async def test_a_pomodoro_the_bridge_cannot_finish_setting_up_is_undone(fake_bridge):
    posted = 0

    def refuses_the_third_timer(request: httpx2.Request) -> httpx2.Response:
        nonlocal posted
        if request.method == "POST" and request.url.path.endswith("/schedules"):
            posted += 1
            if posted == 3:
                full = {"type": 301, "address": "/schedules", "description": "table is full"}
                return httpx2.Response(200, json=[{"error": full}])
        return fake_bridge.handle(request)

    bridge = HueBridge(CONFIG, transport=httpx2.MockTransport(refuses_the_third_timer))
    async with Client(build_server(lambda: bridge)) as client:
        message = await call_failing(client, "start_pomodoro", room="Living room")
    assert "table is full" in message
    assert fake_bridge.schedules == {}


async def test_a_look_still_fading_is_saved_once_it_holds_still(fake_bridge, resources):
    floor = resource(resources, "light-floor")
    levels = iter([50.0, 60.0, 70.0])

    def fading(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET" and request.url.path == "/clip/v2/resource":
            floor["dimming"]["brightness"] = next(levels, 70.0)
        return fake_bridge.handle(request)

    bridge = HueBridge(CONFIG, transport=httpx2.MockTransport(fading))
    async with Client(build_server(lambda: bridge)) as client:
        await call(client, "start_pomodoro", room="Living room", cycles=1)
    actions = focus_scene(fake_bridge)["actions"]
    floor_look = next(a["action"] for a in actions if a["target"]["rid"] == "light-floor")
    assert floor_look["dimming"] == {"brightness": 70.0}


async def test_lights_that_never_hold_still_do_not_stall_the_start(fake_bridge, resources):
    floor = resource(resources, "light-floor")
    reads = 0

    def always_fading(request: httpx2.Request) -> httpx2.Response:
        nonlocal reads
        if request.method == "GET" and request.url.path == "/clip/v2/resource":
            reads += 1
            floor["dimming"]["brightness"] = float(reads)
        return fake_bridge.handle(request)

    bridge = HueBridge(CONFIG, transport=httpx2.MockTransport(always_fading))
    async with Client(build_server(lambda: bridge)) as client:
        await call(client, "start_pomodoro", room="Living room", cycles=1)
    assert reads == 1 + server_module.SETTLE_TRIES + 1  # Settling, then the saved scene's id.


async def test_saving_the_look_during_focus_updates_the_focus_scene(
    client, fake_bridge, resources
):
    await call(client, "start_pomodoro", room="Living room")
    resource(resources, "light-floor")["dimming"]["brightness"] = 20.0  # Dimmed while focusing.
    result = await call(client, "save_pomodoro_look")
    scene = focus_scene(fake_bridge)
    update = [body for _, path, body in fake_bridge.writes if path.endswith(f"/{scene['id']}")]
    actions = update[-1]["actions"]
    floor_look = next(a["action"] for a in actions if a["target"]["rid"] == "light-floor")
    assert floor_look["dimming"] == {"brightness": 20.0}
    assert result == {"saved": "the current look of Living room as its focus look"}


async def test_the_look_cannot_be_saved_during_a_break(client, fake_bridge):
    back_to_focus = {"address": LIVING_ROOM, "method": "PUT", "body": {"scene": "FoCuS"}}
    fake_bridge.schedules = {
        "5": our_timer(description="pomodoro: focus 2 of 4 in Living room", command=back_to_focus)
    }
    assert "break time" in await call_failing(client, "save_pomodoro_look")
    assert fake_bridge.writes == []


@pytest.mark.parametrize("tool", ["save_pomodoro_look", "stop_pomodoro"])
async def test_without_a_pomodoro_there_is_nothing_to_save_or_stop(client, fake_bridge, tool):
    fake_bridge.schedules = {"4": our_timer()}
    assert "No pomodoro is running" in await call_failing(client, tool)
    assert fake_bridge.writes == []


async def test_stopping_cancels_only_the_pomodoro_and_brings_back_the_focus_look(
    client, fake_bridge
):
    await call(client, "start_pomodoro", room="Living room")
    fake_bridge.schedules["other"] = our_timer()
    result = await call(client, "stop_pomodoro")
    assert set(fake_bridge.schedules) == {"other"}
    scene = focus_scene(fake_bridge)
    assert fake_bridge.writes[-1] == (
        "PUT",
        f"/clip/v2/resource/scene/{scene['id']}",
        {"recall": {"action": "active"}},
    )
    assert result == {"stopped": "the pomodoro in Living room"}


GONE_ROOM = {"address": f"/api/{APP_KEY}/groups/99/action", "method": "PUT", "body": {"on": True}}


async def test_a_pomodoro_whose_room_was_deleted_cannot_save_a_look(client, fake_bridge):
    gone = our_timer(description="pomodoro: break 1 of 4", command=GONE_ROOM)
    fake_bridge.schedules = {"5": gone}
    assert "no longer exists" in await call_failing(client, "save_pomodoro_look")


async def test_stopping_a_pomodoro_whose_room_or_scene_is_gone_still_cancels_it(
    client, fake_bridge
):
    gone = our_timer(description="pomodoro: break 1 of 4", command=GONE_ROOM)
    fake_bridge.schedules = {"5": gone}
    assert await call(client, "stop_pomodoro") == {"stopped": "the pomodoro"}
    assert fake_bridge.schedules == {}


async def test_a_focus_scene_the_bridge_does_not_list_is_reported(fake_bridge):
    def hides_new_scenes(request: httpx2.Request) -> httpx2.Response:
        response = fake_bridge.handle(request)
        if request.method == "GET" and request.url.path == "/clip/v2/resource":
            shown = [r for r in fake_bridge.resources if not r["id"].startswith("new-")]
            return httpx2.Response(200, json={"errors": [], "data": shown})
        return response

    bridge = HueBridge(CONFIG, transport=httpx2.MockTransport(hides_new_scenes))
    async with Client(build_server(lambda: bridge)) as client:
        message = await call_failing(client, "start_pomodoro", room="Living room")
    assert "didn't list the focus scene" in message
    assert fake_bridge.schedules == {}


def fire(fake_bridge, description: str) -> None:
    """Make the bridge's timer whose description starts with `description` fire (it's removed)."""
    timer_id = next(
        i for i, t in fake_bridge.schedules.items() if t["description"].startswith(description)
    )
    del fake_bridge.schedules[timer_id]


def show_break_look(resources, *light_ids: str) -> None:
    x, y = break_look()["xy"]
    for light_id in light_ids:
        light = resource(resources, light_id)
        light["on"]["on"] = True
        light["dimming"]["brightness"] = 40.0
        if "color" in light:
            light["color"]["xy"] = {"x": x, "y": y}
            light["color_temperature"]["mirek_valid"] = False


def recalls(fake_bridge) -> list[str]:
    recall = {"recall": {"action": "active"}}
    return [path for _, path, body in fake_bridge.writes if body == recall]


async def test_restarting_during_a_break_keeps_the_saved_focus_look(
    client, fake_bridge, resources
):
    await call(client, "start_pomodoro", room="Living room")
    scene = focus_scene(fake_bridge)
    fire(fake_bridge, "pomodoro: break 1")
    show_break_look(resources, "light-floor", "light-ceiling")
    fake_bridge.requests.clear()

    result = await call(client, "start_pomodoro", room="Living room")
    resaved = [body for _, path, body in fake_bridge.writes if "actions" in (body or {})]
    assert resaved == []
    assert recalls(fake_bridge) == [f"/clip/v2/resource/scene/{scene['id']}"]
    assert "Brought back the focus look from before the break." in result["warnings"]


async def test_restarting_after_the_last_break_keeps_the_saved_focus_look(
    client, fake_bridge, resources
):
    await call(client, "start_pomodoro", room="Living room", cycles=1)
    fire(fake_bridge, "pomodoro: done")
    show_break_look(resources, "light-floor", "light-ceiling")
    fake_bridge.requests.clear()
    await call(client, "start_pomodoro", room="Living room", cycles=1)
    assert recalls(fake_bridge) == [f"/clip/v2/resource/scene/{focus_scene(fake_bridge)['id']}"]


async def test_the_break_look_is_never_saved_as_a_focus_look(client, fake_bridge, resources):
    show_break_look(resources, "light-floor", "light-ceiling")
    message = await call_failing(client, "start_pomodoro", room="Living room")
    assert "shows the break look" in message
    assert fake_bridge.writes == []


async def test_replacing_a_pomodoro_on_a_break_elsewhere_brings_that_room_back(
    client, fake_bridge, resources
):
    await call(client, "start_pomodoro", room="Living room")
    living_focus = focus_scene(fake_bridge)
    fire(fake_bridge, "pomodoro: break 1")
    resource(resources, "light-bedside")["on"]["on"] = True
    fake_bridge.requests.clear()

    result = await call(client, "start_pomodoro", room="Bedroom", cycles=1)
    assert result["replaced"] == "the pomodoro that was running"
    assert recalls(fake_bridge) == [f"/clip/v2/resource/scene/{living_focus['id']}"]
    assert all("Living room" not in t["description"] for t in fake_bridge.schedules.values())


async def test_a_failed_restart_keeps_the_pomodoro_that_was_running(fake_bridge):
    bridge = HueBridge(CONFIG, transport=httpx2.MockTransport(fake_bridge.handle))
    async with Client(build_server(lambda: bridge)) as client:
        await call(client, "start_pomodoro", room="Living room")
    before = dict(fake_bridge.schedules)
    posted = 0

    def refuses_the_third_timer(request: httpx2.Request) -> httpx2.Response:
        nonlocal posted
        if request.method == "POST" and request.url.path.endswith("/schedules"):
            posted += 1
            if posted == 3:
                full = {"type": 301, "address": "/schedules", "description": "table is full"}
                return httpx2.Response(200, json=[{"error": full}])
        return fake_bridge.handle(request)

    bridge = HueBridge(CONFIG, transport=httpx2.MockTransport(refuses_the_third_timer))
    async with Client(build_server(lambda: bridge)) as client:
        assert "table is full" in await call_failing(client, "start_pomodoro", room="Living room")
    assert fake_bridge.schedules == before


async def test_cleanup_carries_on_past_timers_it_cannot_remove(fake_bridge):
    posted = 0

    def refuses_the_third_timer_and_one_delete(request: httpx2.Request) -> httpx2.Response:
        nonlocal posted
        if request.method == "POST" and request.url.path.endswith("/schedules"):
            posted += 1
            if posted == 3:
                full = {"type": 301, "address": "/schedules", "description": "table is full"}
                return httpx2.Response(200, json=[{"error": full}])
        if request.method == "DELETE" and request.url.path.endswith("/schedules/7"):
            return httpx2.Response(500, json={})
        return fake_bridge.handle(request)

    transport = httpx2.MockTransport(refuses_the_third_timer_and_one_delete)
    bridge = HueBridge(CONFIG, transport=transport)
    async with Client(build_server(lambda: bridge)) as client:
        message = await call_failing(client, "start_pomodoro", room="Living room")
    assert "table is full Timers 7 remain; stop_pomodoro removes them." in message
    assert list(fake_bridge.schedules) == ["7"]


async def test_single_pomodoro_switches_cannot_be_cancelled(client, fake_bridge):
    await call(client, "start_pomodoro", room="Living room")
    message = await call_failing(client, "cancel_timer", timer_id="7")
    assert "one switch of the pomodoro; stop_pomodoro ends it all" in message
    assert "7" in fake_bridge.schedules


async def test_the_timer_limit_says_how_many_timers_the_pomodoro_holds(client, fake_bridge):
    await call(client, "start_pomodoro", room="Living room")
    fake_bridge.schedules |= {f"other{i}": our_timer() for i in range(3)}
    message = await call_failing(client, "set_timer", target="Bedroom", minutes=5)
    assert "10 timers are already set (7 by the pomodoro)" in message


async def test_stopping_carries_on_past_timers_that_already_fired(fake_bridge):
    def fired_meanwhile(request: httpx2.Request) -> httpx2.Response:
        if request.method == "DELETE" and request.url.path.endswith("/schedules/7"):
            gone = {"type": 3, "address": "/schedules/7", "description": "resource not available"}
            fake_bridge.schedules.pop("7", None)
            return httpx2.Response(200, json=[{"error": gone}])
        return fake_bridge.handle(request)

    bridge = HueBridge(CONFIG, transport=httpx2.MockTransport(fired_meanwhile))
    async with Client(build_server(lambda: bridge)) as client:
        await call(client, "start_pomodoro", room="Living room")
        assert await call(client, "stop_pomodoro") == {"stopped": "the pomodoro in Living room"}
    assert fake_bridge.schedules == {}


async def test_concurrent_starts_save_one_focus_scene(fake_bridge):
    async def slow_bridge(request: httpx2.Request) -> httpx2.Response:
        await anyio.sleep(0.01)  # Lets the other start run in between.
        return fake_bridge.handle(request)

    bridge = HueBridge(CONFIG, transport=httpx2.MockTransport(slow_bridge))
    async with Client(build_server(lambda: bridge)) as client:

        async def start() -> None:
            await call(client, "start_pomodoro", room="Living room")

        async with anyio.create_task_group() as starts:
            for _ in range(2):
                starts.start_soon(start)
    names = [r.get("metadata", {}).get("name") for r in fake_bridge.resources]
    assert names.count("Pomodoro focus") == 1


async def test_settling_watches_only_the_rooms_lights(fake_bridge, resources):
    desk = resource(resources, "light-desk")  # Not in the Living room.
    reads = 0

    def desk_fading(request: httpx2.Request) -> httpx2.Response:
        nonlocal reads
        if request.method == "GET" and request.url.path == "/clip/v2/resource":
            reads += 1
            desk["dimming"]["brightness"] = float(reads)
        return fake_bridge.handle(request)

    bridge = HueBridge(CONFIG, transport=httpx2.MockTransport(desk_fading))
    async with Client(build_server(lambda: bridge)) as client:
        result = await call(client, "start_pomodoro", room="Living room", cycles=1)
    assert reads == 3  # A first look, one confirming it held still, then the saved scene's id.
    assert "warnings" not in result


async def test_a_look_that_never_settles_is_saved_with_a_warning(fake_bridge, resources):
    floor = resource(resources, "light-floor")
    reads = 0

    def always_fading(request: httpx2.Request) -> httpx2.Response:
        nonlocal reads
        if request.method == "GET" and request.url.path == "/clip/v2/resource":
            reads += 1
            floor["dimming"]["brightness"] = float(reads)
        return fake_bridge.handle(request)

    bridge = HueBridge(CONFIG, transport=httpx2.MockTransport(always_fading))
    async with Client(build_server(lambda: bridge)) as client:
        result = await call(client, "start_pomodoro", room="Living room", cycles=1)
    assert "The lights were still changing; saved them mid-fade." in result["warnings"]


async def test_a_switch_with_no_readable_time_is_not_taken_for_the_next_one(client, fake_bridge):
    focus_with_no_time = our_timer(
        description="pomodoro: focus 2 of 4 in Living room",
        command={"address": LIVING_ROOM, "method": "PUT", "body": {"scene": "X"}},
    )
    del focus_with_no_time["starttime"]
    next_break = our_timer(
        minutes_ago=0,
        description="pomodoro: break 2 of 4 in Living room",
        command={"address": LIVING_ROOM, "method": "PUT", "body": break_look()},
    )
    fake_bridge.schedules = {"5": focus_with_no_time, "6": next_break}
    await call(client, "save_pomodoro_look")  # Focus time: the next readable switch is a break.


async def test_lights_that_cannot_be_reached_do_not_count_as_on(client, fake_bridge, resources):
    for device in ("dev-floor", "dev-ceiling"):
        resource(resources, f"zc-{device}")["status"] = "connectivity_issue"
    assert "are off" in await call_failing(client, "start_pomodoro", room="Living room")


async def test_a_break_refusal_says_when_the_break_ends(client, fake_bridge):
    back_to_focus = {"address": LIVING_ROOM, "method": "PUT", "body": {"scene": "FoCuS"}}
    fake_bridge.schedules = {
        "5": our_timer(description="pomodoro: focus 2 of 4 in Living room", command=back_to_focus)
    }
    message = await call_failing(client, "save_pomodoro_look")
    assert re.search(r"break time until \d\d:\d\d", message)


async def test_rooms_without_color_are_told_breaks_only_dim(client, fake_bridge, resources):
    only_white = [{"rid": "light-ceiling", "rtype": "light"}]
    resource(resources, "zone-downstairs")["children"] = only_white
    result = await call(client, "start_pomodoro", room="Downstairs", cycles=1)
    dims = "The lights in Downstairs can't show color; breaks dim them instead."
    assert dims in result["warnings"]


async def test_changing_a_pomodoro_room_during_focus_reminds_to_save_the_look(client, fake_bridge):
    await call(client, "start_pomodoro", room="Living room")
    reminder = "A pomodoro is running in Living room; save_pomodoro_look keeps this for focus."
    dimmed = await call(client, "set_lights", target="Floor lamp", brightness=30)
    assert reminder in dimmed["warnings"]
    budgeted = await call(client, "set_power", watts=8, target="Living room")
    assert reminder in budgeted["warnings"]
    elsewhere = await call(client, "set_lights", target="Desk lamp", on=True)
    assert "warnings" not in elsewhere


async def test_no_reminder_during_a_break(client, fake_bridge):
    await call(client, "start_pomodoro", room="Living room")
    fire(fake_bridge, "pomodoro: break 1")
    result = await call(client, "set_lights", target="Floor lamp", brightness=30)
    assert "warnings" not in result


def deletes_fail(fake_bridge, timer_id: str):
    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.method == "DELETE" and request.url.path.endswith(f"/schedules/{timer_id}"):
            return httpx2.Response(500, json={})
        return fake_bridge.handle(request)

    return httpx2.MockTransport(handle)


async def test_old_timers_that_cannot_be_cancelled_are_named_when_replacing(fake_bridge):
    fake_bridge.schedules = {"3": our_timer(description="pomodoro: break 1 of 4 in Bedroom")}
    bridge = HueBridge(CONFIG, transport=deletes_fail(fake_bridge, "3"))
    async with Client(build_server(lambda: bridge)) as client:
        result = await call(client, "start_pomodoro", room="Living room", cycles=1)
    assert "Couldn't cancel the old pomodoro's timers 3." in result["warnings"]


async def test_timers_that_cannot_be_cancelled_are_named_when_stopping(fake_bridge):
    fake_bridge.schedules = {"3": our_timer(description="pomodoro: break 1 of 4 in Bedroom")}
    bridge = HueBridge(CONFIG, transport=deletes_fail(fake_bridge, "3"))
    async with Client(build_server(lambda: bridge)) as client:
        result = await call(client, "stop_pomodoro")
    assert "Couldn't cancel timers 3; try again." in result["warnings"]


async def test_a_look_saved_while_still_changing_says_so(client, fake_bridge, resources):
    await call(client, "start_pomodoro", room="Living room")
    floor = resource(resources, "light-floor")
    original_handle = fake_bridge.handle
    reads = 0

    def always_fading(request: httpx2.Request) -> httpx2.Response:
        nonlocal reads
        if request.method == "GET" and request.url.path == "/clip/v2/resource":
            reads += 1
            floor["dimming"]["brightness"] = float(reads)
        return original_handle(request)

    bridge = HueBridge(CONFIG, transport=httpx2.MockTransport(always_fading))
    async with Client(build_server(lambda: bridge)) as fading_client:
        result = await call(fading_client, "save_pomodoro_look")
    assert result["warnings"] == ["The lights were still changing; saved mid-fade."]


async def test_stopping_without_a_saved_focus_scene_just_cancels(client, fake_bridge):
    on_a_switch = {"address": LIVING_ROOM, "body": break_look()}
    fake_bridge.schedules = {
        "3": our_timer(description="pomodoro: break 1 of 4 in Living room", command=on_a_switch)
    }
    assert await call(client, "stop_pomodoro") == {"stopped": "the pomodoro in Living room"}
    assert recalls(fake_bridge) == []


async def test_a_break_whose_end_cannot_be_read_still_refuses_saving(client, fake_bridge):
    back_to_focus = our_timer(
        description="pomodoro: focus 2 of 4 in Living room",
        command={"address": LIVING_ROOM, "method": "PUT", "body": {"scene": "FoCuS"}},
    )
    del back_to_focus["starttime"]
    fake_bridge.schedules = {"5": back_to_focus}
    message = await call_failing(client, "save_pomodoro_look")
    assert "break time until the next switch" in message
