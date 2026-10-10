/* Posture Coach — mobile.
 *
 * The phone is the camera; the server is the brain. We capture frames here,
 * post them to /m/frame, and get back the people the server found: their ids,
 * scores, which way they are facing and where their joints are. We draw the
 * skeleton ourselves (cheap, and no video ever comes back down the wire) and we
 * speak the roast the server sends us over SSE.
 *
 * No framework, no build step, no CDN - same rules as the desktop page.
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const CAPTURE_WIDTH = 640; // what we upload; the server scores normalized coords
  const PREFS_KEY = "posturecoach.mobile.v1";

  const state = {
    streaming: false,
    sending: false,
    roundTrip: 0,
    failures: 0,
    lastFrameAt: 0,
    people: [],
    prefs: { speak: true, mirror: false, skeleton: true, fps: 7, facing: "environment" },
    device:
      (window.sessionStorage && sessionStorage.getItem("pc.device")) ||
      `phone-${Math.random().toString(36).slice(2, 7)}`,
  };
  try {
    sessionStorage.setItem("pc.device", state.device);
  } catch (e) {
    /* private mode: the id just will not persist */
  }

  /* ------------------------------------------------------------- persistence */
  function loadPrefs() {
    try {
      const saved = JSON.parse(localStorage.getItem(PREFS_KEY) || "null");
      if (saved && typeof saved === "object") Object.assign(state.prefs, saved);
    } catch (e) {
      /* ignore a corrupt blob */
    }
    $("opt-speak").checked = !!state.prefs.speak;
    $("opt-mirror").checked = !!state.prefs.mirror;
    $("opt-skeleton").checked = !!state.prefs.skeleton;
    $("opt-fps").value = String(state.prefs.fps);
    $("opt-facing").value = state.prefs.facing;
    applyMirror();
  }

  function savePrefs() {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify(state.prefs));
    } catch (e) {
      /* ignore */
    }
  }

  function applyMirror() {
    const flip = state.prefs.mirror ? "scaleX(-1)" : "none";
    $("cam").style.transform = flip;
    $("overlay").style.transform = flip;
  }

  /* ------------------------------------------------------------------ camera */
  function cameraBlocked() {
    if (!window.isSecureContext) return "insecure";
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return "unsupported";
    return null;
  }

  function explainBlock(reason) {
    const https = `https://${location.host}/m`;
    const origin = location.origin;
    const hint = $("hint");
    const menuNote = $("menu-note");
    if (reason === "insecure") {
      hint.textContent = "Camera needs a secure page";
      const message =
        `Browsers only hand the camera to a <b>secure</b> page, and this one is on plain http. ` +
        `Either reopen it as <b>${https}</b> and accept the self-signed certificate once, ` +
        `or in Chrome open <b>chrome://flags/#unsafely-treat-insecure-origin-as-secure</b>, ` +
        `add <b>${origin}</b>, restart the browser and come back.`;
      notice("Camera blocked by the browser", message);
      menuNote.innerHTML = message;
    } else {
      hint.textContent = "No camera API here";
      const message =
        "This browser has no getUserMedia. Use Chrome or Safari on the phone, or open the desktop page.";
      notice("Camera unavailable", message);
      menuNote.textContent = message;
    }
  }

  function notice(title, html) {
    $("notice-title").textContent = title;
    $("notice-text").innerHTML = html;
    $("notice").hidden = false;
  }

  async function startCamera() {
    const blocked = cameraBlocked();
    if (blocked) {
      explainBlock(blocked);
      return;
    }
    $("hint").textContent = "Starting the camera…";
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: {
          facingMode: state.prefs.facing,
          width: { ideal: 1280 },
          height: { ideal: 720 },
        },
        audio: false,
      });
      const video = $("cam");
      if (video.srcObject) {
        video.srcObject.getTracks().forEach((track) => track.stop());
      }
      video.srcObject = stream;
      await video.play();
      state.streaming = true;
      state.failures = 0;
      $("btn-start").hidden = true;
      $("notice").hidden = true; // whatever it said is stale now
      $("hint").textContent = "Streaming";
      sizeOverlay();
      loop();
    } catch (err) {
      $("hint").textContent = "Camera refused";
      notice("Camera refused", `Could not start the camera: ${err && err.message ? err.message : err}`);
    }
  }

  function stopCamera() {
    const video = $("cam");
    const stream = video.srcObject;
    if (stream) stream.getTracks().forEach((track) => track.stop());
    video.srcObject = null;
    state.streaming = false;
    $("btn-start").hidden = false;
    $("hint").textContent = "Camera stopped";
  }

  /* ----------------------------------------------------------------- capture */
  const grab = document.createElement("canvas");
  grab.width = CAPTURE_WIDTH;
  grab.height = 480;
  const grabCtx = grab.getContext("2d");

  function sizeOverlay() {
    const canvas = $("overlay");
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(window.innerWidth * ratio);
    canvas.height = Math.round(window.innerHeight * ratio);
    canvas.getContext("2d").setTransform(ratio, 0, 0, ratio, 0, 0);
  }

  function captureSize() {
    const video = $("cam");
    const vw = video.videoWidth || 1280;
    const vh = video.videoHeight || 720;
    grab.height = Math.max(1, Math.round((CAPTURE_WIDTH * vh) / Math.max(vw, 1)));
    return { w: CAPTURE_WIDTH, h: grab.height };
  }

  /** Map normalized frame coords onto the screen the same way `object-fit: cover` does. */
  function projector() {
    const canvas = $("overlay");
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const { w, h } = captureSize();
    const scale = Math.max(vw / w, vh / h);
    const offX = (vw - w * scale) / 2;
    const offY = (vh - h * scale) / 2;
    canvas.dataset.projScale = String(scale);
    return { x: (nx) => offX + nx * w * scale, y: (ny) => offY + ny * h * scale };
  }

  async function sendFrame() {
    const video = $("cam");
    if (!video.videoWidth) return;
    const { w, h } = captureSize();
    grabCtx.drawImage(video, 0, 0, w, h);
    const blob = await new Promise((resolve) => grab.toBlob(resolve, "image/jpeg", 0.6));
    if (!blob) return;
    state.sending = true;
    const started = performance.now();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 4000);
    try {
      const res = await fetch(`/m/frame?device=${encodeURIComponent(state.device)}`, {
        method: "POST",
        body: blob,
        headers: { "Content-Type": "image/jpeg" },
        signal: controller.signal,
      });
      const data = await res.json();
      state.roundTrip = performance.now() - started;
      state.failures = 0;
      handleFrame(data);
    } catch (err) {
      state.failures += 1;
    } finally {
      clearTimeout(timeout);
      state.sending = false;
    }
  }

  function loop() {
    if (!state.streaming) return;
    const interval = 1000 / Math.max(2, state.prefs.fps);
    const since = performance.now() - state.lastFrameAt;
    if (!state.sending && since >= interval) {
      state.lastFrameAt = performance.now();
      sendFrame().then(() => {
        if (state.streaming) setTimeout(loop, 0);
      });
      return;
    }
    requestAnimationFrame(loop);
  }

  /* ----------------------------------------------------------------- overlay */
  function drawOverlay(people) {
    const canvas = $("overlay");
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, window.innerWidth, window.innerHeight);
    if (!state.prefs.skeleton) return;
    const proj = projector();
    for (const person of people || []) {
      const points = person.points || {};
      const colour = person.bad ? "#d98080" : person.good ? "#82c08e" : "#8b93a1";
      const line = (a, b) => {
        if (!points[a] || !points[b]) return;
        ctx.beginPath();
        ctx.moveTo(proj.x(points[a][0]), proj.y(points[a][1]));
        ctx.lineTo(proj.x(points[b][0]), proj.y(points[b][1]));
        ctx.stroke();
      };
      ctx.strokeStyle = colour;
      ctx.lineWidth = 3;
      ctx.lineCap = "round";
      line("shoulder_left", "shoulder_right");
      line("hip_left", "hip_right");
      line("shoulder_right", "hip_right");
      line("shoulder_left", "hip_left");
      line("ear", "shoulder_right");
      line("hip_right", "knee");
      ctx.fillStyle = colour;
      for (const key of ["ear", "shoulder", "shoulder_left", "shoulder_right", "hip", "hip_left", "hip_right", "knee"]) {
        if (!points[key]) continue;
        ctx.beginPath();
        ctx.arc(proj.x(points[key][0]), proj.y(points[key][1]), 4, 0, Math.PI * 2);
        ctx.fill();
      }
      // The label sits on the shoulder, so the roster row and the body match up.
      const anchor = points.shoulder || points.shoulder_right || points.ear;
      if (anchor) {
        const text = `${person.label}  ${person.score === null ? "—" : person.score.toFixed(2)}`;
        ctx.font = "600 12px 'Overpass Mono', monospace";
        ctx.fillStyle = "rgba(7,9,11,0.7)";
        const width = ctx.measureText(text).width + 10;
        ctx.fillRect(proj.x(anchor[0]) - 4, proj.y(anchor[1]) - 30, width, 19);
        ctx.fillStyle = colour;
        ctx.fillText(text, proj.x(anchor[0]) + 1, proj.y(anchor[1]) - 16);
      }
    }
  }

  /* ------------------------------------------------------------------ roster */
  function renderRoster(roster) {
    const list = $("roster");
    const seen = new Set();
    for (const person of roster || []) {
      seen.add(String(person.id));
      let row = list.querySelector(`[data-person="${person.id}"]`);
      if (!row) {
        row = document.createElement("li");
        row.dataset.person = person.id;
        row.className = "person";
        row.innerHTML =
          '<span class="swatch"></span>' +
          '<span><span class="who"></span><span class="meta"></span></span>' +
          '<span class="score"></span>';
        list.appendChild(row);
      }
      row.classList.toggle("good", !!person.good);
      row.classList.toggle("bad", !!person.bad);
      row.classList.toggle("unknown", person.score === null || person.score === undefined);
      row.classList.toggle("stale", person.stale_for > 1.5);
      row.querySelector(".who").textContent = person.label;
      row.querySelector(".meta").textContent = person.calibrating
        ? `${person.metric || person.view} · measuring…`
        : `${person.metric} · ${person.state}`;
      row.querySelector(".score").textContent =
        person.score === null || person.score === undefined ? "—" : person.score.toFixed(2);
    }
    for (const row of Array.from(list.children)) {
      if (!seen.has(row.dataset.person)) row.remove();
    }
    // Worst first: the person who needs the roast is at the top of the list.
    const order = new Map((roster || []).map((p, index) => [String(p.id), index]));
    Array.from(list.children)
      .sort((a, b) => (order.get(a.dataset.person) ?? 99) - (order.get(b.dataset.person) ?? 99))
      .forEach((row) => list.appendChild(row));

    $("roster-count").textContent = String((roster || []).length);
    $("roster-empty").hidden = (roster || []).length > 0;
  }

  function handleFrame(data) {
    if (!data || !data.ok) {
      $("hint").textContent = "Server could not read the frame";
      return;
    }
    state.people = data.people || [];
    $("count-good").textContent = String(data.good || 0);
    $("count-bad").textContent = String(data.bad || 0);
    renderRoster(data.roster);
    drawOverlay(data.people);
    const rt = Math.round(state.roundTrip);
    const net = $("net");
    net.textContent = `${data.seen || 0} in view · ${rt}ms`;
    net.className = "net" + (rt > 900 ? " bad" : rt > 400 ? " warn" : "");
    $("hint").textContent = state.failures ? `Retrying… (${state.failures})` : "Streaming";
  }

  /* ------------------------------------------------------------------- roast */
  let speaking = false;

  function speak(text) {
    if (!state.prefs.speak || !text || !window.speechSynthesis) return;
    try {
      window.speechSynthesis.cancel(); // a new roast replaces the last one
      const utterance = new SpeechSynthesisUtterance(text);
      const voice = (speechSynthesis.getVoices() || []).find((v) => /^en/i.test(v.lang));
      if (voice) utterance.voice = voice;
      utterance.rate = 0.98;
      utterance.pitch = 0.95;
      utterance.onstart = () => {
        speaking = true;
      };
      utterance.onend = () => {
        speaking = false;
      };
      window.speechSynthesis.speak(utterance);
    } catch (e) {
      /* speaking is a bonus; never break the stream over it */
    }
  }

  function showCaption(text, who) {
    if (!text) return;
    $("caption-text").textContent = text;
    $("caption-who").textContent = who ? `for ${who}` : "";
    $("caption").hidden = false;
    $("notice").hidden = true; // a roast is more interesting than a caveat
    clearTimeout(showCaption.timer);
    showCaption.timer = setTimeout(() => {
      $("caption").hidden = true;
    }, 7000);
  }

  function flashPerson(label) {
    if (!label) return;
    for (const row of Array.from($("roster").children)) {
      if (row.querySelector(".who") && row.querySelector(".who").textContent === label) {
        row.classList.remove("flash");
        void row.offsetWidth; // restart the animation
        row.classList.add("flash");
      }
    }
  }

  function connectEvents() {
    try {
      const source = new EventSource("/events");
      source.onmessage = (event) => {
        let data;
        try {
          data = JSON.parse(event.data);
        } catch (e) {
          return;
        }
        if (data && data.kind === "roast" && data.text) {
          showCaption(data.text, data.person);
          flashPerson(data.person);
          speak(data.text);
        }
      };
      source.onerror = () => {
        /* EventSource retries on its own */
      };
    } catch (e) {
      /* SSE is optional: frames still work */
    }
  }

  /* ------------------------------------------------------------------ wiring */
  function bind() {
    $("btn-start").addEventListener("click", startCamera);
    $("btn-menu").addEventListener("click", () => {
      $("menu").hidden = false;
    });
    $("btn-close").addEventListener("click", () => {
      $("menu").hidden = true;
    });
    $("btn-collapse").addEventListener("click", () => {
      $("roster-panel").classList.toggle("collapsed");
    });
    $("btn-notice").addEventListener("click", () => {
      $("notice").hidden = true;
    });
    $("opt-speak").addEventListener("change", (e) => {
      state.prefs.speak = e.target.checked;
      savePrefs();
    });
    $("opt-mirror").addEventListener("change", (e) => {
      state.prefs.mirror = e.target.checked;
      applyMirror();
      savePrefs();
    });
    $("opt-skeleton").addEventListener("change", (e) => {
      state.prefs.skeleton = e.target.checked;
      savePrefs();
      if (!state.prefs.skeleton) drawOverlay([]);
    });
    $("opt-fps").addEventListener("change", (e) => {
      state.prefs.fps = Number(e.target.value) || 7;
      savePrefs();
    });
    $("opt-facing").addEventListener("change", async (e) => {
      state.prefs.facing = e.target.value;
      savePrefs();
      if (state.streaming) {
        stopCamera();
        await startCamera();
      }
    });
    $("btn-test-roast").addEventListener("click", async () => {
      $("menu").hidden = true;
      try {
        const res = await fetch("/api/test-roast", { method: "POST" });
        const data = await res.json();
        if (data && data.text) {
          showCaption(data.text, "the room");
          speak(data.text);
        }
      } catch (err) {
        $("menu-note").textContent = "Could not reach the server for a test roast.";
      }
    });    window.addEventListener("resize", () => {
      sizeOverlay();
      drawOverlay(state.people);
    });
    window.addEventListener("orientationchange", () => setTimeout(() => {
      sizeOverlay();
      drawOverlay(state.people);
    }, 300));
    document.addEventListener("visibilitychange", () => {
      if (document.hidden && state.streaming) {
        // Stop spending battery (and the server's CPU) on a hidden tab.
        $("hint").textContent = "Paused (tab hidden)";
      }
    });
  }

  function boot() {
    loadPrefs();
    sizeOverlay();
    bind();
    connectEvents();
    const blocked = cameraBlocked();
    if (blocked) explainBlock(blocked);
    if (window.speechSynthesis && typeof speechSynthesis.getVoices === "function") {
      // Voices load asynchronously in Chrome; asking early warms the list.
      speechSynthesis.getVoices();
    }
  }

  // A handle for the phone's own devtools (and for screenshot checks): pasting
  // `__pc.handleFrame({ok:true,seen:2,good:1,bad:1,people:[],roster:[...]})` into
  // the console renders a roster with no camera and no server round trip.
  window.__pc = { state, handleFrame, renderRoster, drawOverlay, showCaption, speak, startCamera };

  boot();
})();
