import json
import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest

from hue_mcp import looks
from hue_mcp import watcher as watcher_module
from hue_mcp.bridge import HueBridge
from hue_mcp.config import Location, save_location, state_dir
from hue_mcp.errors import HueError
from hue_mcp.home import Home
from hue_mcp.session import AWAY_LIMIT_S, IDLE_TIMEOUT_S
from hue_mcp.watcher import NOT_RUNNING, Watcher, ask_watcher, notify_desktop, rounds_by_day
from hue_mcp.wayland_idle import WaylandError

from conftest import AREA_ID, CONFIG, FakeBridge, resource, with_entertainment_area

pytestmark = pytest.mark.anyio

PLACE = Location(latitude=38.72, longitude=-9.14, label="Lisbon, Portugal")
FOCUS_S, SHORT_S, LONG_S = 25 * 60, 5 * 60, 30 * 60
START = {
    "room": "Living room",
    "task_light": "Ceiling",
    "focus_minutes": 25,
    "short_break_minutes": 5,
    "long_break_minutes": 30,
    "rounds": 4,
}


class LiveBridge(FakeBridge):
    """A FakeBridge whose lights follow commands, scenes included. Lights in `deaf` miss
    every command, like a bulb with a weak link."""

    def __init__(self, resources: list[dict[str, Any]]):
        super().__init__(resources)
        self.deaf: set[str] = set()
        self.before_each_read: Callable[[], None] = lambda: None

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            self.before_each_read()
        response = super().handle(request)
        path = request.url.path
        if request.method == "PUT" and path not in self.write_responses:
            rtype, _, rid = path.removeprefix("/clip/v2/resource/").partition("/")
            body = json.loads(request.content)
            if rtype == "light":
                self.set_light(rid, body)
            elif rtype == "grouped_light":
                group = next(
                    g for g in Home(self.resources).groups if g.grouped_light["id"] == rid
                )
                for light in group.lights:
                    self.set_light(light.id, body)
            elif rtype == "scene" and "recall" in body:
                for action in resource(self.resources, rid)["actions"]:
                    self.set_light(action["target"]["rid"], action["action"])
            elif rtype == "scene":
                resource(self.resources, rid).update(body)
        return response

    def set_light(self, light_id: str, body: dict[str, Any]) -> None:
        if light_id in self.deaf:
            return
        light = resource(self.resources, light_id)
        if "on" in body:
            light["on"] = dict(body["on"])
        if "dimming" in body:
            light["dimming"] = {**light["dimming"], "brightness": body["dimming"]["brightness"]}
        if "dimming_delta" in body:
            step = body["dimming_delta"]
            sign = 1 if step["action"] == "up" else -1
            brightness = light["dimming"]["brightness"] + sign * step["brightness_delta"]
            light["dimming"] = {**light["dimming"], "brightness": min(max(brightness, 1), 100)}
        if "color" in body and "color" in light:
            light["color"] = {**light["color"], "xy": dict(body["color"]["xy"])}
            if "color_temperature" in light:
                light["color_temperature"] = {
                    **light["color_temperature"],
                    "mirek": None,
                    "mirek_valid": False,
                }
        if "color_temperature" in body and "color_temperature" in light:
            mirek = body["color_temperature"]["mirek"]
            light["color_temperature"] = {
                **light["color_temperature"],
                "mirek": mirek,
                "mirek_valid": True,
            }

    def scene_named(self, name: str) -> dict[str, Any]:
        return next(r for r in self.resources if r.get("metadata", {}).get("name") == name)

    def puts(self, rtype: str) -> list[tuple[str, Any]]:
        prefix = f"/clip/v2/resource/{rtype}/"
        return [
            (path.removeprefix(prefix), body)
            for method, path, body in self.writes
            if method == "PUT" and path.startswith(prefix)
        ]


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def runtime_dir(monkeypatch: pytest.MonkeyPatch):
    # Unix socket paths are short (108 bytes), so not under pytest's deep tmp_path.
    directory = tempfile.mkdtemp(prefix="hm-")
    monkeypatch.setenv("XDG_RUNTIME_DIR", directory)
    yield Path(directory)
    shutil.rmtree(directory)


@pytest.fixture
def band(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    current = {"now": "midday"}
    monkeypatch.setattr(watcher_module, "band_at", lambda when, location: current["now"])
    return current


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runtime_dir: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for constant in (
        "SETTLE_INTERVAL_S",
        "REPORT_LAG_INTERVAL_S",
        "PULSE_HOLD_S",
        "DIP_HOLD_S",
        "PULSE_BACK_S",
        "DIP_BACK_S",
        "AFTER_STREAM_S",
        "RESTORE_CHECK_AFTER_S",
    ):
        monkeypatch.setattr(watcher_module, constant, 0)
    save_location(PLACE)


@pytest.fixture
def live(resources: list[dict[str, Any]]) -> LiveBridge:
    return LiveBridge(with_entertainment_area(resources))


class Harness:
    def __init__(self, live: LiveBridge):
        self.live = live
        self.clock = Clock(1000.0)
        self.wall = Clock(1_800_000_000.0)
        self.asleep = Clock(0.0)
        self.notifications: list[tuple[str, str]] = []
        bridge = HueBridge(CONFIG, transport=httpx2.MockTransport(live.handle))
        self.watcher = Watcher(lambda: bridge, self.clock, self.wall, self.notify, self.asleep)
        self.watcher.tracker.connected(self.clock.now)
        self.watcher.tracker.resumed()  # The user is at the computer.

    async def notify(self, title: str, body: str) -> None:
        self.notifications.append((title, body))

    def advance(self, seconds: float) -> None:
        self.clock.now += seconds
        self.wall.now += seconds

    def suspend(self, seconds: float) -> None:
        self.advance(seconds)
        self.asleep.now += seconds

    async def tick_through(self, seconds: float, task_group: Any, idle: bool = False) -> None:
        """Tick once a second for `seconds`, with the user idle or active throughout."""
        for _ in range(int(seconds)):
            self.advance(1)
            if idle and not self.watcher.tracker.at(self.clock.now).idle:
                self.watcher.tracker.idled(self.clock.now)
            await self.watcher.tick(task_group)
            await self.watcher._pulse_done.wait()


@pytest.fixture
def harness(live: LiveBridge, band: dict[str, str]) -> Harness:
    return Harness(live)


def look_of(live: LiveBridge, light_id: str) -> dict[str, Any]:
    light = resource(live.resources, light_id)
    return {key: light.get(key) for key in ("on", "dimming", "color", "color_temperature")}


def shows_scene(live: LiveBridge, name: str) -> bool:
    scene = live.scene_named(name)
    return all(
        looks.shows(resource(live.resources, action["target"]["rid"]), action["action"])
        for action in scene["actions"]
    )


async def test_starting_creates_the_looks_and_shows_the_focus_look_for_the_time_of_day(harness):
    result = await harness.watcher.handle({"command": "start", **START})
    live = harness.live
    created = [body["metadata"]["name"] for method, path, body in live.writes if method == "POST"]
    assert created == [look.scene_name for look in looks.ALL_LOOKS]
    midday = live.scene_named("Pomodoro midday")
    [(scene_id, recall)] = live.puts("scene")
    assert (scene_id, recall) == (midday["id"], {"recall": {"action": "active", "duration": 3000}})
    assert shows_scene(live, "Pomodoro midday")
    ceiling = next(a for a in midday["actions"] if a["target"]["rid"] == "light-ceiling")
    assert "color_temperature" in ceiling["action"]  # The task light reads by white.
    assert result["running"] is True
    assert (result["phase"], result["round"], result["look"]) == ("focus", "1 of 4", "midday")
    assert result["ends_at"]
    assert harness.notifications[0][0] == "Focus 1 of 4"
    assert (state_dir() / "pomodoro.json").exists()


async def test_starting_again_keeps_the_looks_and_any_changes_to_them(harness):
    await harness.watcher.handle({"command": "start", **START})
    posts = len([w for w in harness.live.writes if w[0] == "POST"])
    result = await harness.watcher.handle({"command": "start", **START})
    assert len([w for w in harness.live.writes if w[0] == "POST"]) == posts
    assert result["replaced"] == "the pomodoro that was running in Living room"


async def test_starting_in_another_room_brings_the_first_room_back_to_focus(harness):
    await harness.watcher.handle({"command": "start", **START})
    await harness.watcher.handle(
        {"command": "start", **START, "room": "Bedroom", "task_light": None}
    )
    living_focus = harness.live.scene_named("Pomodoro midday")
    assert ("scene", living_focus["id"]) in [
        ("scene", rid) for rid, _ in harness.live.puts("scene")
    ]
    assert harness.watcher.session is not None
    assert harness.watcher.session.room_name == "Bedroom"


async def test_starting_needs_a_location(harness, tmp_path):
    (tmp_path / "config" / "hue-mcp" / "location.json").unlink()
    with pytest.raises(HueError, match="set-location"):
        await harness.watcher.handle({"command": "start", **START})


async def test_the_task_light_must_be_in_the_room(harness):
    with pytest.raises(HueError, match="isn't in Living room"):
        await harness.watcher.handle({"command": "start", **START, "task_light": "Bedside"})


async def test_focus_then_break_then_a_return_starts_round_two(harness, band):
    w, live = harness.watcher, harness.live
    await w.handle({"command": "start", **START})
    async with anyio.create_task_group() as tg:
        harness.advance(FOCUS_S - 1)
        band["now"] = "golden"
        await harness.tick_through(2, tg)
        assert w.session is not None
        assert w.session.phase == "short_break"
        assert ("Break time", "5 minutes. Step away from the screen.") in harness.notifications
        await harness.tick_through(7, tg)  # The switch, then its check.
        assert shows_scene(live, "Pomodoro short break")
        # Away for the end of the break: the room waits and pulses red.
        await harness.tick_through(SHORT_S - 9 + 11, tg, idle=True)
        assert w.session.phase == "waiting"
        reds = [body for _, body in live.puts("grouped_light") if "color" in body]
        assert reds == [looks.red_pulse_body()]
        assert shows_scene(live, "Pomodoro short break")  # And back after the pulse.
        # Back at the computer.
        w.tracker.resumed()
        await harness.tick_through(1, tg)
        assert (w.session.phase, w.session.round, w.session.look) == (
            "focus",
            2,
            "Pomodoro golden",
        )
        await harness.tick_through(7, tg)
        assert shows_scene(live, "Pomodoro golden")
    log = (state_dir() / "focus_log.jsonl").read_text().splitlines()
    assert [json.loads(line)["band"] for line in log] == ["midday"]


async def test_a_light_that_missed_the_switch_gets_it_again(harness):
    w, live = harness.watcher, harness.live
    await w.handle({"command": "start", **START})
    async with anyio.create_task_group() as tg:
        live.deaf = {"light-floor"}
        harness.advance(FOCUS_S - 1)
        await harness.tick_through(2, tg)
        live.deaf = set()  # It hears the resend.
        await harness.tick_through(7, tg)
    assert shows_scene(live, "Pomodoro short break")
    resent = [rid for rid, body in live.puts("light")]
    assert resent == ["light-floor"]


async def test_a_light_someone_changed_right_after_a_switch_keeps_the_change(harness):
    w, live = harness.watcher, harness.live
    await w.handle({"command": "start", **START})
    async with anyio.create_task_group() as tg:
        harness.advance(FOCUS_S - 1)
        await harness.tick_through(2, tg)
        live.set_light("light-floor", {"dimming": {"brightness": 5}})
        await harness.tick_through(7, tg)
    assert live.puts("light") == []
    assert look_of(live, "light-floor")["dimming"]["brightness"] == 5


async def waiting(harness: Harness, tg: Any) -> None:
    await harness.watcher.handle({"command": "start", **START})
    harness.advance(FOCUS_S + SHORT_S - 1)
    harness.watcher.tracker.idled(harness.clock.now - 60)
    await harness.tick_through(2, tg, idle=True)
    session = harness.watcher.session
    assert session is not None
    assert session.phase == "waiting"


async def test_turning_the_lights_off_at_the_switch_while_waiting_ends_the_session(harness):
    w, live = harness.watcher, harness.live
    async with anyio.create_task_group() as tg:
        await waiting(harness, tg)
        live.set_light("light-floor", {"on": {"on": False}})
        live.set_light("light-ceiling", {"on": {"on": False}})
        before = len(live.writes)
        await harness.tick_through(15, tg, idle=True)
    assert w.session is None
    assert live.writes[before:] == []  # Neither a pulse nor a restore.
    assert harness.notifications[-1] == (
        "Pomodoro ended",
        "The lights were changed from the switch or the Hue app.",
    )
    assert not (state_dir() / "pomodoro.json").exists()


async def test_a_change_during_the_red_pulse_ends_the_session_too(harness, monkeypatch):
    w, live = harness.watcher, harness.live
    async with anyio.create_task_group() as tg:
        await waiting(harness, tg)

        async def press_the_switch_mid_pulse(seconds: float) -> None:
            live.set_light("light-ceiling", {"on": {"on": False}})

        monkeypatch.setattr(watcher_module.anyio, "sleep", press_the_switch_mid_pulse)
        await harness.tick_through(10, tg, idle=True)
        monkeypatch.undo()
        await w.tick(tg)
    assert w.session is None
    assert not shows_scene(live, "Pomodoro short break")


async def test_two_hours_away_turns_the_room_off_slowly(harness):
    w, live = harness.watcher, harness.live
    async with anyio.create_task_group() as tg:
        await waiting(harness, tg)
        harness.advance(AWAY_LIMIT_S)
        await w.tick(tg)
    assert w.session is None
    off = {"on": {"on": False}, "dynamics": {"duration": 10_000}}
    assert live.puts("grouped_light")[-1] == ("gl-living", off)
    assert harness.notifications[-1] == ("Pomodoro ended", "You were away for 2 hours.")


async def test_working_through_a_break_gets_the_breath_and_the_end_gets_the_dip(harness):
    w, live = harness.watcher, harness.live
    await w.handle({"command": "start", **START})
    async with anyio.create_task_group() as tg:
        harness.advance(FOCUS_S - 1)
        await harness.tick_through(SHORT_S - 1, tg)
    steps = [body["dimming_delta"]["action"] for _, body in live.puts("grouped_light")]
    assert steps[0] == "up"
    assert steps[-1] == "down"
    assert steps.count("down") == 1
    assert shows_scene(live, "Pomodoro short break")


async def test_claude_changing_the_room_holds_off_nudges_for_a_minute(harness):
    w, live = harness.watcher, harness.live
    async with anyio.create_task_group() as tg:
        await waiting(harness, tg)
        reply = await w.handle({"command": "touched", "lights": ["light-floor"]})
        assert reply == {"room": "Living room", "look": "short break"}
        live.set_light("light-floor", {"dimming": {"brightness": 7}})
        before = len(live.writes)
        await harness.tick_through(55, tg, idle=True)
        assert live.writes[before:] == []
        # Unsaved, the change lasts until the next nudge: it isn't the switch or the Hue app.
        await harness.tick_through(10, tg, idle=True)
        assert w.session is not None
        assert shows_scene(live, "Pomodoro short break")


async def test_touching_lights_outside_the_session_room_changes_nothing(harness):
    assert await harness.watcher.handle({"command": "touched", "lights": ["light-floor"]}) == {}
    await harness.watcher.handle({"command": "start", **START})
    reply = await harness.watcher.handle({"command": "touched", "lights": ["light-bedside"]})
    assert reply == {}


async def test_saving_the_look_updates_the_scene_showing_now(harness):
    w, live = harness.watcher, harness.live
    await w.handle({"command": "start", **START})
    live.set_light("light-floor", {"dimming": {"brightness": 12}})
    assert await w.handle({"command": "save_look"}) == {"saved_into": "the midday look"}
    floor = next(
        a
        for a in live.scene_named("Pomodoro midday")["actions"]
        if a["target"]["rid"] == "light-floor"
    )
    assert floor["action"]["dimming"] == {"brightness": 12}


async def test_saving_mid_fade_says_so(harness):
    w, live = harness.watcher, harness.live
    await w.handle({"command": "start", **START})
    brightness = iter(range(10, 90, 5))
    live.before_each_read = lambda: live.set_light(
        "light-floor", {"dimming": {"brightness": next(brightness)}}
    )
    result = await w.handle({"command": "save_look"})
    assert result["warnings"] == ["The lights were still changing; saved them mid-fade."]


async def test_stopping_brings_back_the_focus_look_for_now(harness, band):
    w, live = harness.watcher, harness.live
    await w.handle({"command": "start", **START})
    band["now"] = "night"
    assert await w.handle({"command": "stop"}) == {"stopped": "the pomodoro in Living room"}
    assert shows_scene(live, "Pomodoro night")
    assert await w.handle({"command": "status"}) == {
        "running": False,
        "rounds_by_day": rounds_by_day(7),
    }
    with pytest.raises(HueError, match="No pomodoro is running"):
        await w.handle({"command": "stop"})


async def test_status_tells_the_phase_and_the_rounds_done(harness):
    w = harness.watcher
    await w.handle({"command": "start", **START})
    status = await w.handle({"command": "status", "days": 3})
    assert status["phase"] == "focus"
    assert list(status["rounds_by_day"].values()) == [0, 0, 0]


async def test_status_while_waiting_says_the_round_starts_on_return(harness):
    async with anyio.create_task_group() as tg:
        await waiting(harness, tg)
    status = await harness.watcher.handle({"command": "status", "days": 1})
    assert "starts when the user is back" in status["note"]


async def test_an_unknown_command_is_refused(harness):
    with pytest.raises(HueError, match="doesn't know the command 'dance'"):
        await harness.watcher.handle({"command": "dance"})


async def test_a_restart_picks_the_session_up_and_checks_for_a_red_bulb(harness, live):
    w = harness.watcher
    await w.handle({"command": "start", **START})
    live.set_light("light-floor", {"color": looks.red_pulse_body()["color"]})  # Mid-pulse.
    restarted = Harness(live)
    restarted.clock.now, restarted.wall.now = 5.0, harness.wall.now + 30
    restarted.watcher.restore()
    assert restarted.watcher.session is not None
    async with anyio.create_task_group() as tg:
        await restarted.watcher.tick(tg)
    assert shows_scene(live, "Pomodoro midday")


async def test_a_session_saved_long_ago_is_dropped_without_touching_the_lights(harness, live):
    await harness.watcher.handle({"command": "start", **START})
    restarted = Harness(live)
    restarted.wall.now = harness.wall.now + AWAY_LIMIT_S + 1
    before = len(live.writes)
    restarted.watcher.restore()
    assert restarted.watcher.session is None
    assert not (state_dir() / "pomodoro.json").exists()
    assert live.writes[before:] == []


async def test_an_unreadable_saved_session_is_dropped(harness, live):
    path = state_dir() / "pomodoro.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"phase": "focus"}')
    harness.watcher.restore()
    assert harness.watcher.session is None
    assert not path.exists()


async def test_after_the_computer_sleeps_it_waits_before_acting(harness):
    w = harness.watcher
    await w.handle({"command": "start", **START})
    async with anyio.create_task_group() as tg:
        await w.tick(tg)
        harness.suspend(FOCUS_S + SHORT_S + 600)  # Asleep through the focus and the break.
        await w.tick(tg)
        assert w.session is not None
        assert w.session.phase == "focus"
        await harness.tick_through(14, tg)
        assert w.session.round == 1
        await harness.tick_through(2, tg)
        assert (w.session.phase, w.session.round) == ("focus", 2)  # Back and active.


async def test_a_switch_that_fails_is_retried(harness):
    w, live = harness.watcher, harness.live
    await w.handle({"command": "start", **START})
    short_break = live.scene_named("Pomodoro short break")["id"]
    live.write_responses[f"/clip/v2/resource/scene/{short_break}"] = httpx2.Response(500)
    async with anyio.create_task_group() as tg:
        harness.advance(FOCUS_S - 1)
        await harness.tick_through(2, tg)
        assert not shows_scene(live, "Pomodoro short break")
        del live.write_responses[f"/clip/v2/resource/scene/{short_break}"]
        await harness.tick_through(6, tg)
    assert shows_scene(live, "Pomodoro short break")


async def test_activity_is_followed_and_the_compositor_reconnected(harness, monkeypatch):
    connections = []

    reconnected = anyio.Event()

    async def changes(timeout_ms: int) -> AsyncIterator[bool]:
        connections.append(timeout_ms)
        if len(connections) == 1:
            yield False
            yield True
            yield False
            raise WaylandError("gone")
        yield False
        reconnected.set()
        await anyio.sleep_forever()

    monkeypatch.setattr(watcher_module, "idle_changes", changes)
    monkeypatch.setattr(watcher_module, "RECONNECT_FIRST_S", 0)
    w = Watcher(lambda: HueBridge(CONFIG), harness.clock, harness.wall, harness.notify)
    async with anyio.create_task_group() as tg:
        tg.start_soon(w.watch_activity)
        with anyio.fail_after(5):
            await reconnected.wait()
        assert not w.tracker.at(harness.clock.now).known  # Reconnecting isn't a return.
        assert w.tracker.at(harness.clock.now + IDLE_TIMEOUT_S + 2).known
        tg.cancel_scope.cancel()
    assert connections == [round(IDLE_TIMEOUT_S * 1000)] * 2


async def test_requests_and_answers_cross_the_socket(harness, monkeypatch):
    listening = anyio.Event()
    real_restore = harness.watcher.restore

    def restore_then_say_so() -> None:  # Called once the socket is listening.
        real_restore()
        listening.set()

    monkeypatch.setattr(harness.watcher, "restore", restore_then_say_so)
    async with anyio.create_task_group() as tg:
        tg.start_soon(harness.watcher.run)
        with anyio.fail_after(5):
            await listening.wait()
        assert await ask_watcher("status", days=1) == {
            "running": False,
            "rounds_by_day": rounds_by_day(1),
        }
        with pytest.raises(HueError, match="doesn't know the command"):
            await ask_watcher("dance")
        with pytest.raises(HueError, match="already running"):
            await Watcher(lambda: HueBridge(CONFIG)).run()
        stream = await anyio.connect_unix(watcher_module.socket_path())
        async with stream:
            await stream.send(b"[1, 2]\n")
            assert b"must be a JSON object" in await stream.receive()
        stream = await anyio.connect_unix(watcher_module.socket_path())
        async with stream:
            await stream.send(b"{not json\n")
            assert b"Bad request" in await stream.receive()
        tg.cancel_scope.cancel()


async def test_asking_a_watcher_that_isnt_running_says_how_to_start_it():
    with pytest.raises(HueError) as error:
        await ask_watcher("status")
    assert str(error.value) == NOT_RUNNING


async def test_a_watcher_answering_nonsense_is_reported(runtime_dir):
    async def nonsense(stream: Any) -> None:
        async with stream:
            await stream.receive()
            await stream.send(b'{"surprise": true}\n')

    watcher_module.runtime_dir().mkdir()
    listener = await anyio.create_unix_listener(watcher_module.socket_path())
    async with listener, anyio.create_task_group() as tg:
        tg.start_soon(listener.serve, nonsense)
        with pytest.raises(HueError, match="answered nonsense"):
            await ask_watcher("status")
        tg.cancel_scope.cancel()


def test_without_a_runtime_dir_the_watcher_has_nowhere_to_listen(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    with pytest.raises(HueError, match="XDG_RUNTIME_DIR"):
        watcher_module.socket_path()


def test_rounds_are_counted_per_day_skipping_damaged_lines():
    path = state_dir() / "focus_log.jsonl"
    path.parent.mkdir(parents=True)
    today = next(iter(rounds_by_day(1)))
    path.write_text(
        json.dumps({"date": today}) + "\n{broken\n" + json.dumps({"date": "1999-01-01"}) + "\n"
    )
    assert rounds_by_day(2) == {today: 1, list(rounds_by_day(2))[1]: 0}


@pytest.fixture
def fake_notify_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "notify-send"
    script.write_text(f'#!/bin/sh\necho "$@" >> {tmp_path / "shown"}\nexit ${{FAIL:-0}}\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    return tmp_path / "shown"


async def test_notifications_go_to_the_desktop(fake_notify_send):
    await notify_desktop("Break time", "5 minutes.")
    assert (
        fake_notify_send.read_text()
        == "--app-name=hue-mcp --expire-time=8000 Break time 5 minutes.\n"
    )


async def test_a_failing_notification_is_only_logged(fake_notify_send, monkeypatch, caplog):
    monkeypatch.setenv("FAIL", "1")
    await notify_desktop("Break time", "5 minutes.")
    assert "Couldn't show a desktop notification" in caplog.text


async def test_without_notify_send_there_are_no_notifications(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("PATH", str(tmp_path))
    await notify_desktop("Break time", "5 minutes.")
    assert "notify-send isn't installed" in caplog.text


def test_state_lives_under_the_xdg_state_dir():
    assert state_dir() == Path(os.environ["XDG_STATE_HOME"]) / "hue-mcp"


async def test_claude_changing_the_room_mid_pulse_brings_the_look_back_first(harness, monkeypatch):
    w, live = harness.watcher, harness.live
    monkeypatch.setattr(watcher_module, "PULSE_HOLD_S", 60)  # Caught while red.
    async with anyio.create_task_group() as tg:
        await waiting(harness, tg)
        for _ in range(12):
            harness.advance(1)
            await w.tick(tg)
        await anyio.wait_all_tasks_blocked()
        assert not shows_scene(live, "Pomodoro short break")  # Red right now.
        await w.handle({"command": "touched", "lights": ["light-floor"]})
        assert shows_scene(live, "Pomodoro short break")
        assert live.puts("scene")[-1][1] == {"recall": {"action": "active", "duration": 400}}


async def test_a_new_phase_cancels_a_pulse_without_its_way_back(harness, monkeypatch):
    w, live = harness.watcher, harness.live
    monkeypatch.setattr(watcher_module, "PULSE_HOLD_S", 60)
    async with anyio.create_task_group() as tg:
        await waiting(harness, tg)
        for _ in range(12):
            harness.advance(1)
            await w.tick(tg)
        await anyio.wait_all_tasks_blocked()
        w.tracker.resumed()
        harness.advance(1)
        await w.tick(tg)
    focus = live.scene_named("Pomodoro midday")["id"]
    assert live.puts("scene")[-1] == (focus, {"recall": {"action": "active", "duration": 3000}})
    assert shows_scene(live, "Pomodoro midday")


async def test_a_failed_pulse_is_only_logged(harness, caplog):
    w, live = harness.watcher, harness.live
    async with anyio.create_task_group() as tg:
        await waiting(harness, tg)
        live.write_responses["/clip/v2/resource/grouped_light/gl-living"] = httpx2.Response(500)
        await harness.tick_through(12, tg, idle=True)
    assert "A red pulse in Living room failed" in caplog.text
    assert w.session is not None


async def test_a_failed_check_after_a_switch_is_retried(harness, caplog):
    w, live = harness.watcher, harness.live
    await w.handle({"command": "start", **START})
    calls = {"reads": 0}

    def fail_the_first_check() -> None:
        calls["reads"] += 1
        if calls["reads"] == 1:
            raise httpx2.ReadTimeout("bridge busy")

    async with anyio.create_task_group() as tg:
        live.before_each_read = fail_the_first_check
        await harness.tick_through(7, tg)
        assert "Couldn't check the lights in Living room" in caplog.text
        live.before_each_read = lambda: None
        await harness.tick_through(6, tg)
    assert w._verify_due is None


async def test_a_room_that_couldnt_be_turned_off_is_only_logged(harness, caplog):
    w, live = harness.watcher, harness.live
    async with anyio.create_task_group() as tg:
        await waiting(harness, tg)
        live.write_responses["/clip/v2/resource/grouped_light/gl-living"] = httpx2.Response(500)
        harness.advance(AWAY_LIMIT_S)
        await w.tick(tg)
    assert w.session is None
    assert "Couldn't turn Living room off" in caplog.text


async def test_the_long_break_is_announced_with_the_rounds_done(harness):
    await harness.watcher.handle({"command": "start", **START, "rounds": 1})
    async with anyio.create_task_group() as tg:
        harness.advance(FOCUS_S - 1)
        await harness.tick_through(2, tg)
    assert harness.notifications[-1] == ("Long break", "30 minutes. That was 1 rounds.")


async def test_a_deleted_look_is_reported_not_crashed_on(harness):
    w, live = harness.watcher, harness.live
    await w.handle({"command": "start", **START})
    live.resources.remove(live.scene_named("Pomodoro midday"))
    with pytest.raises(HueError, match="'Pomodoro midday' is gone"):
        await w.handle({"command": "save_look"})
    assert await w.handle({"command": "stop"}) == {
        "stopped": "the pomodoro in Living room",
        "warnings": ["The scene 'Pomodoro midday' is gone, so the lights stay as they are."],
    }


async def test_a_switch_to_a_deleted_look_is_logged(harness, caplog):
    w, live = harness.watcher, harness.live
    await w.handle({"command": "start", **START})
    live.resources.remove(live.scene_named("Pomodoro short break"))
    async with anyio.create_task_group() as tg:
        harness.advance(FOCUS_S - 1)
        await harness.tick_through(2, tg)
    assert "'Pomodoro short break' is gone" in caplog.text


async def test_a_pulse_in_a_room_that_lost_its_look_does_nothing(harness):
    live = harness.live
    async with anyio.create_task_group() as tg:
        await waiting(harness, tg)
        live.resources.remove(live.scene_named("Pomodoro short break"))
        before = len(live.writes)
        await harness.tick_through(12, tg, idle=True)
    assert live.writes[before:] == []


async def test_saving_a_look_for_a_room_that_is_gone_says_so(harness):
    w, live = harness.watcher, harness.live
    await w.handle({"command": "start", **START})
    live.resources.remove(resource(live.resources, "room-living"))
    with pytest.raises(HueError, match="room no longer exists"):
        await w.handle({"command": "save_look"})


async def test_starting_while_another_room_has_no_looks_left_warns(harness):
    w, live = harness.watcher, harness.live
    await w.handle({"command": "start", **START})
    live.resources.remove(live.scene_named("Pomodoro midday"))
    result = await w.handle({"command": "start", **START, "room": "Bedroom", "task_light": None})
    assert result["warnings"] == [
        "The scene 'Pomodoro midday' is gone, so the lights stay as they are."
    ]


async def test_a_failing_tick_is_logged_and_ticking_goes_on(harness, monkeypatch, caplog):
    ticks = []

    async def tick(task_group: Any) -> None:
        ticks.append(1)
        if len(ticks) == 3:
            raise anyio.get_cancelled_exc_class()
        if len(ticks) == 2:
            raise OSError(28, "No space left on device")
        raise HueError("bridge gone")

    monkeypatch.setattr(harness.watcher, "tick", tick)
    monkeypatch.setattr(watcher_module, "TICK_S", 0)
    async with anyio.create_task_group() as tg:
        with pytest.raises(anyio.get_cancelled_exc_class()):
            await harness.watcher._keep_ticking(tg)
    assert "A pomodoro tick failed: bridge gone" in caplog.text
    assert "No space left on device" in caplog.text


async def test_a_watcher_that_hangs_up_or_stalls_is_reported(runtime_dir):
    async def hang_up(stream: Any) -> None:
        await stream.aclose()

    async def stall(stream: Any) -> None:
        async with stream:
            await anyio.sleep_forever()

    watcher_module.runtime_dir().mkdir()
    for server, error in ((hang_up, "Couldn't talk"), (stall, "didn't answer in time")):
        watcher_module.socket_path().unlink(missing_ok=True)
        listener = await anyio.create_unix_listener(watcher_module.socket_path())
        async with listener, anyio.create_task_group() as tg:
            tg.start_soon(listener.serve, server)
            with pytest.raises(HueError, match=error):
                await ask_watcher("status", timeout_s=0.2)
            tg.cancel_scope.cancel()


def test_the_clock_counts_up():
    assert watcher_module.boot_clock() <= watcher_module.boot_clock()


async def test_a_break_turning_to_waiting_keeps_a_change_made_at_the_switch(harness):
    w, live = harness.watcher, harness.live
    await w.handle({"command": "start", **START})
    async with anyio.create_task_group() as tg:
        harness.advance(FOCUS_S - 1)
        await harness.tick_through(10, tg)
        w.tracker.idled(harness.clock.now)
        await harness.tick_through(SHORT_S - 30, tg, idle=True)
        live.set_light("light-floor", {"on": {"on": False}})  # Off at the switch, then away.
        live.set_light("light-ceiling", {"on": {"on": False}})
        before = len(live.writes)
        await harness.tick_through(30, tg, idle=True)
        assert w.session is not None
        assert w.session.phase == "waiting"
        await harness.tick_through(15, tg, idle=True)
    assert live.writes[before:] == []  # Not turned back on, not pulsed.
    assert w.session is None


async def test_red_pulses_run_back_to_back_each_fading_back_with_the_scene(harness):
    live = harness.live
    async with anyio.create_task_group() as tg:
        await waiting(harness, tg)
        before = len(live.writes)
        await harness.tick_through(12, tg, idle=True)
    pulses = [(path.rsplit("/", 2)[-2], body) for _, path, body in live.writes[before:]]
    red_and_back = [
        ("grouped_light", looks.red_pulse_body()),
        ("scene", {"recall": {"action": "active", "duration": 2000}}),
    ]
    assert len(pulses) >= 4
    assert pulses == red_and_back * (len(pulses) // 2)


async def test_a_light_still_reported_midway_back_from_a_pulse_isnt_taken_for_a_change(harness):
    w, live = harness.watcher, harness.live
    async with anyio.create_task_group() as tg:
        await waiting(harness, tg)
        floor_look = next(
            action["action"]
            for action in live.scene_named("Pomodoro short break")["actions"]
            if action["target"]["rid"] == "light-floor"
        )
        # How a real bridge went on reporting a bulb for about 4 s after a red pulse's way back.
        midway = {"color": {"xy": {"x": 0.353, "y": 0.222}}, "dimming": {"brightness": 32}}
        live.set_light("light-floor", midway)
        reads = 0

        def catch_up() -> None:
            nonlocal reads
            reads += 1
            if reads == 3:
                live.set_light("light-floor", floor_look)

        live.before_each_read = catch_up
        await harness.tick_through(12, tg, idle=True)
    assert w.session is not None
    assert w.session.phase == "waiting"
    assert shows_scene(live, "Pomodoro short break")


async def test_a_pulse_that_couldnt_get_back_is_restored_later(harness):
    w, live = harness.watcher, harness.live
    await w.handle({"command": "start", **START})
    short_break = live.scene_named("Pomodoro short break")["id"]
    async with anyio.create_task_group() as tg:
        harness.advance(FOCUS_S - 1)
        await harness.tick_through(10, tg)
        live.write_responses[f"/clip/v2/resource/scene/{short_break}"] = httpx2.Response(500)
        await harness.tick_through(52, tg)  # The breath, 60 s in; its way back fails.
        assert not shows_scene(live, "Pomodoro short break")
        del live.write_responses[f"/clip/v2/resource/scene/{short_break}"]
        await harness.tick_through(6, tg)
    assert shows_scene(live, "Pomodoro short break")


async def test_the_watcher_restarting_while_the_user_is_away_doesnt_start_a_round(harness):
    w = harness.watcher
    async with anyio.create_task_group() as tg:
        await waiting(harness, tg)
        w.tracker.lost()
        w.tracker.connected(harness.clock.now)
        await harness.tick_through(5, tg)
        assert w.session is not None
        assert w.session.phase == "waiting"
        w.tracker.idled(harness.clock.now)  # Still nobody there.
        await harness.tick_through(5, tg)
        assert w.session.phase == "waiting"


async def test_a_task_light_named_later_gets_its_white_in_the_focus_looks(harness):
    w, live = harness.watcher, harness.live
    await w.handle({"command": "start", **START, "task_light": None})

    def floor_in(scene: str) -> dict[str, Any]:
        actions = live.scene_named(scene)["actions"]
        return next(a for a in actions if a["target"]["rid"] == "light-floor")["action"]

    assert "color" in floor_in("Pomodoro night")
    await w.handle({"command": "start", **START, "task_light": "Floor lamp"})
    assert "color_temperature" in floor_in("Pomodoro night")
    assert "color" in floor_in("Pomodoro short break")  # Breaks have no task light.


async def test_a_light_added_to_the_room_joins_the_looks(harness):
    w, live = harness.watcher, harness.live
    await w.handle({"command": "start", **START})
    resource(live.resources, "room-living")["children"].append(
        {"rid": "dev-bedside", "rtype": "device"}
    )
    await w.handle({"command": "start", **START})
    actions = live.scene_named("Pomodoro short break")["actions"]
    assert "light-bedside" in [a["target"]["rid"] for a in actions]


@pytest.mark.parametrize(
    ("request_", "error"),
    [
        ({**START, "room": 5}, "needs the name of a room"),
        ({**START, "task_light": ["Desk"]}, "task_light must be the name"),
        ({**START, "focus_minutes": "25"}, "focus_minutes must be more than 0"),
        ({**START, "short_break_minutes": 0}, "short_break_minutes must be more than 0"),
        ({**START, "long_break_minutes": 121}, "at most 120"),
        ({**START, "rounds": 9}, "rounds must be a whole number"),
        ({**START, "rounds": 2.5}, "rounds must be a whole number"),
    ],
)
async def test_a_start_request_is_checked_before_anything_happens(harness, request_, error):
    with pytest.raises(HueError, match=error):
        await harness.watcher.handle({"command": "start", **request_})
    assert harness.watcher.session is None
    assert harness.live.writes == []


@pytest.mark.parametrize(
    ("request_", "error"),
    [
        ({"command": "status", "days": 0}, "days must be"),
        ({"command": "status", "days": 10**9}, "days must be"),
        ({"command": "touched", "lights": "light-floor"}, "list of light ids"),
        ({"command": "touched"}, "list of light ids"),
    ],
)
async def test_other_requests_are_checked_too(harness, request_, error):
    with pytest.raises(HueError, match=error):
        await harness.watcher.handle(request_)


async def test_a_request_that_fails_unexpectedly_is_answered_not_fatal(harness, monkeypatch):
    async def broken(request: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("surprise")

    monkeypatch.setattr(harness.watcher, "handle", broken)
    listening = anyio.Event()
    real_restore = harness.watcher.restore

    def restore_then_say_so() -> None:
        real_restore()
        listening.set()

    monkeypatch.setattr(harness.watcher, "restore", restore_then_say_so)
    async with anyio.create_task_group() as tg:
        tg.start_soon(harness.watcher.run)
        await listening.wait()
        with pytest.raises(HueError, match="The watcher failed: RuntimeError"):
            await ask_watcher("status")
        with pytest.raises(HueError, match="The watcher failed"):  # Still up, still answering.
            await ask_watcher("status")
        tg.cancel_scope.cancel()


async def test_a_long_phase_keeps_its_saved_state_fresh(harness):
    w = harness.watcher
    await w.handle({"command": "start", **START})
    path = state_dir() / "pomodoro.json"
    saved = json.loads(path.read_text())["saved_at_wall"]
    async with anyio.create_task_group() as tg:
        await harness.tick_through(61, tg)
    assert json.loads(path.read_text())["saved_at_wall"] > saved


# Light shows: run_show is replaced by a stand-in that plays until told why to end.


STATE_FILES = ("light_show.json", "pomodoro.json")


class FakeShow:
    """Stands in for show.run_show; `fail` makes it fail before or after starting."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_before_start: Exception | None = None

    async def __call__(self, bridge, area, mode, placements, should_end, started, order) -> str:
        self.calls.append((mode, placements))
        if self.fail_before_start is not None:
            raise self.fail_before_start
        started()
        while True:  # Like the real show: one look at should_end per frame.
            if (reason := should_end()) is not None:
                return reason
            await anyio.sleep(0.01)


@pytest.fixture
def fake_show(monkeypatch: pytest.MonkeyPatch) -> FakeShow:
    fake = FakeShow()
    monkeypatch.setattr(watcher_module, "run_show", fake)
    return fake


def light_state(live: LiveBridge, light_id: str) -> tuple[Any, Any]:
    light = resource(live.resources, light_id)
    return light["on"]["on"], light["dimming"]["brightness"]


async def test_a_show_pauses_the_pomodoro_and_hands_the_room_back_after(harness, fake_show):
    w, live = harness.watcher, harness.live
    await w.handle({"command": "start", **START})
    before = {light: light_state(live, light) for light in ("light-floor", "light-ceiling")}
    async with anyio.create_task_group() as tg:
        w._task_group = tg
        result = await w.handle({"command": "show_start", "mode": "music"})
        assert result == {
            "playing": "the music light show",
            "area": "Movie area",
            "note": "The pomodoro is paused, and carries on when the show ends.",
        }
        assert w.session is not None
        assert w.session.paused_at is not None
        assert (state_dir() / "light_show.json").exists()
        status = await w.handle({"command": "status", "days": 1})
        assert status["light_show"]["mode"] == "music"
        assert "Paused while a light show runs" in status["note"]
        live.set_light("light-floor", {"dimming": {"brightness": 3}})  # What a show does.
        assert await w.handle({"command": "show_stop"}) == {"stopped": "the music light show"}
    assert {light: light_state(live, light) for light in before} == before
    assert w.session.paused_at is None
    assert not (state_dir() / "light_show.json").exists()
    with pytest.raises(HueError, match="No light show is running"):
        await w.handle({"command": "show_stop"})


async def test_a_paused_pomodoros_clock_stands_still(harness, fake_show):
    w = harness.watcher
    await w.handle({"command": "start", **START})
    async with anyio.create_task_group() as tg:
        w._task_group = tg
        await w.handle({"command": "show_start", "mode": "screen"})
        harness.advance(FOCUS_S * 3)
        await w.tick(tg)
        assert w.session is not None
        assert w.session.phase == "focus"  # Not a break: the clock stood still.
        await w.handle({"command": "show_stop"})
    assert w.session.deadline() == pytest.approx(harness.clock.now + FOCUS_S)


async def test_a_show_that_cant_start_says_why_and_leaves_no_trace(harness, fake_show):
    w = harness.watcher
    await w.handle({"command": "start", **START})
    fake_show.fail_before_start = HueError("The screen wasn't shared.")
    async with anyio.create_task_group() as tg:
        w._task_group = tg
        with pytest.raises(HueError, match="wasn't shared"):
            await w.handle({"command": "show_start", "mode": "screen"})
    assert w._show is None
    assert w.session is not None
    assert w.session.paused_at is None
    assert harness.notifications[-1][0] != "Light show ended"  # The reply said it already.


async def test_a_crash_inside_the_show_ends_it_not_the_watcher(harness, fake_show):
    w = harness.watcher
    fake_show.fail_before_start = RuntimeError("boom")
    async with anyio.create_task_group() as tg:
        w._task_group = tg
        with pytest.raises(HueError, match="RuntimeError"):
            await w.handle({"command": "show_start", "mode": "music"})
    assert w._show is None


async def test_fifteen_minutes_away_ends_the_show_with_a_note(harness, fake_show):
    w = harness.watcher
    async with anyio.create_task_group() as tg:
        w._task_group = tg
        await w.handle({"command": "show_start", "mode": "music"})
        assert w._show is not None
        run = w._show
        w.tracker.idled(harness.clock.now)
        harness.advance(watcher_module.SHOW_AWAY_LIMIT_S + 5)
        with anyio.fail_after(5):
            await run.done.wait()
    assert harness.notifications[-1] == (
        "Light show ended",
        "You were away for 15 minutes, so the light show ended.",
    )


async def test_a_suspend_ends_the_show(harness, fake_show, monkeypatch):
    monkeypatch.setattr(watcher_module, "RESUME_HOLD_S", 0)  # The hand-back waits it out.
    w = harness.watcher
    async with anyio.create_task_group() as tg:
        w._task_group = tg
        await w.handle({"command": "show_start", "mode": "music"})
        assert w._show is not None
        run = w._show
        await w.tick(tg)
        harness.suspend(600)
        await w.tick(tg)
        with anyio.fail_after(5):
            await run.done.wait()
    assert "slept" in harness.notifications[-1][1]


async def test_a_new_show_replaces_the_running_one(harness, fake_show):
    w = harness.watcher
    async with anyio.create_task_group() as tg:
        w._task_group = tg
        await w.handle({"command": "show_start", "mode": "music"})
        first = w._show
        await w.handle({"command": "show_start", "mode": "both"})
        assert first is not None
        assert first.done.is_set()
        assert w._show is not None
        assert w._show.mode == "both"
        await w.handle({"command": "show_stop"})


async def test_claude_changing_a_show_light_hears_that_a_show_is_on(harness, fake_show):
    w = harness.watcher
    async with anyio.create_task_group() as tg:
        w._task_group = tg
        await w.handle({"command": "show_start", "mode": "screen"})
        assert await w.handle({"command": "touched", "lights": ["light-floor"]}) == {
            "show": "screen"
        }
        await w.handle({"command": "show_stop"})


@pytest.mark.parametrize(
    ("request_", "error"),
    [
        ({"mode": "disco"}, "mode must be one of"),
        ({"mode": "music", "room": 3}, "room must be"),
        ({"mode": "music", "room": "Bedroom"}, "No entertainment area holds"),
    ],
)
async def test_a_show_request_is_checked(harness, fake_show, request_, error):
    w = harness.watcher
    async with anyio.create_task_group() as tg:
        w._task_group = tg
        with pytest.raises(HueError, match=error):
            await w.handle({"command": "show_start", **request_})


async def test_a_show_needs_the_watchers_tasks(harness):
    with pytest.raises(HueError, match="isn't running its tasks"):
        await harness.watcher.handle({"command": "show_start", "mode": "music"})


async def test_without_an_area_or_with_several_the_room_must_be_named(harness, fake_show):
    w, live = harness.watcher, harness.live
    async with anyio.create_task_group() as tg:
        w._task_group = tg
        second = {**resource(live.resources, AREA_ID), "id": AREA_ID.replace("1", "2")}
        live.resources.append(second)
        with pytest.raises(HueError, match="Several entertainment areas"):
            await w.handle({"command": "show_start", "mode": "music"})
        live.resources[:] = [
            r for r in live.resources if r["type"] != "entertainment_configuration"
        ]
        with pytest.raises(HueError, match="no entertainment area"):
            await w.handle({"command": "show_start", "mode": "music"})


async def test_lights_are_placed_and_the_placements_reach_the_show(harness, fake_show):
    w = harness.watcher
    placed = await w.handle(
        {
            "command": "show_place",
            "light": "Floor lamp",
            "region": "left",
            "screen": 2,
            "max_brightness": 60,
        }
    )
    assert placed == {
        "placed": "Floor lamp",
        "follows": "left of screen 2 from the left",
        "max_brightness": 60,
    }
    await w.handle({"command": "show_place", "light": "Ceiling", "region": "whole"})
    async with anyio.create_task_group() as tg:
        w._task_group = tg
        await w.handle({"command": "show_start", "mode": "screen"})
        await w.handle({"command": "show_stop"})
    [(_, placements)] = fake_show.calls
    assert placements == {
        "light-floor": watcher_module.Placement("left", 2, 0.6),
        "light-ceiling": watcher_module.Placement("whole", None, 1.0),
    }


@pytest.mark.parametrize(
    ("request_", "error"),
    [
        ({"light": 5, "region": "left"}, "name of a light"),
        ({"light": "Ceiling", "region": "sky"}, "region must be one of"),
        ({"light": "Ceiling", "region": "left", "screen": 0}, "screen must be"),
        ({"light": "Ceiling", "region": "left", "max_brightness": "lots"}, "whole percentage"),
        ({"light": "Ceiling", "region": "left", "max_brightness": 0}, "from 1 to 100"),
    ],
)
async def test_a_placement_is_checked(harness, request_, error):
    with pytest.raises(HueError, match=error):
        await harness.watcher.handle({"command": "show_place", **request_})


def test_damaged_placements_are_skipped():
    from hue_mcp.config import save_placements

    save_placements(
        {
            "good": {"region": "top", "screen": 1, "max_brightness": 0.5},
            "bad-region": {"region": "sky"},
            "bad-screen": {"region": "top", "screen": "one"},
            "bad-brightness": {"region": "top", "max_brightness": 2},
        }
    )
    assert watcher_module._placements() == {"good": watcher_module.Placement("top", 1, 0.5)}


async def test_a_show_cut_off_by_a_restart_hands_the_room_back(harness, fake_show):
    w, live = harness.watcher, harness.live
    await w.handle({"command": "start", **START})
    async with anyio.create_task_group() as tg:
        w._task_group = tg
        await w.handle({"command": "show_start", "mode": "music"})
        saved = {name: (state_dir() / name).read_text() for name in STATE_FILES}
        await w.handle({"command": "show_stop"})
    for name, text in saved.items():  # As if the watcher had crashed mid-show.
        (state_dir() / name).write_text(text)
    live.set_light("light-floor", {"on": {"on": False}})
    restarted = Harness(live)
    restarted.watcher.restore()
    assert restarted.watcher.session is not None
    assert restarted.watcher.session.paused_at is not None
    await restarted.watcher.recover_show()
    assert light_state(live, "light-floor")[0] is True
    assert restarted.watcher.session.paused_at is None
    stops = [body for _, path, body in live.writes if path.endswith(AREA_ID)]
    assert stops[-1] == {"action": "stop"}
    assert not (state_dir() / "light_show.json").exists()


async def test_an_unreadable_saved_show_is_dropped(harness):
    path = state_dir() / "light_show.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{oops")
    await harness.watcher.recover_show()
    assert not path.exists()


async def test_the_lights_order_is_saved_and_reaches_the_show(harness, monkeypatch):
    seen = []

    async def recording(bridge, area, mode, placements, should_end, started, order) -> str:
        seen.append(order)
        started()
        return "done"

    monkeypatch.setattr(watcher_module, "run_show", recording)
    w = harness.watcher
    assert await w.handle({"command": "show_order", "lights": ["Ceiling", "Floor lamp"]}) == {
        "order": ["Ceiling", "Floor lamp"]
    }
    async with anyio.create_task_group() as tg:
        w._task_group = tg
        await w.handle({"command": "show_start", "mode": "cpu"})
    assert seen == [["light-ceiling", "light-floor"]]


@pytest.mark.parametrize(
    ("lights", "error"),
    [
        ([], "list of light names"),
        ("Ceiling", "list of light names"),
        (["Ceiling", "Ceiling"], "twice"),
    ],
)
async def test_an_order_is_checked(harness, lights, error):
    with pytest.raises(HueError, match=error):
        await harness.watcher.handle({"command": "show_order", "lights": lights})


@pytest.mark.parametrize("mode", ["cpu", "race"])
async def test_a_monitor_show_stays_on_while_nobody_is_there(harness, fake_show, mode):
    w = harness.watcher
    async with anyio.create_task_group() as tg:
        w._task_group = tg
        await w.handle({"command": "show_start", "mode": mode})
        assert w._show is not None
        w.tracker.idled(harness.clock.now)
        harness.advance(watcher_module.SHOW_AWAY_LIMIT_S * 4)
        assert w._show_should_end(w._show) is None
        await w.handle({"command": "show_stop"})


class SlowShow(FakeShow):
    """A show whose start waits until released, like one waiting on the screen dialog."""

    def __init__(self) -> None:
        super().__init__()
        self.release = anyio.Event()

    async def __call__(self, bridge, area, mode, placements, should_end, started, order) -> str:
        await self.release.wait()
        return await super().__call__(bridge, area, mode, placements, should_end, started, order)


async def test_two_starts_at_once_leave_one_show_and_nothing_orphaned(harness, monkeypatch):
    slow = SlowShow()
    monkeypatch.setattr(watcher_module, "run_show", slow)
    w = harness.watcher
    replies: list[Any] = []
    both_answered = anyio.Event()

    async def start(mode: str) -> None:
        try:
            replies.append(await w.handle({"command": "show_start", "mode": mode}))
        except HueError as error:
            replies.append(str(error))
        if len(replies) == 2:
            both_answered.set()

    async with anyio.create_task_group() as tg:
        w._task_group = tg
        tg.start_soon(start, "music")
        await anyio.wait_all_tasks_blocked()
        tg.start_soon(start, "cpu")
        await anyio.wait_all_tasks_blocked()
        slow.release.set()
        with anyio.fail_after(5):
            await both_answered.wait()
        assert w._show is not None
        assert w._show.mode == "cpu"
        await w.handle({"command": "show_stop"})
    assert "stopped before it started" in replies[0]
    assert replies[1]["playing"] == "the cpu light show"
    assert w._show is None


async def test_a_pomodoro_started_during_a_show_waits_for_it(harness, fake_show):
    w, live = harness.watcher, harness.live
    async with anyio.create_task_group() as tg:
        w._task_group = tg
        await w.handle({"command": "show_start", "mode": "music"})
        status = await w.handle({"command": "start", **START})
        assert "Paused while a light show runs" in status["note"]
        assert live.puts("scene") == []  # The show has the lights.
        await w.handle({"command": "show_stop"})
        await w.tick(tg)
    assert w.session is not None
    assert w.session.paused_at is None
    assert shows_scene(live, "Pomodoro midday")


async def test_a_failed_hand_back_neither_hangs_nor_kills_the_watcher(harness, fake_show, caplog):
    w = harness.watcher

    async def broken(snapshot: Any) -> None:
        raise OSError("disk full")

    async with anyio.create_task_group() as tg:
        w._task_group = tg
        await w.handle({"command": "show_start", "mode": "music"})
        w._restore_room = broken  # type: ignore[method-assign]
        with anyio.fail_after(5):
            await w.handle({"command": "show_stop"})
    assert "Handing the room back after the light show failed" in caplog.text


async def test_a_pomodoro_left_paused_without_a_show_carries_on_at_start(harness, fake_show):
    w = harness.watcher
    await w.handle({"command": "start", **START})
    assert w.session is not None
    w.session.pause(harness.clock.now)
    w._save()
    restarted = Harness(harness.live)
    restarted.watcher.restore()
    await restarted.watcher.recover_show()
    assert restarted.watcher.session is not None
    assert restarted.watcher.session.paused_at is None


async def test_a_change_across_a_show_and_a_pomodoro_is_handled_for_both(harness, fake_show):
    w, live = harness.watcher, harness.live
    resource(live.resources, "room-living")["children"] = [{"rid": "dev-floor", "rtype": "device"}]
    await w.handle({"command": "start", **START, "task_light": None})
    async with anyio.create_task_group() as tg:
        w._task_group = tg
        area_lights = resource(live.resources, AREA_ID)["channels"]
        area_lights.pop(0)  # The show takes only the ceiling light.
        await w.handle({"command": "show_start", "mode": "cpu"})
        reply = await w.handle({"command": "touched", "lights": ["light-floor", "light-ceiling"]})
        assert reply == {"show": "cpu", "room": "Living room", "look": "midday"}
        await w.handle({"command": "show_stop"})


async def test_a_damaged_placements_file_is_started_over(harness):
    from hue_mcp.config import placements_path

    placements_path().parent.mkdir(parents=True, exist_ok=True)
    placements_path().write_text("{oops")
    placed = await harness.watcher.handle(
        {"command": "show_place", "light": "Ceiling"} | {"region": "top"}
    )
    assert placed["placed"] == "Ceiling"


async def test_a_light_that_missed_a_dips_way_back_is_resent_not_taken_for_the_switch(harness):
    w, live = harness.watcher, harness.live
    async with anyio.create_task_group() as tg:
        await waiting(harness, tg)
        brightness = resource(live.resources, "light-floor")["dimming"]["brightness"]
        live.set_light("light-floor", {"dimming": {"brightness": max(1, brightness - 30)}})
        await harness.tick_through(12, tg, idle=True)
    assert w.session is not None  # Still waiting, not ended as "changed at the switch".
    assert shows_scene(live, "Pomodoro short break")


async def test_a_light_that_missed_the_first_switch_gets_it_again(harness):
    w, live = harness.watcher, harness.live
    live.deaf = {"light-floor"}
    await w.handle({"command": "start", **START})
    live.deaf = set()
    assert not shows_scene(live, "Pomodoro midday")
    async with anyio.create_task_group() as tg:
        await harness.tick_through(7, tg)
    assert shows_scene(live, "Pomodoro midday")
