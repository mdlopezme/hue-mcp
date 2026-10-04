"""A snapshot of the bridge's resources (GET /clip/v2/resource), with lookup by name."""

import difflib
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from hue_mcp.color import GAMUT_C, MIREK_MAX, MIREK_MIN, Gamut
from hue_mcp.errors import HueError

ALL_LIGHTS = "all"
ALL_LIGHTS_LABEL = "all (every light)"
NO_EFFECT = "no_effect"

GroupKind = Literal["room", "zone", "home"]


@dataclass
class Light:
    id: str
    name: str
    reachable: bool
    resource: dict[str, Any]
    room: str | None = None
    model_id: str | None = None

    @property
    def label(self) -> str:
        return f"{self.name} (light in {self.room})" if self.room else f"{self.name} (light)"

    @property
    def supports_color(self) -> bool:
        return "color" in self.resource

    @property
    def supports_color_temperature(self) -> bool:
        return "color_temperature" in self.resource

    @property
    def mirek_range(self) -> tuple[int, int]:
        schema = self.resource["color_temperature"].get("mirek_schema", {})
        return schema.get("mirek_minimum", MIREK_MIN), schema.get("mirek_maximum", MIREK_MAX)

    @property
    def gamut(self) -> Gamut:
        gamut = self.resource["color"].get("gamut")
        if gamut is None:  # Some non-Hue bulbs don't report theirs.
            return GAMUT_C
        red, green, blue = (gamut[corner] for corner in ("red", "green", "blue"))
        return (red["x"], red["y"]), (green["x"], green["y"]), (blue["x"], blue["y"])

    @property
    def effects(self) -> list[str]:
        if "effects_v2" in self.resource:
            values = self.resource["effects_v2"].get("status", {}).get("effect_values", [])
        else:
            values = self.resource.get("effects", {}).get("effect_values", [])
        return [value for value in values if value != NO_EFFECT]

    @property
    def timed_effects(self) -> list[str]:
        values = self.resource.get("timed_effects", {}).get("effect_values", [])
        return [value for value in values if value != NO_EFFECT]


@dataclass
class Group:
    """A room, a zone, or the whole home; controlled together through its grouped_light."""

    kind: GroupKind
    id: str
    name: str
    grouped_light: dict[str, Any]
    lights: list[Light]

    @property
    def label(self) -> str:
        if self.kind == "home":
            return ALL_LIGHTS_LABEL
        return f"{self.name} ({self.kind})"


@dataclass
class Scene:
    id: str
    name: str
    group: Group
    resource: dict[str, Any]

    @property
    def label(self) -> str:
        return f"{self.name} (in {self.group.name})"


class Home:
    def __init__(self, resources: list[dict[str, Any]]):
        by_type: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        for resource in resources:
            by_type[resource["type"]][resource["id"]] = resource
        devices = by_type["device"]
        grouped_lights = by_type["grouped_light"]
        unreachable_devices = {
            connectivity["owner"]["rid"]
            for connectivity in by_type["zigbee_connectivity"].values()
            if connectivity.get("status", "connected") != "connected"
        }

        lights_by_id: dict[str, Light] = {}
        for light_id, light_resource in by_type["light"].items():
            device = devices.get(light_resource["owner"]["rid"])
            lights_by_id[light_id] = Light(
                id=light_id,
                name=_light_name(light_resource, device),
                reachable=light_resource["owner"]["rid"] not in unreachable_devices,
                resource=light_resource,
                model_id=(device or {}).get("product_data", {}).get("model_id"),
            )
        self.lights = sorted(lights_by_id.values(), key=lambda light: light.name.casefold())

        def lights_in(group: dict[str, Any]) -> list[Light]:
            light_ids = []
            for child in group["children"]:
                if child["rtype"] == "light":
                    light_ids.append(child["rid"])
                elif child["rtype"] == "device":
                    services = devices.get(child["rid"], {}).get("services", [])
                    light_ids += [s["rid"] for s in services if s["rtype"] == "light"]
            return [lights_by_id[light_id] for light_id in light_ids if light_id in lights_by_id]

        def group_from(
            kind: GroupKind, resource: dict[str, Any], name: str, lights: list[Light]
        ) -> Group | None:
            grouped_light = next(
                (
                    grouped_lights.get(service["rid"])
                    for service in resource["services"]
                    if service["rtype"] == "grouped_light"
                ),
                None,
            )
            if grouped_light is None or not lights:  # A room or zone with no lights in it.
                return None
            return Group(kind, resource["id"], name, grouped_light, lights)

        rooms_and_zones = [
            group_from(kind, group, group["metadata"]["name"], lights_in(group))
            for kind in ("room", "zone")
            for group in by_type[kind].values()
        ]
        self.groups = sorted(
            (group for group in rooms_and_zones if group is not None),
            key=lambda group: (group.kind != "room", group.name.casefold()),
        )
        group_ids = {group.id for group in self.groups}
        self.empty_groups = sorted(
            (group["metadata"]["name"], kind)
            for kind in ("room", "zone")
            for group in by_type[kind].values()
            if group["id"] not in group_ids
        )
        for room in (group for group in self.groups if group.kind == "room"):
            for light in room.lights:
                light.room = room.name

        homes = [
            group_from("home", bridge_home, ALL_LIGHTS, self.lights)
            for bridge_home in by_type["bridge_home"].values()
        ]
        self.everything = next((home for home in homes if home is not None), None)

        groups_by_id = {group.id: group for group in self.groups}
        if self.everything is not None:
            groups_by_id[self.everything.id] = self.everything
        self.scenes = sorted(
            (
                Scene(
                    id=scene["id"],
                    name=scene["metadata"]["name"],
                    group=groups_by_id[scene["group"]["rid"]],
                    resource=scene,
                )
                for scene in by_type["scene"].values()
                if scene["group"]["rid"] in groups_by_id and scene.get("metadata", {}).get("name")
            ),
            key=lambda scene: scene.name.casefold(),
        )

    def find_target(
        self, name: str, target_type: Literal["light", "room", "zone"] | None = None
    ) -> Light | Group:
        if _means_everything(name):
            if target_type is not None:
                raise HueError(f'"{ALL_LIGHTS}" means every light; leave out target_type.')
            if self.everything is None:
                raise HueError("The bridge has no lights to control.")
            return self.everything
        candidates: list[Light | Group] = []
        if target_type in (None, "light"):
            candidates += self.lights
        candidates += [g for g in self.groups if target_type in (None, g.kind)]
        group_kinds = [kind for kind in ("room", "zone") if target_type in (None, kind)]
        self._refuse_empty_group(name, candidates, group_kinds)
        everything = [self.everything] if self.everything and target_type is None else []
        return _pick(name, candidates, target_type or "light, room or zone", everything)

    def find_group(self, name: str) -> Group:
        if _means_everything(name):
            raise HueError(f'"{ALL_LIGHTS}" means every light, not one room or zone.')
        self._refuse_empty_group(name, self.groups, ["room", "zone"])
        return _pick(name, self.groups, "room or zone")

    def find_scene(self, name: str, group: Group | None = None) -> Scene:
        candidates = [scene for scene in self.scenes if group is None or scene.group is group]
        return _pick(name, candidates, f"scene in {group.label}" if group else "scene")

    def _refuse_empty_group(
        self, name: str, candidates: Sequence["_Named"], kinds: list[str]
    ) -> None:
        """Say a room or zone has no lights, rather than that it doesn't exist."""
        wanted = name.strip().casefold()
        if any(candidate.name.casefold() == wanted for candidate in candidates):
            return
        for empty, kind in self.empty_groups:
            if kind in kinds and empty.casefold() == wanted:
                raise HueError(f"{empty} has no lights in it.")


def _means_everything(name: str) -> bool:
    return name.strip().casefold() in (ALL_LIGHTS, ALL_LIGHTS_LABEL)


def _light_name(light: dict[str, Any], device: dict[str, Any] | None) -> str:
    """The device's name, unless the device has several lights: those keep a name each."""
    lights_on_device = (
        sum(service["rtype"] == "light" for service in device["services"]) if device else 0
    )
    named = device if device is not None and lights_on_device == 1 else light
    name: str = named["metadata"]["name"]
    return name


class _Named(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def label(self) -> str: ...


def _pick[T: _Named](
    query: str, candidates: Sequence[T], what: str, exact_only: Sequence[T] = ()
) -> T:
    """Match an exact name, label or id, else a unique part of a name.

    Close spellings are only suggested: acting on a guess could switch the wrong lights.
    `exact_only` candidates are never matched by part of a name.
    """
    wanted = query.strip().casefold()
    if not wanted:
        raise HueError(f"Name the {what}.")
    everyone = [*candidates, *exact_only]
    matches = [c for c in everyone if wanted in (c.name.casefold(), c.label.casefold(), c.id)]
    if not matches:
        matches = [c for c in candidates if wanted in c.name.casefold()]
    if len(matches) == 1:
        return matches[0]
    if matches:
        labels = [c.label for c in matches]
        if len(set(labels)) < len(labels):  # Same name in the same place: only ids differ.
            options = ", ".join(sorted(f"{c.label} [id {c.id}]" for c in matches))
            raise HueError(f"{query!r} matches several: {options}. Use one of the ids.")
        options = ", ".join(sorted(labels))
        raise HueError(f"{query!r} matches several: {options}. Use one exactly as written.")
    close_names = difflib.get_close_matches(
        wanted, [c.name.casefold() for c in candidates], n=3, cutoff=0.6
    )
    if close_names:
        close = sorted(c.label for c in candidates if c.name.casefold() in close_names)
        raise HueError(f"No {what} named {query!r}. Did you mean: {', '.join(close)}?")
    known = ", ".join(sorted(c.label for c in everyone)) or "none"
    raise HueError(f"No {what} named {query!r}. Known: {known}.")
