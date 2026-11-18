from typing import Any, ClassVar

import anyio
import pytest

from hue_mcp import show
from hue_mcp.audio import SILENCE, Sound
from hue_mcp.entertainment import Area
from hue_mcp.errors import HueError
from hue_mcp.race import Look
from hue_mcp.show import Placement, Renderer, run_show

pytestmark = pytest.mark.anyio

RED, BLUE, GREEN, BLACK = (1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, 1.0, 0.0), (0.0, 0.0, 0.0)
SCREENS: Any = {
    1: {"whole": RED, "left": RED, "right": RED},
    2: {"whole": BLUE, "left": GREEN, "right": BLUE},
}


def run_frames(renderer: Renderer, seconds: float, sound: Sound = SILENCE, screens: Any = None):
    colors = {}
    for step in range(round(seconds / 0.02)):
        colors = renderer.frame(step * 0.02, sound, screens or {})
    return colors


def test_screen_lights_follow_their_monitor_and_region():
    renderer = Renderer(
        "screen",
        {
            "a": Placement("whole", 1),
            "b": Placement("left", 2),
            "c": Placement("right", 2),
            "d": Placement("whole", 9),  # A monitor that isn't shared: dark.
        },
    )
    colors = run_frames(renderer, 2, screens=SCREENS)
    assert colors["a"] == pytest.approx(RED, abs=0.01)
    assert colors["b"] == pytest.approx(GREEN, abs=0.01)
    assert colors["c"] == pytest.approx(BLUE, abs=0.01)
    assert colors["d"] == BLACK


def test_a_light_on_no_monitor_averages_them_all():
    colors = run_frames(Renderer("screen", {"a": Placement()}), 2, screens=SCREENS)
    assert colors["a"] == pytest.approx((0.5, 0.0, 0.5), abs=0.02)  # Half red, half blue.


def test_screen_colors_are_followed_smoothly():
    renderer = Renderer("screen", {"a": Placement()})
    first = renderer.frame(0.0, SILENCE, {1: {"whole": RED}})["a"]
    second = renderer.frame(0.02, SILENCE, {1: {"whole": RED}})["a"]
    assert max(first) == 0.0  # The first frame starts from dark...
    assert 0 < max(second) < 0.5  # ...and rises over about 150 ms, not at once.


def test_a_brightness_cap_holds():
    colors = run_frames(Renderer("screen", {"a": Placement("whole", 1, 0.6)}), 2, screens=SCREENS)
    assert max(colors["a"]) == pytest.approx(0.6)


def test_music_brightens_with_loudness_and_spreads_colors():
    renderer = Renderer("music", {"a": Placement(), "b": Placement()})
    quiet = run_frames(renderer, 1, Sound(0.1, 0.1, False))
    loud = run_frames(
        Renderer("music", {"a": Placement(), "b": Placement()}), 1, Sound(1, 1, False)
    )
    assert max(loud["a"]) > max(quiet["a"])
    assert loud["a"] != loud["b"]  # Neighbors take different colors.


def test_a_beat_punches_then_fades():
    renderer = Renderer("music", {"a": Placement()})
    run_frames(renderer, 1, Sound(0.2, 0.2, False))
    before = max(renderer.frame(1.0, Sound(0.2, 0.2, False), {})["a"])
    punched = max(renderer.frame(1.5, Sound(0.2, 0.2, True), {})["a"])
    later = max(renderer.frame(2.5, Sound(0.2, 0.2, False), {})["a"])
    assert punched > before + 0.3
    assert later < punched


def test_both_takes_the_screens_hue_and_the_musics_brightness():
    renderer = Renderer("both", {"a": Placement("whole", 1)})
    colors = run_frames(renderer, 2, Sound(0.2, 0.2, False), SCREENS)["a"]
    assert colors[1] == colors[2] == 0.0  # Red's hue...
    assert colors[0] == pytest.approx(0.22, abs=0.01)  # ...at the music's brightness.
    silent = run_frames(Renderer("both", {"a": Placement("whole", 1)}), 2, SILENCE, SCREENS)
    assert silent["a"] == pytest.approx(RED, abs=0.01)  # Silent: just the screen.


def test_both_over_a_grey_screen_glows_warm_white():
    renderer = Renderer("both", {"a": Placement()})
    grey: Any = {1: {"whole": (0.3, 0.3, 0.3)}}
    color = run_frames(renderer, 2, Sound(1, 1, False), grey)["a"]
    assert color[0] > color[1] > color[2] > 0


@pytest.mark.parametrize("strobe_hz", [4, 6, 12.5])
def test_flashes_come_at_most_three_a_second_whatever_the_screen_does(strobe_hz):
    """Through the real smoothing: a flash is a rise of FLASH_STEP over the darkest of the
    last FLASH_GAP_S, however many frames it takes."""
    renderer = Renderer("screen", {"a": Placement()})
    shown: list[tuple[float, float]] = []
    flashes, flashing = 0, False
    for step in range(round(4 / 0.02)):
        now = step * 0.02
        lit = (now * strobe_hz) % 1 < 0.5
        screens: Any = {1: {"whole": RED if lit else BLACK}}
        brightness = max(renderer.frame(now, SILENCE, screens)["a"])
        low = min((b for t, b in shown if t > now - show.FLASH_GAP_S), default=brightness)
        rising = brightness - low >= show.FLASH_STEP
        flashes += rising and not flashing
        flashing = rising
        shown.append((now, brightness))
    assert flashes <= 3 * 4 + 1


def test_a_held_light_isnt_stuck_dim():
    renderer = Renderer("screen", {"a": Placement()})
    screens: Any = {1: {"whole": RED}}
    colors = run_frames(renderer, 3, screens=screens)
    assert max(colors["a"]) == pytest.approx(1.0, abs=0.01)


class FakeStream:
    instances: ClassVar[list["FakeStream"]] = []
    ours = True

    def __init__(self, bridge: Any, area: Area):
        self.sent: list[dict[str, Any]] = []
        FakeStream.instances.append(self)

    async def __aenter__(self) -> "FakeStream":
        return self

    async def __aexit__(self, *_: object) -> None:
        pass

    def send(self, colors: dict[str, Any]) -> None:
        self.sent.append(colors)

    async def still_ours(self) -> bool:
        return FakeStream.ours


class FakeScreen:
    share = True

    def __init__(self) -> None:
        self.colors: Any = {}
        self.sharing = anyio.Event()

    async def run(self) -> None:
        if FakeScreen.share:
            self.colors = SCREENS
            self.sharing.set()
        await anyio.sleep_forever()


class FakeListener:
    def __init__(self) -> None:
        self.sound = Sound(0.5, 0.5, False)
        self.beats = 0

    async def run(self) -> None:
        await anyio.sleep_forever()


@pytest.fixture(autouse=True)
def fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeStream.instances, FakeStream.ours, FakeScreen.share = [], True, True
    monkeypatch.setattr(show, "Stream", FakeStream)
    monkeypatch.setattr(show, "Screen", FakeScreen)
    monkeypatch.setattr(show, "Listener", FakeListener)


AREA = Area("a1b2c3d4-0000-4000-8000-000000000001", "Area", {0: "light-a", 1: "light-b"})


async def test_a_show_plays_until_told_why_it_ends():
    started, frames = [], 0

    def should_end() -> str | None:
        nonlocal frames
        frames += 1
        return "enough" if frames > 10 else None

    reason = await run_show(
        None,
        AREA,
        "both",
        {"light-a": Placement("whole", 1)},
        should_end,
        lambda: started.append(1),
    )  # type: ignore[arg-type]
    assert reason == "enough"
    assert started == [1]
    [stream] = FakeStream.instances
    assert len(stream.sent) == 10
    assert set(stream.sent[-1]) == {"light-a", "light-b"}


async def test_a_show_ends_when_the_bridge_takes_the_lights_back(monkeypatch):
    monkeypatch.setattr(show, "CHECK_EVERY_S", 0)
    FakeStream.ours = False
    reason = await run_show(None, AREA, "music", {}, lambda: None, lambda: None)  # type: ignore[arg-type]
    assert "Another app took over" in reason


async def test_an_unshared_screen_ends_the_start(monkeypatch):
    monkeypatch.setattr(show, "SHARE_TIMEOUT_S", 0.05)
    FakeScreen.share = False
    with pytest.raises(HueError, match="wasn't shared"):
        await run_show(None, AREA, "screen", {}, lambda: None, lambda: None)  # type: ignore[arg-type]
    assert FakeStream.instances == []  # The lights were never taken.


def test_cpu_lights_show_the_machines_load_blue_when_idle_red_when_busy():
    busy, idle = Renderer("cpu", {"a": Placement()}), Renderer("cpu", {"a": Placement()})
    for step in range(200):
        busy_color = busy.frame(step * 0.02, SILENCE, {}, pressure=(1.0, 1.0))["a"]
        idle_color = idle.frame(step * 0.02, SILENCE, {}, pressure=(0.0, 0.0))["a"]
    assert busy_color[0] > 0.8
    assert busy_color[1] < 0.05  # Busy: red, and bright.
    assert idle_color[2] > idle_color[0]
    assert max(idle_color) == pytest.approx(0.2, abs=0.02)  # Idle: blue, and dim.


async def test_a_cpu_show_reads_the_meter(monkeypatch):
    class FakeMeter:
        def __init__(self) -> None:
            self.pressure = (0.5, 0.25)

        async def run(self) -> None:
            await anyio.sleep_forever()

    monkeypatch.setattr(show, "CpuMeter", FakeMeter)
    frames = 0

    def should_end() -> str | None:
        nonlocal frames
        frames += 1
        return "enough" if frames > 3 else None

    assert await run_show(None, AREA, "cpu", {}, should_end, lambda: None) == "enough"  # type: ignore[arg-type]


def test_race_lights_glide_to_the_karts_look():
    renderer = Renderer("race", {"a": Placement(), "b": Placement(max_brightness=0.3)})
    green = Look((0.0, 1.0, 0.0), 0.8)
    for step in range(100):
        colors = renderer.frame(step * 0.02, SILENCE, {}, race=green)
    assert colors["a"] == pytest.approx((0.0, 0.8, 0.0), abs=0.01)
    assert colors["b"] == pytest.approx((0.0, 0.3, 0.0), abs=0.01)  # Its cap still holds.
    assert Renderer("race", {"a": Placement()}).frame(0, SILENCE, {})["a"] == BLACK


class FakeRace:
    subscribes = True

    def __init__(self) -> None:
        self.ready = anyio.Event()

    def look(self, now: float) -> Look:
        return Look((1.0, 0.0, 0.0), 1.0)

    async def run(self) -> None:
        if FakeRace.subscribes:
            self.ready.set()
        await anyio.sleep_forever()


async def test_a_race_show_follows_the_kart(monkeypatch):
    monkeypatch.setattr(show, "Race", FakeRace)
    FakeRace.subscribes = True
    frames = 0

    def should_end() -> str | None:
        nonlocal frames
        frames += 1
        return "enough" if frames > 30 else None

    assert await run_show(None, AREA, "race", {}, should_end, lambda: None) == "enough"  # type: ignore[arg-type]
    [stream] = FakeStream.instances
    red, green, blue = stream.sent[-1]["light-a"]
    assert red > 0.2  # Rising from black, held back by the flash limiter.
    assert green == blue == 0.0


async def test_a_race_helper_that_never_subscribes_ends_the_start(monkeypatch):
    monkeypatch.setattr(show, "Race", FakeRace)
    monkeypatch.setattr(show, "RACE_START_TIMEOUT_S", 0.05)
    FakeRace.subscribes = False
    with pytest.raises(HueError, match="didn't subscribe"):
        await run_show(None, AREA, "race", {}, lambda: None, lambda: None)  # type: ignore[arg-type]
    assert FakeStream.instances == []  # The lights were never taken.


def test_lights_play_in_the_users_order_then_the_rest():
    area = Area(AREA.id, "Area", {0: "a", 1: "b", 2: "c", 3: "d"})
    assert show.in_order(area, ["c", "a", "unknown"]) == ["c", "a", "b", "d"]
    assert show.in_order(area, None) == ["a", "b", "c", "d"]


@pytest.mark.parametrize(
    ("load", "dominant"),
    [(0.0, "blue"), (0.2, "cyan"), (0.4, "green"), (0.6, "yellow"), (0.8, "orange"), (1.0, "red")],
)
def test_the_load_scale_reads_like_a_weather_map(load, dominant):
    r, g, b = show._load_color(load, 1.0)
    looks_like = {
        "blue": b > max(r, g),
        "cyan": g > r and b > r,
        "green": g > max(r, b),
        "yellow": r > b and g > b and abs(r - g) < 0.2,
        "orange": r > g > b,
        "red": r > 4 * max(g, b),
    }
    assert looks_like[dominant]


def test_a_burst_colors_the_lights_but_only_sustained_work_brightens_them():
    burst, sustained = Renderer("cpu", {"a": Placement()}), Renderer("cpu", {"a": Placement()})
    for step in range(200):
        burst_color = burst.frame(step * 0.02, SILENCE, {}, pressure=(1.0, 0.0))["a"]
        sustained_color = sustained.frame(step * 0.02, SILENCE, {}, pressure=(1.0, 1.0))["a"]
    assert burst_color[0] > 4 * max(burst_color[1:])  # Red already...
    assert max(burst_color) == pytest.approx(0.2, abs=0.02)  # ...but still dim.
    assert max(sustained_color) == pytest.approx(0.85, abs=0.02)


async def test_each_beat_reaches_exactly_one_frame(monkeypatch):
    seen: list[bool] = []
    real_frame = Renderer.frame

    def recording(self, now, sound, screens, pressure=None, race=None):
        seen.append(sound.beat)
        return real_frame(self, now, sound, screens, pressure, race)

    monkeypatch.setattr(Renderer, "frame", recording)
    listeners: list[FakeListener] = []

    class BeatingListener(FakeListener):
        def __init__(self) -> None:
            super().__init__()
            listeners.append(self)

    monkeypatch.setattr(show, "Listener", BeatingListener)
    frames = 0

    def should_end() -> str | None:
        nonlocal frames
        frames += 1
        if frames == 3:
            listeners[0].beats = 2  # Two beats between frames: one frame shows them.
        return "enough" if frames > 6 else None

    await run_show(None, AREA, "music", {}, should_end, lambda: None)  # type: ignore[arg-type]
    assert seen.count(True) == 1


async def test_an_unexpected_failure_comes_out_as_itself(monkeypatch):
    class BrokenListener(FakeListener):
        async def run(self) -> None:
            raise OSError("parec vanished")

    monkeypatch.setattr(show, "Listener", BrokenListener)
    with pytest.raises(OSError, match="parec vanished"):
        await run_show(None, AREA, "music", {}, lambda: None, lambda: None)  # type: ignore[arg-type]
