from pathlib import Path

import pytest

from hue_mcp import cpu
from hue_mcp.cpu import CpuMeter
from hue_mcp.errors import HueError

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("loadavg", "cores", "expected"),
    [
        ("4.00 8.00 2.00 1/100 42", 16, (0.25, 0.5)),
        ("40.0 1 1 1/1 1", 16, (1.0, 1 / 16)),
        ("0.00 0 0 1/1 1", 0, (0.0, 0.0)),
    ],
)
def test_pressure_is_the_load_average_per_core(loadavg, cores, expected):
    assert cpu.pressure(loadavg, cores) == pytest.approx(expected)


async def test_the_meter_samples_until_the_file_goes_bad(tmp_path: Path, monkeypatch):
    loadavg = tmp_path / "loadavg"
    loadavg.write_text("1.00 0.50 0.25 1/100 42\n")
    meter = CpuMeter(loadavg, cores=2)
    real_sleep = cpu.anyio.sleep

    async def then_garbage(seconds: float) -> None:
        loadavg.write_text("garbage")
        await real_sleep(0)

    monkeypatch.setattr(cpu.anyio, "sleep", then_garbage)
    with pytest.raises(HueError, match="can't read the load average"):
        await meter.run()
    assert meter.pressure == pytest.approx((0.5, 0.25))  # 1.00 and 0.50 over 2 cores.


def test_the_meter_counts_this_machines_cores():
    assert CpuMeter().cores >= 1
