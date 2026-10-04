import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from hue_mcp.errors import HueError


@dataclass(frozen=True)
class BridgeConfig:
    bridge_id: str
    ip: str
    app_key: str


def config_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(config_home) / "hue-mcp" / "bridge.json"


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
    """Write atomically, readable only by the user: the app key grants control of the lights."""
    path = config_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    # A unique name, so concurrent writers (setup and a running server) can't mix their files.
    fd, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=".bridge-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as config_file:
            json.dump(asdict(config), config_file, indent=2)
            config_file.flush()
            os.fsync(config_file.fileno())
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)
