"""Multi-person, any-angle posture measurement for the phone camera.

The Pi app measures **one seated person from the side**. The mobile app points a
phone at a room: several people, any viewing angle, everyone moving and half of
them walking away. So this module does three things the side-view detector does
not:

1. **Classify the viewing angle** from landmark geometry — ``side``, ``front`` or
   ``back`` — because a neck angle is meaningless when someone is facing you.
2. **Measure whichever metric that angle can actually support.** From the side,
   the forward-head and torso angles (the same ones the Pi app uses). From the
   front, *head sink* (how far the head has dropped between the shoulders,
   measured in shoulder widths) and *shoulder tilt*.
3. **Keep a stable identity per person** so a roster can say who is good and who
   is not, frame after frame.

It is deliberately free of cv2 / MediaPipe / Flask imports: it consumes the plain
dicts :func:`vision.landmarks.PoseLandmarker.detect_multi` returns, which keeps
the whole thing testable without a camera.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import angles

#: Shoulder spread ÷ torso length below which the camera is seeing them from the
#: side. Side on, the two shoulders sit almost on top of each other in x.
SIDE_VIEW_RATIO = 0.45

#: How far a person's shoulder midpoint may move between frames (normalized
#: image units) and still count as the same person.
MATCH_DISTANCE = 0.22

#: Seconds without being seen before a track is dropped from the roster.
TRACK_TTL = 3.0

#: Fallback upright references, used until that person has been watched for
#: ``calibration_seconds`` in that view.
DEFAULT_SIDE_BASELINE = {"neck": 12.0, "torso": 4.0}
DEFAULT_FACING_BASELINE = {"sink": 0.62, "tilt": 0.03}

#: How far past the person's own upright each metric may drift before the score
#: hits zero. Side tolerances are degrees, facing ones are ratios.
SIDE_TOLERANCE_DEG = 20.0
SINK_TOLERANCE = 0.20
TILT_TOLERANCE = 0.12


def _dist(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return math.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1]))


def _mid(a: Sequence[float], b: Sequence[float]) -> Tuple[float, float]:
    return ((float(a[0]) + float(b[0])) / 2.0, (float(a[1]) + float(b[1])) / 2.0)


def _visible(point: Optional[Sequence[float]], minimum: float = 0.3) -> bool:
    return bool(point) and len(point) > 2 and float(point[2]) >= minimum


@dataclass
class Metrics:
    """Everything measurable about one person in one frame."""

    view: str  # side | front | back
    side: str  # which body side is toward the camera
    visible: bool = True
    neck_angle: Optional[float] = None
    torso_angle: Optional[float] = None
    head_sink: Optional[float] = None
    shoulder_tilt: Optional[float] = None
    #: Normalized shoulder midpoint: the identity anchor across frames.
    anchor: Tuple[float, float] = (0.5, 0.5)
    #: Normalized points for the phone to draw its own overlay.
    points: Dict[str, Tuple[float, float]] = field(default_factory=dict)

    @property
    def facing(self) -> bool:
        return self.view in ("front", "back")


def measure_pose(pose: Dict[str, object]) -> Metrics:
    """Turn one pose dict into :class:`Metrics`, choosing metrics by view angle."""
    size = pose.get("size") or (1.0, 1.0)
    width = float(size[0]) or 1.0
    height = float(size[1]) or 1.0

    def both(name: str) -> Dict[str, Sequence[float]]:
        out = {}
        for side in ("left", "right"):
            point = (pose.get(side) or {}).get(name)
            if point:
                out[side] = point
        return out

    shoulders = both("shoulder")
    hips = both("hip")
    ears = both("ear")
    knee = pose.get("knee")
    side = str(pose.get("side") or "right")

    metrics = Metrics(view="side", side=side)

    shoulder_mid = None
    if len(shoulders) == 2:
        left, right = shoulders["left"], shoulders["right"]
        spread = abs(float(left[0]) - float(right[0]))
        shoulder_mid = _mid(left, right)
        torso_len = 0.0
        if len(hips) == 2:
            torso_len = _dist(shoulder_mid, _mid(hips["left"], hips["right"]))
        ratio = spread / torso_len if torso_len > 1e-6 else 1.0
        if ratio < SIDE_VIEW_RATIO:
            metrics.view = "side"
        else:
            nose = pose.get("nose")
            eyes = both("eye")
            seeing_face = _visible(nose) or any(_visible(p) for p in eyes.values())
            metrics.view = "front" if seeing_face else "back"

    if shoulder_mid is None:
        # Only one shoulder visible: fall back to whatever the picked side gives.
        shoulder_mid = (
            (float(pose["shoulder"][0]), float(pose["shoulder"][1])) if pose.get("shoulder") else (0.0, 0.0)
        )
        spread = 0.0

    # -- metrics ----------------------------------------------------------
    ear = pose.get("ear")
    shoulder = pose.get("shoulder")
    hip = pose.get("hip")
    if metrics.view == "side":
        if _visible(ear) and _visible(shoulder):
            metrics.neck_angle = angles.neck_angle(ear, shoulder)
        if _visible(shoulder) and _visible(hip):
            metrics.torso_angle = angles.torso_angle(shoulder, hip)
    else:
        # Facing the camera: forward-head angles are meaningless, but how far the
        # head has sunk between the shoulders, and how uneven the shoulders are,
        # both survive the change of angle.
        scale = spread if spread > 1e-6 else max(float(size[0]) * 0.25, 1e-6)
        ear_ys = [float(p[1]) for p in ears.values() if _visible(p)]
        if ear_ys and shoulder_mid is not None:
            metrics.head_sink = (shoulder_mid[1] - (sum(ear_ys) / len(ear_ys))) / scale
        if len(shoulders) == 2:
            metrics.shoulder_tilt = (
                abs(float(shoulders["left"][1]) - float(shoulders["right"][1])) / scale
            )

    metrics.anchor = (shoulder_mid[0] / width, shoulder_mid[1] / height)
    points: Dict[str, Tuple[float, float]] = {}
    for name, point in (
        ("ear", ear),
        ("shoulder", shoulder),
        ("hip", hip),
        ("knee", knee),
    ):
        if point:
            points[name] = (float(point[0]) / width, float(point[1]) / height)
    for name, point in shoulders.items():
        points[f"shoulder_{name}"] = (float(point[0]) / width, float(point[1]) / height)
    for name, point in hips.items():
        points[f"hip_{name}"] = (float(point[0]) / width, float(point[1]) / height)
    metrics.points = points
    return metrics


def score_metrics(
    metrics: Metrics,
    baseline: Dict[str, float],
    *,
    side_tolerance: float = SIDE_TOLERANCE_DEG,
    sink_tolerance: float = SINK_TOLERANCE,
    tilt_tolerance: float = TILT_TOLERANCE,
) -> Tuple[float, str, Dict[str, float]]:
    """Score one person against their *own* upright reference for this view.

    Returns ``(score, part, drifts)`` where ``part`` names what is giving way in
    the vocabulary the roast prompts already understand: ``neck`` (head forward or
    sunk) or ``torso`` (upper back, or uneven shoulders).
    """
    if metrics.view == "side":
        neck_score = angles.posture_score(metrics.neck_angle or 0.0, baseline.get("neck", 12.0), side_tolerance)
        torso_score = angles.posture_score(
            metrics.torso_angle or 0.0, baseline.get("torso", 4.0), side_tolerance
        )
        neck_drift = max(0.0, (metrics.neck_angle or 0.0) - baseline.get("neck", 12.0))
        torso_drift = max(0.0, (metrics.torso_angle or 0.0) - baseline.get("torso", 4.0))
    else:
        # Sinking *reduces* the head height, so the drift runs the other way.
        sink = metrics.head_sink if metrics.head_sink is not None else baseline.get("sink", 0.62)
        tilt = metrics.shoulder_tilt if metrics.shoulder_tilt is not None else baseline.get("tilt", 0.03)
        neck_drift = max(0.0, baseline.get("sink", 0.62) - sink)
        torso_drift = max(0.0, tilt - baseline.get("tilt", 0.03))
        neck_score = 1.0 - min(1.0, neck_drift / max(sink_tolerance, 1e-6))
        torso_score = 1.0 - min(1.0, torso_drift / max(tilt_tolerance, 1e-6))

    score = angles.combine_scores(neck_score, torso_score)
    if neck_drift > torso_drift:
        part = "neck"
    elif torso_drift > neck_drift:
        part = "torso"
    else:
        part = "even"
    return score, part, {"neck": round(neck_drift, 3), "torso": round(torso_drift, 3)}


class PassiveMachine:
    """Stand-in for the state machine when none is injected.

    ``vision`` must not import ``server``, so the tracker takes its machines by
    injection. Without one you still get a roster and scores - just no roasts,
    which is the honest degradation for a pure-visualisation use.
    """

    class _Config:
        lower_threshold = 0.7
        upper_threshold = 0.8

    def __init__(self):
        self.config = PassiveMachine._Config()
        self.state = "good"

    def update(self, reading: Dict) -> List[Dict]:
        self.state = "good" if (reading.get("score") or 0) >= self.config.lower_threshold else "slouching"
        return []


class Track:
    """One person, as watched over time."""

    def __init__(self, track_id: int, machine, *, calibration_seconds: float = 2.0, fps: float = 8.0):
        self.id = int(track_id)
        self.label = f"Person {self.id}"
        self.machine = machine
        self.calibration_seconds = float(calibration_seconds)
        self._calib_frames = max(3, int(calibration_seconds * max(fps, 1.0)))
        self.first_seen: float = 0.0
        self.last_seen: float = 0.0
        self.samples = 0
        self.best = 1.0
        self.worst = 1.0
        #: view -> the person's upright reference in that view
        self.baselines: Dict[str, Dict[str, float]] = {}
        self._calib: Dict[str, List[Dict[str, float]]] = {}
        self.metrics: Optional[Metrics] = None
        self.score: Optional[float] = None
        self.part = "even"
        self.drifts: Dict[str, float] = {}
        #: The last reading handed to the state machine, kept so the desktop HUD
        #: can be shown the same numbers the phone sees.
        self.reading: Optional[Dict] = None

    # -- calibration ------------------------------------------------------
    def baseline_for(self, metrics: Metrics) -> Tuple[Dict[str, float], bool]:
        """This person's upright reference for this view, and whether it is final."""
        view = "side" if metrics.view == "side" else "facing"
        default = DEFAULT_SIDE_BASELINE if view == "side" else DEFAULT_FACING_BASELINE
        stored = self.baselines.get(view)
        if stored is not None:
            return stored, True
        samples = self._calib.setdefault(view, [])
        if self.metrics is not None and _distance(self.metrics.anchor, metrics.anchor) > 0.05:
            # They moved while we were learning: the samples are not one posture.
            samples.clear()
        sample: Dict[str, float] = {}
        if metrics.view == "side":
            if metrics.neck_angle is not None:
                sample["neck"] = metrics.neck_angle
            if metrics.torso_angle is not None:
                sample["torso"] = metrics.torso_angle
        else:
            if metrics.head_sink is not None:
                sample["sink"] = metrics.head_sink
            if metrics.shoulder_tilt is not None:
                sample["tilt"] = metrics.shoulder_tilt
        if sample:
            samples.append(sample)
        if len(samples) >= self._calib_frames:
            keys = set().union(*[set(s) for s in samples])
            baseline = {
                key: sum(s.get(key, default.get(key, 0.0)) for s in samples) / len(samples) for key in keys
            }
            for key, value in default.items():
                baseline.setdefault(key, value)
            self.baselines[view] = baseline
            return baseline, True
        return default, False

    # -- per frame --------------------------------------------------------
    def update(self, metrics: Metrics, ts: float) -> Dict:
        self.metrics = metrics
        self.samples += 1
        self.last_seen = ts
        if not self.first_seen:
            self.first_seen = ts
        baseline, ready = self.baseline_for(metrics)
        score, part, drifts = score_metrics(metrics, baseline)
        self.score = score
        self.part = part
        self.drifts = drifts
        self.best = max(self.best, score) if self.samples > 1 else score
        self.worst = min(self.worst, score) if self.samples > 1 else score
        reading = {
            "ts": ts,
            "person": True,
            "seated": True,  # the phone decides who is worth watching; posture is posture
            "score": score,
            "neck_angle": metrics.neck_angle if metrics.neck_angle is not None else 0.0,
            "torso_angle": metrics.torso_angle if metrics.torso_angle is not None else 0.0,
            "neck_drift": drifts["neck"],
            "torso_drift": drifts["torso"],
            "slouch_part": part,
            "calibrated": ready,
        }
        events = self.machine.update(reading)
        self.reading = reading
        return {
            "reading": reading,
            "events": events,
        }

    def describe(self, ts: float) -> Dict:
        """The roster row the phone renders."""
        metrics = self.metrics
        view = metrics.view if metrics else "?"
        label = view
        if metrics:
            if view == "side":
                if metrics.neck_angle is not None:
                    label = f"side · neck {metrics.neck_angle:.0f}°"
                else:
                    label = "side"
            else:
                if metrics.head_sink is not None:
                    label = f"{view} · head sink {metrics.head_sink * 100:.0f}%"
                else:
                    label = view
        state = getattr(self.machine, "state", "good")
        score = self.score
        good = bool(score is not None and score >= getattr(self.machine.config, "upper_threshold", 0.8))
        return {
            "id": self.id,
            "label": self.label,
            "view": view,
            "metric": label,
            "score": None if score is None else round(score, 3),
            "state": state,
            "part": self.part,
            "good": good,
            "bad": bool(score is not None and score < getattr(self.machine.config, "lower_threshold", 0.7)),
            "calibrating": not self.baselines,
            "seen_for": round(max(0.0, ts - self.first_seen), 1),
            "stale_for": round(max(0.0, ts - self.last_seen), 1),
            "samples": self.samples,
            "best": round(self.best, 3),
            "worst": round(self.worst, 3),
            "drifts": self.drifts,
            # Normalized joints, so the phone draws its own skeleton instead of
            # streaming annotated video back down the wire.
            "points": dict(self.metrics.points) if self.metrics else {},
            "anchor": [round(self.metrics.anchor[0] if self.metrics else 0.5, 3),
                       round(self.metrics.anchor[1] if self.metrics else 0.5, 3)],
        }


def _distance(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


class MultiTracker:
    """Matches the people in this frame to the people we already know about."""

    def __init__(
        self,
        machine_factory: Callable[[], object],
        *,
        match_distance: float = MATCH_DISTANCE,
        ttl: float = TRACK_TTL,
        calibration_seconds: float = 2.0,
        fps: float = 8.0,
        max_people: int = 3,
        sleep_disabled: bool = True,
    ):
        self.machine_factory = machine_factory or (lambda: None)
        self.match_distance = float(match_distance)
        self.ttl = float(ttl)
        self.calibration_seconds = float(calibration_seconds)
        self.fps = float(fps)
        self.max_people = max(1, int(max_people))
        self.tracks: Dict[int, Track] = {}
        self._next_id = 1
        #: Ids of people who walked away, handed out again so a long demo does not
        #: creep up to "Person 41" and so somebody coming back is Person 1 again.
        self._free_ids: List[int] = []
        self.frames = 0

    # -- matching ---------------------------------------------------------
    def _match(self, metrics: Metrics, claimed: Optional[set] = None) -> Optional[Track]:
        """The nearest track not already used *this frame*.

        One person cannot be two people: without the ``claimed`` set, two poses
        that land on the same anchor would both update the same track and the
        roster would show them twice with one id.
        """
        best: Optional[Track] = None
        best_distance = self.match_distance
        for track_id, track in self.tracks.items():
            if claimed and track_id in claimed:
                continue
            if track.metrics is None:
                continue
            distance = _distance(track.metrics.anchor, metrics.anchor)
            if distance < best_distance:
                best, best_distance = track, distance
        return best

    def _new_track(self, metrics: Metrics, ts: float) -> Track:
        if self._free_ids:
            self._free_ids.sort()
            track_id = self._free_ids.pop(0)
        else:
            track_id = self._next_id
            self._next_id += 1
        track = Track(
            track_id,
            self.machine_factory() or PassiveMachine(),
            calibration_seconds=self.calibration_seconds,
            fps=self.fps,
        )
        self.tracks[track.id] = track
        return track

    def update(self, poses: Sequence[Dict[str, object]], ts: float) -> Dict:
        """Score every visible person; return the roster and any roasts it earned."""
        self.frames += 1
        measured = [measure_pose(pose) for pose in poses or []]
        # Left-to-right, so a brand-new "Person 1" is the leftmost one and the
        # numbering is stable while people hold still.
        measured.sort(key=lambda m: m.anchor[0])

        seen: List[Track] = []
        earned: List[Tuple[Track, Dict]] = []
        claimed: set = set()
        for metrics in measured[: self.max_people]:
            track = self._match(metrics, claimed)
            if track is None:
                track = self._new_track(metrics, ts)
            claimed.add(track.id)
            outcome = track.update(metrics, ts)
            seen.append(track)
            for event in outcome["events"]:
                if event.get("event") == "roast_due":
                    earned.append((track, event))

        # Identity is per-frame, so a lost person has to leave the roster.
        for track in list(self.tracks.values()):
            if ts - track.last_seen > self.ttl:
                del self.tracks[track.id]
                self._free_ids.append(track.id)

        roasts: List[Dict] = []
        for track, event in earned:
            event = dict(event)
            # The roast should know who it is talking about: the phone speaks it
            # and the roster highlights them, so naming them is the whole point.
            event["context"] = {
                "person": track.label,
                "view": track.metrics.view if track.metrics else "?",
                "metric": track.describe(ts)["metric"],
                "mobile": True,
            }
            event["person"] = track.label
            roasts.append(event)

        roster = [track.describe(ts) for track in self.tracks.values()]
        roster.sort(key=lambda row: (row["score"] is None, row["score"] if row["score"] is not None else 1.0))
        # The worst person in the frame is what the desktop HUD is shown, so a
        # phone walking around a room drives the big screen too.
        primary = None
        for track in seen:
            if track.score is None:
                continue
            if primary is None or track.score < primary.score:
                primary = track
        return {
            "ts": ts,
            "people": [track.describe(ts) for track in seen],
            "roster": roster,
            "roasts": roasts,
            "seen": len(seen),
            "good": sum(1 for row in roster if row["good"]),
            "bad": sum(1 for row in roster if row["bad"]),
            "primary": primary,
        }
