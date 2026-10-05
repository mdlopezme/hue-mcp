import httpx2
import pytest

from hue_mcp.config import Location
from hue_mcp.errors import HueError
from hue_mcp.places import find_places

pytestmark = pytest.mark.anyio


def answering(response: httpx2.Response) -> httpx2.MockTransport:
    return httpx2.MockTransport(lambda request: response)


async def test_matches_come_with_their_region_and_country():
    seen = []

    def search(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url)
        results = [
            {
                "name": "Lisbon",
                "latitude": 38.72,
                "longitude": -9.13,
                "admin1": "Lisbon",
                "country": "Portugal",
            },
            {"name": "Lisbon", "latitude": 44.03, "longitude": -70.1, "country": "United States"},
            {"name": "Nowhere"},  # No coordinates: left out, like the rest below.
            {"name": "Truth", "latitude": True, "longitude": 0},
            {"name": "Beyond", "latitude": 91, "longitude": 0},
            {"latitude": 1, "longitude": 1},
            "Atlantis",
        ]
        return httpx2.Response(200, json={"results": results})

    places = await find_places("Lisbon", httpx2.MockTransport(search))
    assert places == [
        Location(38.72, -9.13, "Lisbon, Lisbon, Portugal"),
        Location(44.03, -70.1, "Lisbon, United States"),
    ]
    assert seen[0].params["name"] == "Lisbon"


@pytest.mark.parametrize("reply", [{}, {"results": None}])
async def test_no_match_is_an_empty_list(reply):
    assert await find_places("Xyzzy", answering(httpx2.Response(200, json=reply))) == []


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx2.Response(503), "Couldn't search"),
        (httpx2.Response(200, text="<html>"), "nonsense"),
        (httpx2.Response(200, json=[1, 2]), "nonsense"),
        (httpx2.Response(200, json={"results": "many"}), "nonsense"),
    ],
)
async def test_a_failed_search_says_so(response, error):
    with pytest.raises(HueError, match=error):
        await find_places("Lisbon", answering(response))
