"""Mobile posture tests: any-angle measurement, multi-person tracking.

Everything here is offline: the poses are plain dicts in the shape
``vision.landmarks.PoseLandmarker.detect_multi`` returns, so the whole mobile
pipeline is exercised without a camera, MediaPipe or a network.
"""

import unittest

from server.state_machine import StateMachineConfig, PostureStateMachine
from vision.multipose import MultiTracker, PassiveMachine, measure_pose, score_metrics

WIDTH, HEIGHT = 640.0, 480.0


def facing_pose(centre=250.0, spread=110.0, ear_y=90.0, tilt=0.0, seen=True):
    """A person square-on to the camera: shoulders ~0.8x the torso length."""
    half = spread / 2.0

    def side(offset):
        return {
            "ear": (centre + offset, ear_y, 1.0),
            "eye": (centre + offset + 2, ear_y - 6, 1.0),
            "shoulder": (centre + offset * (half / 35.0), 170.0 + (tilt if offset < 0 else 0.0), 1.0),
            "hip": (centre + offset * (half / 35.0), 310.0, 1.0),
            "knee": (centre + offset * (half / 35.0), 430.0, 1.0),
        }

    return {
        "side": "right",
        "ear": side(-20.0)["ear"],
        "eye": side(-18.0)["eye"],
        "shoulder": side(half)["shoulder"],
        "hip": side(half)["hip"],
        "knee": side(half)["knee"],
        "nose": (centre, ear_y + 12.0, 1.0 if seen else 0.05),
        "left": side(-35.0),
        "right": side(35.0),
        "size": (WIDTH, HEIGHT),
    }


def side_pose(ear_x=270.0, ear_y=110.0, centre=250.0):
    """A person side-on: the two shoulders almost overlap in x."""
    return {
        "side": "right",
        "ear": (ear_x, ear_y, 1.0),
        "eye": (ear_x + 40.0, ear_y + 8.0, 1.0),
        "shoulder": (centre, 170.0, 1.0),
        "hip": (centre, 310.0, 1.0),
        "knee": (centre + 60.0, 420.0, 1.0),
        "nose": (ear_x + 50.0, ear_y + 14.0, 1.0),
        "left": {"ear": (ear_x - 6.0, ear_y, 1.0), "eye": (ear_x + 34.0, ear_y + 8.0, 1.0),
                 "shoulder": (centre - 6.0, 170.0, 1.0), "hip": (centre - 6.0, 310.0, 1.0),
                 "knee": (centre + 54.0, 420.0, 1.0)},
        "right": {"ear": (ear_x + 6.0, ear_y, 1.0), "eye": (ear_x + 46.0, ear_y + 8.0, 1.0),
                  "shoulder": (centre + 6.0, 170.0, 1.0), "hip": (centre + 6.0, 310.0, 1.0),
                  "knee": (centre + 66.0, 420.0, 1.0)},
        "size": (WIDTH, HEIGHT),
    }


def machine(**overrides):
    config = StateMachineConfig(calibration_seconds=0.3, sleep_threshold_seconds=0.0)
    for key, value in overrides.items():
        setattr(config, key, value)
    return PostureStateMachine(config)


class TestViewAngles(unittest.TestCase):
    def test_square_on_is_a_front_view(self):
        metrics = measure_pose(facing_pose())
        self.assertEqual(metrics.view, "front")

    def test_shoulders_overlapping_is_a_side_view(self):
        metrics = measure_pose(side_pose())
        self.assertEqual(metrics.view, "side")

    def test_no_face_visible_is_a_back_view(self):
        landmarks = facing_pose(seen=False)
        landmarks["left"]["eye"] = (0.0, 0.0, 0.01)
        landmarks["right"]["eye"] = (0.0, 0.0, 0.01)
        self.assertEqual(measure_pose(landmarks).view, "back")

    def test_front_view_measures_head_sink_not_a_neck_angle(self):
        metrics = measure_pose(facing_pose())
        self.assertIsNone(metrics.neck_angle)
        self.assertIsNotNone(metrics.head_sink)

    def test_side_view_measures_a_neck_angle(self):
        metrics = measure_pose(side_pose())
        self.assertIsNotNone(metrics.neck_angle)
        self.assertGreater(metrics.neck_angle, 15.0)
        self.assertIsNone(metrics.head_sink)

    def test_the_head_sinking_lowers_the_score_from_the_front(self):
        upright = measure_pose(facing_pose(ear_y=90.0))
        slumped = measure_pose(facing_pose(ear_y=150.0))
        self.assertGreater(upright.head_sink, slumped.head_sink)
        baseline = {"sink": upright.head_sink, "tilt": 0.0}
        good, _, _ = score_metrics(upright, baseline)
        bad, part, _ = score_metrics(slumped, baseline)
        self.assertGreater(good, 0.9)
        self.assertLess(bad, 0.2)
        self.assertEqual(part, "neck")

    def test_uneven_shoulders_count_as_the_torso_from_the_front(self):
        metrics = measure_pose(facing_pose(tilt=30.0))
        score, part, _ = score_metrics(metrics, {"sink": metrics.head_sink, "tilt": 0.0})
        self.assertLess(score, 0.9)
        self.assertEqual(part, "torso")

    def test_side_view_drift_still_uses_the_side_metrics(self):
        upright = measure_pose(side_pose(ear_x=250.0, ear_y=110.0))
        slumped = measure_pose(side_pose(ear_x=320.0, ear_y=110.0))
        baseline = {"neck": upright.neck_angle, "torso": upright.torso_angle}
        good, _, _ = score_metrics(upright, baseline)
        bad, part, _ = score_metrics(slumped, baseline)
        self.assertGreater(good, 0.9)
        self.assertLess(bad, 0.5)
        self.assertEqual(part, "neck")

    def test_no_state_machine_still_roster_scores(self):
        metrics = measure_pose(facing_pose())
        score, part, _ = score_metrics(metrics, {"sink": metrics.head_sink, "tilt": 0.0})
        self.assertGreater(score, 0.9)
        self.assertIn(part, ("neck", "torso", "even"))
        self.assertEqual(PassiveMachine().update({"score": 0.2}), [])


class TestTracking(unittest.TestCase):
    def make(self, **kwargs):
        options = {"calibration_seconds": 0.3, "fps": 10, "max_people": 3, "ttl": 3.0}
        options.update(kwargs)
        return MultiTracker(machine, **options)

    def test_two_people_get_two_tracks_with_distinct_labels(self):
        tracker = self.make()
        out = tracker.update([facing_pose(centre=150.0), facing_pose(centre=430.0)], 0.0)
        self.assertEqual(out["seen"], 2)
        labels = sorted(row["label"] for row in out["roster"])
        self.assertEqual(labels, ["Person 1", "Person 2"])

    def test_people_are_numbered_left_to_right(self):
        tracker = self.make()
        out = tracker.update([facing_pose(centre=430.0), facing_pose(centre=150.0)], 0.0)
        by_label = {row["label"]: row for row in out["roster"]}
        self.assertLess(by_label["Person 1"]["anchor"][0], by_label["Person 2"]["anchor"][0])

    def test_a_person_keeps_their_id_while_they_move(self):
        tracker = self.make()
        tracker.update([facing_pose(centre=300.0)], 0.0)
        for step in range(1, 8):
            out = tracker.update([facing_pose(centre=300.0 + step * 6.0)], step * 0.1)
        self.assertEqual(len(out["roster"]), 1)
        self.assertEqual(out["roster"][0]["id"], 1)
        self.assertEqual(out["roster"][0]["label"], "Person 1")

    def test_walking_away_leaves_the_roster(self):
        tracker = self.make()
        tracker.update([facing_pose(centre=150.0), facing_pose(centre=430.0)], 0.0)
        out = tracker.update([facing_pose(centre=430.0)], 1.0)
        self.assertEqual(out["seen"], 1)
        # ...and after the TTL they are gone, not just unseen.
        out = tracker.update([facing_pose(centre=430.0)], 4.5)
        self.assertEqual(len(out["roster"]), 1)

    def test_a_pair_of_poses_cannot_share_one_track(self):
        tracker = self.make()
        # Identical anchors: without the claim set this would be one person twice.
        out = tracker.update([facing_pose(centre=250.0), facing_pose(centre=250.0)], 0.0)
        self.assertEqual(len(out["roster"]), 2)
        self.assertNotEqual(out["roster"][0]["id"], out["roster"][1]["id"])

    def test_ids_are_reused_so_the_roster_does_not_climb_forever(self):
        tracker = self.make()
        tracker.update([facing_pose(centre=150.0)], 0.0)
        tracker.update([], 9.0)  # everybody leaves
        out = tracker.update([facing_pose(centre=150.0)], 10.0)
        self.assertEqual(out["roster"][0]["label"], "Person 1")

    def test_the_roster_is_sorted_worst_first(self):
        tracker = self.make()
        ts = 0.0
        for _ in range(20):
            ts += 0.1
            out = tracker.update(
                [facing_pose(centre=150.0), facing_pose(centre=430.0, ear_y=150.0)], ts
            )
        scores = [row["score"] for row in out["roster"] if row["score"] is not None]
        self.assertEqual(scores, sorted(scores))

    def test_a_slumper_is_measured_against_their_own_upright(self):
        tracker = self.make()
        ts = 0.0
        # Both people have a *hunched* reference; the one who does not change is
        # still "good for them", which is the whole point of per-person baselines.
        for _ in range(10):
            ts += 0.1
            tracker.update([facing_pose(centre=150.0, ear_y=135.0)], ts)
        out = tracker.update([facing_pose(centre=150.0, ear_y=135.0)], ts + 0.1)
        row = out["roster"][0]
        self.assertFalse(row["calibrating"])
        self.assertGreater(row["score"], 0.9)

    def test_the_roast_names_the_person(self):
        tracker = self.make()
        style = machine(roast_seconds=1.0, bad_score_seconds=0.4, cooldown_seconds=1.0)
        tracker.machine_factory = lambda: style
        ts = 0.0
        roasts = []
        # Measure them upright first, so the slump is a change and not the
        # baseline they are judged against.
        for _ in range(5):
            ts += 0.1
            tracker.update([facing_pose(centre=150.0, ear_y=90.0)], ts)
        for _ in range(40):
            ts += 0.1
            out = tracker.update([facing_pose(centre=150.0, ear_y=150.0)], ts)
            roasts += out["roasts"]
        self.assertTrue(roasts)
        first = roasts[0]
        self.assertEqual(first["person"], "Person 1")
        self.assertEqual(first["context"]["person"], "Person 1")
        self.assertIn(first["context"]["view"], ("front", "back", "side"))
        self.assertIn("head sink", first["context"]["metric"])

    def test_nobody_in_frame_is_an_empty_but_valid_answer(self):
        tracker = self.make()
        out = tracker.update([], 0.0)
        self.assertEqual(out["seen"], 0)
        self.assertEqual(out["roster"], [])
        self.assertEqual(out["roasts"], [])
        self.assertIsNone(out["primary"])

    def test_primary_is_the_person_who_needs_help(self):
        tracker = self.make()
        ts = 0.0
        # Both stand up straight first (their own baselines), then one slumps.
        for _ in range(5):
            ts += 0.1
            tracker.update(
                [facing_pose(centre=150.0), facing_pose(centre=430.0)], ts
            )
        for _ in range(20):
            ts += 0.1
            out = tracker.update(
                [facing_pose(centre=150.0), facing_pose(centre=430.0, ear_y=155.0)], ts
            )
        self.assertIsNotNone(out["primary"])
        self.assertLess(out["primary"].score, 0.5)
        self.assertIn(out["primary"].label, ("Person 1", "Person 2"))

    def test_points_come_back_normalized_for_the_phone_to_draw(self):
        tracker = self.make()
        out = tracker.update([facing_pose(centre=320.0)], 0.0)
        points = out["roster"][0]["points"]
        self.assertIn("shoulder_left", points)
        self.assertIn("shoulder_right", points)
        for name, (x, y) in points.items():
            self.assertGreaterEqual(x, 0.0, name)
            self.assertLessEqual(x, 1.0, name)
            self.assertGreaterEqual(y, 0.0, name)
            self.assertLessEqual(y, 1.0, name)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
