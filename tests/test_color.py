import pytest

from hue_mcp.color import (
    GAMUT_C,
    _cross,
    _inside_triangle,
    clamp_to_gamut,
    hex_to_xy,
    kelvin_to_mirek,
    mirek_to_kelvin,
    xy_to_hex,
)
from hue_mcp.errors import HueError


def test_srgb_primaries_and_white_convert_to_their_cie_coordinates():
    assert hex_to_xy("#ff0000") == pytest.approx((0.640, 0.330), abs=1e-3)
    assert hex_to_xy("00ff00") == pytest.approx((0.300, 0.600), abs=1e-3)
    assert hex_to_xy("#FFFFFF") == pytest.approx((0.3127, 0.3290), abs=1e-3)


def test_hex_round_trips_through_xy():
    for color in ("#ff0000", "#ff8800", "#3366ff"):
        assert xy_to_hex(hex_to_xy(color)) == color


@pytest.mark.parametrize("bad", ["red", "#fff", "#gg0000", "#000000"])
def test_bad_or_black_colors_are_refused(bad):
    with pytest.raises(HueError):
        hex_to_xy(bad)


def test_in_gamut_colors_are_unchanged():
    assert clamp_to_gamut((0.4, 0.4), GAMUT_C) == (0.4, 0.4)


def test_out_of_gamut_colors_move_to_the_nearest_edge():
    outside = (0.1, 0.9)
    clamped = clamp_to_gamut(outside, GAMUT_C)
    assert clamped != outside
    assert _inside_triangle(clamped, GAMUT_C)
    green = GAMUT_C[1]
    assert clamped == pytest.approx(green, abs=0.05)


def test_kelvin_to_mirek_clamps_to_what_hue_accepts():
    assert kelvin_to_mirek(2700) == 370
    assert kelvin_to_mirek(1000) == 500
    assert kelvin_to_mirek(10000) == 153
    assert mirek_to_kelvin(370) == 2700


def test_colors_mixing_dark_and_bright_channels_convert_exactly():
    # 8/255 sits in sRGB's linear segment; 255 in its curved one.
    assert hex_to_xy("#ff0800") == pytest.approx((0.6386, 0.3312), abs=1e-3)
    assert xy_to_hex(hex_to_xy("#ff0800")) == "#ff0800"


def test_trailing_text_after_a_hex_color_is_refused():
    with pytest.raises(HueError):
        hex_to_xy("#ff8800zz")


def test_colors_beyond_the_blue_red_edge_move_onto_it():
    red, _, blue = GAMUT_C
    beyond = (0.45, 0.10)  # Purple, below the line from blue to red.
    clamped = clamp_to_gamut(beyond, GAMUT_C)
    on_edge = _cross(blue, red, clamped)
    assert on_edge == pytest.approx(0, abs=1e-9)
    assert clamped != beyond
