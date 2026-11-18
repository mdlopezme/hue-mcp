import ast
from pathlib import Path

import pytest

from hue_mcp import race, show
from hue_mcp.config import RosConfig, save_ros
from hue_mcp.errors import HueError
from hue_mcp.race import (
    EMERGENCY,
    FLAG_LOOKS,
    IMMEDIATE,
    LINK_LOST,
    NO_FLAG,
    SAFE,
    STOP_LOOKS,
    Look,
    Race,
    Reading,
    race_look,
)

pytestmark = pytest.mark.anyio

NOW = 100.0
NOMINAL = Reading(0, NOW)


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def flags(*bits: int, at: float = NOW) -> Reading:
    return Reading(sum(1 << bit for bit in bits), at)


def flag_look(*bits: int) -> Look:
    return next(look for flag_bits, look in FLAG_LOOKS if set(bits) & set(flag_bits))


def test_the_helper_and_the_reader_agree_on_the_exit_code():
    """The helper runs outside the package, so it keeps its own copy."""
    source = (Path(race.__file__).parent / "race_listener.py").read_text()
    values = {
        target.id: node.value.value
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert values["EXIT_MISSING"] == race.EXIT_MISSING


def test_no_word_from_the_kart_is_a_lost_link():
    assert race_look(NOW, None, flags(2)) is LINK_LOST


def test_a_silent_fault_status_is_a_lost_link_whatever_the_flag():
    stale = Reading(0, NOW - race.FAULTS_STALE_S - 0.1)
    assert race_look(NOW, stale, flags(2)) is LINK_LOST


@pytest.mark.parametrize("stop", [SAFE, IMMEDIATE, EMERGENCY])
def test_a_stop_request_beats_a_green_flag(stop):
    assert race_look(NOW, Reading(stop, NOW), flags(race.GREEN_FLAG)) is STOP_LOOKS[stop]


def test_an_unknown_stop_type_is_taken_for_the_worst():
    assert race_look(NOW, Reading(9, NOW), None) is STOP_LOOKS[EMERGENCY]


def test_problems_breathe_and_flags_hold_steady():
    assert LINK_LOST.breathe_s is not None
    assert all(look.breathe_s is not None for look in STOP_LOOKS.values())
    assert all(look.breathe_s is None for _, look in FLAG_LOOKS)
    assert NO_FLAG.breathe_s is None


def test_without_flags_or_with_stale_ones_the_room_shows_no_flag():
    assert race_look(NOW, NOMINAL, None) is NO_FLAG
    assert race_look(NOW, NOMINAL, flags(race.GREEN_FLAG, at=NOW - 5.1)) is NO_FLAG


@pytest.mark.parametrize(
    ("bits", "expected"),
    [
        ((race.GREEN_FLAG,), race.GREEN),
        ((race.WAVED_GREEN,), race.GREEN),
        ((8,), race.GREEN),  # G100, a speed-limited green.
        ((race.RED_FLAG,), race.RED),
        ((race.FULL_COURSE_YELLOW,), race.YELLOW),
        ((race.CHECKERED,), race.WHITE),
        ((race.GREEN_FLAG, race.YELLOW_FLAG), race.YELLOW),  # The kart's own flag wins...
        ((race.GREEN_FLAG, race.DOUBLE_YELLOW), race.YELLOW),
        ((race.GREEN_FLAG, race.STOP), race.RED),
        ((race.GREEN_FLAG, race.EMERGENCY_KILL), race.RED),
        ((race.GREEN_FLAG, race.PURPLE_FLAG), race.PURPLE),
        ((race.GREEN_FLAG, race.ORANGE_FLAG), race.ORANGE),
        ((race.GREEN_FLAG, race.BLUE_FLAG), race.BLUE),
        ((race.RED_FLAG, race.BLUE_FLAG), race.RED),  # ...unless the track's is graver.
        ((race.GREEN_FLAG, 30), race.GREEN),  # ATTACKER isn't shown.
    ],
)
def test_the_gravest_flag_up_colors_the_room(bits, expected):
    assert race_look(NOW, NOMINAL, flags(*bits)).color == expected


def test_a_black_flag_is_a_dim_white():
    look = race_look(NOW, NOMINAL, flags(race.GREEN_FLAG, race.BLACK_FLAG))
    assert look == flag_look(race.BLACK_FLAG)
    assert look.color == race.WHITE
    assert look.brightness < NO_FLAG.brightness * 2


def test_flags_with_nothing_to_show_are_no_flag():
    assert race_look(NOW, NOMINAL, flags(32)) is NO_FLAG  # GR1, an engine map.


def test_a_steady_look_is_its_color_at_its_brightness():
    assert Look(race.GREEN, 0.5).at(12.3) == pytest.approx((0.0, 0.5, 0.1))


@pytest.mark.parametrize("look", [LINK_LOST, *STOP_LOOKS.values()], ids=repr)
def test_a_breath_swings_between_its_low_and_its_brightness_slower_than_a_flash(look):
    frames = [max(look.at(step * show.FRAME_S)) for step in range(round(10 / show.FRAME_S))]
    assert max(frames) == pytest.approx(look.brightness, abs=0.01)
    assert min(frames) == pytest.approx(look.brightness * race.BREATH_LOW, abs=0.01)
    window = round(show.FLASH_GAP_S / show.FRAME_S)
    rises = [frames[i] - min(frames[max(0, i - window) : i + 1]) for i in range(len(frames))]
    assert max(rises) < show.FLASH_STEP  # The flash limiter never has to hold a breath back.


def test_the_race_mode_needs_the_stacks_workspace():
    with pytest.raises(HueError, match="hue-mcp set-ros"):
        Race()


@pytest.fixture
def fake_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A setup script that talks on stdout, and a stand-in helper that prints `lines` (with
    ROS_DOMAIN_ID, from the saved settings, in place of DOMAIN) and exits with `code`."""

    def make(lines: list[str], code: int) -> None:
        setup = tmp_path / "setup.bash"
        setup.write_text("echo 'not for the reader'\n")
        save_ros(RosConfig(str(setup), {"ROS_DOMAIN_ID": "42"}))
        helper = tmp_path / "helper.py"
        domain = "os.environ['ROS_DOMAIN_ID']"
        printed = "".join(f"print({line!r}.replace('DOMAIN', {domain}))\n" for line in lines)
        helper.write_text(f"import os, sys\n{printed}sys.exit({code})\n")
        monkeypatch.setattr(race, "HELPER", helper)

    return make


async def test_the_reader_keeps_the_latest_readings_and_says_why_the_helper_stopped(fake_helper):
    fake_helper(
        ['{"ready": true}', '{"later": 1}', '{"stop": 1}', '{"flags": 4}', '{"flags": DOMAIN}'], 6
    )
    kart = Race()
    with pytest.raises(HueError, match="doesn't provide rclpy and race_msgs"):
        await kart.run()
    assert kart.ready.is_set()
    assert kart.stop is not None
    assert kart.stop.value == SAFE
    assert kart.flags is not None
    assert kart.flags.value == 42  # The saved middleware settings reach the helper.
    assert kart.look(kart.stop.at) is STOP_LOOKS[SAFE]


async def test_an_unexplained_helper_exit_says_its_code(fake_helper):
    fake_helper([], 9)
    with pytest.raises(HueError, match="code 9"):
        await Race().run()


async def test_an_unreadable_line_ends_the_reading(fake_helper):
    fake_helper(["not json"], 0)
    with pytest.raises(HueError, match="unreadable"):
        await Race().run()
