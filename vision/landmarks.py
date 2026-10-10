"""MediaPipe pose-landmark wrapper.

MediaPipe is imported lazily so the server, website and tests run on machines
that do not have it (only the vision thread needs it).
"""

from __future__ import annotations

import logging
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple

import config

log = logging.getLogger(__name__)

# BlazePose 33-point landmark indices.
EAR = {"left": 7, "right": 8}
EYE = {"left": 2, "right": 5}
SHOULDER = {"left": 11, "right": 12}
WRIST = {"left": 15, "right": 16}
HIP = {"left": 23, "right": 24}
KNEE = {"left": 25, "right": 26}
NOSE = 0

MODEL_URLS = {
    "lite": (
        "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
        "pose_landmarker_lite/float16/1/pose_landmarker_lite.task"
    ),
    "full": (
        "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
        "pose_landmarker_full/float16/1/pose_landmarker_full.task"
    ),
    "heavy": (
        "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
        "pose_landmarker_heavy/float16/1/pose_landmarker_heavy.task"
    ),
}

#: (x, y, visibility) in *pixels*.
Point = Tuple[float, float, float]


def pick_side(pose_landmarks) -> str:
    """Return the body side facing the camera.

    MediaPipe reports both sides even when one is hidden; the far side is
    partly a guess, so pick whichever side has the higher summed visibility
    across ear, shoulder, hip and knee.
    """
    scores = {}
    for side in ("left", "right"):
        total = 0.0
        for indices in (EAR, EYE, SHOULDER, WRIST, HIP, KNEE):
            lm = pose_landmarks[indices[side]]
            vis = getattr(lm, "visibility", None)
            total += float(vis) if vis is not None else 1.0
        scores[side] = total
    return "left" if scores["left"] >= scores["right"] else "right"


class PoseLandmarker:
    """Thin wrapper around ``mediapipe.tasks`` pose landmarking."""

    def __init__(
        self,
        model: str = "lite",
        model_dir: Optional[Path] = None,
        min_detection_confidence: float = 0.5,
        min_presence_confidence: float = 0.5,
        min_tracking_confidence: float = 0.5,
    ):
        model = (model or "lite").lower()
        if model not in MODEL_URLS:
            raise ValueError(f"unknown model {model!r}; pick one of {sorted(MODEL_URLS)}")
        self.model = model
        self.model_dir = Path(model_dir or config.MODELS_DIR)
        self.model_path = self._ensure_model(model)
        self._last_ms = 0
        self._landmarker = self._build(
            min_detection_confidence,
            min_presence_confidence,
            min_tracking_confidence,
        )

    # -- setup ------------------------------------------------------------
    def _ensure_model(self, model: str) -> Path:
        self.model_dir.mkdir(parents=True, exist_ok=True)
        path = self.model_dir / f"pose_landmarker_{model}.task"
        if not path.exists() or path.stat().st_size == 0:
            url = MODEL_URLS[model]
            log.info("downloading MediaPipe model %s -> %s", model, path)
            tmp = path.with_suffix(".part")
            urllib.request.urlretrieve(url, tmp)
            tmp.replace(path)
        return path

    def _build(self, det: float, pres: float, track: float):
        import mediapipe as mp  # lazy: heavy import

        options = mp.tasks.vision.PoseLandmarkerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=str(self.model_path)),
            running_mode=mp.tasks.vision.RunningMode.VIDEO,
            num_poses=1,
            min_pose_detection_confidence=det,
            min_pose_presence_confidence=pres,
            min_tracking_confidence=track,
        )
        return mp.tasks.vision.PoseLandmarker.create_from_options(options)

    # -- inference --------------------------------------------------------
    def detect(self, frame_bgr) -> Optional[Dict[str, object]]:
        """Return pixel-space landmarks for the visible side, or ``None``."""
        import cv2
        import mediapipe as mp

        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

        ms = int(self._last_ms) + 1  # detect_for_video needs increasing stamps
        self._last_ms = ms
        result = self._landmarker.detect_for_video(mp_image, ms)

        if not result.pose_landmarks:
            return None

        lms = result.pose_landmarks[0]
        side = pick_side(lms)
        height, width = frame_bgr.shape[:2]

        def point(indices) -> Point:
            lm = lms[indices[side]]
            vis = getattr(lm, "visibility", None)
            return (float(lm.x) * width, float(lm.y) * height, float(vis) if vis is not None else 1.0)

        nose = lms[NOSE]
        nose_vis = getattr(nose, "visibility", None)
        return {
            "side": side,
            "ear": point(EAR),
            "eye": point(EYE),
            "shoulder": point(SHOULDER),
            "wrist": point(WRIST),
            "hip": point(HIP),
            "knee": point(KNEE),
            "nose": (float(nose.x) * width, float(nose.y) * height, float(nose_vis) if nose_vis is not None else 1.0),
        }

    def close(self) -> None:
        try:
            self._landmarker.close()
        except Exception:  # pragma: no cover
            pass
