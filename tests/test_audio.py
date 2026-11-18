import math
from array import array
from itertools import pairwise
from pathlib import Path

import pytest

from hue_mcp.audio import FRAME_SAMPLES, RATE_HZ, SILENCE, Analyzer, Listener
from hue_mcp.errors import HueError

pytestmark = pytest.mark.anyio


def tone(frequency_hz: float, amplitude: float, frames: int = 1, start: int = 0) -> array[int]:
    count = frames * FRAME_SAMPLES
    return array(
        "h",
        (
            round(amplitude * 32767 * math.sin(2 * math.pi * frequency_hz * (start + i) / RATE_HZ))
            for i in range(count)
        ),
    )


def frames(samples: array[int]) -> list[array[int]]:
    return [samples[i : i + FRAME_SAMPLES] for i in range(0, len(samples), FRAME_SAMPLES)]


def test_silence_is_silence():
    assert Analyzer().feed(array("h", [0] * FRAME_SAMPLES)) is SILENCE


def test_a_steady_tone_sets_its_own_level():
    analyzer = Analyzer()
    sounds = [analyzer.feed(frame) for frame in frames(tone(440, 0.3, 50))]
    assert sounds[-1].level == pytest.approx(1.0, abs=0.05)  # The loudest of late is full.


def test_bass_hears_low_notes_not_high_ones():
    low, high = Analyzer(), Analyzer()
    low_sound = [low.feed(frame) for frame in frames(tone(60, 0.5, 20))][-1]
    for frame in frames(tone(3000, 0.5, 20)):
        high.feed(frame)
    assert low_sound.bass > 0.9
    # Both are normalized against themselves, so compare their raw bass energy instead.
    assert high._peak_bass < low._peak_bass / 5


def test_kicks_are_beats_at_most_three_a_second():
    analyzer = Analyzer()
    beats = []
    for frame_index in range(250):  # 5 s: a kick every 4 frames (12.5 a second) over a hum.
        kick = frame_index % 4 == 0
        samples = tone(60, 0.8 if kick else 0.05, 1, start=frame_index * FRAME_SAMPLES)
        beats.append(analyzer.feed(samples).beat)
    assert 10 <= sum(beats) <= 16  # Three a second, and the first second has no history.
    times = [i * 0.02 for i, beat in enumerate(beats) if beat]
    assert min(b - a for a, b in pairwise(times)) >= 1 / 3 - 1e-9


@pytest.fixture
def fake_parec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "parec"
    loud = tone(60, 0.5, 30).tobytes()
    (tmp_path / "sound.raw").write_bytes(loud)
    script.write_text(f"#!/bin/sh\nexec /bin/cat {tmp_path / 'sound.raw'}\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    return script


async def test_the_listener_keeps_the_latest_sound_until_parec_stops(fake_parec):
    listener = Listener()
    with pytest.raises(HueError, match="parec stopped"):
        await listener.run()
    assert listener.sound.level > 0.5
    assert listener.beats == 1  # A steady hum: one beat, where it starts out of silence.


async def test_without_parec_music_modes_say_what_to_install(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(HueError, match="pulseaudio-utils"):
        await Listener().run()
