import struct

import pytest

from hue_mcp.errors import HueError
from hue_mcp.huestream import encode

AREA = "1a8d99cc-967b-44f2-9202-43f976c0fa6b"


def test_a_frame_has_the_header_the_area_and_each_channel_in_order():
    frame = encode(AREA, 258, {1: (0.0, 0.5, 1.0), 0: (1.0, 0.0, 0.0)})
    assert frame[:16] == b"HueStream" + bytes([2, 0, 2, 0, 0, 0, 0])  # Sequence wraps at 256.
    assert frame[16:52] == AREA.encode()
    assert struct.unpack(">BHHH", frame[52:59]) == (0, 0xFFFF, 0, 0)
    assert struct.unpack(">BHHH", frame[59:66]) == (1, 0, 0x8000, 0xFFFF)
    assert len(frame) == 66


def test_levels_outside_zero_to_one_are_clamped():
    frame = encode(AREA, 0, {3: (-0.5, 1.5, 0.25)})
    assert struct.unpack(">BHHH", frame[52:]) == (3, 0, 0xFFFF, 0x4000)


@pytest.mark.parametrize("area", ["short", AREA.replace("1", "é")])
def test_an_area_id_must_be_a_uuid(area):
    with pytest.raises(HueError, match="isn't an entertainment area id"):
        encode(area, 0, {})


def test_at_most_twenty_channels():
    with pytest.raises(HueError, match="at most 20"):
        encode(AREA, 0, {channel: (0.0, 0.0, 0.0) for channel in range(21)})
