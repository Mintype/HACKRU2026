---
name: posture-coach
description: Orientation for the Posture Coach repo — a Raspberry Pi side-view webcam that watches your posture and roasts you out loud through a Google Nest. Use this when running the app, running tests, or making any cross-cutting change to vision, server, voice, web, settings or the run CLI.
compatibility: Requires Python 3.11+ and the project .venv. Camera or MediaPipe optional (simulation mode).
---

# Posture Coach — orientation

A side-mounted webcam measures neck/torso angles with MediaPipe; a debounced state
machine decides when a slouch deserves an insult; a pre-generated (or live-generated)
Gemini/ElevenLabs roast plays on a Nest while Flask serves a fullscreen browser HUD.

**Start here, then load the area skill:** `posture-coach-vision`, `posture-coach-voice`,
`posture-coach-hud`.

## Run and test

```bash
python run.py                  # real webcam; auto-relaunches into .venv
python run.py --simulate       # scripted readings + synthetic stick-figure feed, no camera
python run.py --demo           # short thresholds (roast 10s) so the cycle is visible fast
python run.py --preview        # standalone OpenCV window, no browser (good for aiming the camera)
python run.py --list-cameras   # probe camera indices (Windows: DirectShow)
python run.py --test-voice "Sit up."   # voice one line now; reports which engine spoke
python run.py --help

python -m unittest discover -s tests -t .   # the whole suite, no hardware, no network
python -m tests.smoke                        # boots the real Flask app via the test client
```

`run.py` re-execs itself inside `.venv` when the deps are missing, so a bare
`python run.py` works even if PATH points at another interpreter.

## Layout

| Path | Owns |
| ---- | ---- |
| `vision/` | frames → readings: `angles.py` (pure math), `landmarks.py` (MediaPipe), `detector.py`, `calibrate.py`, `source.py`, `synthetic.py` |
| `server/` | `app.py` (Flask + composition), `camera.py` (vision thread), `state_machine.py`, `events.py` (SSE), `db.py` (SQLite), `settings.py` |
| `voice/` | `gemini_text.py` (writes roasts), `eleven_tts.py` (speaks them), `tts_local.py`, `pool.py`, `player.py`, `service.py`, `insults.py`, `prompts/`, `roasts.py` |
| `web/` | `index.html`, `static/{app.js,style.css,fonts.css,fonts/}` |
| `fakes/` | `fake_readings.py` — scripted readings used by `--simulate` and the tests |
| `tests/` | unit tests + `smoke.py` |
| `data/` | runtime, git-ignored: `posture.db`, `insults.txt`, `roasts/*.wav`, `settings.json` |

## Contracts (do not change casually)

**Reading** (vision → everything downstream; downstream never sees frames):
```json
{"ts": 1760101234.5, "person": true, "seated": true,
 "neck_angle": 18.4, "torso_angle": 7.1, "score": 0.86}
```
Extra keys the rest of the system relies on: `slouch_part` (`neck`/`torso`/`even`),
`neck_drift`, `torso_drift`, `state`.

**`roast_due`** (state machine → voice):
```json
{"event": "roast_due", "reason": "bad_posture", "slouch_count": 5,
 "session_minutes": 23, "worst_neck_angle": 41, "slouch_part": "neck"}
```

Other events: `session_start`, `calibrated`, `good`, `session_end`, `state`.

## Gotchas that have already cost time

- **`0.0` is a valid timestamp.** Never write `reading.get("ts") or time.time()` — a
  `ts` of `0.0` is falsy and gets replaced by wall-clock, which silently breaks tests.
  Use an explicit `is None` check. This bug appeared twice.
- **Clips are pre-generated.** `RoastPool` voices text in the background at startup,
  before any session exists. Nothing session-dependent may be baked into a pooled
  clip — that is how a literal `{n}` once ended up inside spoken audio. The audio is
  cached by text hash + engine, so it can never be repaired later, only regenerated.
- **Never block the vision thread.** Anything that touches the network goes on a
  worker thread (`RoastService`'s live worker, the pool thread). Blocking it freezes
  the feed and the state machine.
- **Tests must not hit the network.** A real `.env` key is present on dev machines, so
  tests neutralise it (`offline_writer()`, `offline_tts()`, `offline_eleven()` in
  `tests/test_voice.py`). Keep that up when adding tests around the cloud APIs.
- **`data/` and `.env` are runtime.** `.env` holds a live API key and must never be
  committed; `.gitignore` covers `.env`, `.env.*` (with `!.env.example`) and `data/`.
- **Keep HTML/JS/CSS in sync.** Panels are declared in the `PANELS` registry in
  `web/static/app.js` *and* exist in `index.html`. There is a fast cross-check in the
  hud skill — run it after touching the UI.
- Bump the `localStorage` layout key in `web/static/app.js` (`posturecoach.hud.vN`)
  whenever default panel positions change, or existing users keep the old defaults.

## Working agreements

- Commit on the branch `therealsolo2/deepseek`; push when a change is verified.
- Prefer small commits with a message that says *why*.
- Every change should leave `python -m unittest discover -s tests -t .` green.
