# Posture Coach

**Slouch, and the room roasts you.** A webcam watches you from the side, a
Raspberry Pi 5 measures your posture, and when you slouch for too long a Google
Nest speaker insults you out loud with a fresh line written by Gemini.

Built at **HackRU 2026**.

```
side webcam ─▶ MediaPipe pose ─▶ neck + torso angles ─▶ score ─▶ state machine
                                                                     │
                                                    roast_due ───────┘
                                                        │
                                  Gemini writes it ─▶ ElevenLabs voices it
                                                        │
                                         Google Nest over Bluetooth
```

A Flask website on the Pi shows the live annotated video, the posture score,
session stats and every setting, so anyone in the room can watch and tune it.

---

## How it works

1. **See.** OpenCV grabs frames from a USB webcam placed to the user's side.
   MediaPipe Pose Landmarker finds the body's landmarks, and we keep the ear,
   shoulder, hip and knee on the side facing the camera.
2. **Measure.** Two angles describe posture: the **neck** angle (ear over
   shoulder: how far the head juts forward) and the **torso** angle (shoulder
   over hip: how much the back hunches). A near-horizontal thigh means the person
   is seated, which starts a session.
3. **Calibrate.** For the first few seconds the user sits up straight; that
   becomes their personal baseline, so everyone is judged against themselves.
4. **Decide.** Each frame is scored 0–1 against the baseline, smoothed, and fed to
   a state machine (`away → calibrating → good ⇄ slouching → roasted`, plus
   `sleeping`). Hysteresis and timers mean one bad frame never triggers anything.
   A roast fires on a sustained slouch, a deep slouch, or "asleep at the desk"
   (slumped, motionless, head down), and a cooldown stops it nagging.
5. **Write.** Gemini writes the roast live, given the session context: how many
   slouches, which body part is failing, the worst angles. Insults escalate from
   mild to spicy for repeat offenders, never repeat (every line said is logged and
   banned), and only ever attack the slouch, never the person.
6. **Censor.** Swearing is generated as a `BLEEP` marker, never the real word.
   The line is voiced around it and a real 1 kHz TV bleep is spliced in.
7. **Speak.** ElevenLabs turns the line into speech (a local voice is the offline
   fallback), and the clip streams over Bluetooth to a Google Nest through
   PipeWire.

---

## Hardware

- Raspberry Pi 5 (Raspberry Pi OS 64-bit)
- USB webcam, mounted to the side of the chair at shoulder height, 1.5–2 m away,
  seeing the person from head to knee
- Google Nest speaker (paired to the Pi over Bluetooth)
- Everything on one network that allows device-to-device traffic (we used a
  laptop hotspot; venue Wi-Fi usually blocks it)

---

## Quick start (laptop, no hardware)

```bash
sh setup.sh                                  # creates .venv, installs everything
cp .env.example .env                         # then add your API keys (optional)
sh run.sh --simulate --no-audio --port 5001  # open http://localhost:5001
```

`--simulate` needs no camera: scripted readings drive the state machine and a
stick figure stands in for the video. On macOS, port 5000 is taken by AirPlay,
hence `--port 5001`. On Windows use `setup.bat` and `run.bat`.

Without API keys it still works: roasts come from a hand-written pool and are
spoken by the computer's built-in voice.

---

## Running on the Raspberry Pi

```bash
sudo apt install -y python3-venv python3-dev bluez pulseaudio-utils \
     libspa-0.2-bluetooth espeak-ng
sh setup.sh
nano .env                                    # API keys + NEST_BLUETOOTH_MAC
.venv/bin/python run.py --list-cameras       # find the webcam's index
.venv/bin/python run.py --camera 0 --demo    # then open http://<pi-ip>:5000
```

Run as the desktop user (not `sudo`): that user owns the PipeWire audio and the
Bluetooth pairing. `voice/test_nest_bluetooth.sh` checks the whole Pi-to-Nest
audio link step by step. To start on boot, install `deploy/posture-coach.service`
with systemd (see [docs/REFERENCE.md](docs/REFERENCE.md#raspberry-pi-setup)).

### Useful flags

| Flag | What it does |
| ---- | ------------ |
| `--demo` | Short timers for live demos: roast after 5 s, 10 s cooldown, 3 s calibration |
| `--camera N` | Use webcam index `N` (find it with `--list-cameras`) |
| `--force-seated` | Count anyone the camera sees as seated (when the desk or angle hides the thigh) |
| `--simulate` | No camera: scripted readings and a synthetic stick figure |
| `--test-voice "text"` | Speak one line through the configured voice and speaker, then exit |
| `--no-audio` | Print roasts instead of playing them |
| `--port N` | Serve the website on port `N` (default 5000) |
| `--preview` | Standalone OpenCV window instead of the website |

---

## Configuration

Secrets and device settings live in `.env` (copy `.env.example`; never commit
`.env`):

| Variable | Purpose |
| -------- | ------- |
| `GEMINI_API_KEY` | Gemini writes the roasts. Without it, the hand-written pool is used. |
| `ELEVENLABS_API_KEY` | ElevenLabs speaks them. Without it, the local voice is used. |
| `NEST_BLUETOOTH_MAC` | The Nest's Bluetooth address (`bluetoothctl devices`). |

Timers, thresholds, nice mode, voice and audio route can all be changed live from
the website's **Controls** panel. The roast personality lives in plain text:
[voice/prompts/roast.txt](voice/prompts/roast.txt) (and `nice.txt` for nice mode).

---

## The website

Open `http://<pi-ip>:5000` from any device on the same network:

- **Live feed** with the ear–shoulder–hip skeleton, coloured green / amber / red
- **Posture panel** with the score and the countdown to the next roast
- **Controls**: calibrate, test roast, nice mode, and every setting
- **Debug**: the internals live (detection, angles, timers, camera fps) — the
  answer to "why hasn't it roasted me yet?"

---

## Tests

No camera, network or keys needed:

```bash
.venv/bin/python -m unittest discover -s tests -t .   # unit tests
.venv/bin/python -m tests.smoke                        # boots the app, hits every route
```

---

## Repository layout

```
run.py             entry point (flags above)
config.py          paths and environment
vision/            camera, MediaPipe landmarks, angles, sit-down detection, overlay
server/            Flask app, state machine, settings, SQLite stats, live events
voice/             Gemini writer, roast pools, BLEEP splicing, ElevenLabs + local TTS,
                   Bluetooth / cast playback, prompts/
web/               the dashboard (HTML, CSS, JS)
deploy/            systemd service for start-on-boot
fakes/             scripted readings for --simulate
tests/             unit tests and the smoke test
docs/REFERENCE.md  the full technical reference: every setting, the HTTP API,
                   troubleshooting
```

---

## Full reference

Every number, setting, HTTP endpoint and troubleshooting tip is in
**[docs/REFERENCE.md](docs/REFERENCE.md)**.
