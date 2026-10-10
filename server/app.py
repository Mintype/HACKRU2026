"""Flask application factory.

One Python process on the Pi: a Flask app plus a few background threads
(vision, roast pool, audio player).  Flask routes only ever *read* the shared
snapshot the vision thread publishes.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Dict, Optional

from flask import Flask, Response, jsonify, render_template, request, send_from_directory

import config
from vision.source import open_source
from voice.player import AudioPlayer
from voice.service import RoastService

from .camera import VisionService
from .db import Database
from .events import EventBroker
from .mobile import MobileVision
from .settings import AUDIO_ROUTES, MODELS, Settings
from .state_machine import PostureStateMachine

log = logging.getLogger(__name__)

#: Mobile mode calibrates each person quickly: a phone gets pointed at somebody
#: for a second or two, not at a fixed desk for five.
MOBILE_CALIBRATION_SECONDS = 2.0


class Coach:
    """Owns every moving part so the Flask app can stay thin."""

    def __init__(
        self,
        *,
        simulate: bool = False,
        camera: Optional[int] = None,
        video: Optional[str] = None,
        camera_backend: Optional[str] = None,
        model: Optional[str] = None,
        no_audio: bool = False,
        host: str = "0.0.0.0",
        port: int = 5000,
        settings_path: Optional[str] = None,
        settings_overrides: Optional[Dict[str, object]] = None,
        fake_readings=None,
        mobile: bool = False,
        mobile_max_people: Optional[int] = None,
        mobile_landmarker=None,
    ):
        config.ensure_dirs()
        self.host = host
        self.port = port
        self.no_audio = no_audio
        self.requested_simulate = bool(simulate)
        #: Mobile mode: the camera is a phone in a browser (see server/mobile.py).
        self.mobile_mode = bool(mobile)
        self.mobile: Optional[MobileVision] = None

        self.settings = Settings(path=settings_path, overrides=settings_overrides)
        if model:
            self.settings.update({"model": model})

        self.broker = EventBroker()
        self.db = Database()
        self.machine_config = self.settings.machine_config()
        self.state_machine = PostureStateMachine(self.machine_config)

        # -- vision -------------------------------------------------------
        # Side-view capture.  The camera index is remembered in settings so the
        # side-mounted USB camera (rather than the built-in one) is used next
        # time without repeating --camera.
        self.detector = None
        self.source = None
        self.simulate = bool(simulate)
        self.camera_index: Optional[int] = None
        self.mobile_max_people = max(
            1,
            int(mobile_max_people or self.settings.get("mobile_max_people") or 3),
        )

        if video is not None:
            source_spec: Optional[str | int] = video
        elif self.simulate:
            source_spec = None
        else:
            if camera is not None:
                self.camera_index = int(camera)
                self.settings.update({"camera_index": self.camera_index})
            else:
                self.camera_index = int(self.settings.get("camera_index", 0) or 0)
            source_spec = self.camera_index
        self.source_spec = source_spec

        # Mobile mode still needs the pose model, but it runs on frames posted by
        # phones rather than on a local camera, so it is built by hand here and
        # asked for several people at once.
        self.mobile_landmarker = mobile_landmarker
        if self.mobile_mode and self.mobile_landmarker is None:
            try:
                from vision.landmarks import PoseLandmarker

                self.mobile_landmarker = PoseLandmarker(
                    model=self.settings.get("model"),
                    num_poses=self.mobile_max_people,
                )
                log.info("mobile mode: pose model ready for up to %d people", self.mobile_max_people)
            except Exception as exc:  # noqa: BLE001
                log.error("mobile pose setup failed (%s); the roster will stay empty", exc)
                self.mobile_landmarker = None

        if source_spec is not None and not self.mobile_mode:
            try:
                from vision.detector import PostureDetector
                from vision.landmarks import PoseLandmarker
                from vision.source import resolve_backend

                landmarker = PoseLandmarker(model=self.settings.get("model"))
                self.detector = PostureDetector(
                    landmarker,
                    tolerance=float(self.settings.get("delta_angle")),
                )
                self.source = open_source(source_spec, backend=resolve_backend(camera_backend))
            except Exception as exc:
                log.error("vision setup failed (%s); falling back to simulated feed", exc)
                self.simulate = True
                self.detector = None
                self.source = None

        # -- voice --------------------------------------------------------
        self.player = AudioPlayer(
            route=str(self.settings.get("audio_route")),
            host=host,
            port=port,
            cast_name=str(self.settings.get("cast_name") or "") or None,
            no_audio=no_audio,
        )
        self.roast_service = RoastService(
            player=self.player,
            settings=self.settings,
            context_provider=self.roast_context,
            on_played=self._on_roast_played,
            pool_size=int(self.settings.get("roast_pool_size")),
        )

        # -- vision thread ------------------------------------------------
        self.vision = VisionService(
            state_machine=self.state_machine,
            broker=self.broker,
            db=self.db,
            source=self.source,
            detector=self.detector,
            simulate=self.simulate,
            settings=self.settings,
            fake_readings=fake_readings,
            on_event=self._on_vision_event,
            mobile=self.mobile_mode,
        )

        # -- mobile (the phone is the camera) -----------------------------
        if self.mobile_mode:
            self.mobile = MobileVision(
                landmarker=self.mobile_landmarker,
                machine_factory=self.mobile_machine,
                max_people=self.mobile_max_people,
                on_roast=self._on_mobile_roast,
                publish=self.vision.publish_external_frame,
                calibration_seconds=MOBILE_CALIBRATION_SECONDS,
            )

    # -- mobile helpers ---------------------------------------------------
    def mobile_machine(self) -> PostureStateMachine:
        """A state machine per tracked person, with mobile-appropriate settings.

        It is the same machine the Pi app uses, so hysteresis, roast timing and
        the cooldown behave identically and only the two things that are wrong
        for a room full of people are changed: calibration is quick (a phone is
        pointed at someone for a second or two, not at a fixed desk) and sleep is
        off, because people walk around instead of sitting still.
        """
        cfg = self.settings.machine_config()
        cfg.calibration_seconds = MOBILE_CALIBRATION_SECONDS
        cfg.sleep_threshold_seconds = 0.0
        return PostureStateMachine(cfg)

    def _on_mobile_roast(self, event: Dict) -> None:
        """A tracked person earned a roast; let the voice layer handle it.

        The event carries a per-person context, so the insult can name them, and
        the text reaches the phone through the broker when it is played (or
        decided, if the local audio route is off - the phone speaks it either
        way).
        """
        try:
            self.roast_service.on_roast_due(event)
        except Exception:  # noqa: BLE001
            log.warning("could not dispatch a mobile roast", exc_info=True)

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        self.roast_service.start()
        if not self.mobile_mode:
            self.vision.start()
        log.info(
            "Posture Coach ready on http://%s:%s (mode=%s, audio=%s)",
            self.host,
            self.port,
            "mobile" if self.mobile_mode else ("simulate" if self.simulate else "camera"),
            self.player.route,
        )

    def stop(self) -> None:
        self.vision.stop()
        self.roast_service.stop()
        try:
            self.db.close()
        except Exception:
            pass

    # -- context for the roast prompt ------------------------------------
    def roast_context(self) -> Dict:
        snap = self.state_machine.snapshot()
        reading = self.vision.latest_reading() or {}
        return {
            "slouch_count": snap.get("slouch_count", 0),
            "session_minutes": snap.get("session_minutes", 0.0),
            "worst_neck_angle": snap.get("worst_neck_angle"),
            "worst_torso_angle": snap.get("worst_torso_angle"),
            "slouch_part": snap.get("slouch_part"),
            "sleeping": bool(snap.get("sleeping")),
            "sleep_seconds": snap.get("sleep_seconds"),
            "head_down_deg": reading.get("head_down_deg"),
            "head_drop_deg": reading.get("head_drop_deg"),
            "neck_drift": reading.get("neck_drift"),
            "torso_drift": reading.get("torso_drift"),
        }

    # -- callbacks --------------------------------------------------------
    def _on_vision_event(self, event: Dict, reading: Dict) -> None:
        """Dispatch state-machine events that other layers care about."""
        if event.get("event") == "roast_due":
            self.roast_service.on_roast_due(event)

    def _on_roast_played(self, clip, event: Dict) -> None:
        origin = "test" if (event or {}).get("event") == "test_roast" else "auto"
        try:
            self.db.log_roast(
                self.vision.session_id,
                time.time(),
                clip.text,
                clip.tier,
                clip.source,
                (event or {}).get("slouch_count"),
                (event or {}).get("worst_neck_angle"),
                voice=getattr(clip, "voice", None),
                origin=origin,
            )
        except Exception:
            log.warning("could not log roast", exc_info=True)
        self.broker.publish(
            {
                "kind": "roast",
                "ts": time.time(),
                "text": clip.text,
                "tier": clip.tier,
                "source": clip.source,
                "voice": getattr(clip, "voice", None),
                "origin": origin,
                "slouch_count": (event or {}).get("slouch_count"),
                "neck_angle": (event or {}).get("worst_neck_angle"),
                "state": self.state_machine.state,
                # Mobile mode: which tracked person the insult is about, so the
                # phone can name them and highlight the right roster row.
                "person": (event or {}).get("person"),
                "person_context": (event or {}).get("context"),
            }
        )


def create_app(
    *,
    simulate: bool = False,
    camera: Optional[int] = None,
    video: Optional[str] = None,
    camera_backend: Optional[str] = None,
    model: Optional[str] = None,
    no_audio: bool = False,
    host: str = "0.0.0.0",
    port: int = 5000,
    settings_path: Optional[str] = None,
    settings_overrides: Optional[Dict[str, object]] = None,
    fake_readings=None,
    mobile: bool = False,
    mobile_max_people: Optional[int] = None,
    mobile_landmarker=None,
) -> Flask:
    config.ensure_dirs()
    app = Flask(
        __name__,
        template_folder=str(config.WEB_DIR),
        static_folder=str(config.WEB_DIR / "static"),
        static_url_path="/static",
    )
    app.config["JSON_SORT_KEYS"] = False

    coach = Coach(
        simulate=simulate,
        camera=camera,
        video=video,
        camera_backend=camera_backend,
        model=model,
        no_audio=no_audio,
        host=host,
        port=port,
        settings_path=settings_path,
        settings_overrides=settings_overrides,
        fake_readings=fake_readings,
        mobile=mobile,
        mobile_max_people=mobile_max_people,
        mobile_landmarker=mobile_landmarker,
    )
    app.coach = coach  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ pages
    @app.get("/")
    def index():
        return render_template("index.html")

    # ------------------------------------------------------------------ mobile
    # The phone page: camera in the browser, frames posted here, roster back.
    @app.get("/m")
    def mobile_page():
        return render_template("mobile.html")

    @app.post("/m/frame")
    def mobile_frame():
        if coach.mobile is None:
            return jsonify({"ok": False, "error": "start the server with --mobile"}), 409
        device = (request.args.get("device") or "phone").strip()[:40] or "phone"
        payload = request.get_data(cache=False, as_text=False)
        if not payload:
            return jsonify({"ok": False, "error": "empty frame"}), 400
        return jsonify(coach.mobile.handle_frame(payload, time.time(), device))

    @app.get("/m/state")
    def mobile_state():
        if coach.mobile is None:
            return jsonify({"ok": False, "error": "start the server with --mobile"}), 409
        return jsonify(coach.mobile.state())

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "mode": "simulate" if coach.simulate else "camera"})

    # ------------------------------------------------------------- video feed
    @app.get("/video_feed")
    def video_feed():
        def generate():
            last = None
            while True:
                jpeg = coach.vision.latest_jpeg()
                if not jpeg:
                    time.sleep(0.1)
                    continue
                if jpeg is not last:
                    last = jpeg
                    yield (
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(jpeg)).encode()
                        + b"\r\n\r\n"
                        + jpeg
                        + b"\r\n"
                    )
                time.sleep(0.02)

        return Response(
            generate(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
        )

    # ------------------------------------------------------------------ events
    @app.get("/events")
    def events():
        def generate():
            queue = coach.broker.subscribe()
            try:
                yield "retry: 2000\n\n"
                while True:
                    event = coach.broker.drain(queue, timeout=15)
                    if event is None:
                        yield ": keepalive\n\n"
                        continue
                    yield f"data: {json.dumps(event)}\n\n"
            finally:
                coach.broker.unsubscribe(queue)

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ------------------------------------------------------------------- stats
    @app.get("/api/stats")
    def api_stats():
        session_id = coach.vision.session_id
        try:
            stats = coach.db.session_stats(session_id)
        except Exception:
            stats = {"session_id": session_id}
        stats["live"] = coach.state_machine.snapshot()
        stats["reading"] = coach.vision.latest_reading()
        stats["fps"] = coach.vision.fps
        stats["state"] = coach.state_machine.state
        stats["settings"] = coach.settings.as_dict()
        return jsonify(stats)

    @app.get("/api/history")
    def api_history():
        session_id = coach.vision.session_id
        return jsonify(
            {
                "session_id": session_id,
                "readings": coach.db.history(session_id),
                "roasts": coach.db.roast_markers(session_id),
            }
        )

    @app.get("/api/roasts")
    def api_roasts():
        # The hall of shame is "what has it said lately", so it spans sessions
        # and includes test roasts (which may have no session at all).
        limit = request.args.get("limit", default=3, type=int)
        limit = max(1, min(int(limit or 3), 50))
        return jsonify({"session_id": coach.vision.session_id, "roasts": coach.db.recent_roasts(limit)})

    @app.get("/api/status")
    def api_status():
        return jsonify(
            {
                "vision": coach.vision.status(),
                "audio": coach.player.status(),
                "voice": coach.roast_service.status(),
                "server": {
                    "host": coach.host,
                    "port": coach.port,
                    "simulate": coach.simulate,
                    "camera_index": coach.camera_index,
                    "subscribers": coach.broker.subscriber_count,
                    "mobile": coach.mobile_mode,
                },
                "mobile": coach.mobile.status() if coach.mobile is not None else None,
                "db": {"path": str(coach.db.path)},
            }
        )

    # ---------------------------------------------------------------- controls
    @app.post("/api/calibrate")
    def api_calibrate():
        ok = coach.state_machine.request_calibration()
        if not ok:
            return jsonify({"ok": False, "error": "no active session - sit down first"}), 409
        coach.broker.publish({"kind": "event", "event": "calibrating", "ts": time.time()})
        return jsonify({"ok": True, "calibrating": True})

    @app.post("/api/test-roast")
    def api_test_roast():
        clip = coach.roast_service.test_roast()
        if clip is None:
            return jsonify({"ok": False, "error": "player busy, try again"}), 409
        return jsonify({"ok": True, "text": clip.text, "tier": clip.tier, "source": clip.source})

    # ---------------------------------------------------------------- settings
    @app.get("/api/settings")
    def api_settings_get():
        data = coach.settings.as_dict()
        data["audio_routes"] = list(AUDIO_ROUTES)
        data["models"] = list(MODELS)
        return jsonify(data)

    @app.post("/api/settings")
    def api_settings_post():
        patch = request.get_json(silent=True) or {}
        applied = coach.settings.update(patch)
        coach.settings.apply_to(coach.machine_config)
        if coach.detector is not None and "delta_angle" in applied:
            coach.detector.tolerance = float(applied["delta_angle"])
        if "audio_route" in applied:
            coach.player.set_route(str(applied["audio_route"]))
        if "tts_mode" in applied:
            # Ready clips have the old voice baked in, so drop them and let the
            # pool refill with the chosen engine.
            dropped = coach.roast_service.pool.clear()
            log.info("tts_mode -> %s; cleared %d pooled clips", applied["tts_mode"], dropped)
        if "cast_name" in applied:
            coach.player.cast_name = str(applied["cast_name"]) or None
        coach.broker.publish({"kind": "settings", "settings": coach.settings.as_dict()})
        return jsonify({"ok": True, "applied": applied, "settings": coach.settings.as_dict()})

    # ------------------------------------------------------------ roast clips
    @app.get("/media/roasts/<path:name>")
    def media_roast(name: str):
        return send_from_directory(str(config.ROAST_DIR), name, mimetype="audio/wav")

    return app
