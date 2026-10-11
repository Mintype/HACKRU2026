"""WAV helpers.

Gemini's TTS models return raw PCM samples, so a proper WAV header has to be
written.  Every clip also gets ~0.5 s of leading silence because a Bluetooth
speaker tends to clip the start while the link wakes up.
"""

from __future__ import annotations

import audioop
import wave
from pathlib import Path

LEAD_SILENCE_SECONDS = 0.5


def write_wav(
    path: str | Path,
    pcm: bytes,
    sample_rate: int = 24000,
    channels: int = 1,
    sample_width: int = 2,
    lead_silence: float = LEAD_SILENCE_SECONDS,
) -> Path:
    """Write raw little-endian PCM to ``path`` as a WAV file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    silence = _silence_bytes(lead_silence, sample_rate, channels, sample_width)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        if silence:
            wav.writeframes(silence)
        wav.writeframes(pcm)
    return path


def _silence_bytes(seconds: float, sample_rate: int, channels: int, sample_width: int) -> bytes:
    frames = int(max(0.0, seconds) * sample_rate)
    return b"\x00" * frames * channels * sample_width


def prepend_silence(path: str | Path, seconds: float = LEAD_SILENCE_SECONDS) -> Path:
    """Rewrite an existing WAV with leading silence (idempotent-ish)."""
    path = Path(path)
    if seconds <= 0:
        return path
    with wave.open(str(path), "rb") as wav:
        params = wav.getparams()
        frames = wav.readframes(params.nframes)
    silence = _silence_bytes(seconds, params.framerate, params.nchannels, params.sampwidth)
    with wave.open(str(path), "wb") as wav:
        wav.setparams(params)
        wav.writeframes(silence + frames)
    return path


def duration_seconds(path: str | Path) -> float:
    try:
        with wave.open(str(path), "rb") as wav:
            return wav.getnframes() / float(wav.getframerate() or 1)
    except Exception:
        return 0.0


def normalize(path: str | Path, target_dbfs: float = -3.0) -> Path:
    """Cheap loudness-normalise so Gemini clips and espeak clips match."""
    path = Path(path)
    try:
        with wave.open(str(path), "rb") as wav:
            params = wav.getparams()
            frames = wav.readframes(params.nframes)
        peak = audioop.max(frames, params.sampwidth) or 1
        target = int((10 ** (target_dbfs / 20.0)) * ((1 << (8 * params.sampwidth - 1)) - 1))
        factor = min(8.0, target / peak)
        if abs(factor - 1.0) > 0.05:
            frames = audioop.mul(frames, params.sampwidth, factor)
            with wave.open(str(path), "wb") as wav:
                wav.setparams(params)
                wav.writeframes(frames)
    except Exception:
        pass
    return path


def pcm_mime_to_params(mime_type: str):
    """Parse ``audio/L16;rate=24000`` -> (rate, width, channels)."""
    rate = 24000
    width = 2
    channels = 1
    mime = (mime_type or "").lower()
    for part in mime.split(";"):
        part = part.strip()
        if part.startswith("rate="):
            try:
                rate = int(part.split("=", 1)[1])
            except ValueError:
                pass
        elif "l16" in part or "pcm" in part:
            width = 2
    if "stereo" in mime:
        channels = 2
    return rate, width, channels
