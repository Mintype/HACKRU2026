"""The camera / vision thread.

One thread per the build guide: grab a frame, run pose detection, run the state
machine, draw the overlay, encode the frame as JPEG *once* and stash it in a
shared object.  Flask routes only ever read that shared object.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, Optional

from .state_machine import PostureStateMachine

log = logging.getLogger(__name__)


class VisionService(threading.Thread):
    def __init__(
        self,
        *,
        state_machine: PostureStateMachine,
        broker=None,
        db=None,
        on_event: Optional[Callable[[Dict, Dict], None]] = None,
        source=None,
        detector=None,
        simulate: bool = False,
        fake_readings=None,
        settings=None,
        fps_target: float = 10.0,
        jpeg_quality: int = 70,
        sample_interval: float = 1.0,
        publish_interval: float = 0.5,
    ):
        super().__init__(name="vision", daemon=True)
        self.state_machine = state_machine
        self.broker = broker
        self.db = db
        self.on_event = on_event
        self.source = source
        self.detector = detector
        self.simulate = bool(simulate or source is None)
        self.fake_readings = fake_readings
        self.settings = settings

        self.fps_target = float(fps_target)
        self.jpeg_quality = int(jpeg_quality)
        self.sample_interval = float(sample_interval)
        self.publish_interval = float(publish_interval)

        self._lock = threading.Lock()
        self._jpeg: Optional[bytes] = None
        self._frame = None
        self._reading: Dict = {"person": False, "seated": False, "score": None}
        self._stop = threading.Event()
        self._fps = 0.0
        self._last_publish = 0.0
        self._last_log = 0.0
        self.session_id: Optional[int] = None
        self.frames = 0
        self.errors = 0

    # -- shared state -----------------------------------------------------
    def latest_jpeg(self) -> Optional[bytes]:
        with self._lock:
            return self._jpeg

    def latest_frame(self):
        """The latest annotated BGR frame (used by the standalone preview)."""
        with self._lock:
            return self._frame

    def latest_reading(self) -> Dict:
        with self._lock:
            return dict(self._reading)

    @property
    def fps(self) -> float:
        return round(self._fps, 1)

    def stop(self) -> None:
        self._stop.set()

    # -- main loop --------------------------------------------------------
    def run(self) -> None:
        fake = self.fake_readings
        if self.simulate and fake is None:
            from fakes.fake_readings import scripted_readings

            fake = scripted_readings()

        next_frame_at = time.time()
        interval = 1.0 / max(self.fps_target, 0.1)
        fps_window = []

        while not self._stop.is_set():
            loop_start = time.time()
            frame = None
            try:
                if self.simulate:
                    wait = next_frame_at - loop_start
                    if wait > 0 and self._stop.wait(wait):
                        break
                    next_frame_at = max(next_frame_at, time.time()) + interval
                    reading = next(fake)
                    raw_ts = reading.get("ts")
                    ts = float(raw_ts) if raw_ts is not None else time.time()
                    frame = self._render_frame(reading)
                else:
                    frame = self.source.read()
                    if frame is None:
                        self._stop.wait(0.05)
                        continue
                    ts = time.time()
                    if self._mirror_enabled():
                        # Flip before pose detection so the overlay is drawn on
                        # the mirrored frame and its text stays upright.
                        frame = self._flip(frame)
                    reading = self.detector.process(frame, ts)

                events = self.state_machine.update(reading)
                for event in events:
                    self._handle_event(event, reading)

                reading["state"] = self.state_machine.state
                if frame is not None:
                    self._overlay(frame, reading)
                    self._store_frame(frame, reading, ts)
                else:
                    self._store_reading(reading)
                self._maybe_publish(ts, reading)
                self._maybe_log(ts, reading)

                self.frames += 1
                fps_window.append(loop_start)
                fps_window[:] = [t for t in fps_window if loop_start - t <= 2.0]
                if fps_window:
                    self._fps = len(fps_window) / max(1e-6, (loop_start - fps_window[0]) or 1.0)
            except StopIteration:
                log.info("simulated readings exhausted; stopping vision thread")
                break
            except Exception as exc:  # keep the demo alive
                self.errors += 1
                log.warning("vision loop error: %s", exc, exc_info=True)
                self._stop.wait(0.2)

        if self.source is not None:
            try:
                self.source.release()
            except Exception:
                pass

    # -- helpers ----------------------------------------------------------
    def _render_frame(self, reading: Dict):
        try:
            from vision import synthetic

            return synthetic.render(reading, mirror=self._mirror_enabled())
        except Exception:
            return None

    def _mirror_enabled(self) -> bool:
        if self.settings is None:
            return False
        try:
            return bool(self.settings.get("mirror"))
        except Exception:
            return False

    def _flip(self, frame):
        try:
            import cv2

            return cv2.flip(frame, 1)
        except Exception:
            return frame

    def _overlay(self, frame, reading: Dict) -> None:
        if self.detector is None:
            return
        try:
            self.detector.draw_overlay(frame, reading)
        except Exception:
            pass

    def _store_frame(self, frame, reading: Dict, ts: float) -> None:
        jpeg = self._encode(frame)
        with self._lock:
            if jpeg is not None:
                self._jpeg = jpeg
            self._frame = frame
            self._reading = reading

    def _store_reading(self, reading: Dict) -> None:
        with self._lock:
            self._reading = reading

    def _encode(self, frame) -> Optional[bytes]:
        try:
            import cv2

            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality])
            return buf.tobytes() if ok else None
        except Exception:
            return None

    def _handle_event(self, event: Dict, reading: Dict) -> None:
        name = event.get("event")

        if name == "session_start":
            if self.db is not None:
                try:
                    self.session_id = self.db.start_session(event.get("ts"))
                except Exception:
                    pass
        elif name == "calibrated":
            if self.detector is not None:
                self.detector.set_baseline(event.get("neck_baseline", 0.0), event.get("torso_baseline", 0.0))
            if self.db is not None and self.session_id is not None:
                try:
                    self.db.set_session_baseline(
                        self.session_id, event.get("neck_baseline", 0.0), event.get("torso_baseline", 0.0)
                    )
                except Exception:
                    pass
        elif name == "state":
            if self.db is not None:
                try:
                    self.db.log_state(self.session_id, event.get("ts"), event.get("state"), event.get("prev"))
                except Exception:
                    pass
        elif name == "session_end":
            if self.db is not None and self.session_id is not None:
                try:
                    self.db.end_session(self.session_id, event.get("ts"))
                except Exception:
                    pass
            self.session_id = None

        if self.broker is not None:
            self.broker.publish({"kind": "event", **event})
        if self.on_event is not None:
            try:
                self.on_event(event, reading)
            except Exception:
                log.warning("event handler failed for %s", name, exc_info=True)

    def _maybe_publish(self, ts: float, reading: Dict) -> None:
        if self.broker is None or (ts - self._last_publish) < self.publish_interval:
            return
        self._last_publish = ts
        snapshot = self.state_machine.snapshot(ts)
        self.broker.publish(
            {
                "kind": "reading",
                "ts": ts,
                "person": reading.get("person"),
                "seated": reading.get("seated"),
                "neck_angle": reading.get("neck_angle"),
                "torso_angle": reading.get("torso_angle"),
                "score": reading.get("score"),
                "raw_score": reading.get("raw_score"),
                "neck_drift": reading.get("neck_drift"),
                "torso_drift": reading.get("torso_drift"),
                "slouch_part": reading.get("slouch_part"),
                "thigh_angle": reading.get("thigh_angle"),
                "head_down_deg": reading.get("head_down_deg"),
                "hands_up": reading.get("hands_up"),
                "phone": reading.get("phone"),
                "phone_seconds": reading.get("phone_seconds"),
                "state": reading.get("state"),
                "fps": self.fps,
                "calibrated": reading.get("calibrated"),
                # everything the web page's Debug panel needs to tick live
                "slouch_seconds": snapshot.get("slouch_seconds"),
                "cooldown_remaining": snapshot.get("cooldown_remaining"),
                "calibration_progress": snapshot.get("calibration_progress"),
                "next_roast": snapshot.get("next_roast"),
            }
        )

    def _maybe_log(self, ts: float, reading: Dict) -> None:
        # Log readings sampled, not every frame.
        if self.db is None or (ts - self._last_log) < self.sample_interval:
            return
        self._last_log = ts
        try:
            self.db.log_reading(self.session_id, reading)
        except Exception:
            pass

    def status(self) -> Dict:
        return {
            "mode": "simulate" if self.simulate else "camera",
            "source": self.source.describe() if self.source is not None else "simulated",
            "fps": self.fps,
            "frames": self.frames,
            "errors": self.errors,
            "running": self.is_alive() and not self._stop.is_set(),
            "session_id": self.session_id,
        }
