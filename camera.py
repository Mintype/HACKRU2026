"""Always-on webcam capture + pose landmarks + posture metrics (OpenCV + MediaPipe).

A background thread keeps the camera open for as long as the server runs and
grabs frames continuously. A second thread runs MediaPipe pose estimation and
feeds a PostureAnalyzer, which for a side-on, seated view:

  * smooths the landmarks over time (One Euro filter: no jitter, little lag)
  * picks the side of the body facing the camera (with hysteresis)
  * works out which way the person faces
  * computes neck / trunk angles from vertical and a 0-100 posture score
  * optionally calibrates to the user's own "sitting well" baseline
"""
import json
import math
import os
import statistics
import threading
import time
import urllib.request
from pathlib import Path

import cv2

CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", 0))
FRAME_W = int(os.environ.get("FRAME_W", 640))
FRAME_H = int(os.environ.get("FRAME_H", 480))
TARGET_FPS = int(os.environ.get("TARGET_FPS", 30))   # FPS requested from the device
STREAM_FPS = int(os.environ.get("STREAM_FPS", 30))   # max FPS pushed to the browser
DETECT_EVERY = float(os.environ.get("DETECT_EVERY", 0.15))  # min seconds between pose runs
PRESENCE_TIMEOUT = 3.0      # person counts as "present" for this long after last detection
DETECT_WIDTH = 320          # fallback box detector downscales frames to this width
MAX_CAMERAS = int(os.environ.get("MAX_CAMERAS", 5))  # indices 0..MAX_CAMERAS-1 are probed

# ---- MediaPipe pose landmarker (Tasks API). Model is downloaded on first run. ----
# lite = fastest, full = noticeably more accurate (default), heavy = best but slow on a Pi.
POSE_VARIANT = os.environ.get("POSE_VARIANT", "full")
POSE_MODEL_PATH = os.environ.get(
    "POSE_MODEL_PATH",
    str(Path(__file__).parent / "models" / f"pose_landmarker_{POSE_VARIANT}.task"),
)
POSE_MODEL_URL = os.environ.get(
    "POSE_MODEL_URL",
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
    f"pose_landmarker_{POSE_VARIANT}/float16/1/pose_landmarker_{POSE_VARIANT}.task",
)
# Profile views are harder for the detector than front views, so be a bit lenient.
POSE_CONF = float(os.environ.get("POSE_CONF", 0.4))
VIS_THRESHOLD = 0.5         # ignore landmarks the model isn't confident about

# BlazePose indices for the joints we care about in a side view.
LANDMARK_NAMES = {
    0: "nose",
    7: "left_ear", 8: "right_ear",
    11: "left_shoulder", 12: "right_shoulder",
    23: "left_hip", 24: "right_hip",
}
SIDES = ("left", "right")

# ---- Smoothing (One Euro filter, units = pixels / seconds) ----
SMOOTH_MIN_CUTOFF = 0.5     # lower = smoother when still
SMOOTH_BETA = 0.02          # higher = less lag when moving fast

# ---- Posture scoring (degrees from vertical; all tunable) ----
SIDE_SWITCH_MARGIN = 0.4    # other side must beat the current one by this much to switch
MIN_FACING_PX = 6           # nose must be this far in front of the ear to trust "facing"
NECK_REF_DEFAULT = 8.0      # typical upright forward-head angle when not calibrated
TRUNK_REF_DEFAULT = 5.0     # typical upright trunk lean when not calibrated
NECK_GOOD_EXCESS, NECK_BAD_EXCESS = 5.0, 30.0    # degrees beyond reference: no penalty -> full
TRUNK_GOOD_EXCESS, TRUNK_BAD_EXCESS = 7.0, 25.0
W_NECK, W_TRUNK = 0.6, 0.4
CALIB_FILE = Path(os.environ.get("CALIB_FILE", Path(__file__).parent / "calibration.json"))


def list_cameras(skip=None, max_index=MAX_CAMERAS):
    """Indices that open as a camera. `skip` (e.g. the in-use one) is always
    included so the current choice never disappears from the UI."""
    found = set()
    if skip is not None:
        found.add(skip)
    for i in range(max_index):
        if i == skip:
            continue
        cap = cv2.VideoCapture(i)
        try:
            if cap.isOpened():
                found.add(i)
        finally:
            cap.release()
    return sorted(found)


class PersonDetector:
    """Looks for a person seen from the SIDE (profile face / upper body),
    with HOG full-body as a fallback. Only used if MediaPipe is unavailable."""

    def __init__(self):
        base = cv2.data.haarcascades
        self.profile = cv2.CascadeClassifier(base + "haarcascade_profileface.xml")
        self.upper = cv2.CascadeClassifier(base + "haarcascade_upperbody.xml")
        self.hog = cv2.HOGDescriptor()
        self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

    def detect(self, frame):
        """Return the first (x, y, w, h) box in ORIGINAL frame coordinates, or None."""
        h, w = frame.shape[:2]
        scale = DETECT_WIDTH / w
        small = cv2.resize(frame, (DETECT_WIDTH, int(h * scale)))
        gray = cv2.equalizeHist(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
        sw = gray.shape[1]

        box = None

        # 1) profile face facing one way, then the other (flip the image)
        faces = self.profile.detectMultiScale(gray, 1.1, 4, minSize=(30, 30))
        if len(faces):
            box = tuple(faces[0])
        else:
            flipped = cv2.flip(gray, 1)
            faces = self.profile.detectMultiScale(flipped, 1.1, 4, minSize=(30, 30))
            if len(faces):
                x, y, bw, bh = faces[0]
                box = (sw - x - bw, y, bw, bh)

        # 2) upper body
        if box is None:
            bodies = self.upper.detectMultiScale(gray, 1.1, 3, minSize=(60, 60))
            if len(bodies):
                box = tuple(bodies[0])

        # 3) HOG full body (slowest, so last)
        if box is None:
            rects, _ = self.hog.detectMultiScale(small, winStride=(8, 8), padding=(8, 8), scale=1.05)
            if len(rects):
                box = tuple(rects[0])

        if box is None:
            return None
        inv = 1 / scale
        return tuple(int(v * inv) for v in box)


def ensure_pose_model():
    """Download the pose landmarker model on first use (cached on disk)."""
    path = Path(POSE_MODEL_PATH)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[camera] downloading pose model -> {path}")
    tmp = path.with_suffix(path.suffix + ".part")
    urllib.request.urlretrieve(POSE_MODEL_URL, tmp)
    tmp.replace(path)
    return path


class PoseEstimator:
    """MediaPipe pose landmarks (Tasks API). Returns named joints in pixel coords
    as {name: (x, y, visibility)} plus a bounding box of the visible joints."""

    def __init__(self):
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision

        ensure_pose_model()
        base = mp_python.BaseOptions(model_asset_path=POSE_MODEL_PATH)
        options = vision.PoseLandmarkerOptions(
            base_options=base,
            running_mode=vision.RunningMode.VIDEO,
            num_poses=1,
            min_pose_detection_confidence=POSE_CONF,
            min_pose_presence_confidence=POSE_CONF,
            min_tracking_confidence=0.5,
        )
        self._landmarker = vision.PoseLandmarker.create_from_options(options)
        self._ts = 0
        print(f"[camera] pose model ready: {POSE_VARIANT}")

    def detect(self, frame):
        """Return (box, landmarks), either of which may be None."""
        import mediapipe as mp

        h, w = frame.shape[:2]
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        # detect_for_video needs strictly increasing timestamps
        self._ts = max(self._ts + 1, int(time.time() * 1000))
        result = self._landmarker.detect_for_video(image, self._ts)
        if not result.pose_landmarks:
            return None, None

        pose = result.pose_landmarks[0]
        landmarks = {}
        for idx, name in LANDMARK_NAMES.items():
            p = pose[idx]
            # keep sub-pixel precision; the analyzer smooths these
            landmarks[name] = (p.x * w, p.y * h, float(getattr(p, "visibility", 1.0)))

        visible = [(x, y) for x, y, v in landmarks.values() if v >= VIS_THRESHOLD]
        if not visible:
            return None, None
        xs = [x for x, _ in visible]
        ys = [y for _, y in visible]
        box = (int(min(xs)), int(min(ys)), int(max(xs) - min(xs)), int(max(ys) - min(ys)))
        return box, landmarks


class Detector:
    """Pose landmarks when MediaPipe is available, else the box detector."""

    def __init__(self):
        self._pose = None
        self._fallback = None
        try:
            self._pose = PoseEstimator()
        except Exception as exc:  # mediapipe missing / model download failed
            print(f"[camera] pose model unavailable ({exc}); using box detection")
            self._fallback = PersonDetector()

    def detect(self, frame):
        if self._pose is not None:
            return self._pose.detect(frame)
        return self._fallback.detect(frame), None


# --------------------------------------------------------------------------
# Smoothing + posture maths
# --------------------------------------------------------------------------
def _alpha(cutoff, dt):
    tau = 1.0 / (2 * math.pi * cutoff)
    return 1.0 / (1.0 + tau / dt)


class OneEuro:
    """One Euro filter: heavy smoothing when still, quick response when moving."""

    def __init__(self, min_cutoff=SMOOTH_MIN_CUTOFF, beta=SMOOTH_BETA, d_cutoff=1.0):
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.x = None
        self.dx = 0.0
        self.t = None

    def __call__(self, x, t):
        if self.t is None or t - self.t > 1.0:      # first sample, or stale -> restart
            self.x, self.dx, self.t = x, 0.0, t
            return x
        dt = max(t - self.t, 1e-3)
        dx = (x - self.x) / dt
        a_d = _alpha(self.d_cutoff, dt)
        self.dx = a_d * dx + (1 - a_d) * self.dx
        a = _alpha(self.min_cutoff + self.beta * abs(self.dx), dt)
        self.x = a * x + (1 - a) * self.x
        self.t = t
        return self.x


def _ramp(value, good, bad):
    """0 while value <= good, 1 once value >= bad, linear in between."""
    return min(1.0, max(0.0, (value - good) / (bad - good)))


def _angle_from_vertical(top, bottom, direction):
    """Degrees between the bottom->top line and vertical. Positive = top is
    displaced FORWARD (in the direction the person faces)."""
    dx = (top[0] - bottom[0]) * direction
    dy = bottom[1] - top[1]          # image y points down, so this is >0 when top is above
    return math.degrees(math.atan2(dx, dy))


class PostureAnalyzer:
    """Stateful: smooths landmarks and turns them into posture metrics.

    Metrics (angles are degrees from vertical, positive = leaning forward):
      neck_angle  ear vs shoulder  (forward head)
      back_angle  shoulder vs hip  (trunk lean)
      score       0-100, higher is better
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._filters = {}
        self._side = None
        self._facing = None            # +1 = faces image-right, -1 = image-left
        self._baseline = self._load_baseline()
        self._calib = None             # {"until": t, "neck": [], "trunk": []} while calibrating

    # ----- calibration -----
    @staticmethod
    def _load_baseline():
        try:
            data = json.loads(CALIB_FILE.read_text())
            return {"neck": float(data["neck"]),
                    "trunk": None if data.get("trunk") is None else float(data["trunk"])}
        except Exception:
            return None

    @property
    def calibrated(self):
        with self._lock:
            return self._baseline is not None

    @property
    def calibrating(self):
        with self._lock:
            return self._calib is not None

    def start_calibration(self, seconds=3.0):
        """Collect samples for `seconds` while the user sits in their BEST posture."""
        with self._lock:
            self._calib = {"until": time.time() + seconds, "neck": [], "trunk": []}

    def clear_calibration(self):
        with self._lock:
            self._baseline = None
            self._calib = None
        try:
            CALIB_FILE.unlink()
        except FileNotFoundError:
            pass

    def _calib_tick(self, now, neck=None, trunk=None):
        with self._lock:
            c = self._calib
            if c is None:
                return
            if now < c["until"]:
                if neck is not None:
                    c["neck"].append(neck)
                if trunk is not None:
                    c["trunk"].append(trunk)
                return
            self._calib = None
            if len(c["neck"]) >= 5:      # need a handful of samples or we keep the old baseline
                self._baseline = {
                    "neck": statistics.median(c["neck"]),
                    "trunk": statistics.median(c["trunk"]) if len(c["trunk"]) >= 5 else None,
                }
                try:
                    CALIB_FILE.write_text(json.dumps(self._baseline))
                except OSError:
                    pass

    # ----- per-frame -----
    def reset(self):
        """Forget temporal state (person left / camera switched)."""
        self._filters.clear()
        self._side = None
        self._facing = None

    def update(self, landmarks, now):
        """Return (smoothed_landmarks, metrics); both None if no usable pose."""
        if not landmarks:
            self.reset()
            self._calib_tick(now)
            return None, None

        smooth = {}
        for name, (x, y, v) in landmarks.items():
            if v < VIS_THRESHOLD:                  # don't feed junk into the filters
                smooth[name] = (x, y, v)
                continue
            fx, fy = self._filters.setdefault(name, (OneEuro(), OneEuro()))
            smooth[name] = (fx(x, now), fy(y, now), v)

        metrics = self._metrics(smooth)
        if metrics:
            self._calib_tick(now, metrics["neck_angle"], metrics["back_angle"])
        else:
            self._calib_tick(now)
        return smooth, metrics

    def _pick_side(self, lm):
        def vis(side):
            return sum(lm[f"{side}_{j}"][2] for j in ("ear", "shoulder", "hip"))
        if self._side is None:
            self._side = "left" if vis("left") >= vis("right") else "right"
        else:
            other = "right" if self._side == "left" else "left"
            if vis(other) > vis(self._side) + SIDE_SWITCH_MARGIN:   # hysteresis: no flicker
                self._side = other
        return self._side

    def _metrics(self, lm):
        side = self._pick_side(lm)
        ear, shoulder, hip = lm[f"{side}_ear"], lm[f"{side}_shoulder"], lm[f"{side}_hip"]
        if ear[2] < VIS_THRESHOLD or shoulder[2] < VIS_THRESHOLD:
            return None

        # which way is the person facing? the nose sits in front of the ear
        nose = lm["nose"]
        if nose[2] >= VIS_THRESHOLD and abs(nose[0] - ear[0]) > MIN_FACING_PX:
            self._facing = 1 if nose[0] > ear[0] else -1
        if self._facing is None or ear[1] >= shoulder[1]:     # unknown direction / ear below shoulder
            return None

        neck = _angle_from_vertical(ear, shoulder, self._facing)
        hip_ok = hip[2] >= VIS_THRESHOLD and hip[1] > shoulder[1]
        trunk = _angle_from_vertical(shoulder, hip, self._facing) if hip_ok else None

        with self._lock:
            base = self._baseline
        ref_neck = base["neck"] if base else NECK_REF_DEFAULT
        ref_trunk = base["trunk"] if base and base["trunk"] is not None else TRUNK_REF_DEFAULT

        p_neck = _ramp(neck - ref_neck, NECK_GOOD_EXCESS, NECK_BAD_EXCESS)
        if trunk is not None:
            p_trunk = _ramp(abs(trunk - ref_trunk), TRUNK_GOOD_EXCESS, TRUNK_BAD_EXCESS)
            penalty = W_NECK * p_neck + W_TRUNK * p_trunk
            conf = (ear[2] + shoulder[2] + hip[2]) / 3
        else:                                   # hip hidden (desk/chair): judge on the neck alone
            penalty = p_neck
            conf = (ear[2] + shoulder[2]) / 2

        return {
            "side": side,
            "facing": "right" if self._facing > 0 else "left",
            "neck_angle": round(neck, 1),
            "back_angle": None if trunk is None else round(trunk, 1),
            "score": round(100 * (1 - penalty), 1),
            "confidence": round(conf, 2),
            "calibrated": base is not None,
        }


# --------------------------------------------------------------------------
# Camera
# --------------------------------------------------------------------------
def _score_color(score):
    return (0, 190, 0) if score >= 70 else (0, 165, 255) if score >= 40 else (0, 0, 230)


class Camera:
    def __init__(self, index=CAMERA_INDEX):
        self.index = index
        self._lock = threading.Lock()
        self._thread = None
        self._detect_thread = None
        self._stop = threading.Event()
        self._switch = threading.Event()  # ask the loop to reopen on self.index
        self._frame_cond = threading.Condition()  # signals a new frame to streamers
        self._frame_id = 0
        self._analyzer = PostureAnalyzer()

        self._frame = None
        self._box = None
        self._landmarks = None
        self._metrics = None
        self._last_seen = 0.0
        self._opened = False
        self._error = None
        self._fps = 0.0

    # ----- lifecycle -----
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="camera", daemon=True)
        self._thread.start()
        # detection runs on its own thread so it never blocks frame capture
        self._detect_thread = threading.Thread(target=self._detect_loop, name="detect", daemon=True)
        self._detect_thread.start()

    def stop(self):
        self._stop.set()

    def set_index(self, index):
        """Switch to another camera index at runtime (reopens the device)."""
        index = int(index)
        with self._lock:
            if index == self.index:
                return
            self.index = index
            self._opened = False
            self._error = None
            self._box = None
            self._landmarks = None
            self._metrics = None
            self._last_seen = 0.0
        self._analyzer.reset()
        self._switch.set()

    def available_cameras(self):
        """Indices the UI can choose from (current one always included)."""
        with self._lock:
            current = self.index
        return {"current": current, "available": list_cameras(skip=current)}

    # ----- calibration (call from a route) -----
    def calibrate(self, seconds=3.0):
        """Ask the user to sit in their best posture, then call this."""
        self._analyzer.start_calibration(seconds)

    def clear_calibration(self):
        self._analyzer.clear_calibration()

    def _open(self):
        cap = cv2.VideoCapture(self.index, cv2.CAP_V4L2)  # V4L2 = USB webcams on the Pi
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(self.index)  # fallback for non-Linux dev machines
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_W)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_H)
            cap.set(cv2.CAP_PROP_FPS, TARGET_FPS)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            return cap
        cap.release()
        return None

    def _loop(self):
        """Capture thread: read frames as fast as the camera allows."""
        cap = None
        frames, fps_t = 0, time.time()

        while not self._stop.is_set():
            # a camera switch was requested: drop the current one so we reopen
            if self._switch.is_set():
                if cap is not None:
                    cap.release()
                    cap = None
                self._switch.clear()

            # (re)open the camera if needed; keep retrying forever
            if cap is None:
                cap = self._open()
                if cap is None:
                    self._set_state(opened=False, error=f"cannot open camera {self.index}")
                    time.sleep(2)
                    continue
                self._set_state(opened=True, error=None)

            ok, frame = cap.read()
            if not ok:
                cap.release()
                cap = None
                self._set_state(opened=False, error="lost camera, retrying")
                time.sleep(1)
                continue

            with self._lock:
                self._frame = frame
                self._frame_id += 1
            with self._frame_cond:
                self._frame_cond.notify_all()  # wake any MJPEG streamers

            frames += 1
            now = time.time()
            if now - fps_t >= 2:
                self._fps = frames / (now - fps_t)
                frames, fps_t = 0, now

        if cap is not None:
            cap.release()

    def _detect_loop(self):
        """Detection thread: run pose estimation on the latest frame, then analyse it."""
        detector = Detector()
        last_id = -1
        while not self._stop.is_set():
            with self._lock:
                if self._frame is None or self._frame_id == last_id:
                    frame = None
                else:
                    frame = self._frame.copy()
                    last_id = self._frame_id
            if frame is None:
                time.sleep(0.02)
                continue

            t0 = time.time()
            box, raw = detector.detect(frame)
            now = time.time()
            landmarks, metrics = self._analyzer.update(raw, now)
            with self._lock:
                self._box = box
                self._landmarks = landmarks
                self._metrics = metrics
                if box is not None:
                    self._last_seen = now

            # pace detection so it doesn't hog the CPU
            time.sleep(max(0.0, DETECT_EVERY - (now - t0)))

    def _set_state(self, opened, error):
        with self._lock:
            self._opened = opened
            self._error = error

    # ----- accessors -----
    def person_present(self):
        with self._lock:
            return (time.time() - self._last_seen) < PRESENCE_TIMEOUT

    def posture(self):
        """Latest posture metrics dict, or None if nobody is in view."""
        with self._lock:
            if (time.time() - self._last_seen) >= PRESENCE_TIMEOUT:
                return None
            return dict(self._metrics) if self._metrics else None

    def status(self):
        with self._lock:
            present = (time.time() - self._last_seen) < PRESENCE_TIMEOUT
            status = {
                "camera_open": self._opened,
                "camera_index": self.index,
                "person_detected": present,
                "box": self._box if present else None,
                "metrics": dict(self._metrics) if present and self._metrics else None,
                "fps": round(self._fps, 1),
                "error": self._error,
            }
        status["calibrated"] = self._analyzer.calibrated
        status["calibrating"] = self._analyzer.calibrating
        return status

    # ----- drawing -----
    def _annotate(self, frame, box, landmarks, metrics):
        """Draw pose landmarks + metrics if we have them, else the detector box."""
        if landmarks:
            self._draw_pose(frame, landmarks, metrics)
        elif box is not None:
            x, y, w, h = box
            cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)
        self._draw_hud(frame, metrics)
        return frame

    @staticmethod
    def _text(frame, text, org, color, scale=0.5, thick=1):
        cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), thick + 2, cv2.LINE_AA)
        cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, thick, cv2.LINE_AA)

    def _draw_hud(self, frame, metrics):
        if self._analyzer.calibrating:
            self._text(frame, "Calibrating... sit in your BEST posture", (10, 28), (0, 255, 255), 0.6, 2)
            return
        if not metrics:
            return
        score = metrics["score"]
        self._text(frame, f"Posture {score:.0f}/100", (10, 28), _score_color(score), 0.8, 2)
        if not metrics["calibrated"]:
            self._text(frame, "default thresholds (not calibrated)", (10, 50), (200, 200, 200), 0.45)

    def _draw_pose(self, frame, landmarks, metrics=None):
        """Highlight ear -> shoulder -> hip with the neck/back lines and their angles."""
        h, w = frame.shape[:2]

        def point(name):
            pt = landmarks.get(name)
            if pt is None or pt[2] < VIS_THRESHOLD:
                return None
            return (int(pt[0]), int(pt[1]))

        shoulder_color, hip_color, ear_color = (0, 255, 0), (0, 165, 255), (255, 255, 0)
        back_color, neck_color = (0, 220, 255), (255, 0, 255)

        if metrics:
            side = metrics["side"]
        else:  # side facing the camera = the one whose joints are most visible
            side = max(SIDES, key=lambda s: sum(
                landmarks[f"{s}_{j}"][2] for j in ("ear", "shoulder", "hip") if f"{s}_{j}" in landmarks))

        ear, shoulder, hip = point(f"{side}_ear"), point(f"{side}_shoulder"), point(f"{side}_hip")

        # faint vertical reference through the shoulder: the angles are measured from it
        if shoulder:
            cv2.line(frame, (shoulder[0], max(0, shoulder[1] - 150)),
                     (shoulder[0], min(h - 1, shoulder[1] + 150)), (200, 200, 200), 1, cv2.LINE_AA)

        # back (hip -> shoulder) and neck (shoulder -> ear) lines
        if shoulder and hip:
            cv2.line(frame, shoulder, hip, back_color, 3, cv2.LINE_AA)
        if ear and shoulder:
            cv2.line(frame, ear, shoulder, neck_color, 3, cv2.LINE_AA)

        # faint markers for the far side so the whole pose reads on screen
        for name, color in (("left_shoulder", shoulder_color), ("right_shoulder", shoulder_color),
                            ("left_hip", hip_color), ("right_hip", hip_color)):
            pt = point(name)
            if pt:
                cv2.circle(frame, pt, 4, color, -1, cv2.LINE_AA)

        # labelled joints of the visible side (with the measured angle where we have one)
        neck_txt = f"Ear (neck {metrics['neck_angle']:.0f} deg)" if metrics else "Ear"
        back_txt = ("Hip (back %.0f deg)" % metrics["back_angle"]
                    if metrics and metrics["back_angle"] is not None else "Hip")
        for pt, color, label in ((shoulder, shoulder_color, "Shoulder"),
                                 (hip, hip_color, back_txt),
                                 (ear, ear_color, neck_txt)):
            if not pt:
                continue
            cv2.circle(frame, pt, 7, color, -1, cv2.LINE_AA)
            cv2.circle(frame, pt, 7, (255, 255, 255), 1, cv2.LINE_AA)
            self._text(frame, label, (pt[0] + 10, pt[1] - 10), color)
        return frame

    def get_frame(self, annotate=True):
        """Latest frame (BGR) with landmarks/metrics drawn on it, or None."""
        with self._lock:
            if self._frame is None:
                return None
            frame = self._frame.copy()
            box, landmarks, metrics = self._box, self._landmarks, self._metrics
        if annotate:
            self._annotate(frame, box, landmarks, metrics)
        return frame

    def mjpeg(self):
        """Generator for a multipart MJPEG stream, one image per new frame."""
        interval = 1.0 / STREAM_FPS if STREAM_FPS > 0 else 0.0
        last_id = -1
        while True:
            with self._frame_cond:
                self._frame_cond.wait(timeout=1.0)  # wake as soon as a frame lands
            with self._lock:
                if self._frame_id == last_id or self._frame is None:
                    continue
                last_id = self._frame_id
                frame = self._frame.copy()
                box, landmarks, metrics = self._box, self._landmarks, self._metrics
            self._annotate(frame, box, landmarks, metrics)
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n")
            if interval:
                time.sleep(interval)