import json
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from hue_mcp.errors import HueError

CLIENT_KEY = re.compile(r"[0-9A-Fa-f]{32}")  # 16 bytes, the streaming key.


@dataclass(frozen=True)
class BridgeConfig:
    bridge_id: str
    ip: str
    app_key: str
    client_key: str | None = None  # For streaming light shows; pairings before them lack it.


@dataclass(frozen=True)
class Location:
    """Where the lights are, for the sun times the pomodoro's looks follow."""

    latitude: float
    longitude: float
    label: str

    def __post_init__(self) -> None:
        if not (
            _finite(self.latitude)
            and _finite(self.longitude)
            and -90 <= self.latitude <= 90
            and -180 <= self.longitude <= 180
            and isinstance(self.label, str)
        ):
            raise HueError(f"{self.label!r} has no valid coordinates.")


@dataclass(frozen=True)
class RosConfig:
    """How the race light show reaches a ROS 2 stack: the workspace's setup script, and the
    middleware settings of the shell it was saved from."""

    setup: str
    environment: dict[str, str]

    def __post_init__(self) -> None:
        if not (
            isinstance(self.setup, str)
            and self.setup
            and isinstance(self.environment, dict)
            and all(
                isinstance(name, str) and isinstance(value, str)
                for name, value in self.environment.items()
            )
        ):
            raise HueError("it has no setup script, or settings that aren't text.")


def config_dir() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(config_home) / "hue-mcp"


def state_dir() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(state_home) / "hue-mcp"


def config_path() -> Path:
    return config_dir() / "bridge.json"


def location_path() -> Path:
    return config_dir() / "location.json"


def load_config() -> BridgeConfig | None:
    path = config_path()
    try:
        config = BridgeConfig(**json.loads(path.read_text()))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError) as error:
        raise HueError(f"{path} is unreadable ({error}); run `hue-mcp setup` again.") from error
    required = (config.bridge_id, config.ip, config.app_key)
    if not all(isinstance(value, str) and value for value in required):
        raise HueError(f"{path} is incomplete; run `hue-mcp setup` again.")
    if config.client_key is not None and not CLIENT_KEY.fullmatch(str(config.client_key)):
        raise HueError(f"{path} has a malformed client key; run `hue-mcp setup` again.")
    return config


def save_config(config: BridgeConfig) -> None:
    """Readable only by the user: the app key grants control of the lights."""
    atomic_write_json(config_path(), asdict(config))


def load_location() -> Location | None:
    path = location_path()
    hint = 'run `hue-mcp set-location "<city>"` again'
    try:
        return Location(**json.loads(path.read_text()))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, HueError) as error:
        raise HueError(f"{path} is unusable ({error}); {hint}.") from error


def save_location(location: Location) -> None:
    atomic_write_json(location_path(), asdict(location))


def ros_path() -> Path:
    return config_dir() / "ros.json"


def load_ros() -> RosConfig | None:
    path = ros_path()
    hint = "run `hue-mcp set-ros <workspace>/install/setup.bash` again"
    try:
        return RosConfig(**json.loads(path.read_text()))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, HueError) as error:
        raise HueError(f"{path} is unusable ({error}); {hint}.") from error


def save_ros(ros: RosConfig) -> None:
    atomic_write_json(ros_path(), asdict(ros))


def placements_path() -> Path:
    return config_dir() / "show.json"


def load_placements() -> dict[str, dict[str, Any]]:
    """Each light's part of the screen and brightness cap for light shows, by light id."""
    path = placements_path()
    try:
        placements = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise HueError(f"{path} is unreadable ({error}); place the lights again.") from error
    if not isinstance(placements, dict) or not all(
        isinstance(placement, dict) for placement in placements.values()
    ):
        raise HueError(f"{path} isn't a list of placements; place the lights again.")
    return placements


def save_placements(placements: dict[str, dict[str, Any]]) -> None:
    atomic_write_json(placements_path(), placements)


def show_order_path() -> Path:
    return config_dir() / "show_order.json"


def load_show_order() -> list[str]:
    """The light ids in the order the user gave for light shows; empty when none."""
    path = show_order_path()
    try:
        order = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as error:
        raise HueError(f"{path} is unreadable ({error}); set the lights' order again.") from error
    if not isinstance(order, list) or not all(isinstance(light, str) for light in order):
        raise HueError(f"{path} isn't a list of lights; set the lights' order again.")
    return order


def save_show_order(light_ids: list[str]) -> None:
    atomic_write_json(show_order_path(), light_ids)


def _finite(value: object) -> bool:
    """A real number: not a bool, NaN or infinity, nor too big for a float."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def atomic_write_json(path: Path, data: Any) -> None:
    """Readers see the old file or the new one, never half of one; only the user can read it."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    # A unique name, so concurrent writers (setup and a running server) can't mix their files.
    fd, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(data, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)
