"""Always-on webcam capture + pose landmarks (OpenCV + MediaPipe).

A background thread keeps the camera open for as long as the server runs and
grabs frames continuously. A second thread runs MediaPipe pose estimation a few
times a second and exposes shoulder / back (hip) landmarks for a side-on view,
which the video stream draws on the frame.
"""
import os
import threading
import time
import urllib.request
from pathlib import Path

import cv2

CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", 0))
FRAME_W, FRAME_H = 640, 480
TARGET_FPS = int(os.environ.get("TARGET_FPS", 30))   # FPS requested from the device
STREAM_FPS = int(os.environ.get("STREAM_FPS", 30))   # max FPS pushed to the browser
DETECT_EVERY = 0.25         # seconds between detection runs (keeps the Pi cool)
PRESENCE_TIMEOUT = 3.0      # person counts as "present" for this long after last detection
DETECT_WIDTH = 320          # frames are downscaled to this width before detecting
MAX_CAMERAS = int(os.environ.get("MAX_CAMERAS", 5))  # indices 0..MAX_CAMERAS-1 are probed

# MediaPipe pose landmarker (Tasks API). The model is downloaded on first run.
POSE_MODEL_PATH = os.environ.get(
    "POSE_MODEL_PATH", str(Path(__file__).parent / "models" / "pose_landmarker_lite.task")
)
POSE_MODEL_URL = os.environ.get(
    "POSE_MODEL_URL",
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
    "pose_landmarker_lite/float16/1/pose_landmarker_lite.task",
)
VIS_THRESHOLD = 0.5         # ignore landmarks the model isn't confident about

# BlazePose indices for the joints we care about in a side view.
LANDMARK_NAMES = {
    0: "nose",
    7: "left_ear", 8: "right_ear",
    11: "left_shoulder", 12: "right_shoulder",
    23: "left_hip", 24: "right_hip",
}
SIDES = ("left", "right")


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
    with HOG full-body as a fallback. Placeholder until we move to pose
    landmarks (e.g. MediaPipe) for the actual posture angles."""

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
    """MediaPipe pose landmarks (Tasks API). Returns named joints in pixel coords.

    For a side-on view we keep the ear, shoulder and hip of each side; drawing
    picks whichever side faces the camera.
    """

    def __init__(self):
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision

        ensure_pose_model()
        base = mp_python.BaseOptions(model_asset_path=POSE_MODEL_PATH)
        options = vision.PoseLandmarkerOptions(
            base_options=base,
            running_mode=vision.RunningMode.VIDEO,
            num_poses=1,
            min_pose_detection_confidence=0.5,
            min_pose_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self._landmarker = vision.PoseLandmarker.create_from_options(options)
        self._ts = 0

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
            landmarks[name] = (int(p.x * w), int(p.y * h), float(getattr(p, "visibility", 1.0)))

        visible = [(x, y) for x, y, v in landmarks.values() if v >= VIS_THRESHOLD]
        if not visible:
            return None, None
        xs = [x for x, _ in visible]
        ys = [y for _, y in visible]
        box = (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
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

        self._frame = None
        self._box = None
        self._landmarks = None
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
            self._last_seen = 0.0
        self._switch.set()

    def available_cameras(self):
        """Indices the UI can choose from (current one always included)."""
        with self._lock:
            current = self.index
        return {"current": current, "available": list_cameras(skip=current)}

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
        """Detection thread: periodically run pose estimation on the latest frame."""
        detector = Detector()
        while not self._stop.is_set():
            with self._lock:
                frame = None if self._frame is None else self._frame.copy()
            if frame is None:
                time.sleep(0.05)
                continue

            t0 = time.time()
            box, landmarks = detector.detect(frame)
            now = time.time()
            with self._lock:
                self._box = box
                self._landmarks = landmarks
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

    def status(self):
        with self._lock:
            present = (time.time() - self._last_seen) < PRESENCE_TIMEOUT
            return {
                "camera_open": self._opened,
                "camera_index": self.index,
                "person_detected": present,
                "box": self._box if present else None,
                "fps": round(self._fps, 1),
                "error": self._error,
            }

    # ----- drawing -----
    def _annotate(self, frame, box, landmarks):
        """Draw pose landmarks if we have them, else the detector box."""
        if landmarks:
            self._draw_pose(frame, landmarks)
        elif box is not None:
            x, y, w, h = box
            cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)
        return frame

    @staticmethod
    def _draw_pose(frame, landmarks):
        """Highlight ear -> shoulder -> hip with the back/neck lines labelled."""
        def point(name):
            pt = landmarks.get(name)
            if pt is None or pt[2] < VIS_THRESHOLD:
                return None
            return (int(pt[0]), int(pt[1]))

        shoulder_color, hip_color, ear_color = (0, 255, 0), (0, 165, 255), (255, 255, 0)
        back_color, neck_color = (0, 220, 255), (255, 0, 255)

        # side facing the camera = the one whose joints are most visible
        def side_score(side):
            return sum(landmarks[k][2] for k in
                       (f"{side}_ear", f"{side}_shoulder", f"{side}_hip") if k in landmarks)
        side = max(SIDES, key=side_score)

        ear = point(f"{side}_ear")
        shoulder = point(f"{side}_shoulder")
        hip = point(f"{side}_hip")

        # back (shoulder -> hip) and neck (ear -> shoulder) lines
        if shoulder and hip:
            cv2.line(frame, shoulder, hip, back_color, 3, cv2.LINE_AA)
        if ear and shoulder:
            cv2.line(frame, ear, shoulder, neck_color, 3, cv2.LINE_AA)

        # faint markers for both sides so the whole pose reads on screen
        for name, color in (("left_shoulder", shoulder_color), ("right_shoulder", shoulder_color),
                            ("left_hip", hip_color), ("right_hip", hip_color)):
            pt = point(name)
            if pt:
                cv2.circle(frame, pt, 4, color, -1, cv2.LINE_AA)

        # labelled joints of the visible side
        for pt, color, label in ((shoulder, shoulder_color, "Shoulder"),
                                 (hip, hip_color, "Back / hip"),
                                 (ear, ear_color, "Ear")):
            if not pt:
                continue
            cv2.circle(frame, pt, 7, color, -1, cv2.LINE_AA)
            cv2.circle(frame, pt, 7, (255, 255, 255), 1, cv2.LINE_AA)
            org = (pt[0] + 10, pt[1] - 10)
            cv2.putText(frame, label, org, cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(frame, label, org, cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
        return frame

    def get_frame(self, annotate=True):
        """Latest frame (BGR) with landmarks/box drawn on it, or None."""
        with self._lock:
            if self._frame is None:
                return None
            frame = self._frame.copy()
            box = self._box
            landmarks = self._landmarks
        if annotate:
            self._annotate(frame, box, landmarks)
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
                box = self._box
                landmarks = self._landmarks
            self._annotate(frame, box, landmarks)
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n")
            if interval:
                time.sleep(interval)