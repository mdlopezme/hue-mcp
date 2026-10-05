import pytest

from hue_mcp.errors import HueError
from hue_mcp.watcher import NOT_RUNNING

from conftest import call, call_failing

pytestmark = pytest.mark.anyio

IN_SESSION = {"room": "Living room", "look": "midday"}


async def test_start_hands_the_session_to_the_watcher_with_its_defaults(client, fake_watcher):
    fake_watcher.replies["start"] = {"running": True, "phase": "focus"}
    assert await call(client, "start_pomodoro", room="Living room", task_light="Floor lamp") == {
        "running": True,
        "phase": "focus",
    }
    assert fake_watcher.requests == [
        (
            "start",
            {
                "room": "Living room",
                "task_light": "Floor lamp",
                "focus_minutes": 25,
                "short_break_minutes": 5,
                "long_break_minutes": 30,
                "rounds": 4,
            },
        )
    ]


async def test_start_explains_how_to_run_the_watcher_when_it_isnt(client):
    assert (await call_failing(client, "start_pomodoro", room="Living room")).endswith(NOT_RUNNING)


async def test_the_watchers_refusals_reach_claude(client, fake_watcher):
    fake_watcher.replies["stop"] = HueError("No pomodoro is running.")
    assert (await call_failing(client, "stop_pomodoro")).endswith(": No pomodoro is running.")


@pytest.mark.parametrize("rounds", [0, 9])
async def test_rounds_are_limited(client, rounds):
    message = await call_failing(client, "start_pomodoro", room="Living room", rounds=rounds)
    assert "rounds" in message


@pytest.mark.parametrize(
    ("tool", "arguments", "request_"),
    [
        ("stop_pomodoro", {}, ("stop", {})),
        ("save_pomodoro_look", {}, ("save_look", {})),
        ("get_pomodoro", {}, ("status", {"days": 7})),
        ("get_pomodoro", {"days": 30}, ("status", {"days": 30})),
    ],
)
async def test_the_other_tools_ask_the_watcher(client, fake_watcher, tool, arguments, request_):
    fake_watcher.replies[request_[0]] = {"ok": "yes"}
    assert await call(client, tool, **arguments) == {"ok": "yes"}
    assert fake_watcher.requests == [request_]


async def test_changing_lights_tells_the_watcher_first(client, fake_watcher):
    fake_watcher.replies["touched"] = IN_SESSION
    result = await call(client, "set_lights", target="Living room", brightness=40)
    assert fake_watcher.requests == [("touched", {"lights": ["light-floor", "light-ceiling"]})]
    assert fake_watcher.writes_before == [0]  # Before the change reached the bridge.
    assert fake_watcher.timeouts == [3.0]  # A stuck watcher can't hold the change up long.
    assert result["warnings"] == [
        "A pomodoro is running in Living room; this change lasts until its next switch or "
        "nudge, unless save_pomodoro_look keeps it in its midday look."
    ]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("set_power", {"watts": 10, "target": "Living room"}),
        ("activate_scene", {"scene": "Movie"}),
        ("set_effect", {"target": "Floor lamp", "effect": "candle"}),
    ],
)
async def test_every_light_changing_tool_tells_the_watcher(client, fake_watcher, tool, arguments):
    fake_watcher.replies["touched"] = IN_SESSION
    result = await call(client, tool, **arguments)
    assert [command for command, _ in fake_watcher.requests] == ["touched"]
    assert "save_pomodoro_look" in result["warnings"][0]


async def test_no_note_when_the_lights_arent_in_the_session(client, fake_watcher):
    fake_watcher.replies["touched"] = {}
    result = await call(client, "set_lights", target="Bedside", on=True)
    assert "warnings" not in result


async def test_a_watcher_that_cant_be_told_is_reported_and_the_change_still_made(
    client, fake_watcher, fake_bridge
):
    fake_watcher.replies["touched"] = HueError("The pomodoro watcher didn't answer in time.")
    result = await call(client, "set_lights", target="Bedside", on=True)
    assert result["warnings"] == [
        "Couldn't tell the pomodoro watcher (The pomodoro watcher didn't answer in time.); a "
        "pomodoro may undo this."
    ]
    assert fake_bridge.writes


async def test_changing_lights_works_without_a_watcher(client, fake_bridge):
    result = await call(client, "set_lights", target="Bedside", on=True)
    assert "warnings" not in result
    assert fake_bridge.writes
