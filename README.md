# Posture Coach 🪑

Watches you from the side with a webcam on a Raspberry Pi 5, and when you slouch
too long the Google Nest roasts you with an insult written by Gemini.

Everything runs on the Pi: one Python process with a few background threads.
The computer only opens the website in a browser.

```
camera → MediaPipe landmarks → neck/torso angles → score → debounced state machine
      → pre-generated Gemini roast (pool) → Bluetooth to the Nest
      → Flask website: live annotated feed, stats, chart, controls
```

Gemini is deliberately **kept out of the real-time path**: roasts are written
and voiced ahead of time into a pool, so a slouch gets audio in about a second
instead of waiting on an API call. If Gemini or the network is down, hand-written
roasts plus a local TTS engine keep the demo alive.

---

## Contents

- [Quick start (laptop, no hardware)](#quick-start-laptop-no-hardware)
- [Repository layout](#repository-layout)
- [How it works](#how-it-works)
- [Interface contracts](#interface-contracts)
- [HTTP API](#http-api)
- [Configuration](#configuration)
- [Running modes](#running-modes)
- [Fakes for parallel development](#fakes-for-parallel-development)
- [Raspberry Pi setup](#raspberry-pi-setup)
- [Run on boot (systemd)](#run-on-boot-systemd)
- [Testing](#testing)
- [Tuning and troubleshooting](#tuning-and-troubleshooting)
- [Team roles and day plan](#team-roles-and-day-plan)
- [Demo script and pitch](#demo-script-and-pitch)
- [Stretch goals](#stretch-goals)

---

## Quick start (laptop, no hardware)

The whole system runs in **simulation mode**: scripted readings drive the state
machine and a synthetic stick-figure feed stands in for the camera. No webcam,
no Nest required.

**One-time setup** (creates `.venv` and installs everything, including MediaPipe):

```bash
./setup.sh        # macOS / Linux / Pi
setup.bat         # Windows
```

**Run it:**

```bash
./run.sh --simulate --no-audio     # macOS / Linux / Pi
run.bat --simulate --no-audio      # Windows
```

Or activate the venv and use `python run.py` directly:

```bash
source .venv/bin/activate              # Windows: .venv\Scripts\activate
python run.py --simulate --no-audio
```

> `run.py` also detects when it was started with a Python that lacks the
dependencies and automatically relaunches itself using `.venv`, so a bare
`python run.py` works even if `.venv` is not activated. (`setup.bat` / `run.bat`
are just thin wrappers around the same thing.)

Open <http://localhost:5000>. Within a few seconds you will see the person
"arrive", calibrate, slouch, get warned, and get roasted — with the caption,
chart and stats all updating live.

To use the real vision code against a recorded clip:

```bash
python run.py --video teammates-slouching.mp4
```

### Manual install (if you prefer)

```bash
python -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\activate
pip install -r requirements.txt           # Flask, OpenCV, Gemini SDK, cast
pip install -r requirements-vision.txt    # adds MediaPipe (pose detection)
python run.py --simulate --no-audio
```

---

## Repository layout

```
.
├── run.py                    # entry point / CLI (auto-uses .venv)
├── run.bat / run.sh          # launchers that use .venv without activation
├── setup.bat / setup.sh      # one-time dependency install
├── config.py                 # paths + .env loader
├── vision/                   # ROLE 1 — frame -> reading
│   ├── angles.py             #   pure geometry (no cv2/mediapipe)
│   ├── landmarks.py          #   MediaPipe pose wrapper
│   ├── detector.py           #   angles, seated detection, score, overlay
│   ├── calibrate.py          #   5 s upright baseline
│   ├── source.py             #   webcam / video-file sources
│   └── synthetic.py          #   fake camera feed for --simulate
├── server/                   # ROLE 2 — Flask, threads, state machine, DB
│   ├── app.py                #   app factory + routes
│   ├── camera.py             #   the vision thread
│   ├── state_machine.py      #   away/calibrating/good/slouch/warn/roast
│   ├── events.py             #   SSE broker
│   ├── db.py                 #   SQLite logging + session stats
│   └── settings.py           #   persisted, live-editable settings
├── voice/                    # ROLE 3 — Gemini roasts, pool, audio
│   ├── prompts/              #   editable prompt text: roast.txt, nice.txt
│   ├── gemini_text.py        #   structured roast batches
│   ├── gemini_tts.py         #   text -> PCM -> WAV
│   ├── tts_local.py          #   espeak-ng / piper / say / SAPI fallback
│   ├── wavutil.py            #   WAV writing, lead silence, normalise
│   ├── pool.py               #   pre-generated clip pool
│   ├── player.py             #   Bluetooth + cast playback, one at a time
│   ├── service.py            #   orchestration + fallbacks
│   └── roasts.py             #   hand-written fallback roasts
├── web/                      # ROLE 4 — the website judges see
│   ├── index.html
│   └── static/{app.js,style.css}   # fullscreen feed + draggable glass HUD
├── fakes/                    # scripted readings to build against
├── tests/                    # unit tests + HTTP smoke test
├── deploy/posture-coach.service
├── requirements*.txt
└── .env.example
```

---

## How it works

### One process, a few threads

| Component      | Runs as                     | Job |
| -------------- | --------------------------- | --- |
| Camera + vision | background thread          | grab frames, landmarks, angles, overlay |
| State machine  | called by the vision thread | decides sit-down, good, slouch, away, roast timing |
| Roast pool     | background thread           | keeps ~9 ready clips per tier, refills from Gemini |
| Audio player   | background thread + queue   | plays clips on the Nest, one at a time |
| Flask          | main thread                 | website, video stream, stats, settings |
| SQLite         | file on the Pi              | sessions, readings, state changes, roasts |

Only one program can open the webcam, so one process is simpler than several.
The camera thread encodes each frame as JPEG **once** and stores it in a shared
object behind a lock; Flask routes only read from it, so extra browser tabs cost
almost nothing.

### Scores, smoothing and hysteresis

MediaPipe normalized coordinates are converted to pixels first (otherwise the
angles are distorted by the non-square frame). Both angles are measured from
vertical using `atan2`:

- **neck** = ear→shoulder line (forward head posture)
- **torso** = shoulder→hip line (hunching/leaning)
- **thigh** = hip→knee line from horizontal (sit-down detection)

Each angle becomes a score `1 - min(1, max(0, θ - θ₀) / Δθ)` against the
user's own calibrated baseline, and the **lower** of the two wins. The score is
smoothed with an EMA (`α = 0.2`) so one noisy frame cannot trigger a roast.
Thresholds use hysteresis: enter slouching below **0.7**, return to good above
**0.8**, and require ~2 s of good posture to reset (debounce).

### Escalation

Two triggers — either one can fire a roast:

- **sustained slouch** — slouching for `roast_seconds` (~30 s) → `roast_due`
- **bad posture** — the score dropping below `bad_score_threshold` (~0.45) and
  staying there for `bad_score_seconds` (~3 s) → `roast_due` straight away,
  without waiting out the timer

The event carries a `reason` (`sustained_slouch` or `bad_posture`). A
`cooldown_seconds` gap (~90 s) applies to both, so a long bad stretch cannot
spam. The website still turns yellow (`warn`) at ~10 s, and `away` (no person /
not seated) is never roasted. Set `bad_score_threshold` to `0` to disable the
score trigger entirely.

### Where a roast comes from

| `roast_source` | Behaviour |
| -------------- | --------- |
| `live` (default) | On `roast_due` the Pi asks Gemini for a fresh line at that moment, on a worker thread so the vision loop never blocks, then voices it. `live_api_cooldown_seconds` (~30 s) additionally rate-limits those calls. |
| `pool` | A background worker keeps pre-generated clips ready, so the roast is instant and the model is never in the real-time path at all. |

In `live` mode the pool stops refilling, so the two paths never both spend the
Gemini quota. Live is a little slower (network round trip) but never repeats a
line; the pool is instant but finite.

### Roast prompts

The wording sent to Gemini lives in two editable text files, not in the code:

| File | Mode |
| ---- | ---- |
| `voice/prompts/roast.txt` | normal — actually insulting, still PG |
| `voice/prompts/nice.txt` | nice mode — genuine encouragement |

Both are templates with `{{TOKEN}}` placeholders (`{{CONTEXT}}`, `{{EXCLUDE}}`,
`{{COUNT}}`, `{{TIER}}`, `{{TIER_BRIEF}}`) filled in by
`voice/gemini_text.py:build_prompt`. Plain string replacement is used rather
than `str.format`, so the JSON example braces in the templates stay literal.
If a file is missing or empty the built-in defaults are used, so a deleted file
can never break the app. Hand-written fallbacks live in `voice/roasts.py`.

The **delivery** for the spoken voice is a third file,
`voice/prompts/voice.txt`: a shared base plus `[mood mild|medium|spicy|nice]`
sections. The tier of an insult picks the mood, so a repeat offender does not
sound like a first slip. It is re-read on each clip, so it can be tuned while
the app runs.

### Never repeat

Every insult that is actually said is appended to **`data/insults.txt`**, one per
line — human-readable and hand-editable. That file is the never-repeat list:

- its most recent lines are passed to Gemini as banned in the prompt
- the hand-written fallbacks skip anything in it
- the pool will not queue a line that is already in it or already queued
- `validate()` drops any generated line that matches, whatever the model returns

The hand-written pool is finite, so once every fallback has been used it may
start repeating rather than saying nothing at all. The System panel shows how
many insults have been used so far.

### Audio routes

| Route | How | Strength | Weakness |
| ----- | --- | -------- | -------- |
| **Bluetooth** (primary) | Pi plays to the Nest as a Bluetooth speaker | matches the design, no network | Linux Bluetooth can drop when idle |
| **Wi-Fi cast** (backup) | `pychromecast` tells the Nest to play a clip served by Flask | reliable on a shared network | needs the Pi and Nest on one network |

The player reconnects Bluetooth before each clip and falls back to cast
automatically if Bluetooth playback fails. Switch routes any time on the
website. Every clip gets ~0.5 s of leading silence so the speaker does not clip
the first syllable while the link wakes up.

---

## Interface contracts

Agreed in hour 0, so all four people can build in parallel against fakes.

**Vision → state machine** — one reading per frame:

```json
{
  "ts": 1760101234.5,
  "person": true,
  "seated": true,
  "neck_angle": 18.4,
  "torso_angle": 7.1,
  "score": 0.86
}
```

Angles are degrees from vertical; `score` is 0–1 where 1 matches the calibrated
upright posture.

**State machine → voice** — the voice module only reacts to `roast_due`:

```json
{ "event": "roast_due", "slouch_count": 5, "session_minutes": 23, "worst_neck_angle": 41 }
```

Other events: `session_start`, `calibrated`, `warn`, `good`, `session_end`.

---

## The website (HUD)

The page is a **fullscreen feed with a translucent HUD** on top — closer to an
instrument overlay than a dashboard. Everything on it is a panel you can move or
turn off, and the arrangement is remembered in the browser's `localStorage`.

| Panel | Shows |
| ----- | ----- |
| **Posture** | live neck/torso angles, score with the 0.7/0.8 threshold ticks, cooldown |
| **Session** | good %, slouches, roasts, longest good streak |
| **Score** | score over the session with roast markers (resizable) |
| **Controls** | calibrate, test roast, nice mode, thresholds and audio route |
| **System** | camera/backend, fps, Nest, audio route, Gemini text + voice, local TTS, pool |
| **Hall of shame** | this session's roasts, newest first, with tier and angle |

The top bar carries the state plate (away → good → slouch → warn → roast), the
session clock and the live rate, then three icon buttons:

- **Panels** (`P`) — checkboxes to add/hide any panel, a **dim** slider for
  video brightness, and *Reset layout*
- **Mirror** (`M`) — flips the camera. This is done on the server *before* the
  pose overlay is drawn, so the angle labels stay readable
- **Fullscreen** (`F`) — browser fullscreen (`Esc` leaves the panel menu)

There is deliberately no idle "live" badge; the top bar only shows an `offline`
plate if the event stream actually drops.

Panels are dragged by their title bar and resized from the bottom-right corner
(Score). A roast pops up as a caption in the lower third for a few seconds.
Content is pushed over server-sent events, so nothing here needs a refresh.

### Styling

Instrument-like on purpose: hairline borders, 2 px corners, no pill shapes, and
frosted translucency — every surface (panels, top bar, menus, caption) is
see-through and blurs the feed behind it. The chrome (labels, numbers, controls)
is set in **Overpass Mono** and prose (roast captions, hall of shame) in
**Overpass** — both self-hosted as woff2 in `web/static/fonts/`, so the page
renders correctly with no internet. Icons are
[Heroicons](https://heroicons.com) (MIT).

The score chart is coloured by value through a single vertical ramp — green at
the top, amber at the warn/roast thresholds, red at the bottom — with a matching
gradient fill under the trace, so the colour of the line *is* the reading.

## HTTP API

| Method | Route | Purpose |
| ------ | ----- | ------- |
| GET | `/` | the website |
| GET | `/video_feed` | live annotated MJPEG stream |
| GET | `/events` | server-sent events: readings, state changes, roasts |
| GET | `/api/stats` | current session stats + live snapshot |
| GET | `/api/history` | readings for the chart (+ roast markers) |
| GET | `/api/roasts` | recent roasts (hall of shame) |
| GET | `/api/status` | camera fps, Nest connection, Gemini reachability |
| POST | `/api/calibrate` | start the 5-second upright calibration |
| GET | `/api/settings` | thresholds, nice mode, audio route |
| POST | `/api/settings` | update them (live) |
| POST | `/api/test-roast` | play a roast right now (demo button) |
| GET | `/media/roasts/<name>` | serves generated WAVs to the Nest for casting |
| GET | `/healthz` | liveness probe |

---

## Configuration

Copy `.env.example` to `.env` and fill it in. `.env` is git-ignored.

| Variable | Meaning |
| -------- | ------- |
| `GEMINI_API_KEY` | from Google AI Studio; without it the app uses fallbacks |
| `GEMINI_TEXT_MODEL` | text model id (check Google's models page on the day) |
| `GEMINI_TTS_MODEL` / `GEMINI_TTS_VOICE` | TTS model and voice persona |
| `NEST_BLUETOOTH_MAC` | from `bluetoothctl devices` |
| `NEST_CAST_NAME` | friendly name for the Wi-Fi cast backup |
| `POSTURE_BLUETOOTH_PLAYER` | override player command (`paplay`, `pw-play`, `aplay`) |
| `POSTURE_CAMERA_BACKEND` | `dshow` (Windows default), `msmf`, `auto` |
| `POSTURE_DATA_DIR` | where the DB and clips live |
| `POSTURE_DISABLE_LOCAL_TTS` | set to `1` to disable the local TTS fallback |

Runtime thresholds (warn/roast/cooldown, tolerance, nice mode, audio route) are
edited on the website and persisted to `data/settings.json`.

> Google's API moves fast: the Python SDK is **`google-genai`** (the older
> `google-generativeai` package is deprecated). Model IDs are `preview` and
> change often — pick them from Google's models page on the day.

---

## Running modes

```bash
python run.py                     # real webcam, serves the website
python run.py --preview           # standalone window (no browser) - good for aiming
python run.py --list-cameras      # show which camera indices work
python run.py --simulate          # scripted fake readings + synthetic feed
python run.py --video clip.mp4    # develop vision against a recording
python run.py --no-audio          # print roasts instead of playing them
python run.py --camera 1          # pick a specific camera (remembered)
python run.py --model full        # MediaPipe pose model (lite/full/heavy)
python run.py --port 5000 --host 0.0.0.0
python run.py --debug
```

The program runs **here** (camera, pose detection, state machine, audio) and the
website is just its UI — the browser only displays the stream and sends button
presses. `--preview` skips the server entirely and opens a plain desktop window
with the annotated feed, which is the easiest way to position the camera.

## Choosing the camera (side view)

This is a **side-view** system: the camera sits at shoulder height, 1–2 m to the
person's side, roughly level, with the whole head and hip in frame. If the
laptop's built-in front camera is used instead, the neck/torso angles are
meaningless — pick the side-mounted camera instead.

```bash
python run.py --list-cameras                    # index + resolution + speed
python run.py --list-cameras --list-snapshots   # also save a still per camera
python run.py --camera 1                        # use index 1
python run.py --camera 1 --preview              # aim it (q to quit)
```

`--camera N` is remembered in `data/settings.json`, so afterwards plain
`python run.py` uses that camera. Open `data/camera-scan/camera_<n>.png` (from
`--list-snapshots`) to see which physical camera each index is.

On Windows the default backend is **DirectShow** because Media Foundation can
take ~20 s to open and copes poorly with several cameras. Override with
`--camera-backend msmf` or `POSTURE_CAMERA_BACKEND=dshow|msmf|auto`.

---

## Fakes for parallel development

- **Recorded video as the camera** — `python run.py --video clip.mp4`, or
  `VideoFileSource` directly. Vision can be built without the Pi.
- **Fake readings** — `python -m fakes.fake_readings` prints a scripted sequence
  of good and slouching readings at 1 Hz. The server and website can be built
  before vision works. `--simulate` uses the same generator.
- **Fake player** — `--no-audio` (or `audio_route = off`) prints the roast
  instead of playing it, so the server can be tested without Bluetooth.

---

## Raspberry Pi setup

### Hardware

- Raspberry Pi 5 with the **official 27 W USB-C supply** (weak supplies throttle and drop USB)
- Active cooler (pose detection runs the CPU hard all day)
- 32 GB+ microSD with Raspberry Pi OS 64-bit, plus a spare card with the same image
- USB webcam mounted at the side, roughly shoulder height, 1–2 m away
- Google Nest Mini / Nest Audio, set up in a Google Home account
- Mobile hotspot or travel router so Pi, computer and Nest share one network
- Ethernet or keyboard+HDMI for recovery if Wi-Fi setup fails

### Software

```bash
sudo apt update
sudo apt install -y python3-venv python3-dev bluez pulseaudio-utils espeak-ng

git clone <your-repo> ~/posture-coach
cd ~/posture-coach
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-pi.txt        # includes MediaPipe
cp .env.example .env && nano .env          # add GEMINI_API_KEY, NEST_BLUETOOTH_MAC

python -c "import mediapipe; print('mediapipe ok')"   # find pip problems NOW, not at hour 1
```

If MediaPipe wheel trouble hits, try piwheels:

```bash
pip install --extra-index-url https://www.piwheels.org/simple mediapipe
```

### Bluetooth to the Nest (do this in hour 1 — it is the riskiest step)

```bash
bluetoothctl
[bluetooth]# scan on
[bluetooth]# pair AA:BB:CC:DD:EE:FF
[bluetooth]# trust AA:BB:CC:DD:EE:FF     # lets the Pi reconnect automatically
[bluetooth]# connect AA:BB:CC:DD:EE:FF
[bluetooth]# quit
```

Put `AA:BB:CC:DD:EE:FF` in `.env` as `NEST_BLUETOOTH_MAC`. Test with a WAV:

```bash
aplay test.wav      # or: paplay test.wav   (PipeWire default sink)
```

Then confirm it still plays after 10 minutes of silence — the whole reason the
player reconnects before every clip.

### Networking

Set a known hostname so the website is always at `http://posture.local:5000`:

```bash
sudo raspi-config        # System Options -> Hostname -> posture
```

Enable SSH for every teammate, then run:

```bash
python run.py --host 0.0.0.0 --port 5000
```

---

## Run on boot (systemd)

```bash
sudo cp deploy/posture-coach.service /etc/systemd/system/
sudo nano /etc/systemd/system/posture-coach.service   # fix User / paths if needed
sudo systemctl daemon-reload
sudo systemctl enable --now posture-coach
systemctl status posture-coach
journalctl -u posture-coach -f
```

Reboot the Pi: the website should come back by itself within about a minute.

---

## Testing

```bash
python -m unittest discover -s tests -t .   # 50+ unit tests, no hardware needed
python -m tests.smoke                        # HTTP smoke test in simulation mode
```

The suite covers the angle math, the full escalation/cooldown/debounce state
machine, the detector with a stub landmarker, the roast prompt/validation,
the WAV writer, the pool, the SSE broker, SQLite logging and the scripted
readings end-to-end. The smoke test boots the real Flask app in simulation mode
and exercises every route with the test client.

### Testing checklist before judging

- Each teammate sits, calibrates and slouches — works across body types and chair heights
- Test in the venue's actual lighting with the camera in its final position
- Stand up mid-session: goes `away`, nothing is roasted
- Disconnect the Pi's internet: roasts still play from the pool
- Turn the Nest's Bluetooth off mid-session: the website shows it; cast works
- Reboot the Pi: the service comes back by itself
- Run a 10-minute session end to end without touching code
- Record a backup demo video

---

## Tuning and troubleshooting

| Symptom | Likely cause | Fix |
| ------- | ------------ | --- |
| Nest silent, roast shows on the site | Bluetooth dropped / not paired | player reconnects automatically; else switch route to Wi-Fi cast |
| Under ~8 fps, laggy overlay | pose model too heavy | `POSTURE_MODEL=lite`, smaller frames, detect every second frame (5 fps is plenty — posture changes slowly) |
| Computer can't reach the Pi, cast can't find the Nest | venue Wi-Fi blocks device-to-device traffic | use your own hotspot / travel router from the start |
| Pool stops refilling | no internet / rate limited | pre-generated clip pool + hand-written roasts + local TTS |
| Gemini returns empty / off-tone | prompt rejected or too spicy | keep the prompt PG; validation drops bad lines and falls back |
| Flickering seated state, wild angles | desk hides hips/knees, baggy clothes | raise/angle the camera down; visibility-based fallback handles sit-down |
| Throttling / random reboots | power or heat | official PSU + active cooler |
| `address already in use` | second copy running (auto-reloader) | the app disables the reloader on purpose; kill the stale process |

Tune `warn_seconds`, `roast_seconds`, `cooldown_seconds`, `delta_angle`,
`lower_threshold` and `upper_threshold` live from the website and watch the
effect on the chart.

---

## Team roles and day plan

| Role | Owns | Builds |
| ---- | ---- | ------ |
| 1. Vision | `vision/` | landmarks, side selection, angles, sit-down detection, calibration, score, hysteresis |
| 2. Server | `server/` | Flask app, camera thread, MJPEG, state machine, SQLite, SSE, systemd, fake readings |
| 3. Voice | `voice/` | Bluetooth to the Nest, Gemini prompt + schema, Gemini TTS, roast pool, player queue, all fallbacks |
| 4. Website + demo lead | `web/` | website, controls, settings, live chart, captions, pitch, demo script, backup video |

Rules that keep four people from colliding:

- one folder per person; commit small and often
- only one person deploys to the Pi at a time — announce it before restarting the service
- contracts only change with everyone's agreement, announced out loud
- every two hours, a 5-minute standup: what works, what's blocked, what's next

If a gate slips, take its fallback instead of pushing the schedule — every later
phase depends on the one before it. Pair up when blocked: vision + server on Pi
frame rate, voice + website on captions and the test-roast button.

---

## Demo script and pitch (~3 minutes)

1. **Problem (20 s)** — students spend hours hunched over laptops; polite
   reminders are easy to ignore. Insults are not.
2. **Live demo (90 s)** — a judge sits down; the system notices, calibrates, and
   they slouch on purpose. The Nest roasts them; the roast appears on the website
   beside the live angle overlay.
3. **Architecture (45 s)** — one slide: side-view geometry and per-user
   calibration, the debounced state machine, and the LLM kept out of the
   real-time path by a pre-generated, context-aware roast pool.
4. **What's next (25 s)** — posture history and trends, multiple user profiles,
   roast personalities.

Be ready for: *why a side view* (it measures real neck and torso angles),
*how you avoid false alarms* (calibration, smoothing, hysteresis, the away
state), and *what happens if Gemini is down* (pre-generated pool plus offline
fallbacks).

Keep the roasts playful: the prompt roasts posture only — never appearance,
body, identity or intelligence — stays PG and under ~15 words, and escalates in
tiers. A **nice mode** toggle on the website switches to encouragement.

---

## Stretch goals

Only after everything else works:

- **Hall of shame** — already logged: recent roasts with the angle that earned them
  (see the panel and `GET /api/roasts`)
- Gemini-written end-of-session report card
- Leaderboard comparing teammates' good-posture percentages (`Database.all_sessions`)
