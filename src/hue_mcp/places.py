"""Finding a city's coordinates, once, for the sun times: Open-Meteo's free place search."""

from typing import Any

import httpx2

from hue_mcp.config import Location
from hue_mcp.errors import HueError

SEARCH_URL = "https://geocoding-api.open-meteo.com/v1/search"
MAX_MATCHES = 10


async def find_places(
    name: str, transport: httpx2.AsyncBaseTransport | None = None
) -> list[Location]:
    try:
        async with httpx2.AsyncClient(transport=transport, timeout=10) as client:
            response = await client.get(
                SEARCH_URL, params={"name": name, "count": MAX_MATCHES, "format": "json"}
            )
            response.raise_for_status()
            payload = response.json()
    except httpx2.HTTPError as error:
        raise HueError(f"Couldn't search for {name!r}: {error}") from error
    except ValueError as error:
        raise HueError(f"The place search answered nonsense: {error}") from error
    results = payload.get("results") if isinstance(payload, dict) else "not a dict"
    if not isinstance(results or [], list):  # No matches leave "results" out.
        raise HueError("The place search answered nonsense.")
    locations = (_location(place) for place in results or [])
    return [location for location in locations if location is not None]


def _location(place: Any) -> Location | None:
    """None for a match without a name or usable coordinates."""
    if not isinstance(place, dict) or not isinstance(place.get("name"), str):
        return None
    parts = [place["name"], place.get("admin1"), place.get("country")]
    label = ", ".join(str(part) for part in parts if part)
    try:
        return Location(latitude=place["latitude"], longitude=place["longitude"], label=label)
    except (KeyError, HueError):
        return None
