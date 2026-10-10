---
name: posture-coach-vision
description: Work on the side-view posture measurement in Posture Coach — MediaPipe landmarks, neck/torso/thigh angles, sit-down detection, calibration, score smoothing, hysteresis and roast trigger timing. Use when changing vision/, the state machine, camera selection, or when roasts fire at the wrong time or not at all.
compatibility: MediaPipe and OpenCV optional — the geometry and state machine are testable without a camera.
---

# Posture Coach — vision and roast timing

Read `posture-coach` first for layout and contracts.

## Geometry (the part that must stay right)

Angles are degrees from vertical, measured on **pixel** coordinates — convert
MediaPipe's normalised `0..1` values before computing, or a non-square frame distorts
them. Image `y` grows **downward**, so "up" offsets are `y_below - y_above`.

| Angle | Points | Meaning |
| ----- | ------ | ------- |
| neck | ear → shoulder | forward-head posture |
| torso | shoulder → hip | hunching / leaning |
| thigh | hip → knee, from **horizontal** | small ⇒ seated |

- Use `atan2`, never `atan(dx/dy)`, so a near-zero denominator cannot blow up
  (`vision/angles.py`).
- Pick the body side with the higher summed landmark visibility — the far side is
  partly a guess.
- `score = 1 - min(1, max(0, θ - θ₀) / Δθ)` against the person's **own** calibrated
  baseline; the worse of neck/torso wins; smooth with an EMA (`α = 0.2`).
- Hysteresis: enter slouching below `lower_threshold` (0.7), leave above
  `upper_threshold` (0.8), and require `good_debounce_seconds` (~2 s) of good posture
  to reset — otherwise a brief sit-up dodges the roast.

## When a roast fires

Two triggers, either can fire, both gated by the same `cooldown_seconds`:

1. **sustained slouch** — score below the lower threshold for `roast_seconds` (~30 s)
2. **bad posture** — score below `bad_score_threshold` (~0.45) held for
   `bad_score_seconds` (~3 s), firing without waiting out the timer

`away` (no person, or not seated) is **never** roasted. `warn` fires at
`warn_seconds` (~10 s). See the timeline for the numbers rather than guessing:

```bash
python .pi/skills/posture-coach-vision/scripts/roast_timeline.py
python .pi/skills/posture-coach-vision/scripts/roast_timeline.py --profile bad --cooldown 20
python .pi/skills/posture-coach-vision/scripts/roast_timeline.py --warn 5 --roast 10 --cooldown 15
```

One consequence worth knowing: with the default `bad_score_threshold` (0.45) a
steep slouch can fire `roast_due` **before** `warn` (the timeline shows roast at
~21 s against warn at ~26 s). That is the score trigger being deliberately faster
than the 10 s warn, not a bug — raise `bad_score_seconds` or lower
`bad_score_threshold` if you want the warning first, or set the threshold to 0 to
use only the sustained-slouch timer.

## Sit-down detection

Thigh angle under `seated_threshold` (~30°) for `seated_delay` (~2 s). When a desk
hides the knee the angle is unavailable, so the detector falls back to "person present
+ hip parked in one spot + torso upright enough". If the state sticks at
`person · not seated`, that is the code path to look at — and the website's System
panel prints exactly that state so you can see it.

## Testing vision without a camera

- **Stub the landmarker.** `tests/test_detector.py` has a `StubLandmarker` that
  returns fixed pixel points, so angles, seated detection, smoothing and the overlay
  are all testable offline. This is the fastest loop for geometry changes.
- **Scripted readings.** `fakes/fake_readings.py` drives `--simulate`, the smoke test
  and the timeline script. It is the "fake readings" fake; keep `slouch_part` and the
  drift fields in it so simulate exercises the same context the camera does.
- **A recorded clip.** `python run.py --video clip.mp4` runs the real detector against
  a file instead of the webcam.
- **Cameras.** `python run.py --list-cameras` probes indices; on Windows the default
  backend is DirectShow because Media Foundation can take ~20 s to open and copes
  poorly with several cameras. `--camera N` is remembered in `data/settings.json`.

## Tuning checklist

- Change a threshold in `server/settings.py` defaults *and* in the Controls form, or
  the UI will not offer it.
- Anything the state machine reads at runtime must be copied across in
  `Settings.apply_to()`; a setting that is only in `DEFAULTS` silently does nothing.
- After touching the state machine, re-run `tests/test_state_machine.py` — it covers
  debounce, cooldown, one-roast-per-episode, the score trigger and `away`.
