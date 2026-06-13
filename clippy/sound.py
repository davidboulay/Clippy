"""Short, modern 'copy' sounds.

Each sound is synthesised on demand (no binary assets to ship) and cached as a
WAV under ``DATA_DIR/sounds/<id>.v<rev>.wav``, then played via whatever
PipeWire/PulseAudio/ALSA player is available. GTK-free.

The voices layer the ingredients real UI sounds use — a filtered-noise attack
transient, FM tones (glassy/woody rather than plain sine), soft saturation, and
a little early-reflection 'air' — then normalise to a consistent, audible peak.
"""
from __future__ import annotations

import math
import random
import shutil
import struct
import subprocess
import wave
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import config

_RATE = 44_100
# Linux players first; "afplay" is the built-in macOS player (same [player, path]
# invocation). shutil.which picks whichever exists, so this stays platform-safe.
_PLAYERS = ("pw-play", "paplay", "aplay", "afplay")

DEFAULT_SOUND = "tap"

# Bump whenever a synth function changes so cached WAVs are regenerated rather
# than reused (cached files are named ``<id>.v<rev>.wav``).
_SOUND_REV = 3

Env = Callable[[float], float]


# -- DSP toolkit -----------------------------------------------------------
def _expdec(tau: float, attack: float = 0.0015) -> Env:
    """Exponential decay with a short linear attack to avoid an onset click."""
    return lambda t: math.exp(-t / tau) * min(1.0, t / attack)


def _alpha(fc: float) -> float:
    """One-pole filter coefficient for a given cutoff (Hz)."""
    return 1.0 - math.exp(-2.0 * math.pi * fc / _RATE)


def _lowpass(samples: List[float], fc: float) -> List[float]:
    a, y, out = _alpha(fc), 0.0, []
    for x in samples:
        y += a * (x - y)
        out.append(y)
    return out


def _highpass(samples: List[float], fc: float) -> List[float]:
    lp = _lowpass(samples, fc)
    return [x - l for x, l in zip(samples, lp)]


def _sine(n: int, f: float, env: Env) -> List[float]:
    return [math.sin(2 * math.pi * f * i / _RATE) * env(i / _RATE) for i in range(n)]


def _glide(n: int, f0: float, f1: float, tau: float, env: Env) -> List[float]:
    """Sine whose pitch glides exponentially from f0 to f1."""
    out, ph = [], 0.0
    for i in range(n):
        t = i / _RATE
        f = f1 + (f0 - f1) * math.exp(-t / tau)
        ph += 2 * math.pi * f / _RATE
        out.append(math.sin(ph) * env(t))
    return out


def _fm(n: int, fc: float, ratio: float, index: Env, env: Env) -> List[float]:
    """Two-operator FM voice — the source of glassy/woody, non-MIDI timbres."""
    fmod, out = fc * ratio, []
    for i in range(n):
        t = i / _RATE
        s = math.sin(2 * math.pi * fc * t + index(t) * math.sin(2 * math.pi * fmod * t))
        out.append(s * env(t))
    return out


def _noise(n: int, env: Env, lp: Optional[float] = None,
           hp: Optional[float] = None) -> List[float]:
    s = [random.uniform(-1.0, 1.0) for _ in range(n)]
    if lp:
        s = _lowpass(s, lp)
    if hp:
        s = _highpass(s, hp)
    return [x * env(i / _RATE) for i, x in enumerate(s)]


def _mix(*layers: List[float]) -> List[float]:
    n = max(len(l) for l in layers)
    out = [0.0] * n
    for layer in layers:
        for i, x in enumerate(layer):
            out[i] += x
    return out


def _sat(samples: List[float], drive: float) -> List[float]:
    """Soft saturation (tanh) — adds warmth and tames peaks."""
    return [math.tanh(drive * x) for x in samples]


def _air(samples: List[float], mix: float) -> List[float]:
    """Cheap early reflections for a sense of space (not a full reverb)."""
    out = list(samples)
    for delay_ms, g in ((11.0, 0.5), (19.0, 0.32), (29.0, 0.2)):
        d = int(_RATE * delay_ms / 1000.0)
        for i in range(d, len(out)):
            out[i] += mix * g * samples[i - d]
    return out


def _finalize(samples: List[float], peak: float) -> List[float]:
    """Normalise to a target peak and apply a short fade-out (no end click)."""
    m = max((abs(s) for s in samples), default=0.0) or 1.0
    g = peak / m
    n = len(samples)
    fade = min(96, n)  # ~2 ms
    out = []
    for i, s in enumerate(samples):
        v = s * g
        if i > n - fade:
            v *= (n - i) / fade
        out.append(v)
    return out


# -- voices ----------------------------------------------------------------
def _tap() -> List[float]:
    """A soft, rounded finger-tap on a surface — warm and neutral."""
    n = int(_RATE * 0.075)
    transient = _noise(int(_RATE * 0.018), _expdec(0.006, 0.0005), lp=2200)
    body = _glide(n, 260, 118, 0.03, _expdec(0.032, 0.002))
    s = _sat(_mix([0.45 * x for x in transient], body), 1.3)
    return _finalize(_air(s, 0.14), 0.5)


def _click() -> List[float]:
    """A crisp, tactile click — bright noise snap with a tiny low body."""
    n = int(_RATE * 0.045)
    snap = _noise(int(_RATE * 0.012), _expdec(0.0032, 0.0003), hp=1600, lp=7000)
    body = _sine(n, 330, _expdec(0.018, 0.001))
    s = _mix(snap, [0.4 * x for x in body])
    return _finalize(_air(s, 0.1), 0.5)


def _pop() -> List[float]:
    """A round 'pop' — a saturated sine that drops in pitch."""
    n = int(_RATE * 0.06)
    body = _glide(n, 520, 175, 0.018, _expdec(0.022, 0.0015))
    s = _sat(body, 1.7)
    return _finalize(_air(s, 0.12), 0.5)


def _drop() -> List[float]:
    """A water-drop blip — a quick FM bell over a downward pitch glide."""
    n = int(_RATE * 0.13)
    body = _glide(n, 900, 430, 0.04, _expdec(0.05, 0.002))
    bell = _fm(n, 660, 2.0, lambda t: 2.5 * math.exp(-t / 0.03), _expdec(0.055))
    s = _mix(body, [0.5 * x for x in bell])
    return _finalize(_air(s, 0.2), 0.48)


def _ping() -> List[float]:
    """A clean glassy FM ping with a slight shimmer and a short tail."""
    n = int(_RATE * 0.16)
    bell = _fm(n, 1200, 2.01, lambda t: 3.0 * math.exp(-t / 0.05) + 0.5,
               _expdec(0.07, 0.001))
    return _finalize(_air(bell, 0.22), 0.46)


def _chime() -> List[float]:
    """A gentle two-note notification — two soft FM bells in sequence."""
    def note(f: float, dur: float) -> List[float]:
        return _fm(int(_RATE * dur), f, 2.0,
                   lambda t: 2.2 * math.exp(-t / 0.05) + 0.4, _expdec(0.08, 0.001))

    n1, n2 = note(784.0, 0.18), note(1174.7, 0.24)   # G5 → D6
    off = int(_RATE * 0.075)
    s = [0.0] * (off + len(n2))
    for i, x in enumerate(n1):
        s[i] += x
    for i, x in enumerate(n2):
        s[off + i] += x
    return _finalize(_air(s, 0.22), 0.44)


# Ordered registry: id -> (label, generator). Order drives the settings dropdown.
_SOUNDS: Dict[str, Tuple[str, Callable[[], List[float]]]] = {
    "tap": ("Tap", _tap),
    "click": ("Click", _click),
    "pop": ("Pop", _pop),
    "drop": ("Drop", _drop),
    "ping": ("Ping", _ping),
    "chime": ("Chime", _chime),
}

# Public (id, label) list for the settings UI.
SOUND_CHOICES: List[Tuple[str, str]] = [(k, v[0]) for k, v in _SOUNDS.items()]


def _path(sound_id: str) -> Path:
    return config.DATA_DIR / "sounds" / f"{sound_id}.v{_SOUND_REV}.wav"


def _resolve(sound_id: Optional[str]) -> str:
    if sound_id in _SOUNDS:
        return sound_id  # type: ignore[return-value]
    from . import settings
    choice = settings.get("sound_choice")
    return choice if choice in _SOUNDS else DEFAULT_SOUND


def _write_wav(path: Path, samples: List[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = bytearray()
    for s in samples:
        frames += struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(_RATE)
        wav.writeframes(bytes(frames))


def ensure(sound_id: Optional[str] = None) -> bool:
    """Create the chosen sound file if missing. Returns True if it exists."""
    sound_id = _resolve(sound_id)
    path = _path(sound_id)
    if path.exists():
        return True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Drop stale caches (legacy/older revisions, removed ids) so the current
        # sounds replace them instead of accumulating.
        for old in path.parent.glob("*.wav"):
            if not old.name.endswith(f".v{_SOUND_REV}.wav"):
                old.unlink()
        _write_wav(path, _SOUNDS[sound_id][1]())
        return True
    except OSError:
        return False


def _player() -> Optional[str]:
    for p in _PLAYERS:
        if shutil.which(p):
            return p
    return None


def play(sound_id: Optional[str] = None) -> None:
    """Fire-and-forget playback of a copy sound (the chosen one by default)."""
    sound_id = _resolve(sound_id)
    if not ensure(sound_id):
        return
    player = _player()
    if not player:
        return
    try:
        subprocess.Popen(
            [player, str(_path(sound_id))],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass
