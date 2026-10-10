"""Synthetic camera feed used by ``--simulate``.

Draws a side-view stick figure whose lean follows the reading, so the website
looks alive on a laptop with no camera and no MediaPipe install.  Returns
``None`` when NumPy/OpenCV are missing -- the API still works without video.
"""

from __future__ import annotations

import math
from typing import Dict, Optional, Tuple

WIDTH, HEIGHT = 640, 480
TORSO_LEN = 115.0
NECK_LEN = 48.0
THIGH_LEN = 95.0
SHIN_LEN = 95.0


def available() -> bool:
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401

        return True
    except Exception:
        return False


def _polar(origin: Tuple[float, float], angle_deg: float, length: float, dy_sign: float):
    """Point ``length`` away from ``origin``, ``angle_deg`` off vertical.

    ``dy_sign`` is ``-1`` for "up" (image y grows downward).
    """
    rad = math.radians(angle_deg)
    x = origin[0] + math.sin(rad) * length
    y = origin[1] + dy_sign * math.cos(rad) * length
    return (x, y)


def render(reading: Dict[str, object], width: int = WIDTH, height: int = HEIGHT, mirror: bool = False):
    if not available():
        return None

    import cv2
    import numpy as np

    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:] = (32, 28, 26)
    # Room: floor + a faint wall grid.
    cv2.line(frame, (0, int(height * 0.72)), (width, int(height * 0.72)), (48, 44, 42), 2)
    for x in range(0, width, 64):
        cv2.line(frame, (x, 0), (x, height), (40, 36, 34), 1)

    score = reading.get("score")
    if score is None:
        color = (180, 180, 180)
    elif score >= 0.8:
        color = (90, 220, 120)
    elif score >= 0.7:
        color = (60, 200, 240)
    else:
        color = (80, 80, 240)

    person = bool(reading.get("person"))
    seated = bool(reading.get("seated"))

    if not person:
        cv2.putText(frame, "AWAY - no person detected", (24, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (140, 140, 140), 2)
        cv2.putText(frame, "SIMULATED FEED", (24, height - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (110, 110, 110), 1)
        return frame

    neck = float(reading.get("neck_angle") or 0.0)
    torso = float(reading.get("torso_angle") or 0.0)
    thigh = reading.get("thigh_angle")
    thigh = float(thigh) if thigh is not None else (6.0 if seated else 88.0)

    hip = (width * 0.46, height * 0.72 - (SHIN_LEN if seated else 0.0))
    shoulder = _polar(hip, torso, TORSO_LEN, -1)
    ear = _polar(shoulder, neck, NECK_LEN, -1)

    if seated:
        knee = (hip[0] + THIGH_LEN * math.cos(math.radians(thigh)), hip[1] + THIGH_LEN * math.sin(math.radians(thigh)))
        ankle = (knee[0], knee[1] + SHIN_LEN)
    else:
        knee = (hip[0], hip[1] + THIGH_LEN)
        ankle = (knee[0], knee[1] + SHIN_LEN)

    def ipt(p):
        # Mirror only the figure, never the text, so labels stay readable.
        x = (width - p[0]) if mirror else p[0]
        return int(x), int(p[1])

    cv2.line(frame, ipt(ear), ipt(shoulder), color, 4, cv2.LINE_AA)
    cv2.line(frame, ipt(shoulder), ipt(hip), color, 4, cv2.LINE_AA)
    cv2.line(frame, ipt(hip), ipt(knee), (170, 170, 170), 4, cv2.LINE_AA)
    cv2.line(frame, ipt(knee), ipt(ankle), (140, 140, 140), 3, cv2.LINE_AA)
    cv2.circle(frame, ipt(ear), 14, (210, 200, 190), -1, cv2.LINE_AA)
    for pt, radius in ((ear, 5), (shoulder, 6), (hip, 6), (knee, 5)):
        cv2.circle(frame, ipt(pt), radius, color, -1, cv2.LINE_AA)

    label = f"neck {neck:.0f}deg   torso {torso:.0f}deg"
    if score is not None:
        label += f"   score {score:.2f}"
    if reading.get("phone"):
        label += "   PHONE"
    cv2.putText(frame, label, (16, 62), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2, cv2.LINE_AA)

    # A little rectangle in front of the chest, so the demo shows what it saw.
    if reading.get("phone"):
        phone_rect = (int(hip[0] + 30), int(hip[1] - 40))
        cv2.rectangle(
            frame,
            phone_rect,
            (phone_rect[0] + 16, phone_rect[1] + 30),
            (150, 150, 160),
            -1,
        )

    state = str(reading.get("state") or "").upper()
    if state:
        cv2.rectangle(frame, (0, 0), (width, 36), (22, 20, 24), -1)
        cv2.putText(frame, state, (16, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2, cv2.LINE_AA)
    cv2.putText(frame, "SIMULATED FEED", (width - 210, height - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (110, 110, 110), 1)
    return frame
