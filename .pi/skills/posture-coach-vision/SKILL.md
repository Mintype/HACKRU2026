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

1. **sustained slouch** — score below the lower threshold for `roast_seconds` (~10 s),
   repeating every `cooldown_seconds` (~10 s) for as long as the slouch continues
2. **bad posture** — score below `bad_score_threshold` (~0.45) held for
   `bad_score_seconds` (~3 s), firing without waiting out the timer

Both stay armed for the whole episode: staying in the amber (`slouching`) or red
(`roasted`) states keeps earning roasts, spaced by the cooldown. A red state is
still slouching — it must not go quiet, and `time_to_roast()` reports the cooldown
as the countdown rather than "not slouching".

`away` (no person, or not seated) is **never** roasted. There is no warning
stage — slouching turns the website amber, and the roast lands at
`roast_seconds`. See the timeline for the numbers rather than guessing, and use
`--timers` to watch a running server count down:

```bash
python .pi/skills/posture-coach-vision/scripts/roast_timeline.py
python .pi/skills/posture-coach-vision/scripts/roast_timeline.py --profile moderate --seconds 60
python .pi/skills/posture-coach-vision/scripts/roast_timeline.py --profile bad --cooldown 20
python run.py --timers              # live: state, score, slouch time, countdown
```

`--timers` reads `next_roast` from `/api/stats`, computed by
`PostureStateMachine.time_to_roast()`: seconds until the soonest trigger, which
trigger that is, and whether the cooldown is holding it back. The same value
drives the website's countdown and rides along on every SSE reading — so a
countdown that never advances means the machine is not seeing a slouch at all,
and a countdown stuck behind `held by cooldown` means the roast is gated, not
broken.

`--profile moderate` matters: a deep slouch trips the score trigger after ~3 s, so
only a *moderate* slouch (score between `lower_threshold` and
`bad_score_threshold`) exercises the sustained timer. Set the threshold to 0 to
disable the score trigger and use only the timer.

## Phone use

A third fault, and deliberately **not** part of the posture score: someone can sit
up perfectly straight and still stare into their lap.

It is a heuristic, not object detection — `angles.head_pitch()` measures the tilt of
the ear→eye line against horizontal, and a sustained tilt past
`phone_pitch_threshold` (~40°) for `phone_delay` (~1.5 s) sets `phone` on the
reading. That is what a phone, a lap and a keyboard have in common ("looking down
for a long time"), which is why it is tunable rather than clever: **watch
`head_down_deg` on the Debug panel while someone looks at a phone and set the
threshold from what you actually see.**

The state machine runs it outside the slouch machine (`_phone`): on `phone` it
enters the `phone` state (red), and after `phone_threshold_seconds` (~10 s) fires
`roast_due` with `reason="phone"`, repeating every `cooldown_seconds` like the other
triggers. A phone roast does **not** transition to `roasted` — the pill stays on
`phone` until the head comes up — and recovery to `good` is suppressed while the
phone is active, so a good posture score cannot mask the fault.

The insults come from `voice/roasts.py:PHONE`, selected by `fault="phone"` in
`fallback_batch`, and the prompt context gains a line saying this is not a posture
problem. Test it without a camera:

```bash
python .pi/skills/posture-coach-vision/scripts/roast_timeline.py --profile phone --seconds 40
python run.py --simulate                 # the scripted feed has a phone phase
```

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
