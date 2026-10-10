# Posture Coach

A side-mounted webcam watches how you sit. A Raspberry Pi measures your neck and
torso angles, a debounced state machine decides when a slouch has gone on long
enough to deserve an insult, and a speaker in the room says it out loud. Flask
serves a fullscreen browser HUD so anyone can watch, tune it and press buttons.

Nothing runs in the browser: the Pi does the capture, the maths, the decision and
the audio. The website is only a window onto it.

```
side webcam ─▶ MediaPipe landmarks ─▶ angles ─▶ score ─▶ state machine
                                                             │
                                          roast_due ─────────┘
                                              │
                          ElevenLabs voice ───┴─── local voice (offline)
                                              │
                                    Nest over Bluetooth (or Wi-Fi cast)
```

---

## Contents

- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [When a roast fires](#when-a-roast-fires)
- [Reference: every number and option](#reference-every-number-and-option)
  - [Timing and threshold settings](#timing-and-threshold-settings)
  - [Vision parameters](#vision-parameters)
  - [Voice parameters](#voice-parameters)
  - [Playback parameters](#playback-parameters)
  - [Server and website parameters](#server-and-website-parameters)
  - [Environment variables](#environment-variables)
  - [Command line](#command-line)
  - [HTTP API](#http-api)
  - [Files and data](#files-and-data)
- [The website](#the-website)
- [Mobile mode — the phone as the camera](#mobile-mode--the-phone-as-the-camera)
- [Raspberry Pi setup](#raspberry-pi-setup)
- [Troubleshooting](#troubleshooting)
- [Testing checklist](#testing-checklist)
- [Team roles and the pitch](#team-roles-and-the-pitch)
- [Repository layout](#repository-layout)

---

## Quick start

```bash
./setup.sh                 # macOS / Linux / Pi   (Windows: setup.bat)
./run.sh --simulate --no-audio    # Windows: run.bat --simulate --no-audio
```

Then open <http://localhost:5000>. `--simulate` needs no camera: scripted readings
drive the state machine and a synthetic stick figure stands in for the video.

Or use the venv directly — `run.py` re-execs itself inside `.venv`, so a bare
`python run.py` works even when `python` points at another interpreter:

```bash
python run.py                  # real webcam
python run.py --demo           # short thresholds, whole cycle visible in ~30 s
python run.py --preview        # standalone OpenCV window, no browser
python run.py --help
```

Tests need no camera, no network and no keys:

```bash
python -m unittest discover -s tests -t .   # unit tests
python -m tests.smoke                        # boots the real Flask app, hits every route
```

---

## How it works

### 1. Vision — frames to readings

`vision/` opens the camera at 640×480 and runs each frame through MediaPipe's pose
landmarker (the **lite** model on the Pi). It picks whichever body side has the
higher summed landmark visibility, because the far side is partly a guess, and
converts MediaPipe's normalised `0..1` coordinates into **pixels before measuring
anything** — a non-square frame otherwise distorts the angles. Image `y` grows
downward, so "up" is `y_below - y_above`.

| Angle | Points | Meaning |
| ----- | ------ | ------- |
| **neck** | ear → shoulder | forward-head posture; grows as the head juts forward |
| **torso** | shoulder → hip | hunching or leaning |
| **thigh** | hip → knee, from **horizontal** | small ⇒ seated |

Each angle becomes a score against the person's *own* calibrated upright posture:

```
score  = 1 − min(1, max(0, θ − θ₀) / Δθ)      θ = angle, θ₀ = baseline, Δθ = tolerance
```

The **lower** of the neck and torso scores wins, so either kind of slouch counts.
The result is smoothed with an exponential moving average so one noisy frame
cannot trigger anything. Everything downstream sees only this reading — never a
frame:

```json
{
  "ts": 1760101234.5, "person": true, "seated": true,
  "neck_angle": 18.4, "torso_angle": 7.1, "score": 0.86,
  "neck_drift": 6.4, "torso_drift": 3.1, "slouch_part": "neck",
  "head_down_deg": 8.0, "head_ref_deg": 6.0, "head_drop_deg": 2.0,
  "motion": 0.021, "sleepy": false
}
```

`slouch_part` says which part is giving way (`neck`, `torso` or `even`), and the
drifts are how far each angle has moved past the baseline. The last four fields
are the raw material for sleep detection: `head_down_deg` is the head tilt,
`head_ref_deg` is the tilt recorded while calibrating, `head_drop_deg` is the
difference, and `motion` is how much the joints moved since the last frame.
`sleepy` is vision's instant verdict on all of it. The roasts use all of it.

That is the **side-view, one-person** path. `--mobile` swaps in
`vision/multipose.py`, which measures *several* people **from any angle** and adds
the fields that needs — see [Mobile mode](#mobile-mode--the-phone-as-the-camera).
The reading contract, the state machine and the voice layer do not change.

### 2. Calibration and sit-down detection

A session can only start when someone is detected **and seated**. Seated means the
thigh is within `seated_threshold` of horizontal for `seated_delay`. If a desk
hides the knee, the detector falls back to "person present + hip parked in one
place + torso upright enough".

Once seated, `calibration_seconds` of sitting up straight are averaged into the
baseline (`neck` and `torso`). Without a baseline the code falls back to
12° / 4°, but a real calibration is what makes the score personal.

### 3. The state machine — when to speak

`server/state_machine.py` owns every decision. It reads only readings, and emits
events. Its whole vocabulary is six states:

| State | Means | Leaves when |
| ----- | ----- | ----------- |
| `away` | no person, or not seated | seated for ~2 s → a session starts |
| `calibrating` | capturing the upright baseline | baseline saved |
| `good` | score above the upper threshold | score drops below the lower threshold |
| `slouching` | score below the lower threshold | good again, or the roast timer elapses |
| `roasted` | a roast was just played | good again (after the debounce) |
| `sleeping` | collapsed, motionless and head down past the sleep threshold | moves or sits up again |

Hysteresis stops the state flickering: it **enters** slouching below
`lower_threshold` (0.70) and only **returns** to good above `upper_threshold`
(0.80). Between those two values nothing changes. Returning to good also requires
`good_debounce_seconds` (~2 s) of genuinely good posture, so a brief sit-up cannot
dodge a roast.

Two rules matter more than the rest:

- **The cooldown blocks roasts, not tracking.** During a cooldown the state still
  updates and the website still turns yellow; only the audio waits.
- **`away` is never roasted.** Standing up ends the session instead.

### 4. Voice — writing and speaking

Gemini **writes** the insults; ElevenLabs **speaks** them. They are separate jobs
with separate keys and separate quotas, and the words never touch the browser.

The prompt text lives in files, not code:

| File | Controls |
| ---- | -------- |
| `voice/prompts/roast.txt` | normal mode: actually insulting, still PG |
| `voice/prompts/nice.txt` | nice mode: encouragement |

Both are templates with `{{TOKEN}}` placeholders (`{{CONTEXT}}`, `{{EXCLUDE}}`,
`{{COUNT}}`, `{{TIER}}`, `{{TIER_BRIEF}}`), filled in with plain string
replacement rather than `str.format` — the templates contain literal JSON braces.
Built-in defaults are used if a file is missing, so a deleted file cannot break
the app.

The **delivery** is not a prompt: ElevenLabs takes `voice_settings`, so the tier
of an insult maps to stability/style numbers (see the table below).

### The chef

The roast prompt casts the model as **a furious chef inspecting a dish it did not
order: your posture** — everything is RAW, under-seasoned, badly plated. The lines
stay short (**3–9 words**) and land one sharp comparison when it earns its place:
food and kitchens are the house speciality, but they are a **garnish, not the whole
dish**, because a plain furious line beats a forced food joke. Nice mode is the same
chef, delighted. The writing angles in `voice/roasts.py:TOPICS` include a few kitchen
angles, and the offline pools carry chef lines too, so the persona survives without
an API key.

### Never repeating, and never boring. Every insult actually said is appended to
`data/insults.txt`, one per line. That ledger is enforced in four places:

1. its most recent lines are sent to Gemini as banned in the prompt
2. `validate()` drops any generated line that matches one already said
3. the hand-written fallbacks skip lines in it
4. the pool refuses to queue a line already said, or already queued

Exclusions are ordered most-relevant-first (just played → queued → history),
because only the head of that list is shown to the model (the 40 most recent).

On top of that, every batch is given a **writing angle** chosen at random from
`voice/roasts.py:TOPICS` — write as the chair, blame gravity, write as their back's
warranty department — so consecutive roasts do not all sound the same. The
offline fallback pools are ~20 lines per tier (plus 10 neck- and 10 torso-specific
lines) so the hand-written set does not run dry quickly either.

### 5. Where a roast comes from

| `roast_source` | Behaviour |
| -------------- | --------- |
| `live` (default) | On `roast_due` the Pi asks Gemini for a fresh line right then, on a worker thread so the vision loop never blocks, voices it and plays it. `live_api_cooldown_seconds` rate-limits those calls. |
| `pool` | A background worker keeps pre-generated clips ready, so playback is instant and the model is never in the real-time path. |

In `live` mode the pool stops refilling, so the two paths never both spend quota.
Anything that fails falls back to a pooled clip, then a hand-written line.

### 6. Audio playback

`voice/player.py` plays one clip at a time through a **size-1 queue**: if
something is already playing the new clip is skipped rather than stacked.

1. **Bluetooth** (default route) — the Pi plays to the Nest as a Bluetooth
   speaker. It reconnects before each clip, because the link drops when idle.
2. **Wi-Fi cast** (backup) — `pychromecast` tells the Nest to fetch the clip from
   `/media/roasts/<name>` over the network.
3. **Local player** — `pw-play`, `paplay`, `aplay` or `ffplay`, whichever exists,
   which is why a laptop hears roasts through its own speakers.

Every clip gets **0.5 s of leading silence**, because a Bluetooth speaker tends to
clip the first syllable while the link wakes up. `--no-audio` prints instead.

### 7. The website

Flask serves one page plus an MJPEG video stream and a server-sent-events
channel. The camera thread encodes each frame **once** and stores the JPEG behind
a lock; routes only read it, so extra browser tabs cost almost nothing.

---

## When a roast fires

Two independent triggers, either one fires `roast_due`, both gated by the same
cooldown (in mobile mode every tracked person has their own copy of this machine):

| Trigger | Condition | Default |
| ------- | --------- | ------- |
| **sustained slouch** | score below `lower_threshold` for `roast_seconds` | 0.70 for 10 s |
| **bad posture** | score below `bad_score_threshold` held for `bad_score_seconds` | 0.45 for 3 s |
| **sleeping** | collapsed, motionless and head down for `sleep_threshold_seconds` | 25 s |

The sleep trigger is separate on purpose, and it is the slowest and least certain
one: see [Sleep detection](#sleep-detection) below. Once it is ready it takes over
from the slouch trigger, so you get "wake up" instead of a fourth "sit up" while
you are face-down on the desk.

```
sit down ──2s─▶ seated ──5s─▶ calibrated ──▶ slouch ──▶ ROAST
                                             (<0.70)    +10s
                                             ──── deep slouch (<0.45) for 3s ──▶ ROAST immediately
```

There is no warning stage: the first sign of slouching is the website turning
amber, and a roast follows at `roast_seconds`. After a roast, `cooldown_seconds`
before the next one — so with the defaults, a continuous slouch is roasted every
10 seconds. Because the score trigger is deliberately faster, a *steep* slouch
roasts after ~3 s rather than waiting the full 10 — that is expected, not a bug.
Set `bad_score_threshold` to `0` to use only the sustained timer.

Check the real numbers instead of guessing:

```bash
python .pi/skills/posture-coach-vision/scripts/roast_timeline.py
python .pi/skills/posture-coach-vision/scripts/roast_timeline.py --profile bad --cooldown 20
```

An example run at defaults:

```
t=  3.0s  session_start
t=  8.1s  calibrated
t= 20.7s  roast_due   reason=bad_posture       slouch#1  part=neck  score=0.04
t=121.3s  roast_due   reason=bad_posture       slouch#3  part=neck   (+100.6s since the last roast)
```

A deep slouch trips the score trigger; a *moderate* one (score between the lower
threshold and 0.45) waits out the timer:

```
t=  5.1s  calibrated
t= 15.3s  roast_due   reason=sustained_slouch  slouch#1  part=neck  score=0.55
```

**Events.** The state machine emits `session_start`, `calibrated`, `good`,
`roast_due`, `session_end`, plus a `state` event on every transition.

```json
{"event": "roast_due", "reason": "bad_posture", "slouch_count": 5,
 "session_minutes": 23, "worst_neck_angle": 41, "worst_torso_angle": 9,
 "slouch_part": "neck", "slouch_seconds": 12.6, "score": 0.31}
```

### Sleep detection

Sleeping is the one fault a posture score cannot name: a slumped person and a
sleeping person measure the same. So it is detected from **three signals at once**,
all of which must hold:

| Signal | Measures | Default |
| ------ | -------- | ------- |
| collapsed | `score` at or below `sleep_score` | 0.35 |
| motionless | joint movement per frame below `sleep_stillness` | 0.006 |
| head down | `head_drop_deg` past `sleep_head_delta`, or the eye landmarks gone entirely (a head dropped that far is usually out of frame) | 20° |

All three must hold for `sleep_threshold_seconds` (25 s) before anything is said,
and then the state goes **`sleeping`** (red, like every other bad state) and the
insults come from the hand-written `SLEEPING` pool or the model's own sleep angle.

**Be honest about what this is.** MediaPipe pose cannot see closed eyes, so this is
really *"collapsed and completely still for twenty-five seconds"* — a very still
person reading a dense document will look the same. That is why it is slow, why the
threshold is a setting, and why the Debug panel shows every input: **Motion**,
**Head down** (value, calibrated reference, difference) and **Sleeping**.

```bash
python .pi/skills/posture-coach-vision/scripts/roast_timeline.py --profile sleeping
# t=  8.3s  roast_due  reason=bad_posture  ...
# t= 38.3s  roast_due  reason=sleeping     (+10.0s since the last roast)
```

While the sleep trigger is ready the slouch trigger stands down: otherwise the
slouch roast would take the one shared cooldown every time it came free and the
sleep roast would never get a turn.

---

## Reference: every number and option

Everything in this section is editable at runtime on the website under
**Controls**, and is persisted to `data/settings.json`.

The file records a `__version__`. When a default changes, an older file is
migrated so the new default actually takes effect, while the values you tuned by
hand (camera, mirror, voice) are preserved. That matters: a settings file silently
overrides a changed default, which is how a "10 second" roast can keep taking 30.

### Timing and threshold settings

| Setting | Default | Range | Meaning |
| ------- | ------- | ----- | ------- |
| `roast_seconds` | `10` | 2–3600 | Continuous slouching before a roast (the *sustained* trigger). Reset by `good_debounce_seconds` of good posture. |
| `cooldown_seconds` | `10` | 0–3600 | Minimum gap between two roasts. With the defaults you are roasted every 10 s while you keep slouching. Blocks the *audio*, not the tracking — the state, the chart and the website keep updating. |
| `good_debounce_seconds` | `2` | 0–60 | How long good posture must hold before the slouch episode is considered over. Stops a brief sit-up from dodging a roast. |
| `calibration_seconds` | `5` | 1–60 | Length of the upright baseline capture at the start of a session (or when *Calibrate* is pressed). |
| `lower_threshold` | `0.70` | 0.05–1.0 | Score **below** this ⇒ slouching. This is the "enter slouch" edge of the hysteresis. |
| `upper_threshold` | `0.80` | 0.05–1.0 | Score **above** this ⇒ good. Between the two thresholds the state does not change. |
| `bad_score_threshold` | `0.45` | 0–1.0 | The deep-slouch score that roasts without waiting out `roast_seconds`. `0` disables this trigger. |
| `bad_score_seconds` | `3` | 0–60 | How long the bad score must hold before that trigger fires, so one noisy frame cannot cost an API call. |
| `sleep_threshold_seconds` | `25` | 0–600 | How long they may be collapsed, motionless and head down before being accused of sleeping. `0` disables the trigger. |
| `mobile_max_people` | `3` | 1–6 | Mobile only: how many people to track per frame (and how many poses one inference looks for). More people is more CPU — the lite model costs ~30 ms/frame for 3 on a laptop. Restart to change it. |
| `delta_angle` | `20` | 1–60 | Tolerance: how many degrees past the baseline map to a score of `0`. Smaller ⇒ more sensitive. |

### Vision parameters

These are constructor arguments in `vision/detector.py` and `server/camera.py`
rather than website settings.

| Parameter | Default | Meaning |
| --------- | ------- | ------- |
| `DEFAULT_BASELINE` | neck `12°`, torso `4°` | Used only until a calibration happens. |
| `tolerance` (= `delta_angle`) | `20` | Degrees past the baseline that map to score 0. |
| `alpha` (EMA) | `0.2` | Score smoothing. Higher reacts faster, lower is steadier. |
| `seated_threshold` | `30°` | Thigh angle from horizontal below which the person counts as seated. |
| `seated_delay` | `2 s` | How long that must hold before a session starts, so someone still sitting down is not roasted. |
| `standup_delay` | `1 s` | How long "not seated" must hold before the state goes `away`. |
| `min_visibility` | `0.3` | Knee/eye visibility below which that landmark is ignored. |
| `sleep_score` | `0.35` | Collapsed: the score at or below which the slump counts as a slump. |
| `sleep_stillness` | `0.006` | Movement per frame (normalized landmark units) below which they count as motionless. Watch the Debug panel's **Motion** row to tune it: a fidgeting person is an order of magnitude above it. |
| `sleep_head_delta` | `20°` | Extra downward tilt, past the head level recorded while calibrating, that counts as a dropped head. |
| `sleep_head_fallback` | `45°` | Absolute tilt used only until a head reference has been measured. |
| `model` | `lite` | MediaPipe pose model: `lite`, `full` or `heavy`. `lite` is the one that keeps 10+ fps on a Pi. |
| `fps_target` | `10` | Frames per second the vision loop aims for. Posture changes slowly; 5 fps of detection is still enough. |
| `jpeg_quality` | `70` | JPEG quality for the shared frame that `/video_feed` serves. |
| `sample_interval` | `1 s` | How often a reading is written to SQLite (sampled, not every frame). |
| `publish_interval` | `0.5 s` | How often a reading summary is pushed to the browser over SSE. |

### Voice parameters

| Setting | Default | Values | Meaning |
| ------- | ------- | ------ | ------- |
| `tts_mode` | `auto` | `auto`, `elevenlabs`, `local` | Which engine speaks. `auto` = ElevenLabs when a key is present, otherwise the local engine. Forcing one keeps the voice consistent and goes **silent** rather than mixing engines. |
| `roast_source` | `live` | `live`, `pool` | Generate a fresh roast at the moment of the slouch, or play pre-generated clips instantly. |
| `live_api_cooldown_seconds` | `10` | 0–3600 | Minimum gap between two *freshly generated* roasts, on top of `cooldown_seconds`. Within it, a roast still plays from the pre-generated pool. It defaults to the same 10 s so live generation keeps up with the roast cadence, and it is the dial to raise if you want to protect the API quota. |
| `roast_pool_size` | `3` | 1–50 | Clips kept ready **per tier** (so 3 × 3 tiers = 9). Only used when `roast_source = pool`. |
| `nice_mode` | `false` | on/off | Swaps insults for encouragement and uses `prompts/nice.txt`. |

**Escalation tiers** — chosen from the session's slouch count
(`voice/roasts.py:tier_for`):

| Slouches | Tier | Delivery (ElevenLabs `voice_settings`) |
| -------- | ---- | -------------------------------------- |
| 1–3 | `mild` | stability 0.55, similarity 0.75, style 0.15 |
| 4–6 | `medium` | stability 0.40, similarity 0.75, style 0.35 |
| 7+ | `spicy` | stability 0.25, similarity 0.80, style 0.60 |
| nice mode | `nice` | stability 0.65, similarity 0.75, style 0.10 |

Lower stability and higher style make the delivery more animated, so a repeat
offender does not sound like a first slip.

**ElevenLabs** (`voice/eleven_tts.py`) — the preferred voice:

| Item | Default | Notes |
| ---- | ------- | ----- |
| voice | **Sarah** (`EXAVITQu4vr4xnSDxMaL`) | A *premade* voice. Free plans cannot use "library" voices through the API (HTTP 402); if the configured voice is rejected the client lists your account's voices and uses one of those. |
| model | `eleven_flash_v2_5` | Fast and cheap, which suits one-liners. |
| output format | `pcm_24000` | 16-bit PCM, so it drops into the same WAV pipeline as everything else. |
| timeout | `30 s` | Per request. A `422` retries once without the optional `style` setting. |

**Pool and ledger**:

| Item | Default | Meaning |
| ---- | ------- | ------- |
| `refill_interval` | `1.5 s` | How often the pool worker tops the buckets up. |
| `max_batch` | `4` | Most clips generated in one pass, so one refill cannot block for long. |
| `max_exclude` | `300` | Ledger lines retained as exclusions; the prompt shows the 25 most recent. |
| `MAX_WORDS` | `14` | Generated lines longer than this are dropped. The prompt asks for 6–12 words. |
| `TOPICS` | 16 angles | Writing angles handed to the model one batch at a time, so roasts vary. |

### Audio and camera settings

| Setting | Default | Values | Meaning |
| ------- | ------- | ------ | ------- |
| `audio_route` | `bluetooth` | `bluetooth`, `cast`, `off` | Where the roast comes out: the Nest over Bluetooth, the Wi-Fi cast backup, or `off` (printed only — what `--no-audio` sets). |
| `cast_name` | `""` | any device name | The friendly name the Nest advertises to Chromecast, used by the cast backup. |
| `mirror` | `false` | on/off | Mirrors the camera. Applied in the vision thread **before** the overlay is drawn, so the angle labels stay the right way round. |
| `camera_index` | `0` | 0–16 | Which camera to open. Set by `--camera N`, which also remembers it for next time. |

### Playback parameters

| Item | Default | Meaning |
| ---- | ------- | ------- |
| queue size | `1` | One clip at a time; new clips are **skipped** when busy rather than stacked. |
| Bluetooth reconnect | checked every `5 s`, reconnected before each clip | Idle links drop; `bluetoothctl connect` is retried when needed. |
| player command | `pw-play` → `paplay` → `aplay` → `ffplay` | First one found on `PATH` wins; override with `POSTURE_BLUETOOTH_PLAYER`. |
| command timeout | `45 s` | A stuck player cannot wedge the thread. |
| leading silence | `0.5 s` | Prepended to every clip. |
| cast discovery | `6 s`, device wait `8 s`, active wait `12 s` | Timeouts for the Wi-Fi cast backup. |

### Server and website parameters

| Item | Default | Meaning |
| ---- | ------- | ------- |
| host / port | `0.0.0.0` / `5000` | `run.py --host/--port`. |
| Flask threading | on, reloader **off** | The reloader starts the app twice and the second copy cannot open the camera. |
| SSE keepalive | `15 s` | A comment line is sent when nothing else happens, so proxies do not close the stream. |
| SSE subscriber cap | `200` queued events | A slow client is dropped rather than stalling the vision thread. |
| MJPEG loop | `0.02 s` | The feed re-sends the latest JPEG; encoding happens once, in the camera thread. |
| `/api/roasts` default limit | `3` | Clamped to 1–50. |

### Environment variables

`.env` is git-ignored (`!.env.example` is kept). Copy `.env.example` to start.

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `GEMINI_API_KEY` | — | Lets Gemini **write** the roasts. Without it the hand-written fallbacks are used. |
| `GEMINI_TEXT_MODEL` | `gemini-3.8-flash` | Text model for writing roasts. Older ones get **retired for new accounts** (`gemini-2.5-flash` answers 404), so the writer walks a fallback chain. |
| `ELEVENLABS_API_KEY` | — | The voice. Without it, `auto` uses the local engine. |
| `ELEVENLABS_VOICE_ID` | Sarah | Any voice your plan allows. |
| `ELEVENLABS_MODEL` | `eleven_flash_v2_5` | ElevenLabs model. |
| `ELEVENLABS_OUTPUT_FORMAT` | `pcm_24000` | Ask for PCM so it stays in the WAV pipeline. |
| `NEST_BLUETOOTH_MAC` | — | From `bluetoothctl devices`; enables the Bluetooth route. |
| `NEST_CAST_NAME` | — | Friendly name for the Wi-Fi cast backup. |
| `POSTURE_BLUETOOTH_PLAYER` | auto-detected | Force a player, e.g. `paplay`. |
| `POSTURE_LOCAL_VOICE` | `en` (system default) | Local voice, e.g. `Microsoft Zira Desktop`. |
| `POSTURE_DISABLE_LOCAL_TTS` | unset | `1` disables the offline voice entirely. |
| `POSTURE_CAMERA_BACKEND` | `dshow` on Windows | `dshow`, `msmf` or `auto`. |
| `POSTURE_DATA_DIR` | `./data` | Where the database, clips and ledger live. |

### Command line

| Flag | Meaning |
| ---- | ------- |
| `--simulate` / `--fake` | Scripted readings + synthetic feed. No camera, no MediaPipe needed. The script is a repeating loop: away → good → **sleep** (long enough to trip the slow trigger) → good → slouch → good → slouch. |
| `--mobile` | Phone-as-camera mode (see [Mobile mode](#mobile-mode--the-phone-as-the-camera)). No local camera at all: phones post frames to `/m` and get a live roster of everyone in view. Serves HTTPS with a throw-away certificate, because browsers only hand out the camera on a secure origin. |
| `--mobile-people N` | With `--mobile`: how many people to track at once (default: the `mobile_max_people` setting, 3). |
| `--demo` | Short thresholds for a fast demo: roast 10 s, cooldown 15 s, calibration 3 s, pool 2/tier. Applied in memory, not saved. |
| `--preview` | Standalone OpenCV window instead of the website. Best way to aim the camera. |
| `--video FILE` | Run the real detector against a recording instead of the webcam. |
| `--camera N` | Camera index. Remembered in settings, so plain `run.py` reuses it. |
| `--camera-backend NAME` | `auto`, `dshow`, `msmf`, `v4l2`, `avfoundation`. |
| `--list-cameras` | Probe camera indices, print what works, and exit. |
| `--list-snapshots` | With the above: save one still per camera to `data/camera-scan/` so you can tell them apart. |
| `--test-voice [TEXT]` | Voice one line with the configured provider, report which engine spoke, play it, exit. |
| `--timers [URL]` | Live countdown to the next roast, polled from a running server (default `http://127.0.0.1:5000`). Shows the state, score, how long the slouch has lasted, which trigger is next and whether a cooldown is holding it. This is the tool for "why has it not roasted yet". |
| `--model NAME` | MediaPipe model: `lite` (default), `full`, `heavy`. |
| `--no-audio` | Print roasts instead of playing them. |
| `--host` / `--port` | Bind address and port. |
| `--settings PATH` | Use a different settings file. |
| `--debug` | Verbose logging. |

### HTTP API

| Method | Route | Purpose |
| ------ | ----- | ------- |
| GET | `/` | The website. |
| GET | `/m` | The **mobile** page: camera in the browser, roster of everyone in view. Needs `--mobile`. |
| POST | `/m/frame?device=ID` | One JPEG frame from a phone. Returns the people in it (scores, views, normalized joints), the roster and any roasts earned. 409 without `--mobile`. |
| GET | `/m/state` | The current roster without uploading a frame (for a reload or a slow poll). |
| GET | `/video_feed` | Live annotated MJPEG stream. |
| GET | `/events` | Server-sent events. Message kinds: `reading` (~2/s, includes `next_roast` for the countdown), `event` (state changes, roast_due), `roast` (a clip was chosen), `settings`, `calibrating`. |
| GET | `/api/stats` | Current session stats plus the live state, reading, settings and `next_roast` (the countdown: seconds, which trigger, and whether a cooldown is holding it). |
| GET | `/api/history` | Readings for the chart, plus roast markers. |
| GET | `/api/roasts?limit=N` | The quote ledger (default 3, max 50). |
| GET | `/api/status` | Camera, fps, audio route, voice engines, pool and detector state. |
| POST | `/api/calibrate` | Start the upright calibration (409 if no session). |
| POST | `/api/test-roast` | Play a roast immediately (409 if the player is busy). |
| GET | `/api/settings` | All settings and their allowed values. |
| POST | `/api/settings` | Update settings live. `tts_mode` also clears the ready pool. |
| GET | `/media/roasts/<name>` | Serves generated clips — this is what the Nest fetches when casting. |
| GET | `/healthz` | Liveness probe. |

### Files and data

Everything under `data/` is git-ignored and safe to delete.

| Path | Contents |
| ---- | -------- |
| `data/settings.json` | Live settings, saved whenever you press *Save*. |
| `data/posture.db` | SQLite: sessions, sampled readings, state changes and every roast (with `voice` and `origin`). |
| `data/insults.txt` | The quote ledger — every insult actually said, one per line. |
| `data/roasts/*.wav` | Voiced clips, cached per engine: `roast_<text-hash>_<elevenlabs\|local>.wav`. |
| `data/camera-scan/` | Snapshots from `--list-cameras --list-snapshots`. |
| `models/*.task` | Downloaded MediaPipe model. |

Because clips are cached by text hash **and** engine, changing the voice does not
re-voice old clips. Delete `data/roasts/` to have everything re-voiced.

---

## The website

One page, fullscreen. The annotated feed fills the viewport and the UI floats over
it as translucent panels. Data arrives over SSE, so nothing needs refreshing.

| Panel | Shows |
| ----- | ----- |
| **Posture** | live neck and torso angles, the score with its 0.7/0.8 threshold ticks, and the **roast countdown** — `roast in 6.4s · sustained_slouch`, or `held by cooldown` when a cooldown is delaying it. || **Session** | good-posture %, slouches, roasts, longest good streak. |
| **Score** | the score over the session, with roast markers (resizable). |
| **Controls** | calibrate, test roast, nice mode, and every setting in the tables above. |
| **System** | the debugging surface: **Detection** (`person · seated` — the usual reason nothing fires), camera and backend, fps, Nest connection, audio route, the voice engine and its voice, the pool's voice mix, insults used, and the roast source with any API cooldown. |
| **Debug** | the raw internals, live: state, detection, score (smoothed and raw), neck and torso with their drift, which part is failing, the slouch timer, **the roast countdown with its trigger or what is blocking it**, cooldown, calibration progress, the thresholds and timers in use, camera stats, session id and the last state-machine event. Hide it from **Panels** when you do not want it. |

### Colour coding

Deliberately blunt, so a glance is enough:

| Colour | Means |
| ------ | ----- |
| **green** | good posture — score above the upper threshold |
| **red** | bad posture — `slouching`, `roasted` and `sleeping` are the same badness, only the pill text distinguishes them |
| amber | undecided: the score is in the narrow band between the two thresholds, so the state has not changed yet |
| grey | `away` — nobody seated, so nothing is judged |
| blue | `calibrating` |

The score chart uses the same idea: green above the good threshold, red below the
slouch threshold, crossing over only inside that band.

Panels can be dragged by their title bar, collapsed, hidden and re-added from the
**Panels** menu; the layout is remembered in the browser. The header buttons are
*Panels* (**P**), *Mirror* (**M**) and *Fullscreen* (**F**).

### Debugging on the page

The **Debug** panel is the in-app answer to "why has it not roasted yet":

```
State          roasted
Detection      person · seated
Score          0.00   raw 0.00
Neck           41.8°   +29.8°
Torso          18.1°   +14.1°
Failing part   neck
Sleeping       yes  26.4s / 25s
Motion         0.0011
Head down      49°   ref 6°   +43°
Slouched       8.7s
Next roast     8.2s   cooldown      <- the countdown, and what is holding it
Cooldown       8.2s
Thresholds     slouch<0.70  good>0.80  ±20°
Timers         roast 10s  cool 10s  bad<0.45 3s
Camera         camera:1 (dshow)  9.7fps  210f  0err
Session        id 1  4.2min  2 slouches  1 roasts
Last event     roast_due · bad_posture · roasted
```

Read it like this: a countdown that **never starts** means the machine is not
seeing a slouch (look at Detection and the score); a countdown stuck behind
**cooldown** means it is working and rate-limited; and a countdown reaching zero
with no audio means the problem is the voice or the audio route, not the timer.
The same countdown is on the Posture panel, and `run.py --timers` prints it in a
terminal.

---

## Mobile mode — the phone as the camera

```bash
python run.py --mobile            # then open the https://<lan-ip>:5000/m it prints
```

Same server, same brains, different camera. The Pi app needs a USB webcam pointed at
your chair; mobile mode needs a phone, and the phone can walk around. **The AI still
runs on the server:** MediaPipe pose, the scoring, Gemini writing the roasts and
ElevenLabs speaking them. The phone captures a frame, posts a JPEG, draws the
skeleton it is told about and speaks the insult it receives. Nothing expensive runs
in the browser, and no video ever travels back down the wire.

```
phone camera ──JPEG ~7/s──▶ /m/frame ──▶ MediaPipe (up to N people)
                                              │
                    per-person tracking ──────┤──▶ view angle (side/front/back)
                                              │    + the metric that view supports
                                              ▼
                     roster JSON ◀── scores, states, joints, roasts
                                              │
                        roast_due ──▶ Gemini/ElevenLabs ──▶ SSE ──▶ the phone speaks it
```

### Why the angles change everything

The desktop app measures one seated person **from the side**: neck and torso angles
from the ear→shoulder→hip chain. Point a phone at a room and that assumption dies —
a neck angle means nothing when someone is facing you. So mobile mode first works
out **which way the camera is seeing them**:

| View | How it is detected | What is measured |
| ---- | ------------------ | ---------------- |
| **side** | the two shoulders nearly overlap in x (spread ÷ torso length < 0.45) | neck and torso angles — the same metrics the Pi app uses |
| **front** | shoulders spread across the torso, and a face is visible | **head sink** (how far the head has dropped between the shoulders, in shoulder widths) and **shoulder tilt** |
| **back** | shoulders spread, but no nose or eyes visible | head sink, shoulder tilt |

Every score is then measured **against that person's own upright in that view**:
watch somebody for `calibration_seconds` (2 s) and their shoulder-to-head distance,
or their neck angle, becomes their zero. Turn to a new angle and a new reference is
learned for it. That is the honest fix for "all angles": don't pretend a front view
can measure forward-head angles — measure what that view can actually see, and say
so on screen (`side · neck 41°`, `front · head sink 24%`).

**The limits, plainly.** MediaPipe still needs most of the upper body visible, so a
head-and-shoulders shot at 3 m will not work. Identity is positional and per frame:
if somebody leaves and comes back, they may be measured as a new person (ids are
reused, so "Person 1" usually comes back as "Person 1"). And a baseline learned
while somebody is already slumped will call that slump normal — ask people to sit up
for the first second, or press recalibrate on the desktop page.

### The roster

The phone shows everyone in view, **worst first**, each with a colour-coded score,
which view they are being measured from, and the metric behind it. Good and bad
counts sit in the top bar. A roast names the person out loud ("Person 2, sit up") and
flashes their row, because a group insult lands on nobody.

Which person gets roasted is decided **per person**, by that person's own state
machine — the same `PostureStateMachine` the Pi app uses, one per tracked person, so
hysteresis, the roast timer and the cooldown behave identically. Mobile only changes
two things: calibration is quick (2 s, not 5) and sleep detection is off, because
people walk around in a room instead of sitting still at a desk.

The phone also drives the **desktop HUD**: the frames it posts are stored as the
shared frame, so `/video_feed` shows the phone's camera and the Debug panel ticks
from the worst person in view. The picture is shared even when nobody is detected —
aiming the phone is exactly when you want to see what it sees — and the HUD then
simply reads `away` / `nobody`. Open the laptop page on the projector and hand
somebody a phone.

### Getting the camera to work on a phone

Browsers only give a page the camera on a **secure origin**, and `http://192.168.x.x`
is not one. `--mobile` therefore serves HTTPS with a throw-away self-signed
certificate (that is what `cryptography` in `requirements.txt` is for) and prints the
`https://` URL to open. Tap **Advanced → Proceed** once and the camera works. If you
would rather stay on HTTP, or the certificate gets in the way, use Chrome's
`chrome://flags/#unsafely-treat-insecure-origin-as-secure` and add your `http://`
origin. The phone page detects either situation and tells you which one you are in
instead of silently doing nothing.

On the phone, the settings sheet has the knobs that matter for a demo: **speak
roasts on this phone**, mirror, skeleton on/off, frames per second, and which camera
(back to point at other people, front to point at yourself).

### Trying it without a phone

```bash
python run.py --mobile
# then, on the desktop, in a second terminal:
curl -k -X POST --data-binary @frame.jpg -H 'Content-Type: image/jpeg' \
     https://127.0.0.1:5000/m/frame?device=laptop
```

Or drive it from a browser at `https://127.0.0.1:5000/m` (localhost counts as secure,
so the camera works there too). For screenshots and debugging, the page exposes its
internals as `window.__pc`, so this renders a roster with no camera at all:

```js
__pc.handleFrame({ok: true, seen: 1, good: 0, bad: 1, people: [], roster: [
  {id: 1, label: "Person 1", view: "side", metric: "side · neck 41°", score: 0.12,
   state: "slouched", good: false, bad: true, calibrating: false, seen_for: 9,
   stale_for: 0, points: {}}
]});
```

### Tests

The whole mobile pipeline is testable offline: `vision/multipose.py` takes plain
pose dicts (no cv2, no MediaPipe) and `server/mobile.py` takes JPEG **bytes** and
returns a dict, so the tests post a frame with a stub landmarker and assert on the
roster (`tests/test_multipose.py`, `tests/test_mobile.py`). JPEG encoding in the tests
is the only reason those skip anything, and only when OpenCV is absent.

---

## Raspberry Pi setup

**Hardware:** Pi 5 with the official 27 W supply (weak supplies throttle and drop
USB), an active cooler, a 32 GB+ microSD with Raspberry Pi OS 64-bit plus a spare
card, a USB webcam mounted at shoulder height 1–2 m to the side, a Nest, and a
phone hotspot or travel router so everything shares one network.

```bash
sudo apt update
sudo apt install -y python3-venv python3-dev bluez pulseaudio-utils espeak-ng
git clone <your-repo> ~/posture-coach && cd ~/posture-coach
./setup.sh
cp .env.example .env && nano .env      # add the API keys
python -c "import mediapipe; print('mediapipe ok')"
```

Pair the Nest (do this first — it is the riskiest hardware step):

```bash
bluetoothctl
[bluetooth]# scan on
[bluetooth]# pair AA:BB:CC:DD:EE:FF
[bluetooth]# trust AA:BB:CC:DD:EE:FF
[bluetooth]# connect AA:BB:CC:DD:EE:FF
```

Put that MAC in `NEST_BLUETOOTH_MAC`, set the hostname to `posture`, then run on
boot with `deploy/posture-coach.service` (`systemctl enable --now posture-coach`).
Rebooting should bring the website back by itself within a minute.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
| ------- | ------------ | --- |
| State stays `away`, nothing ever fires | nobody detected, or detected but not seated | check the System panel's **Detection** row; make sure the whole head and hip are in frame and the thigh reads as horizontal |
| A roast never plays | the sustained trigger needs 10 s of *continuous* slouching (or a deep slouch for 3 s) | set **Roast (s)** low, or run `run.py --demo`, and watch the pill go amber then red |
| The voice sounds robotic | the local engine, not ElevenLabs | check the System panel's **Voice** row; add `ELEVENLABS_API_KEY`, or run `--test-voice` to see which engine answered |
| Voices change between roasts | a pool filled by two engines, or old cached clips | set **Voice** to one engine (which clears the pool), and delete `data/roasts/` to re-voice everything |
| Insults repeat, or sound generic | Gemini is not writing them — the model was retired (a 404 in the log) and the offline pools are being used | check `Gemini is writing roasts with …` in the log, or run `probe_tts.py --live` to see which model answers; the chain picks a live one, and `GEMINI_TEXT_MODEL` sets which to try first |
| Roasts repeat | the hand-written fallback pool is finite | add a Gemini key so roasts are written fresh; check `data/insults.txt` |
| Roasts are silent but captions appear | audio route, or the Bluetooth link dropped | try **Voice → Local**, switch the route to cast, and confirm the Nest is paired |
| Sleep never fires | it needs all three signals at once, for 25 s: collapsed, motionless *and* head down | watch the **Sleeping**, **Motion** and **Head down** rows on the Debug panel; if Motion stays high they are fidgeting (correct), and if Head down shows `ref --` no head reference has been measured yet |
| Mobile: nothing happens when I tap **Start camera** | the page is not a secure origin, so the browser withholds the camera | the phone page says which case it is. Either open the `https://…:5000/m` URL `--mobile` printed and accept the certificate once, or in Chrome set `chrome://flags/#unsafely-treat-insecure-origin-as-secure` to include your `http://` origin. On `https://` with a self-signed certificate, Chrome treats the page as insecure *until* you accept the exception |
| Mobile: roster stays empty | no pose found in the frames, or nobody is in shot | check **In view** on the phone and `detect_ms` in `/api/status`; the model needs most of the upper body visible, and a dark backlit room defeats it |
| Mobile: laggy, or the numbers crawl | frames are too big, too frequent, or too many people | lower **Frames per second** in the phone's settings, point the phone at fewer people, and keep the upload at the default 640 px wide. The phone skips a frame rather than queueing up |
| Mobile: I hear each roast twice | the server played it through the Nest *and* the phone spoke it | set **Audio** to `off` in Controls, or turn off **Speak roasts on this phone** |
| Mobile: a person who leaves comes back as "Person 1" again | identity is positional, per frame | expected: the roster is a live view of the room, not a name registry. Give the phone a second after re-framing |
| Sleep fires while awake | they were genuinely motionless and slumped for 25 s | raise **Sleep after (s)**, or lower `sleep_stillness` in `vision/detector.py` so tiny movements count as movement |
| Under ~8 fps, laggy overlay | an oversized model | `--model lite`, smaller frames, or detect on every second frame |
| `could not open camera 0` | another program has it, or the wrong index | `run.py --list-cameras`, then `--camera N` |
| Page looks stale after an edit | Flask caches the template, and the browser caches CSS/JS | restart the server and hard-refresh (`Ctrl+F5`) |
| 429 from Gemini | the free TTS tier is ~10 requests/day (Gemini no longer speaks, so this only affects *writing*) | wait, add billing, or rely on the fallback roasts |
| 402 from ElevenLabs | the configured voice is a paid "library" voice | pick a premade voice, or leave it to the automatic fallback |

---

## Testing checklist

- Each teammate sits, calibrates and slouches — works across body types and chair heights.
- Test in the venue's lighting, with the camera in its final position.
- Stand up mid-session: the state goes `away` and nothing is roasted.
- Disconnect the internet: roasts still play from the pool or the local voice.
- Turn the Nest's Bluetooth off mid-session: the site shows it, and switching to cast works.
- Reboot the Pi: the service comes back by itself.
- Run a 10-minute session end to end without touching the code.
- Record a backup demo video.

---

## Team roles and the pitch

| Role | Owns | Builds |
| ---- | ---- | ------ |
| 1. Vision | `vision/` | landmarks, side selection, angles, sit-down detection, calibration, score, hysteresis, multi-person/any-angle measurement |
| 2. Server | `server/` | Flask, camera thread, MJPEG, state machine, SQLite, SSE, mobile frame intake, systemd, fake readings |
| 3. Voice | `voice/` | Bluetooth, the roast prompt and schema, TTS, the pool, the player queue, all fallbacks |
| 4. Website + demo lead | `web/` | the desktop HUD, the phone page, controls, settings, the chart, the pitch and the demo script |

Collision rules: one folder per person, commit small and often, only one person
deploys to the Pi at a time, and contracts change only with everyone's agreement.

**A three-minute pitch.** *Problem:* students hunch over laptops for hours and
polite reminders are easy to ignore. *Demo:* a judge sits down, the system notices,
calibrates, they slouch on purpose, and the Nest roasts them while the roast
appears beside the live angle overlay. *Then the phone comes out:* `--mobile` turns
any phone into the camera, and the projector shows a live roster of the room —
"Person 2, sit up" — measured from whatever angle the phone happens to be holding.
*Architecture:* side-view geometry with per-user calibration, a debounced state
machine, and a pre-generated context-aware roast pool; the phone only captures,
the server does the thinking. *Next:* posture history, profiles, roast
personalities, and remembering who is who between sessions.

Expect these questions: why a side view (it measures real neck and torso angles),
how mobile handles other angles (it detects the view and switches to head sink and
shoulder tilt, per person), how false alarms are avoided (calibration, smoothing,
hysteresis, the `away` state), and what happens without a connection (the pool, the
local voice, and the hand-written fallbacks).

---

## Repository layout

```
.
├── run.py                    # entry point / CLI (auto-uses .venv)
├── config.py                 # paths + .env loader
├── vision/                   # frames -> readings
│   ├── angles.py             #   pure geometry (no cv2/MediaPipe)
│   ├── landmarks.py          #   MediaPipe pose wrapper (one pose, or many)
│   ├── detector.py           #   angles, sit-down detection, score, overlay
│   ├── multipose.py          #   view angle, per-person tracking (mobile)
│   ├── calibrate.py          #   upright baseline capture
│   ├── source.py             #   webcam / video-file sources
│   └── synthetic.py          #   fake camera feed for --simulate
├── server/                   # Flask, threads, state machine, DB
│   ├── app.py                #   app factory + routes
│   ├── camera.py             #   the vision thread
│   ├── mobile.py             #   phone frames -> tracked people
│   ├── state_machine.py      #   the six states and the roast triggers
│   ├── events.py             #   SSE broker
│   ├── db.py                 #   SQLite logging + session stats
│   └── settings.py           #   persisted, live-editable settings
├── voice/                    # writing and speaking
│   ├── gemini_text.py        #   writes roasts (structured JSON)
│   ├── eleven_tts.py         #   speaks them (ElevenLabs)
│   ├── tts_local.py          #   offline voice: espeak/piper/say/SAPI
│   ├── wavutil.py            #   WAV writing, lead silence, normalise
│   ├── insults.py            #   the quote ledger (never repeat)
│   ├── pool.py               #   pre-generated clip pool
│   ├── player.py             #   Bluetooth + cast playback
│   ├── service.py            #   orchestration, provider order, fallbacks
│   ├── roasts.py             #   hand-written fallbacks
│   └── prompts/              #   roast.txt, nice.txt
├── web/                      # the HUD
│   ├── index.html            #   the desktop page
│   ├── mobile.html           #   the phone page
│   └── static/{app.js,mobile.js,style.css,mobile.css,fonts.css,fonts/}
├── fakes/fake_readings.py    # scripted readings
├── tests/                    # unit tests + smoke.py
├── .pi/skills/               # agent skills for working on this repo
└── deploy/posture-coach.service
```
