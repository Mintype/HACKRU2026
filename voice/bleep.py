"""Censored swearing: turn ``BLEEP`` in a roast into a real TV bleep.

The roast prompt has Gemini write the word ``BLEEP`` wherever a swear word
would go, so the real word never exists anywhere. A TTS engine would just say
"bleep", so a line containing it is voiced piece by piece and spliced back
together with a 1 kHz tone where each ``BLEEP`` was.

Every piece is converted to 16-bit mono at the first piece's sample rate, so
clips from either engine (ElevenLabs PCM, espeak/say WAVs) splice cleanly.
"""

from __future__ import annotations

import audioop
import logging
import math
import re
import struct
import wave
from pathlib import Path
from typing import Callable, List, Optional

from . import wavutil

log = logging.getLogger(__name__)

MARKER = "BLEEP"
_MARKER_RE = re.compile(r"\bBLEEP\b")

BLEEP_HZ = 1000.0
BLEEP_SECONDS = 0.45
BLEEP_LEVEL = 0.5  # fraction of full scale; normalize() evens it out afterwards
FADE_SECONDS = 0.005  # ramps on the tone so it does not click
GAP_SECONDS = 0.06  # breath between a word and the bleep
SILENCE_LEVEL = 400  # 16-bit amplitude below which a TTS edge counts as silent
EDGE_PAD_SECONDS = 0.03  # keep a sliver of each piece's natural attack/decay

#: Voices one piece of text into a WAV at the given path; returns success.
Speak = Callable[[str, Path], bool]


def has_bleep(text: str) -> bool:
    return bool(_MARKER_RE.search(text or ""))


def split(text: str) -> List[Optional[str]]:
    """Split a line into spoken pieces and bleeps (``None``).

    Pieces with nothing speakable (just the "!" after a final BLEEP) are
    dropped so the engine is never asked to voice punctuation.
    """
    parts: List[Optional[str]] = []
    for i, chunk in enumerate(_MARKER_RE.split(text or "")):
        if i:
            parts.append(None)
        # A comma right after a bleep is the bleep's pause, not the voice's.
        chunk = chunk.strip().lstrip(",;:").strip()
        if any(ch.isalnum() for ch in chunk):
            parts.append(chunk)
    return parts


def tone(sample_rate: int, seconds: float = BLEEP_SECONDS) -> bytes:
    """A 16-bit mono sine bleep with short fades at both ends."""
    frames = int(sample_rate * seconds)
    fade = max(1, int(sample_rate * FADE_SECONDS))
    peak = BLEEP_LEVEL * 32767
    samples = []
    for n in range(frames):
        ramp = min(1.0, n / fade, (frames - 1 - n) / fade)
        samples.append(int(peak * ramp * math.sin(2 * math.pi * BLEEP_HZ * n / sample_rate)))
    return struct.pack(f"<{frames}h", *samples)


def _silence(sample_rate: int, seconds: float) -> bytes:
    return b"\x00\x00" * int(sample_rate * seconds)


def _trim(pcm: bytes, sample_rate: int) -> bytes:
    """Cut the quiet edges (including the Bluetooth lead-in) off a 16-bit mono piece."""
    step = max(1, sample_rate // 100) * 2  # 10 ms windows, in bytes
    windows = [pcm[i:i + step] for i in range(0, len(pcm), step)]
    loud = [i for i, w in enumerate(windows) if audioop.max(w, 2) >= SILENCE_LEVEL]
    if not loud:
        return b""
    pad = int(EDGE_PAD_SECONDS * 100)
    start = max(0, loud[0] - pad)
    end = min(len(windows), loud[-1] + 1 + pad)
    return b"".join(windows[start:end])


def _read_as_mono16(path: Path, rate: Optional[int]) -> tuple:
    """Read a WAV as 16-bit mono PCM, resampled to ``rate`` if one is given."""
    with wave.open(str(path), "rb") as wav:
        params = wav.getparams()
        pcm = wav.readframes(params.nframes)
    if params.sampwidth != 2:
        pcm = audioop.lin2lin(pcm, params.sampwidth, 2)
    if params.nchannels == 2:
        pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
    source_rate = params.framerate
    if rate is not None and source_rate != rate:
        pcm, _ = audioop.ratecv(pcm, 2, 1, source_rate, rate, None)
        source_rate = rate
    return pcm, source_rate


def synthesize(text: str, out_path: str | Path, speak: Speak) -> bool:
    """Voice ``text`` into ``out_path``, with a bleep tone for every BLEEP."""
    out_path = Path(out_path)
    rate: Optional[int] = None
    pieces: List[Optional[bytes]] = []

    for i, part in enumerate(split(text)):
        if part is None:
            pieces.append(None)
            continue
        piece_path = out_path.with_name(f"{out_path.stem}.part{i}.wav")
        try:
            if not speak(part, piece_path):
                return False
            pcm, rate = _read_as_mono16(piece_path, rate)
        except Exception as exc:  # noqa: BLE001 - a bad piece fails the clip, not the app
            log.warning("bleep splice: could not voice %r: %s", part, exc)
            return False
        finally:
            piece_path.unlink(missing_ok=True)
        pieces.append(_trim(pcm, rate))

    if not pieces:
        return False
    rate = rate or 24000

    gap = _silence(rate, GAP_SECONDS)
    beep = tone(rate)
    pcm = gap.join(beep if piece is None else piece for piece in pieces)

    wavutil.write_wav(out_path, pcm, sample_rate=rate, channels=1, sample_width=2)
    wavutil.normalize(out_path)
    return True
