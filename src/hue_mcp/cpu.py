"""How loaded the computer is: Linux's 1- and 5-minute load averages, per processor core."""

import os
from pathlib import Path

import anyio

from hue_mcp.errors import HueError

PROC_LOADAVG = Path("/proc/loadavg")
SAMPLE_EVERY_S = 1.0  # The kernel updates the load average every 5 s.


def pressure(loadavg: str, cores: int) -> tuple[float, float]:
    """The 1- and 5-minute load averages per core, each 0 to 1. The kernel already smooths
    them, so they drift with the machine's work instead of jumping with every burst."""
    one, five = (float(field) for field in loadavg.split()[:2])
    return _share(one, cores), _share(five, cores)


def _share(load: float, cores: int) -> float:
    return min(max(load / max(cores, 1), 0.0), 1.0)


class CpuMeter:
    """Keeps the machine's latest load averages per core while `run` samples them."""

    def __init__(self, path: Path = PROC_LOADAVG, cores: int | None = None) -> None:
        self.path = path
        self.cores = cores or os.cpu_count() or 1
        self.pressure: tuple[float, float] | None = None  # 1 minute, 5 minutes.

    async def run(self) -> None:
        try:
            while True:
                self.pressure = pressure(await anyio.Path(self.path).read_text(), self.cores)
                await anyio.sleep(SAMPLE_EVERY_S)
        except (OSError, ValueError, IndexError) as error:
            raise HueError(f"The cpu mode can't read the load average: {error}") from error
