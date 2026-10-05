import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from hue_mcp.errors import HueError


@dataclass(frozen=True)
class BridgeConfig:
    bridge_id: str
    ip: str
    app_key: str


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
    if not all(isinstance(value, str) and value for value in asdict(config).values()):
        raise HueError(f"{path} is incomplete; run `hue-mcp setup` again.")
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
