---
name: posture-coach-hud
description: Work on the Posture Coach website — the fullscreen feed HUD, its draggable/hideable glass panels and saved layout, the score chart, controls, styling and fonts, plus how to screenshot-verify the page. Use whenever index.html, style.css or app.js change, when panels misbehave or overlap, or when the page looks wrong after an update.
compatibility: Needs Node.js for the screenshot script and a Chromium-based browser. The page itself is plain HTML/CSS/JS with no build step.
---

# Posture Coach — the website

Read `posture-coach` first for layout and contracts.

## Shape of it

One page: `web/index.html` + `web/static/{app.js,style.css,fonts.css}`. The camera feed
fills the viewport (`<img src="/video_feed">`, an MJPEG stream) and the UI floats over
it as translucent panels. Data arrives over server-sent events from `/events`;
`/api/stats`, `/api/history`, `/api/settings` and `/api/status` are polled or fetched
on load.

Panels are declared **once** in the `PANELS` registry in `app.js` (title, width,
`column`, `anchor`, `order`, `resizable`, `visible`) and must also exist in
`index.html` as `<section class="panel" data-panel="id">`. `arrange()` parks them in
the corners so the middle of the frame — where the person is — stays clear.

## Layout, dragging and persistence

- Drag by the panel header, collapse with the header buttons, hide/show from the
  **Panels** menu; the Score panel is resizable from its corner.
- The layout lives in `localStorage` under `posturecoach.hud.vN`. **Bump `N` whenever
  default positions change**, or existing users keep the stale defaults.
- `layoutFits()` trusts a stored layout only if every panel is on screen; otherwise it
  re-arranges. Without that guard a stale layout from another viewport renders as a
  pile of panels in one corner.
- `arrange()` runs a second time on the next frame, because panel heights settle once
  the canvas and fonts have laid out.
- `setPointerCapture` is wrapped in try/catch so a synthetic or already-released
  pointer cannot break dragging.

## Styling rules (keep these consistent)

- Self-hosted **Overpass** (prose) and **Overpass Mono** (all chrome: labels, numbers,
  controls). No CDN — the page must render with the venue Wi-Fi off. Files are in
  `web/static/fonts/`.
- Flat and instrument-like: hairline borders, 2 px corners, frosted
  `backdrop-filter` surfaces, no pill shapes, no drop shadows. Icons are inline
  [Heroicons](https://heroicons.com) — inline them, do not add an icon font.
- **Header text centring:** dividers must be drawn as an inset hairline
  (`box-shadow: inset 0 -1px 0 …`), never a real `border-bottom`. A real border sits
  inside the fixed height under `box-sizing: border-box`, so flex centring lands
  **0.5 px high** — measure it if unsure.
- The score chart's colour *is* the reading: one vertical ramp, green at the top,
  amber at the thresholds, red at the bottom, with a matching gradient fill.

## Verify before claiming it works

Two traps: a running server has **cached the Jinja template**, so HTML changes need a
restart (and the browser needs a hard refresh for `app.js`/`style.css`); and
`chrome --screenshot` **hangs on this page** because the SSE stream never lets the page
go idle.

So screenshot by driving Chrome over CDP:

```bash
python run.py --simulate --no-audio &          # or the real camera
node .pi/skills/posture-coach-hud/scripts/screenshot.mjs http://127.0.0.1:5000/ /tmp/hud.png
node .pi/skills/posture-coach-hud/scripts/screenshot.mjs http://127.0.0.1:5000/ /tmp/hud.png 25000
```

Use a long delay (20–30 s) if you want the chart populated and a roast caption on
screen. The script launches its own headless browser, captures, and cleans up.

Also worth running after any UI edit — it catches an element the JS expects but the
HTML does not have (or vice versa):

```bash
node --check web/static/app.js
.venv/Scripts/python.exe -c "
import re
h=open('web/index.html',encoding='utf-8').read(); j=open('web/static/app.js',encoding='utf-8').read(); c=open('web/static/style.css',encoding='utf-8').read()
print('missing ids :', sorted(set(re.findall(r'\\\$\(\"([^\"]+)\"\)',j))-set(re.findall(r'id=\"([^\"]+)\"',h))) or 'none')
print('missing css :', sorted({x for a in re.findall(r'class=\"([^\"]+)\"',h) for x in a.split()}-set(re.findall(r'\.([A-Za-z][\w-]*)',c))) or 'none')
print('panels html :', sorted(re.findall(r'data-panel=\"([^\"]+)\"', h)))
"
```

(`.venv/Scripts/python.exe` is the Windows path; on Linux/macOS use `.venv/bin/python`.)

## Diagnostics on the page

Two panels carry the debugging surface, and both must stay wired:

- **System** — Detection (`person · seated`, the usual reason roasts never fire),
  camera/backend, fps, Nest connection, per-provider voice status, **Voice mix**
  (amber when the pool is mixed), **Roast source** (live/pool, pending live roasts,
  API cooldown) and insults used.
- **Debug** — the raw internals, live: state, detection, score (smoothed + raw),
  neck/torso with drift, failing part, slouch timer, the roast countdown with its
  trigger or blocker, cooldown, calibration progress, the thresholds and timers in
  use, camera stats, session id and the last state-machine event. Its rows use
  `.kv.compact` so the panel stays short enough not to collide with the chart.

The Debug panel is fed by SSE readings (~2/s) for the fast values and by
`/api/stats` + `/api/status` every 5 s for the rest, so adding a field means
adding the id in `index.html` and a `setField("dbg-…")` call in `updateDebugLive`
or `updateDebugStats`. If a countdown never starts, the machine is not seeing a
slouch; if it is stuck behind `cooldown`, the roast is gated, not broken. If a
roast is silent, start elsewhere — the timer is working.
