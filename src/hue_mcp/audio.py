"""What the computer is playing, as loudness, bass and beats: never the sound itself.

parec reads the default output's monitor (PulseAudio or PipeWire), mono at 11025 Hz, in 20 ms
frames. Each frame is reduced to a few numbers and then dropped.
"""

import math
import shutil
import sys
from array import array
from collections import deque
from dataclasses import dataclass

import anyio
from anyio.abc import Process

from hue_mcp.errors import HueError

RATE_HZ = 11025
FRAME_SAMPLES = RATE_HZ // 50  # 20 ms.
BASS_CUTOFF_HZ = 150.0
GAIN_DECAY_PER_FRAME = 0.997  # The loudest level seen fades over about 10 s.
QUIET_FLOOR = 0.003  # Below this (of full scale) counts as silence.
BEAT_RATIO = 1.5  # Bass this much above its recent average is a beat...
BEAT_HISTORY_FRAMES = 50  # ...the average over the last second...
MIN_BEAT_GAP_S = 1 / 3  # ...at most three a second.


@dataclass(frozen=True)
class Sound:
    """`level` and `bass` run 0 to 1, against the loudest of the last seconds."""

    level: float
    bass: float
    beat: bool


SILENCE = Sound(0.0, 0.0, False)


class Analyzer:
    def __init__(self) -> None:
        self._low = 0.0
        self._alpha = 1 - math.exp(-2 * math.pi * BASS_CUTOFF_HZ / RATE_HZ)
        self._peak_level = QUIET_FLOOR
        self._peak_bass = QUIET_FLOOR
        self._bass_history: deque[float] = deque(maxlen=BEAT_HISTORY_FRAMES)
        self._since_beat_s = MIN_BEAT_GAP_S

    def feed(self, samples: array[int]) -> Sound:
        """One frame of signed 16-bit samples."""
        squares = bass_squares = 0.0
        low = self._low
        for sample in samples:
            value = sample / 32768
            low += self._alpha * (value - low)
            squares += value * value
            bass_squares += low * low
        self._low = low
        count = max(len(samples), 1)
        level, bass = math.sqrt(squares / count), math.sqrt(bass_squares / count)
        self._peak_level = max(level, self._peak_level * GAIN_DECAY_PER_FRAME, QUIET_FLOOR)
        self._peak_bass = max(bass, self._peak_bass * GAIN_DECAY_PER_FRAME, QUIET_FLOOR)
        average = sum(self._bass_history) / len(self._bass_history) if self._bass_history else 0
        self._bass_history.append(bass)
        self._since_beat_s += count / RATE_HZ
        beat = (
            bass > QUIET_FLOOR
            and bass > BEAT_RATIO * average
            and self._since_beat_s >= MIN_BEAT_GAP_S
        )
        if beat:
            self._since_beat_s = 0.0
        if level < QUIET_FLOOR:
            return SILENCE
        return Sound(level / self._peak_level, bass / self._peak_bass, beat)


class Listener:
    """Keeps the latest Sound while `run` reads the monitor."""

    def __init__(self) -> None:
        self.sound = SILENCE
        self.beats = 0  # Counted, so a show sees each beat once however it samples.

    async def run(self) -> None:
        parec = shutil.which("parec")
        if parec is None:
            raise HueError("Music modes need parec (pulseaudio-utils) to hear the computer.")
        command = [
            parec,
            "--device=@DEFAULT_MONITOR@",
            "--format=s16le",
            f"--rate={RATE_HZ}",
            "--channels=1",
            "--latency-msec=20",
        ]
        analyzer = Analyzer()
        async with await anyio.open_process(command, stderr=None) as process:
            await self._read(process, analyzer)

    async def _read(self, process: Process, analyzer: Analyzer) -> None:
        if process.stdout is None:
            raise HueError("parec gave no output to read.")
        pending = b""
        frame_bytes = FRAME_SAMPLES * 2
        async for chunk in process.stdout:
            pending += chunk
            while len(pending) >= frame_bytes:
                samples = array("h", pending[:frame_bytes])
                if sys.byteorder == "big":  # parec sends little-endian.
                    samples.byteswap()
                pending = pending[frame_bytes:]
                self.sound = analyzer.feed(samples)
                self.beats += self.sound.beat
        raise HueError("parec stopped, so the show can't hear the computer any more.")
