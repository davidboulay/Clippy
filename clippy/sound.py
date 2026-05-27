"""A short, pleasant 'copy' sound.

The WAV is synthesised once (no binary asset to ship) and played via whatever
PipeWire/PulseAudio/ALSA player is available. GTK-free.
"""
from __future__ import annotations

import math
import shutil
import struct
import subprocess
import wave

from . import config

_RATE = 44_100
_PLAYERS = ("pw-play", "paplay", "aplay")


def ensure() -> bool:
    """Create the sound file if missing. Returns True if a file exists."""
    if config.SOUND_PATH.exists():
        return True
    try:
        _synthesize(config.SOUND_PATH)
        return True
    except OSError:
        return False


def _synthesize(path) -> None:
    """A soft two-partial 'tick' with a quick exponential decay."""
    config.ensure_dirs()
    duration = 0.13
    n = int(_RATE * duration)
    frames = bytearray()
    for i in range(n):
        t = i / _RATE
        env = math.exp(-t / 0.035)          # fast decay
        attack = min(1.0, t / 0.004)        # 4 ms attack to avoid a click
        sample = (
            0.6 * math.sin(2 * math.pi * 1046.5 * t)   # C6
            + 0.4 * math.sin(2 * math.pi * 2093.0 * t)  # C7
        )
        val = int(max(-1.0, min(1.0, sample * env * attack * 0.28)) * 32767)
        frames += struct.pack("<h", val)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(_RATE)
        wav.writeframes(bytes(frames))


def _player():
    for p in _PLAYERS:
        if shutil.which(p):
            return p
    return None


def play() -> None:
    """Fire-and-forget playback of the copy sound."""
    if not ensure():
        return
    player = _player()
    if not player:
        return
    try:
        subprocess.Popen(
            [player, str(config.SOUND_PATH)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass
