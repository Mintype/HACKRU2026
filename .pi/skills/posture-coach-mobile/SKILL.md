# Posture Coach — mobile mode (phone as the camera)

Covers `python run.py --mobile`, `vision/multipose.py`, `server/mobile.py`,
`web/mobile.html` and `web/static/mobile.{js,css}`: the phone captures, the server
thinks, and the room gets a live roster of who is sitting badly.

Read `posture-coach/SKILL.md` first for the repo-wide invariants (commit and push
every change, never commit `.env`/`data/`, tests stay offline, README stays
coverage-complete).

## The shape of it

```
phone camera ──JPEG ~7/s──▶ POST /m/frame ──▶ MediaPipe (num_poses=N)
                                                  │
                    MultiTracker ─────────────────┤──▶ view angle (side/front/back)
                     (one Track per person,        │    + the metric that view allows
                      one PostureStateMachine each)│
                                                  ▼
                     JSON roster ◀── scores, states, normalized joints, roasts
                                                  │
                            roast_due ──▶ Gemini/ElevenLabs ──▶ SSE ──▶ phone speaks
```

- `vision/multipose.py` is **pure logic** — no cv2, MediaPipe or Flask — so the
  interesting parts are testable offline. `measure_pose()` turns one pose dict into
  `Metrics`; `score_metrics()` scores against a per-person baseline; `MultiTracker`
  assigns identities and drives one `PostureStateMachine` per person.
- `server/mobile.py` is Flask-ignorant too: JPEG **bytes** in, dict out.
  `server/app.py` only wires routes.
- The phone draws its own skeleton from normalized `points`. **Never** send annotated
  video back down: the upload is one-way on purpose.

## The three traps

1. **`getUserMedia` needs a secure origin.** `http://192.168.x.x` is not one, so
   `--mobile` serves HTTPS with an adhoc self-signed certificate (`cryptography`,
   already in `requirements.txt`) and prints the URL. A page with an *unaccepted*
   certificate error is also not a secure context — that is why the phone page
   detects it and explains both fixes (accept the cert, or Chrome's
   `chrome://flags/#unsafely-treat-insecure-origin-as-secure`). On localhost,
   plain HTTP is fine, so that is the easy way to test on a laptop.
2. **Flask caches templates.** Editing `web/mobile.html` (or `index.html`) without
   restarting the server serves the old markup to fresh browsers while the *static*
   JS updates — which fails in bizarre ways (`$("notice-title")` is suddenly null).
   Restart after every template edit.
3. **A baseline learned while somebody is already slumped calls that slump normal.**
   Per-person calibration is what makes any-angle scoring work, and it is also its
   weakness. The UI shows `calibrating` while it learns; ask people to sit up for
   the first second of a demo.

## Things that are deliberately different from the Pi app

- **Sleep detection is off** per track (`sleep_threshold_seconds = 0`): people walk
  around a room, and 25 s of stillness means something else there.
- **`seated` is forced True** for mobile readings. The phone operator decides who is
  worth watching; mobile measures *posture*, not sitting.
- **Calibration is 2 s**, not 5 (`MOBILE_CALIBRATION_SECONDS`).
- **Identity is positional and per frame** (`MATCH_DISTANCE`, `TRACK_TTL`), matched on
  the normalized shoulder midpoint. Ids are recycled from a free list so a long demo
  does not creep to "Person 41". One pose cannot claim a track already claimed *this
  frame* — without that guard, two people at the same anchor render as one row twice.
- **The phone drives the desktop HUD**: `MobileVision._publish` pushes the frame into
  `VisionService.publish_external_frame` and the *worst* person's reading, so
  `/video_feed` and the Debug panel show the phone's camera. The **frame is shared
  even when nobody is detected** - aiming the phone is precisely when you want to see
  what it sees - but the reading then says `person: false`/`score: null` instead of
  freezing on the last person who was there. Getting this wrong made the desktop feed
  stay black until somebody walked into shot.

## Verifying without a phone

```bash
python run.py --mobile            # note the https URL it prints
# laptop browser, localhost is a secure context:
#   https://127.0.0.1:5000/m
# no camera? drive the pipeline straight from the server:
python .pi/skills/posture-coach-hud/scripts/screenshot.mjs \
    https://127.0.0.1:5000/m .shots/m.png 3000 390 844 --mobile
# fake camera, so getUserMedia really runs (needs plain http on localhost):
python run.py --mobile            # with cryptography unavailable -> http
python .pi/skills/posture-coach-hud/scripts/screenshot.mjs \
    http://127.0.0.1:5000/m .shots/m.png 16000 390 844 --mobile --fake-camera \
    --eval "__pc.startCamera()"
```

`window.__pc` is exposed for exactly this: `__pc.handleFrame({...})` renders a roster
with no camera and no round trip, which is how the roster CSS gets screenshot-checked
against a **real** payload. Generate that payload from the real pipeline rather than
hand-writing it — `.shots/make_state.py` (scratch, gitignored) does it by posting
synthetic poses to a test client with a stub landmarker.

Server-side counters to check: `/api/status` → `mobile.frames`, `decode_ms`,
`detect_ms`, `devices`, `last_frame_ago`. If `frames` stays 0, the phone never got
its frames out (secure-context problem, most likely).

To check that the **desktop** page is showing the phone's picture, keep a phone
streaming and read the MJPEG stream while it runs:

```bash
node .pi/skills/posture-coach-mobile/scripts/keep_phone.mjs http://127.0.0.1:5000/m 25 390 844 &
# then pull one JPEG out of /video_feed and check it is not the dark placeholder
```

With the fake camera the frame is a bright green field, so "mean green high" is a
simple pass/fail on the plumbing (`mean BGR=(3,147,6)` when it works, near-black when
the frame never arrived).

## Offline test policy

`tests/__init__.py` blanks `GEMINI_API_KEY`/`ELEVENLABS_API_KEY` at import. That is
not decoration: a test that called `config.load_env()` once leaked the real keys into
the whole test process and the voice tests quietly spent 38 s on live API retries.
If you add a test that needs a key, pass one explicitly.
