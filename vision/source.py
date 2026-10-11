"""Frame sources: the live webcam, a recorded video file, or nothing.

The recorded-video source is the "fake camera" from the build guide: the vision
module can be developed without the Pi by pointing it at a clip.

This is a **side-view** system: the camera is mounted at shoulder height to the
person's side (see :mod:`vision.angles`).  ``list_cameras`` and the
``--camera``/``--camera-backend`` options are here so the right physical camera
can be selected on a machine with several (built-in, USB, virtual).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

#: Human-friendly backend names -> OpenCV capture-API constants.
_BACKEND_NAMES = {
    "dshow": "CAP_DSHOW",
    "msmf": "CAP_MSMF",
    "v4l2": "CAP_V4L2",
    "avfoundation": "CAP_AVFOUNDATION",
    "gstreamer": "CAP_GSTREAMER",
    "ffmpeg": "CAP_FFMPEG",
}


def _cv2():
    import cv2  # lazy, so importing this module is cheap

    return cv2


def resolve_backend(name: Optional[str]) -> Optional[int]:
    """Map ``auto``/``dshow``/``msmf``/... to an OpenCV backend constant.

    Returns ``None`` for "let OpenCV decide".
    """
    if not name:
        return None
    key = str(name).strip().lower()
    if key in {"auto", "any", "default", "none"}:
        return None
    cv2 = _cv2()
    attr = _BACKEND_NAMES.get(key)
    if attr is None:
        attr = key.upper()
        if not attr.startswith("CAP_"):
            attr = "CAP_" + attr
    return getattr(cv2, attr, None)


def default_backend() -> Optional[int]:
    """Best default backend for this OS.

    On Windows DirectShow (``CAP_DSHOW``) opens far faster than the Media
    Foundation default and copes much better with several cameras attached.
    Override with ``POSTURE_CAMERA_BACKEND=dshow|msmf|auto``.
    """
    env = os.environ.get("POSTURE_CAMERA_BACKEND")
    if env:
        return resolve_backend(env)
    if os.name == "nt":
        return resolve_backend("dshow")
    return None


def backend_name(backend: Optional[int]) -> str:
    if backend is None:
        return "auto"
    cv2 = _cv2()
    for name, attr in _BACKEND_NAMES.items():
        if getattr(cv2, attr, None) == backend:
            return name
    return str(backend)


class FrameSource:
    """Minimal interface: ``read() -> frame | None``."""

    def read(self):
        raise NotImplementedError

    def release(self) -> None:
        pass

    def describe(self) -> str:
        return self.__class__.__name__


class CameraSource(FrameSource):
    """A USB / CSI webcam via OpenCV."""

    def __init__(
        self,
        index: int = 0,
        width: int = 640,
        height: int = 480,
        fps: int = 30,
        backend: Optional[int] = None,
        warmup_reads: int = 3,
    ):
        cv2 = _cv2()
        self.index = int(index)
        self.backend = default_backend() if backend is None else backend
        self._cap = self._open(cv2)
        if width:
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
        if height:
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
        if fps:
            self._cap.set(cv2.CAP_PROP_FPS, int(fps))
        # Discard a few frames: some devices hand back black/garbage first.
        for _ in range(max(0, int(warmup_reads))):
            self._cap.read()

    def _open(self, cv2):
        cap = (
            cv2.VideoCapture(self.index, self.backend)
            if self.backend is not None
            else cv2.VideoCapture(self.index)
        )
        if not cap.isOpened() and self.backend is not None:
            cap.release()
            log.warning(
                "backend %s could not open camera %s; retrying with OpenCV default",
                backend_name(self.backend),
                self.index,
            )
            self.backend = None
            cap = cv2.VideoCapture(self.index)
        if not cap.isOpened():
            raise RuntimeError(
                f"could not open camera {self.index}. "
                "Is another process using it? Try `python run.py --list-cameras`."
            )
        return cap

    def read(self):
        ok, frame = self._cap.read()
        return frame if ok else None

    def release(self) -> None:
        try:
            self._cap.release()
        except Exception:
            pass

    def describe(self) -> str:
        return f"camera:{self.index} ({backend_name(self.backend)})"


class VideoFileSource(FrameSource):
    """A recorded clip, looped forever and paced at its native frame rate."""

    def __init__(self, path: str | Path, width: int = 640, height: int = 480):
        cv2 = _cv2()

        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        self._cv2 = cv2
        self._width = width
        self._height = height
        self._cap = None
        self._fps = 30.0
        self._next_frame_at = 0.0
        self._open()

    def _open(self) -> None:
        cv2 = self._cv2
        self._cap = cv2.VideoCapture(str(self.path))
        fps = self._cap.get(cv2.CAP_PROP_FPS)
        self._fps = fps if fps and fps > 1 else 30.0
        self._next_frame_at = time.time()

    def read(self):
        ok, frame = self._cap.read()
        if not ok or frame is None:  # end of clip -> loop
            self._open()
            ok, frame = self._cap.read()
            if not ok or frame is None:
                return None
        if self._width and self._height:
            frame = self._cv2.resize(frame, (self._width, self._height))
        # Pace to the clip's frame rate so the state machine sees real time.
        now = time.time()
        if self._next_frame_at > now:
            time.sleep(self._next_frame_at - now)
        self._next_frame_at = max(now, self._next_frame_at) + (1.0 / self._fps)
        return frame

    def release(self) -> None:
        try:
            if self._cap is not None:
                self._cap.release()
        except Exception:
            pass

    def describe(self) -> str:
        return f"video:{self.path.name}"


def open_source(
    spec: Optional[str | int],
    width: int = 640,
    height: int = 480,
    fps: int = 30,
    backend: Optional[int] = None,
) -> Optional[FrameSource]:
    """Build a source from a CLI-ish spec.

    ``spec`` may be ``None``/``"fake"`` (no source, simulation), a camera index
    (``0``, ``"0"``) or a path to a video file.  ``backend`` only applies to
    cameras; ``None`` means "use :func:`default_backend`".
    """
    if spec is None:
        return None
    text = str(spec)
    if text.lower() in {"fake", "sim", "simulate", "none", ""}:
        return None
    if text.isdigit():
        return CameraSource(int(text), width=width, height=height, fps=fps, backend=backend)
    return VideoFileSource(text, width=width, height=height)


def list_cameras(
    max_index: int = 6,
    backend: Optional[int] = None,
    snapshot_dir: Optional[str | Path] = None,
    probe_max_resolution: bool = True,
) -> List[Dict]:
    """Probe camera indices and report which ones actually deliver frames.

    With ``snapshot_dir`` set, a frame from each working camera is written to
    ``camera_<index>.png`` so you can tell the physical cameras apart.
    """
    cv2 = _cv2()
    backend = default_backend() if backend is None else backend
    results: List[Dict] = []

    for index in range(int(max_index)):
        started = time.time()
        info: Dict = {
            "index": index,
            "opened": False,
            "width": None,
            "height": None,
            "max_width": None,
            "max_height": None,
            "snapshot": None,
            "ms": None,
            "backend": backend_name(backend),
        }
        cap = None
        try:
            cap = cv2.VideoCapture(index, backend) if backend is not None else cv2.VideoCapture(index)
            if cap is not None and cap.isOpened():
                info["opened"] = True
                ok, frame = cap.read()
                if ok and frame is not None:
                    info["height"], info["width"] = int(frame.shape[0]), int(frame.shape[1])
                    if snapshot_dir is not None and info["height"] and info["width"]:
                        directory = Path(snapshot_dir)
                        directory.mkdir(parents=True, exist_ok=True)
                        path = directory / f"camera_{index}.png"
                        try:
                            if cv2.imwrite(str(path), frame):
                                info["snapshot"] = str(path)
                        except Exception:
                            pass
                if probe_max_resolution:
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
                    info["max_width"] = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    info["max_height"] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        except Exception as exc:  # a broken driver must not stop the scan
            info["error"] = str(exc)
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
        info["ms"] = round((time.time() - started) * 1000)
        results.append(info)

    return results
