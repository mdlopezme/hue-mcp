"""HueStream v2: the frames an entertainment area streams, one color per channel."""

import struct

from hue_mcp.errors import HueError

RGB = tuple[float, float, float]  # Each 0 to 1.

PROTOCOL = b"HueStream"
VERSION = bytes([2, 0])
COLOR_SPACE_RGB = 0
MAX_CHANNELS = 20
AREA_ID_LENGTH = 36  # A UUID with its dashes.


def encode(area_id: str, sequence: int, colors: dict[int, RGB]) -> bytes:
    """One frame: the header, the area id, then each channel's id and 16-bit R, G and B."""
    if len(area_id) != AREA_ID_LENGTH or not area_id.isascii():
        raise HueError(f"{area_id!r} isn't an entertainment area id.")
    if len(colors) > MAX_CHANNELS:
        raise HueError(f"A frame holds at most {MAX_CHANNELS} channels, not {len(colors)}.")
    header = PROTOCOL + VERSION + bytes([sequence % 256, 0, 0, COLOR_SPACE_RGB, 0])
    channels = b"".join(
        struct.pack(">BHHH", channel, *(_level(value) for value in rgb))
        for channel, rgb in sorted(colors.items())
    )
    return header + area_id.encode("ascii") + channels


def _level(value: float) -> int:
    return round(min(max(value, 0.0), 1.0) * 0xFFFF)
