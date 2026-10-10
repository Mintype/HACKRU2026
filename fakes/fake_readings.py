"""Scripted readings: the "fake readings" fake from the build guide.

Produces a believable, repeating sequence of readings so the server, state
machine and website can be developed (and demoed) without the camera::

    away -> good -> slouch (roast) -> good -> slouch -> away ...

Run directly to print JSON readings at ~1 Hz, which is handy for poking at the
website while the real vision code is still being written::

    python -m fakes.fake_readings
"""

from __future__ import annotations

import argparse
import json
import random
import time
from typing import Dict, Iterator, Optional, Sequence, Tuple

BASELINE_NECK = 12.0
BASELINE_TORSO = 4.0

#: (phase, seconds) -- repeats forever.
DEFAULT_SCRIPT: Sequence[Tuple[str, float]] = (
    ("away", 3.0),
    ("good", 9.0),
    ("phone", 22.0),
    ("good", 10.0),
    ("slouch", 36.0),
    ("good", 10.0),
    ("slouch", 20.0),
    ("good", 8.0),
)


def _ramp(t: float, rising: float) -> float:
    """0 -> 1 over ``rising`` seconds, smooth-stepped."""
    x = max(0.0, min(1.0, t / max(rising, 1e-6)))
    return x * x * (3 - 2 * x)


def scripted_readings(
    *,
    start: Optional[float] = None,
    fps: float = 10.0,
    script: Sequence[Tuple[str, float]] = DEFAULT_SCRIPT,
    seed: int = 7,
    cycles: Optional[int] = None,
) -> Iterator[Dict]:
    """Yield readings forever (or for ``cycles`` passes through the script)."""
    rng = random.Random(seed)
    ts = float(start if start is not None else time.time())
    step = 1.0 / max(fps, 0.1)
    completed = 0

    while cycles is None or completed < cycles:
        for phase, duration in script:
            elapsed = 0.0
            while elapsed < duration:
                if phase == "away":
                    reading = {
                        "ts": ts,
                        "person": False,
                        "seated": False,
                        "neck_angle": None,
                        "torso_angle": None,
                        "score": None,
                        "raw_score": None,
                        "calibrated": True,
                        "thigh_angle": None,
                        "neck_drift": None,
                        "torso_drift": None,
                        "slouch_part": None,
                        "head_down_deg": None,
                        "head_ref_deg": None,
                        "phone_delta_deg": None,
                        "hands_up": False,
                        "phone": False,
                        "phone_seconds": 0.0,
                        "sim": True,
                        "phase": phase,
                    }
                else:
                    # "phone" is the interesting one: sitting up fine, but staring
                    # into a phone, so the score is good and the phone fault fires.
                    if phase == "slouch":
                        drift = _ramp(elapsed, min(duration * 0.45, 14.0))
                    else:
                        drift = 0.0
                    seated = True
                    phone = phase == "phone"

                    neck = BASELINE_NECK + drift * 30.0 + rng.uniform(-1.2, 1.2)
                    torso = BASELINE_TORSO + drift * 14.0 + rng.uniform(-0.8, 0.8)
                    # Score from the larger of the two drifts, against 20 deg.
                    neck_drift = neck - BASELINE_NECK
                    torso_drift = torso - BASELINE_TORSO
                    worst = max(neck_drift, torso_drift)
                    score = max(0.0, min(1.0, 1.0 - worst / 20.0))
                    if neck_drift > torso_drift + 3.0:
                        part = "neck"
                    elif torso_drift > neck_drift + 3.0:
                        part = "torso"
                    else:
                        part = "even"
                    head_down = round(52.0 + rng.uniform(-3.0, 3.0), 1) if phone else round(rng.uniform(2.0, 9.0), 1)
                    reading = {
                        "ts": ts,
                        "person": True,
                        "seated": seated,
                        "neck_angle": round(neck, 2),
                        "torso_angle": round(torso, 2),
                        "score": round(score, 3),
                        "raw_score": round(score, 3),
                        "calibrated": True,
                        "thigh_angle": 6.0 + rng.uniform(-2.0, 2.0),
                        "neck_drift": round(max(0.0, neck_drift), 2),
                        "torso_drift": round(max(0.0, torso_drift), 2),
                        "slouch_part": part,
                        "head_down_deg": head_down,
                        "head_ref_deg": 6.0,
                        "phone_delta_deg": round(head_down - 6.0, 1),
                        "hands_up": phone,
                        "phone": phone,
                        "phone_seconds": round(elapsed, 1) if phone else 0.0,
                        "sim": True,
                        "phase": phase,
                    }
                yield reading
                ts += step
                elapsed += step
        completed += 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Print scripted posture readings as JSON.")
    parser.add_argument("--fps", type=float, default=1.0, help="readings per second (default 1)")
    parser.add_argument("--cycles", type=int, default=None, help="stop after N script passes")
    args = parser.parse_args(argv)

    try:
        for reading in scripted_readings(fps=args.fps, cycles=args.cycles):
            print(json.dumps(reading), flush=True)
            time.sleep(1.0 / max(args.fps, 0.1))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
