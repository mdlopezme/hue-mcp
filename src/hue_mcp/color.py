"""Conversions between the color units people use (hex, kelvin) and Hue's (CIE xy, mirek)."""

import math
import re

from hue_mcp.errors import HueError

Point = tuple[float, float]
Gamut = tuple[Point, Point, Point]  # red, green, blue corners

# Gamut of every current Hue color light; group commands span several lights, so they use it.
GAMUT_C: Gamut = ((0.6915, 0.3083), (0.17, 0.7), (0.1532, 0.0475))

MIREK_MIN = 153
MIREK_MAX = 500

_HEX_COLOR = re.compile(r"#?([0-9a-fA-F]{6})")


def hex_to_xy(hex_color: str) -> Point:
    match = _HEX_COLOR.fullmatch(hex_color.strip())
    if match is None:
        raise HueError(f"{hex_color!r} is not a hex color like #ff8800")
    red, green, blue = (_srgb_to_linear(int(match[1][i : i + 2], 16) / 255) for i in (0, 2, 4))
    cie_x = 0.4124 * red + 0.3576 * green + 0.1805 * blue
    cie_y = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    cie_z = 0.0193 * red + 0.1192 * green + 0.9505 * blue
    total = cie_x + cie_y + cie_z
    if total == 0:
        raise HueError("Black is not a light color; turn the light off instead.")
    return cie_x / total, cie_y / total


def xy_to_hex(xy: Point) -> str:
    """The brightest sRGB color with this chromaticity."""
    x, y = xy
    cie_x, cie_y, cie_z = x / y, 1.0, (1 - x - y) / y
    linear = [
        max(0.0, 3.2406 * cie_x - 1.5372 * cie_y - 0.4986 * cie_z),
        max(0.0, -0.9689 * cie_x + 1.8758 * cie_y + 0.0415 * cie_z),
        max(0.0, 0.0557 * cie_x - 0.2040 * cie_y + 1.0570 * cie_z),
    ]
    peak = max(linear)
    return "#" + "".join(f"{round(_linear_to_srgb(c / peak) * 255):02x}" for c in linear)


def clamp_to_gamut(xy: Point, gamut: Gamut) -> Point:
    """The closest color the light can show; the bridge stores out-of-gamut xy unclamped."""
    if _inside_triangle(xy, gamut):
        return xy
    red, green, blue = gamut
    edge_points = [
        _closest_point_on_segment(xy, start, end)
        for start, end in ((red, green), (green, blue), (blue, red))
    ]
    return min(edge_points, key=lambda point: _distance_squared(xy, point))


def kelvin_to_mirek(kelvin: float, lowest: int = MIREK_MIN, highest: int = MIREK_MAX) -> int:
    return min(max(round(1_000_000 / kelvin), lowest), highest)


def mirek_to_kelvin(mirek: int) -> int:
    return int(round(1_000_000 / mirek, -1))


def _srgb_to_linear(channel: float) -> float:
    if channel <= 0.04045:
        return channel / 12.92
    return math.pow((channel + 0.055) / 1.055, 2.4)


def _linear_to_srgb(channel: float) -> float:
    if channel <= 0.0031308:
        return 12.92 * channel
    return 1.055 * math.pow(channel, 1 / 2.4) - 0.055


def _cross(origin: Point, a: Point, b: Point) -> float:
    return (a[0] - origin[0]) * (b[1] - origin[1]) - (a[1] - origin[1]) * (b[0] - origin[0])


def _inside_triangle(point: Point, triangle: Gamut) -> bool:
    a, b, c = triangle
    signs = [_cross(a, b, point), _cross(b, c, point), _cross(c, a, point)]
    return all(s >= 0 for s in signs) or all(s <= 0 for s in signs)


def _closest_point_on_segment(point: Point, start: Point, end: Point) -> Point:
    dx, dy = end[0] - start[0], end[1] - start[1]
    t = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / (dx * dx + dy * dy)
    t = min(max(t, 0.0), 1.0)
    return start[0] + t * dx, start[1] + t * dy


def _distance_squared(a: Point, b: Point) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2
