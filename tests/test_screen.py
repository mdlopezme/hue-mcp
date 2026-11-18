import ast
import json
from pathlib import Path

import pytest

from hue_mcp import screen
from hue_mcp.errors import HueError
from hue_mcp.screen import HEIGHT, WHOLE_FRAME, WIDTH, Screen, region_colors, screen_layout

pytestmark = pytest.mark.anyio


def frame_of(pixel) -> bytes:
    """A thumbnail whose pixel at (x, y) is pixel(x, y), an (r, g, b) of 0-255."""
    return bytes(value for y in range(HEIGHT) for x in range(WIDTH) for value in pixel(x, y))


def test_regions_take_their_part_of_the_frame():
    def halves(x: int, y: int) -> tuple[int, int, int]:
        return (255, 0, 0) if x < WIDTH // 2 else (0, 0, 255)

    colors = region_colors(frame_of(halves))
    assert colors["left"] == pytest.approx((1.0, 0.0, 0.0))
    assert colors["right"] == pytest.approx((0.0, 0.0, 1.0))
    whole = colors["whole"]
    assert whole[0] == pytest.approx(whole[2])  # An even mix.


def test_a_dark_scene_with_a_red_sign_reads_as_dim_red():
    def sign(x: int, y: int) -> tuple[int, int, int]:
        return (255, 0, 0) if x < 8 and y < 4 else (10, 10, 10)  # About 2.5% of the frame.

    red, green, blue = region_colors(frame_of(sign))["whole"]
    assert red > 10 * max(green, blue)
    assert red < 0.2  # But dim: the scene is dark.


def test_a_black_frame_is_black():
    assert region_colors(bytes(WIDTH * HEIGHT * 3))["center"] == (0.0, 0.0, 0.0)


def test_a_box_holds_one_monitor_of_a_workspace():
    def thirds(x: int, y: int) -> tuple[int, int, int]:
        return (0, 255, 0) if x >= 2 * WIDTH // 3 else (0, 0, 0)

    right_monitor = (2 * WIDTH // 3, WIDTH, 0, HEIGHT)
    assert region_colors(frame_of(thirds), right_monitor)["whole"] == pytest.approx((0, 1, 0))


def header(
    screens: list[list[int | None]],
    monitors: list[list[float]] | None = None,
    workspace: list[int] | None = None,
) -> bytes:
    parsed = {"screens": screens, "monitors": monitors or [], "workspace": workspace}
    return json.dumps(parsed).encode()


def test_separate_monitors_are_numbered_from_the_left():
    layout = screen_layout(
        header([[1920, 0, 1920, 1080], [0, 0, 1920, 1080], [None, None, 10, 10]])
    )
    assert layout == {1: [(1, WHOLE_FRAME)], 0: [(2, WHOLE_FRAME)], 2: [(3, WHOLE_FRAME)]}


def test_a_full_workspace_is_cut_into_its_monitors():
    monitors = [[0.5, 0.0, 1.0, 1.0], [0.0, 0.0, 0.5, 0.5]]
    layout = screen_layout(header([[None, None, 3840, 1080]], monitors, [4800, 1350]))
    half = WIDTH // 2
    assert layout == {0: [(1, (0, half, 0, HEIGHT // 2)), (2, (half, WIDTH, 0, HEIGHT))]}


def test_a_region_share_isnt_mistaken_for_the_workspace():
    monitors = [[0.5, 0.0, 1.0, 1.0], [0.0, 0.0, 0.5, 0.5]]
    layout = screen_layout(header([[None, None, 800, 600]], monitors, [4800, 1350]))
    assert layout == {0: [(1, WHOLE_FRAME)]}


@pytest.mark.parametrize("bad", [b"{nope", b'{"monitors": []}', b'{"screens": [[]]}'])
def test_an_unreadable_header_is_reported(bad):
    with pytest.raises(HueError, match="header is unreadable"):
        screen_layout(bad)


def test_the_helper_and_the_reader_agree_on_sizes_and_codes():
    """The helper runs outside the package, so it keeps its own copies."""
    source = (Path(screen.__file__).parent / "screen_capture.py").read_text()
    values = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            targets = [e.id for t in node.targets if isinstance(t, ast.Tuple) for e in t.elts]
            if isinstance(node.value, ast.Tuple):
                values.update(
                    zip(targets, (ast.literal_eval(v) for v in node.value.elts), strict=True)
                )
            elif isinstance(node.value, ast.Constant):
                values.update(dict.fromkeys(names, node.value.value))
    expected = {
        "WIDTH": screen.WIDTH,
        "HEIGHT": screen.HEIGHT,
        "EXIT_CANCELLED": screen.EXIT_CANCELLED,
        "EXIT_FAILED": screen.EXIT_FAILED,
        "EXIT_CLOSED": screen.EXIT_CLOSED,
        "EXIT_MISSING": screen.EXIT_MISSING,
    }
    assert {name: values[name] for name in expected} == expected


@pytest.fixture
def fake_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A stand-in system Python that prints a header and frames, then exits with a code."""

    def make(frames: list[bytes], code: int) -> None:
        output = tmp_path / "output.bin"
        output.write_bytes(header([[None, None, 64, 20]]) + b"\n" + b"".join(frames))
        python = tmp_path / "python3"
        python.write_text(f"#!/bin/sh\n/bin/cat {output}\nexit {code}\n")
        python.chmod(0o755)
        monkeypatch.setattr(screen, "SYSTEM_PYTHON", str(python))

    return make


async def test_the_reader_keeps_each_screens_colors_and_reports_why_it_ended(fake_helper):
    red = bytes([0]) + bytes([255, 0, 0]) * (WIDTH * HEIGHT)
    fake_helper([red, bytes([7]) + bytes(WIDTH * HEIGHT * 3)], screen.EXIT_CANCELLED)
    watcher_screen = Screen()
    with pytest.raises(HueError, match="wasn't shared"):
        await watcher_screen.run()
    assert watcher_screen.sharing.is_set()
    assert watcher_screen.colors[1]["whole"] == pytest.approx((1.0, 0.0, 0.0))
    assert set(watcher_screen.colors) == {1}  # A frame from an unknown stream is dropped.


async def test_an_unexplained_helper_exit_says_its_code(fake_helper):
    fake_helper([], 9)
    with pytest.raises(HueError, match="code 9"):
        await Screen().run()


async def test_without_the_system_python_screen_modes_say_what_to_install(monkeypatch, tmp_path):
    monkeypatch.setattr(screen, "SYSTEM_PYTHON", str(tmp_path / "missing"))
    with pytest.raises(HueError, match="python3-gi"):
        await Screen().run()


async def test_only_the_newest_frame_of_a_backlog_is_worked_out(fake_helper, monkeypatch):
    computed = []
    real = screen.region_colors

    def counting(frame: bytes, box=WHOLE_FRAME):
        computed.append(frame[:3])
        return real(frame, box)

    monkeypatch.setattr(screen, "region_colors", counting)
    old, new = bytes([0, 1, 2, 3]), bytes([0, 9, 9, 9])
    pad = bytes(WIDTH * HEIGHT * 3 - 3)
    fake_helper([old + pad, new + pad], screen.EXIT_CLOSED)
    with pytest.raises(HueError, match="sharing ended"):
        await Screen().run()
    assert computed == [bytes([9, 9, 9])]
