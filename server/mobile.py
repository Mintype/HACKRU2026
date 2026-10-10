"""Mobile mode: the camera is a phone in a browser, the brains are still here.

The Pi app pulls frames from a USB webcam. Here the phone captures them and posts
them to ``/m/frame``, because a phone is the camera people actually carry around —
and because you can walk it across a room, point it at people from any angle, and
ask "who here is sitting badly?".

Everything expensive stays on the server: MediaPipe pose, the per-person scoring,
the roast writing (Gemini) and the voices (ElevenLabs). The phone only captures,
uploads a JPEG, draws the skeleton it is told about, and speaks the insult it
receives.

This module is the seam between those two halves. It is deliberately ignorant of
Flask: :class:`MobileVision` takes JPEG bytes and returns a plain dict, so the
whole pipeline can be tested by posting a frame in a test.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Dict, List, Optional

from vision.multipose import MultiTracker

log = logging.getLogger(__name__)

#: JPEG frames arrive at roughly this rate; used only to size the per-person
#: calibration window in frames.
ASSUMED_FPS = 8.0


class MobileVision:
    """Decodes posted frames, tracks the people in them, and reports the roster."""

    def __init__(
        self,
        *,
        landmarker=None,
        machine_factory: Optional[Callable[[], object]] = None,
        max_people: int = 3,
        on_roast: Optional[Callable[[Dict], None]] = None,
        publish: Optional[Callable[[Dict, object, float], None]] = None,
        tracker: Optional[MultiTracker] = None,
        calibration_seconds: float = 2.0,
    ):
        self.landmarker = landmarker
        self.on_roast = on_roast
        self.publish = publish
        self.max_people = max(1, int(max_people))
        self.tracker = tracker or MultiTracker(
            machine_factory or (lambda: None),
            max_people=self.max_people,
            calibration_seconds=calibration_seconds,
            fps=ASSUMED_FPS,
        )
        self.devices: Dict[str, float] = {}
        self.frames = 0
        self.dropped = 0
        self.decode_ms = 0.0
        self.detect_ms = 0.0
        self.last_frame_at: Optional[float] = None

    # -- frames -----------------------------------------------------------
    def decode(self, jpeg_bytes: bytes, ts: Optional[float] = None):
        """JPEG bytes -> BGR frame, or ``None`` when the payload is not an image."""
        started = time.perf_counter()
        frame = None
        try:
            import cv2
            import numpy as np

            buffer = np.frombuffer(jpeg_bytes, dtype=np.uint8)
            frame = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
        except Exception as exc:  # noqa: BLE001 - a bad frame must never 500
            log.warning("could not decode a phone frame: %s", exc)
        self.decode_ms = (time.perf_counter() - started) * 1000.0
        return frame

    def handle_frame(self, jpeg_bytes: bytes, ts: Optional[float] = None, device: str = "phone") -> Dict:
        """One posted frame in, one roster + any roasts out."""
        ts = float(ts if ts is not None else time.time())
        frame = self.decode(jpeg_bytes)
        if frame is None:
            self.dropped += 1
            return {"ok": False, "error": "could not decode the frame", "ts": ts}

        started = time.perf_counter()
        poses: List[Dict] = []
        if self.landmarker is not None:
            try:
                poses = self.landmarker.detect_multi(frame)
            except Exception as exc:  # noqa: BLE001 - survive a frame that confuses the model
                log.warning("pose detection failed: %s", exc)
        self.detect_ms = (time.perf_counter() - started) * 1000.0

        result = self.tracker.update(poses, ts)
        self.frames += 1
        self.last_frame_at = ts
        self.devices[device] = ts

        self._publish(result, frame, ts)
        roasts = self._fire_roasts(result.get("roasts") or [])

        return {
            "ok": True,
            "ts": ts,
            "device": device,
            "people": result["people"],
            "roster": result["roster"],
            "roasts": roasts,
            "seen": result["seen"],
            "good": result["good"],
            "bad": result["bad"],
            "timing": {
                "decode_ms": round(self.decode_ms, 1),
                "detect_ms": round(self.detect_ms, 1),
            },
        }

    def state(self, ts: Optional[float] = None) -> Dict:
        """The roster without needing a new frame (for a slow poll or a reload)."""
        ts = float(ts if ts is not None else time.time())
        for track in list(self.tracker.tracks.values()):
            if ts - track.last_seen > self.tracker.ttl:
                del self.tracker.tracks[track.id]
        roster = [track.describe(ts) for track in self.tracker.tracks.values()]
        roster.sort(key=lambda row: (row["score"] is None, row["score"] if row["score"] is not None else 1.0))
        return {
            "ok": True,
            "ts": ts,
            "roster": roster,
            "seen": len(roster),
            "good": sum(1 for row in roster if row["good"]),
            "bad": sum(1 for row in roster if row["bad"]),
            "frames": self.frames,
        }

    def status(self, ts: Optional[float] = None) -> Dict:
        ts = float(ts if ts is not None else time.time())
        return {
            "mode": "mobile",
            "frames": self.frames,
            "dropped": self.dropped,
            "people": len(self.tracker.tracks),
            "max_people": self.max_people,
            "landmarker": self.landmarker is not None,
            "devices": sorted(self.devices),
            "last_frame_ago": (
                None if self.last_frame_at is None else round(max(0.0, ts - self.last_frame_at), 2)
            ),
            "decode_ms": round(self.decode_ms, 1),
            "detect_ms": round(self.detect_ms, 1),
        }

    # -- helpers ----------------------------------------------------------
    def _publish(self, result: Dict, frame, ts: float) -> None:
        """Feed the desktop HUD the phone's picture, and the worst person's numbers.

        The frame is shared even when nobody is in it: aiming the phone is exactly
        when you want to see what it sees, and an empty room should look empty on
        the big screen rather than broken. Only the *reading* changes shape.
        """
        if self.publish is None:
            return
        track = result.get("primary")
        if track is not None and track.reading:
            reading = dict(track.reading)
            reading["state"] = getattr(track.machine, "state", None)
        else:
            # Nobody measurable in this frame: say so rather than freezing the HUD
            # on the last person who was.
            reading = {
                "ts": ts,
                "person": False,
                "seated": False,
                "score": None,
                "neck_angle": None,
                "torso_angle": None,
                "slouch_part": None,
                "calibrated": False,
                "state": None,
            }
        reading["ts"] = ts
        reading["mobile"] = True
        reading["people"] = len(result["roster"])
        try:
            self.publish(reading, frame, ts)
        except Exception:  # noqa: BLE001 - publishing must never break the frame loop
            log.warning("could not publish a mobile reading", exc_info=True)

    def _fire_roasts(self, events: List[Dict]) -> List[Dict]:
        out: List[Dict] = []
        for event in events:
            out.append(
                {
                    "person": event.get("person"),
                    "reason": event.get("reason"),
                    "slouch_count": event.get("slouch_count"),
                    "context": event.get("context"),
                }
            )
            if self.on_roast is not None:
                try:
                    self.on_roast(event)
                except Exception:  # noqa: BLE001 - a failed roast must not kill the stream
                    log.warning("mobile roast dispatch failed", exc_info=True)
        return out
