"""Power estimates. Hue bulbs don't report what they draw, so it is modelled from their rating.

A bulb draws standby power while switched off, and its rated power at full brightness. In
between, the estimate scales linearly with brightness; real bulbs also draw a little less for
saturated colors, which this ignores.
"""

from hue_mcp.home import Light

STANDBY_WATTS = 0.5  # Hue bulbs stay connected to the bridge while switched off.
RATED_WATTS = {  # At full brightness, from Signify's product listings.
    "LCA007": 10.5,  # Hue White and Color Ambiance A19 E26, 1100 lm.
}
TYPICAL_RATED_WATTS = 9.0  # For models not listed above.


def rated_watts(light: Light) -> float:
    return RATED_WATTS.get(light.model_id or "", TYPICAL_RATED_WATTS)


def watts_at(light: Light, brightness: float) -> float:
    """Estimated draw of `light` while on at `brightness` percent."""
    return STANDBY_WATTS + (rated_watts(light) - STANDBY_WATTS) * brightness / 100


def current_watts(light: Light) -> float:
    if not light.resource["on"]["on"]:
        return STANDBY_WATTS
    return watts_at(light, light.resource.get("dimming", {}).get("brightness", 100.0))


def shared_brightness(lights: list[Light], watts: float) -> float:
    """The one brightness percent at which all `lights`, switched on, draw `watts` together."""
    dimmable_watts = sum(rated_watts(light) - STANDBY_WATTS for light in lights)
    return (watts - STANDBY_WATTS * len(lights)) / dimmable_watts * 100
