"""The screen's colors, region by region, from a thumbnail the capture helper streams."""

import json
import logging
from pathlib import Path
from typing import Any, Literal

import anyio

from hue_mcp.config import config_dir
from hue_mcp.errors import HueError
from hue_mcp.huestream import RGB

logger = logging.getLogger(__name__)

Region = Literal["left", "right", "top", "bottom", "center", "whole"]
REGIONS: tuple[Region, ...] = ("left", "right", "top", "bottom", "center", "whole")
SYSTEM_PYTHON = "/usr/bin/python3"  # The helper needs its PyGObject and GStreamer.
# The helper's frame size and exit codes, which a test keeps equal to its own.
WIDTH, HEIGHT = 64, 20
EXIT_CANCELLED, EXIT_FAILED, EXIT_CLOSED, EXIT_MISSING = 3, 4, 5, 6
HELPER = Path(__file__).with_name("screen_capture.py")
WORKSPACE_SHAPE_TOLERANCE = 0.02
COLOR_WEIGHT_FLOOR = 0.05  # Grey pixels still count a little, so a grey screen isn't black.
EXIT_MESSAGES = {
    EXIT_CANCELLED: "The screen wasn't shared, so screen modes can't run.",
    EXIT_CLOSED: "The screen sharing ended.",
    EXIT_MISSING: ("Screen modes need the system packages python3-gi and gstreamer1.0-pipewire."),
}


Box = tuple[int, int, int, int]  # Columns, then rows, of the thumbnail: [from, to).
WHOLE_FRAME: Box = (0, WIDTH, 0, HEIGHT)


def region_colors(frame: bytes, box: Box = WHOLE_FRAME) -> dict[Region, RGB]:
    """Each region's color within `box` (one monitor of a workspace thumbnail, say): the hue
    of its colorful pixels at its average brightness, so a mostly dark scene with a red sign
    reads as dim red rather than grey."""
    x0, x1, y0, y1 = box
    width, height = x1 - x0, y1 - y0
    quarter_w, third_h = max(1, width // 4), max(1, height // 3)
    bounds: dict[Region, Box] = {
        "left": (x0, x0 + quarter_w, y0, y1),
        "right": (x1 - quarter_w, x1, y0, y1),
        "top": (x0, x1, y0, y0 + third_h),
        "bottom": (x0, x1, y1 - third_h, y1),
        "center": (x0 + quarter_w, x1 - quarter_w, y0 + height // 4, y1 - height // 4),
        "whole": box,
    }
    return {region: _color(frame, *bound) for region, bound in bounds.items()}


def _color(frame: bytes, x0: int, x1: int, y0: int, y1: int) -> RGB:
    weighted = [0.0, 0.0, 0.0]
    total_weight = brightness = 0.0
    count = 0
    for y in range(y0, y1):
        row = y * WIDTH * 3
        for x in range(x0, x1):
            r, g, b = (value / 255 for value in frame[row + 3 * x : row + 3 * x + 3])
            peak = max(r, g, b)
            weight = COLOR_WEIGHT_FLOOR + (peak - min(r, g, b)) ** 2
            weighted[0] += weight * r
            weighted[1] += weight * g
            weighted[2] += weight * b
            total_weight += weight
            brightness += peak
            count += 1
    hue_peak = max(weighted)
    if count == 0 or hue_peak <= 0:
        return (0.0, 0.0, 0.0)
    level = brightness / count
    return (
        weighted[0] / hue_peak * level,
        weighted[1] / hue_peak * level,
        weighted[2] / hue_peak * level,
    )


class Screen:
    """Keeps the latest region colors of each shared monitor, numbered from 1 at the left,
    while `run` reads the capture helper."""

    def __init__(self) -> None:
        self.colors: dict[int, dict[Region, RGB]] = {}
        self.sharing = anyio.Event()  # Set at the first frame: the user said yes.

    async def run(self) -> None:
        if not await anyio.Path(SYSTEM_PYTHON).exists():
            raise HueError(EXIT_MESSAGES[EXIT_MISSING])
        token = config_dir() / "screen_token"
        command = [SYSTEM_PYTHON, str(HELPER), str(token)]
        async with await anyio.open_process(command, stderr=None) as process:
            if process.stdout is None:
                raise HueError("The screen helper gave no output to read.")
            layout: Layout | None = None
            frame_bytes = 1 + WIDTH * HEIGHT * 3  # The stream's index, then the frame.
            pending = b""
            async for chunk in process.stdout:
                pending += chunk
                if layout is None:
                    if b"\n" not in pending:
                        continue
                    header, pending = pending.split(b"\n", 1)
                    layout = screen_layout(header)
                    logger.info("Sharing: %s", header.decode())
                newest: dict[int, bytes] = {}  # A backlog's older frames are already stale.
                while len(pending) >= frame_bytes:
                    frame, pending = pending[:frame_bytes], pending[frame_bytes:]
                    newest[frame[0]] = frame[1:]
                for index, frame in newest.items():
                    for number, box in layout.get(index, []):
                        self.colors[number] = region_colors(frame, box)
                    self.sharing.set()
            code = await process.wait()
        message = EXIT_MESSAGES.get(code, f"The screen helper stopped (code {code}).")
        raise HueError(message)


Layout = dict[int, list[tuple[int, Box]]]  # Stream index: (screen number, box) for each.


def screen_layout(header: bytes) -> Layout:
    """Which screens each stream holds, numbered from 1 at the left. A stream per monitor
    holds one; a single full-workspace stream is cut into the monitors the helper measured."""
    try:
        parsed = json.loads(header)
        streams = parsed["screens"]
        lefts = [stream[0] if isinstance(stream[0], int) else None for stream in streams]
        monitors = [[float(edge) for edge in monitor] for monitor in parsed.get("monitors") or []]
    except (ValueError, KeyError, TypeError, IndexError) as error:
        raise HueError(f"The screen helper's header is unreadable: {error}") from error
    if len(streams) == 1 and lefts[0] is None and _is_workspace(streams[0], parsed):
        ordered = sorted(monitors, key=lambda monitor: (monitor[0], monitor[1]))
        return {0: [(number, _box(monitor)) for number, monitor in enumerate(ordered, 1)]}
    order = sorted(range(len(lefts)), key=lambda i: (lefts[i] is None, lefts[i] or 0, i))
    return {index: [(number, WHOLE_FRAME)] for number, index in enumerate(order, 1)}


def _is_workspace(stream: list[Any], header: dict[str, Any]) -> bool:
    """Whether a stream is the whole multi-monitor workspace: its shape matches the monitors'
    bounding box (a region share has a shape of its own)."""
    workspace = header.get("workspace")
    if len(header.get("monitors") or []) < 2 or not workspace or None in stream[2:4]:
        return False
    stream_shape = float(stream[2]) / float(stream[3])
    workspace_shape = float(workspace[0]) / float(workspace[1])
    return abs(stream_shape - workspace_shape) / workspace_shape < WORKSPACE_SHAPE_TOLERANCE


def _box(fractions: list[float]) -> Box:
    left, top, right, bottom = (min(max(edge, 0.0), 1.0) for edge in fractions)
    x0, y0 = round(left * WIDTH), round(top * HEIGHT)
    return x0, max(x0 + 1, round(right * WIDTH)), y0, max(y0 + 1, round(bottom * HEIGHT))
