/* Posture Coach HUD.
   Fullscreen feed; translucent panels you can drag, resize, hide and add.
   Layout + display prefs persist in localStorage. No framework, no build step. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

  const TOPBAR = 44;
  const GAP = 12;
  const EDGE = 12;
  const STORE_KEY = "posturecoach.hud.v4";

  /* ------------------------------------------------------------ registry */
  // column + anchor say where auto-arrange parks a panel; order stacks them in
  // that corner. `visible: false` starts a panel hidden (still in the menu).
  const PANELS = {
    posture:  { title: "Posture",       width: 300, column: "left",  anchor: "top",    order: 0 },
    chart:    { title: "Score",         width: 520, column: "left",  anchor: "bottom", order: 0, resizable: true, minW: 320, minH: 150, height: 200 },
    stats:    { title: "Session",       width: 300, column: "right", anchor: "top",    order: 0 },
    system:   { title: "System",        width: 300, column: "right", anchor: "top",    order: 1 },
    controls: { title: "Controls",      width: 320, column: "right", anchor: "bottom", order: 0 },
    roasts:   { title: "Hall of shame", width: 320, column: "right", anchor: "bottom", order: 1, visible: false },
  };

  const STATE_CLASS = {
    away: "state-away",
    calibrating: "state-calibrating",
    good: "state-good",
    slouching: "state-slouching",
    warned: "state-warned",
    roasted: "state-roasted",
  };
  const STATE_COLOR = {
    away: "#8b93a1",
    calibrating: "#86a9d4",
    good: "#82c08e",
    slouching: "#d8b25c",
    warned: "#dda15e",
    roasted: "#d98080",
  };

  const display = { dim: 35 };
  const panels = {}; // id -> { visible, x, y, w, h, collapsed }
  for (const id in PANELS) {
    panels[id] = {
      visible: PANELS[id].visible !== false,
      x: null,
      y: null,
      w: PANELS[id].width,
      h: PANELS[id].resizable ? PANELS[id].height || 200 : null,
      collapsed: false,
    };
  }

  /* -------------------------------------------------------- persistence */
  function load() {
    try {
      const raw = JSON.parse(localStorage.getItem(STORE_KEY) || "null");
      if (!raw) return;
      if (typeof raw.dim === "number") display.dim = clamp(raw.dim, 0, 80);
      for (const id in panels) {
        if (raw.panels && raw.panels[id]) Object.assign(panels[id], raw.panels[id]);
      }
    } catch (e) {
      /* corrupt storage: fall back to defaults */
    }
  }

  function save() {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify({ dim: display.dim, panels }));
    } catch (e) {
      /* storage disabled (private mode): layout just will not persist */
    }
  }

  /* ------------------------------------------------------------- panels */
  const panelEl = (id) => document.querySelector(`.panel[data-panel="${id}"]`);
  const hudHeight = () => window.innerHeight - TOPBAR;

  function applyVisibility() {
    for (const id in PANELS) {
      const el = panelEl(id);
      const p = panels[id];
      if (!el) continue;
      el.style.display = p.visible ? "" : "none";
      el.classList.toggle("collapsed", !!p.collapsed);
      el.style.width = p.w + "px";
      el.style.height = PANELS[id].resizable ? (p.h || PANELS[id].height || 200) + "px" : "";
      if (p.x != null && p.y != null) {
        el.style.left = p.x + "px";
        el.style.top = p.y + "px";
      }
    }
  }

  function arrange() {
    const vw = window.innerWidth;
    const vh = hudHeight();
    const single = vw < 980;
    // Panels are parked against the edges so the middle of the frame — where
    // the person actually is — stays clear.
    const cursor = {
      left:  { top: 8, bottom: vh - 8 },
      right: { top: 8, bottom: vh - 8 },
    };

    const ids = Object.keys(PANELS).sort((a, b) => {
      const ca = single || PANELS[a].column === "left" ? 0 : 1;
      const cb = single || PANELS[b].column === "left" ? 0 : 1;
      return ca - cb || PANELS[a].order - PANELS[b].order;
    });

    for (const id of ids) {
      const p = panels[id];
      const el = panelEl(id);
      if (!el || !p.visible) continue;

      p.w = Math.min(PANELS[id].width, vw - EDGE * 2);
      el.style.width = p.w + "px";
      if (PANELS[id].resizable) el.style.height = (p.h || PANELS[id].height || 200) + "px";

      const height = el.offsetHeight || 160;
      const column = single ? "left" : PANELS[id].column;
      const anchor = single ? "top" : (PANELS[id].anchor || "top");
      const x = column === "left" ? EDGE : Math.max(EDGE, vw - p.w - EDGE);
      const y = anchor === "top" ? cursor[column].top : cursor[column].bottom - height;

      p.x = x;
      p.y = Math.max(8, Math.min(y, Math.max(8, vh - height - 8)));
      cursor[column][anchor] += anchor === "top" ? height + GAP : -(height + GAP);

      el.style.left = p.x + "px";
      el.style.top = p.y + "px";
    }
    save();
    resizeChart();
  }

  function clampAll() {
    const vw = window.innerWidth;
    const vh = hudHeight();
    for (const id in PANELS) {
      const p = panels[id];
      const el = panelEl(id);
      if (!el || !p.visible || p.x == null) continue;
      p.x = clamp(p.x, 0, Math.max(0, vw - el.offsetWidth));
      p.y = clamp(p.y, 0, Math.max(0, vh - 28));
      el.style.left = p.x + "px";
      el.style.top = p.y + "px";
    }
    save();
  }

  //: A restored layout is only trusted if every panel is actually on screen -
  //: otherwise (different monitor, stale storage, first run) we re-arrange.
  function layoutFits() {
    const vw = window.innerWidth;
    const vh = hudHeight();
    for (const id in PANELS) {
      const p = panels[id];
      const el = panelEl(id);
      if (!el || !p.visible) continue;
      if (p.x == null || p.y == null) return false;
      if (p.x < -1 || p.y < -1) return false;
      if (p.x + el.offsetWidth > vw + 2) return false;
      if (p.y + Math.min(el.offsetHeight, vh) > vh + 2) return false;
    }
    return true;
  }

  function initDrag() {
    for (const id in PANELS) {
      const el = panelEl(id);
      if (!el) continue;
      const head = el.querySelector(".panel-head");
      head.addEventListener("pointerdown", (ev) => {
        if (ev.target.closest(".panel-btn")) return;
        ev.preventDefault();
        const p = panels[id];
        const startX = ev.clientX;
        const startY = ev.clientY;
        const originX = p.x ?? el.offsetLeft;
        const originY = p.y ?? el.offsetTop;
        try { head.setPointerCapture(ev.pointerId); } catch (e) { /* synthetic pointer */ }
        el.classList.add("dragging");

        const onMove = (e) => {
          p.x = clamp(originX + (e.clientX - startX), 0, Math.max(0, window.innerWidth - el.offsetWidth));
          p.y = clamp(originY + (e.clientY - startY), 0, Math.max(0, hudHeight() - 28));
          el.style.left = p.x + "px";
          el.style.top = p.y + "px";
        };
        const onUp = () => {
          head.removeEventListener("pointermove", onMove);
          head.removeEventListener("pointerup", onUp);
          head.removeEventListener("pointercancel", onUp);
          el.classList.remove("dragging");
          save();
        };
        head.addEventListener("pointermove", onMove);
        head.addEventListener("pointerup", onUp);
        head.addEventListener("pointercancel", onUp);
      });
    }
  }

  function initResize() {
    for (const id in PANELS) {
      if (!PANELS[id].resizable) continue;
      const el = panelEl(id);
      const handle = el?.querySelector("[data-resize]");
      if (!handle) continue;
      handle.addEventListener("pointerdown", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        const p = panels[id];
        const startX = ev.clientX;
        const startY = ev.clientY;
        const originW = el.offsetWidth;
        const originH = el.offsetHeight;
        try { handle.setPointerCapture(ev.pointerId); } catch (e) { /* synthetic pointer */ }

        const onMove = (e) => {
          p.w = clamp(originW + (e.clientX - startX), PANELS[id].minW, window.innerWidth - p.x - EDGE);
          p.h = clamp(originH + (e.clientY - startY), PANELS[id].minH, hudHeight() - p.y - 8);
          el.style.width = p.w + "px";
          el.style.height = p.h + "px";
          resizeChart();
        };
        const onUp = () => {
          handle.removeEventListener("pointermove", onMove);
          handle.removeEventListener("pointerup", onUp);
          handle.removeEventListener("pointercancel", onUp);
          save();
          resizeChart();
        };
        handle.addEventListener("pointermove", onMove);
        handle.addEventListener("pointerup", onUp);
        handle.addEventListener("pointercancel", onUp);
      });
    }
  }

  function initPanelButtons() {
    for (const id in PANELS) {
      const el = panelEl(id);
      $$("[data-action]", el).forEach((btn) => {
        btn.addEventListener("click", (ev) => {
          ev.stopPropagation();
          const p = panels[id];
          if (btn.dataset.action === "hide") {
            p.visible = false;
            el.style.display = "none";
          } else if (btn.dataset.action === "collapse") {
            p.collapsed = !p.collapsed;
            el.classList.toggle("collapsed", p.collapsed);
          }
          save();
          buildPanelMenu();
          resizeChart();
        });
      });
    }
  }

  function buildPanelMenu() {
    const list = $("panel-menu-list");
    list.innerHTML = "";
    for (const id in PANELS) {
      const row = document.createElement("label");
      row.className = "menu-row";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = panels[id].visible;
      cb.addEventListener("change", () => {
        const p = panels[id];
        p.visible = cb.checked;
        const el = panelEl(id);
        el.style.display = p.visible ? "" : "none";
        if (p.visible && p.x == null) arrange();
        else {
          clampAll();
          resizeChart();
        }
        save();
      });
      const span = document.createElement("span");
      span.textContent = PANELS[id].title;
      row.append(cb, span);
      list.appendChild(row);
    }
  }

  /* ------------------------------------------------------------ display */
  function applyDisplay() {
    $("scrim").style.opacity = String(display.dim / 100);
    $("dim").value = String(display.dim);
  }

  function toggleMenu(force) {
    const menu = $("panel-menu");
    const show = force !== undefined ? force : menu.classList.contains("hidden");
    menu.classList.toggle("hidden", !show);
    $("btn-panels").classList.toggle("active", show);
  }

  function toggleFullscreen() {
    if (document.fullscreenElement) document.exitFullscreen();
    else document.documentElement.requestFullscreen?.();
  }

  function syncFullscreenIcon() {
    document.body.classList.toggle("is-fullscreen", !!document.fullscreenElement);
  }

  function bindTopbar() {
    $("btn-panels").addEventListener("click", (e) => {
      e.stopPropagation();
      toggleMenu();
    });
    $("btn-mirror").addEventListener("click", async () => {
      mirrorOn = !mirrorOn;
      $("btn-mirror").classList.toggle("active", mirrorOn);
      try {
        await fetchJSON("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mirror: mirrorOn }),
        });
      } catch (e) {
        mirrorOn = !mirrorOn;
        $("btn-mirror").classList.toggle("active", mirrorOn);
        $("settings-status").textContent = "Could not change mirror.";
      }
    });
    $("btn-fullscreen").addEventListener("click", toggleFullscreen);
    $("dim").addEventListener("input", (e) => {
      display.dim = clamp(Number(e.target.value), 0, 80);
      applyDisplay();
      save();
    });
    $("btn-reset-layout").addEventListener("click", () => {
      for (const id in PANELS) {
        panels[id] = {
          visible: PANELS[id].visible !== false,
          collapsed: false,
          x: null,
          y: null,
          w: PANELS[id].width,
          h: PANELS[id].resizable ? PANELS[id].height || 200 : null,
        };
      }
      applyVisibility();
      arrange();
      buildPanelMenu();
      toggleMenu(false);
    });

    document.addEventListener("pointerdown", (ev) => {
      const menu = $("panel-menu");
      if (menu.classList.contains("hidden")) return;
      if (ev.target.closest("#panel-menu") || ev.target.closest("#btn-panels")) return;
      toggleMenu(false);
    });

    document.addEventListener("keydown", (ev) => {
      if (ev.target.matches("input, select, textarea")) return;
      const key = ev.key.toLowerCase();
      if (key === "p") toggleMenu();
      else if (key === "m") $("btn-mirror").click();
      else if (key === "f") toggleFullscreen();
      else if (ev.key === "Escape") toggleMenu(false);
    });

    document.addEventListener("fullscreenchange", () => {
      syncFullscreenIcon();
      resizeChart();
    });
    window.addEventListener("resize", () => {
      clampAll();
      resizeChart();
    });
  }

  /* --------------------------------------------------------------- chart */
  const chart = $("chart");
  const ctx = chart.getContext("2d");
  const series = [];
  const markers = [];
  let chartQueued = false;

  function resizeChart() {
    const dpr = window.devicePixelRatio || 1;
    const rect = chart.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return;
    chart.width = Math.round(rect.width * dpr);
    chart.height = Math.round(rect.height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    drawChart();
  }

  function requestChart() {
    if (chartQueued) return;
    chartQueued = true;
    requestAnimationFrame(() => {
      chartQueued = false;
      drawChart();
    });
  }

  function drawChart() {
    const w = chart.clientWidth;
    const h = chart.clientHeight;
    if (w < 2 || h < 2) return;
    const pad = { l: 30, r: 8, t: 10, b: 16 };
    const plotW = w - pad.l - pad.r;
    const plotH = h - pad.t - pad.b;

    ctx.clearRect(0, 0, w, h);

    if (!series.length) {
      ctx.fillStyle = "#6f7783";
      ctx.font = "11px " + getComputedStyle(document.body).fontFamily;
      ctx.fillText("waiting for a session", pad.l, h / 2);
      return;
    }

    const t0 = series[0].ts;
    const t1 = Math.max(series[series.length - 1].ts, t0 + 1);
    const xOf = (ts) => pad.l + ((ts - t0) / (t1 - t0)) * plotW;
    const yOf = (s) => pad.t + (1 - clamp(s, 0, 1)) * plotH;

    ctx.font = "9.5px " + getComputedStyle(document.body).fontFamily;
    ctx.lineWidth = 1;
    [0, 0.25, 0.5, 0.75, 1].forEach((v) => {
      const y = Math.round(yOf(v)) + 0.5;
      ctx.strokeStyle = "rgba(255,255,255,.07)";
      ctx.beginPath();
      ctx.moveTo(pad.l, y);
      ctx.lineTo(pad.l + plotW, y);
      ctx.stroke();
      ctx.fillStyle = "#6f7783";
      ctx.fillText(v.toFixed(2), 4, y + 3);
    });

    [[0.8, "rgba(130,192,142,.5)"], [0.7, "rgba(216,178,92,.5)"]].forEach(([v, color]) => {
      const y = Math.round(yOf(v)) + 0.5;
      ctx.save();
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = color;
      ctx.beginPath();
      ctx.moveTo(pad.l, y);
      ctx.lineTo(pad.l + plotW, y);
      ctx.stroke();
      ctx.restore();
    });

    // Roast markers sit behind the trace.
    ctx.save();
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = "rgba(217,128,128,.8)";
    markers.forEach((marker) => {
      const x = Math.round(xOf(marker.ts)) + 0.5;
      if (x < pad.l || x > pad.l + plotW) return;
      ctx.beginPath();
      ctx.moveTo(x, pad.t);
      ctx.lineTo(x, pad.t + plotH);
      ctx.stroke();
    });
    ctx.restore();

    // The trace is coloured by score through one vertical ramp: green up top,
    // amber at the warn/roast thresholds, red down low. The colour is the data.
    const ramp = ctx.createLinearGradient(0, pad.t, 0, pad.t + plotH);
    ramp.addColorStop(0.0, "#7fc48d");
    ramp.addColorStop(0.19, "#7fc48d");
    ramp.addColorStop(0.3, "#d8b25c");
    ramp.addColorStop(0.5, "#cf9468");
    ramp.addColorStop(1.0, "#d98080");

    const trace = new Path2D();
    series.forEach((point, i) => {
      const x = xOf(point.ts);
      const y = yOf(point.score);
      if (i === 0) trace.moveTo(x, y);
      else trace.lineTo(x, y);
    });

    if (series.length > 1) {
      const area = new Path2D(trace);
      area.lineTo(xOf(series[series.length - 1].ts), pad.t + plotH);
      area.lineTo(xOf(series[0].ts), pad.t + plotH);
      area.closePath();
      ctx.save();
      ctx.globalAlpha = 0.11;
      ctx.fillStyle = ramp;
      ctx.fill(area);
      ctx.restore();
    }

    ctx.save();
    ctx.strokeStyle = ramp;
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.stroke(trace);
    ctx.restore();

    const last = series[series.length - 1];
    const lx = xOf(last.ts);
    const ly = yOf(last.score);
    ctx.beginPath();
    ctx.arc(lx, ly, 4.5, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(10,12,16,.7)";
    ctx.fill();
    ctx.beginPath();
    ctx.arc(lx, ly, 2.6, 0, Math.PI * 2);
    ctx.fillStyle = STATE_COLOR[last.state] || "#e9ebef";
    ctx.fill();
  }

  /* ------------------------------------------------------------- updates */
  let sessionStartedAt = null;
  let currentState = "away";
  let mirrorOn = false;
  let captionTimer = null;

  function applyState(next) {
    if (!next || next === currentState) return;
    currentState = next;
    const chip = $("state-chip");
    chip.className = "state " + (STATE_CLASS[next] || "state-away");
    $("state-text").textContent = next;
    $("calib").classList.toggle("show", next === "calibrating");
  }

  function updateReading(reading) {
    if (reading.ts && !sessionStartedAt) sessionStartedAt = reading.ts;
    if (reading.neck_angle != null) $("neck-value").textContent = reading.neck_angle.toFixed(1);
    if (reading.torso_angle != null) $("torso-value").textContent = reading.torso_angle.toFixed(1);
    if (reading.score != null) {
      $("score-value").textContent = reading.score.toFixed(2);
      const bar = $("score-bar");
      bar.style.width = clamp(reading.score, 0, 1) * 100 + "%";
      bar.style.backgroundColor = STATE_COLOR[reading.state] || "#e9ebef";
    }
    if (reading.fps !== undefined) {
      const text = reading.fps.toFixed(1) + " fps";
      $("hud-fps").textContent = text;
      $("sys-fps").textContent = text;
    }
    if (reading.state) applyState(reading.state);

    // Why a roast may never fire: nobody detected, or detected but not seated.
    if (reading.person !== undefined) {
      const cell = $("sys-detect");
      const person = reading.person === true;
      const seated = reading.seated === true;
      cell.textContent = person ? (seated ? "person \u00b7 seated" : "person \u00b7 not seated") : "nobody";
      cell.className = person && seated ? "ok" : person ? "warn" : "none";
    }
    if (reading.score != null) {
      series.push({ ts: reading.ts, score: reading.score, state: reading.state });
      if (series.length > 4000) series.shift();
      requestChart();
    }
  }

  function showCaption(text) {
    $("caption-text").textContent = text;
    const caption = $("caption");
    caption.classList.add("show");
    clearTimeout(captionTimer);
    captionTimer = setTimeout(() => caption.classList.remove("show"), 9000);
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function renderRoasts(items) {
    const list = $("shame-list");
    if (!items.length) {
      list.innerHTML = '<li class="empty">No roasts this session.</li>';
      return;
    }
    list.innerHTML = items
      .slice(0, 12)
      .map((r) => {
        const bits = [r.tier, r.source, r.neck_angle != null ? `neck ${Number(r.neck_angle).toFixed(0)}\u00b0` : null].filter(Boolean);
        return `<li>${escapeHtml(r.text)}<span class="meta">${escapeHtml(bits.join("  \u00b7  "))}</span></li>`;
      })
      .join("");
  }

  function updateStats(stats) {
    if (!stats) return;
    const live = stats.live || {};
    if (live.state) applyState(live.state);
    if (live.session_started_at) sessionStartedAt = live.session_started_at;
    if (stats.reading) updateReading({ ...stats.reading, fps: stats.fps, state: stats.state });

    if (stats.good_percent !== undefined) {
      $("stat-good").textContent = Math.round(stats.good_percent) + "%";
      $("stat-slouches").textContent = stats.slouch_count ?? "\u2014";
      $("stat-roasts").textContent = stats.roasts ?? "\u2014";
      $("stat-streak").textContent = Math.round(stats.longest_good_streak || 0) + "s";
    }

    const cooldown = live.cooldown_remaining || 0;
    $("cooldown-row").classList.toggle("hidden", cooldown <= 0);
    if (cooldown > 0) $("cooldown").textContent = Math.ceil(cooldown);
    $("calib-bar").style.width = Math.round((live.calibration_progress || 0) * 100) + "%";
  }

  function setState(el, text, kind) {
    el.textContent = text;
    el.className = kind; // "ok" | "warn" | "none"
  }

  function updateStatus(status) {
    if (!status) return;
    const vision = status.vision || {};
    const audio = status.audio || {};
    const voice = status.voice || {};
    const pool = voice.pool || {};

    $("sys-mode").textContent = vision.mode === "simulate" ? "simulated" : (vision.source || "camera");
    $("sys-fps").textContent = (vision.fps ?? 0).toFixed(1) + " fps";

    const btOn = !!audio.bluetooth_connected;
    const btSet = !!audio.bluetooth_mac;
    setState($("sys-bt"), btOn ? "connected" : btSet ? "disconnected" : "not set", btOn ? "ok" : btSet ? "warn" : "none");
    $("sys-route").textContent = audio.route || "\u2014";
    setState($("sys-gemini"), voice.gemini_text ? "live" : "offline fallback", voice.gemini_text ? "ok" : "warn");
    const ttsReady = voice.gemini_tts_ready !== false;
    setState(
      $("sys-tts"),
      voice.gemini_tts ? (ttsReady ? "live" : "quota hold") : "local fallback",
      voice.gemini_tts && ttsReady ? "ok" : "warn"
    );
    setState($("sys-local"), voice.local_tts || "none", voice.local_tts ? "ok" : "none");

    // A pool voiced by more than one engine sounds inconsistent, so say so.
    const voices = (voice.pool && voice.pool.voices) || {};
    const kinds = Object.keys(voices).filter((k) => voices[k] > 0 && k !== "none");
    const mix = kinds.map((k) => `${k} ${voices[k]}`).join(" / ");
    const voiceCell = $("sys-voice");
    voiceCell.textContent = (voice.tts_mode || "auto") + (mix ? ` \u00b7 ${mix}` : "");
    voiceCell.className = kinds.length > 1 ? "warn" : "";
    $("sys-pool").textContent = `${pool.ready ?? 0}/${pool.target ?? 0}`;
  }

  /* ---------------------------------------------------------------- feed */
  async function fetchJSON(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) throw new Error(`${url} ${response.status}`);
    return response.json();
  }

  function connect() {
    const source = new EventSource("/events");
    // No idle "live" badge: the HUD is only marked when the stream actually drops.
    source.onopen = () => $("conn").classList.add("hidden");
    source.onerror = () => $("conn").classList.remove("hidden");
    source.onmessage = (message) => {
      let data;
      try {
        data = JSON.parse(message.data);
      } catch (e) {
        return;
      }
      if (data.kind === "reading") {
        updateReading(data);
      } else if (data.kind === "roast") {
        showCaption(data.text);
        markers.push({ ts: data.ts, text: data.text });
        requestChart();
        fetchRoasts();
      } else if (data.kind === "event") {
        if (data.state) applyState(data.state);
        if (data.event === "session_start") {
          series.length = 0;
          markers.length = 0;
          sessionStartedAt = data.ts;
          $("shame-list").innerHTML = '<li class="empty">No roasts this session.</li>';
          requestChart();
        } else if (data.event === "session_end") {
          fetchStats();
        }
      } else if (data.kind === "settings") {
        fillSettings(data.settings);
      }
    };
  }

  async function fetchStats() {
    try {
      updateStats(await fetchJSON("/api/stats"));
    } catch (e) {
      /* server restarting; SSE will reconnect */
    }
  }

  async function fetchHistory() {
    try {
      const data = await fetchJSON("/api/history");
      series.length = 0;
      markers.length = 0;
      (data.readings || []).forEach((r) => {
        if (r.score != null) series.push({ ts: r.ts, score: r.score, state: r.state });
      });
      (data.roasts || []).forEach((r) => markers.push({ ts: r.ts, text: r.text }));
      requestChart();
    } catch (e) {
      /* nothing to seed yet */
    }
  }

  async function fetchRoasts() {
    try {
      const data = await fetchJSON("/api/roasts");
      renderRoasts(data.roasts || []);
    } catch (e) {
      /* ignore */
    }
  }

  async function fetchStatus() {
    try {
      updateStatus(await fetchJSON("/api/status"));
    } catch (e) {
      /* ignore */
    }
  }

  function fillSettings(settings) {
    if (!settings) return;
    ["warn_seconds", "roast_seconds", "cooldown_seconds", "delta_angle"].forEach((key) => {
      const input = $("settings-form").elements[key];
      if (input && settings[key] !== undefined) input.value = settings[key];
    });
    if (settings.audio_route) $("settings-form").elements["audio_route"].value = settings.audio_route;
    if (settings.tts_mode) $("settings-form").elements["tts_mode"].value = settings.tts_mode;
    if (settings.nice_mode !== undefined) $("nice-mode").checked = !!settings.nice_mode;
    if (settings.mirror !== undefined) {
      mirrorOn = !!settings.mirror;
      $("btn-mirror").classList.toggle("active", mirrorOn);
    }
  }

  async function fetchSettings() {
    try {
      fillSettings(await fetchJSON("/api/settings"));
    } catch (e) {
      /* ignore */
    }
  }

  /* ------------------------------------------------------------ controls */
  function bindControls() {
    $("btn-calibrate").addEventListener("click", async () => {
      const button = $("btn-calibrate");
      button.disabled = true;
      try {
        await fetchJSON("/api/calibrate", { method: "POST" });
        $("calib").classList.add("show");
        $("calib-bar").style.width = "0%";
      } catch (e) {
        $("settings-status").textContent = "Needs an active session \u2014 sit down first.";
      } finally {
        setTimeout(() => (button.disabled = false), 1500);
      }
    });

    $("btn-roast").addEventListener("click", async () => {
      const button = $("btn-roast");
      button.disabled = true;
      try {
        const result = await fetchJSON("/api/test-roast", { method: "POST" });
        if (result.text) {
          showCaption(result.text);
          fetchRoasts();
        }
      } catch (e) {
        $("settings-status").textContent = "Player busy \u2014 try again.";
      } finally {
        setTimeout(() => (button.disabled = false), 800);
      }
    });

    $("nice-mode").addEventListener("change", async (e) => {
      try {
        await fetchJSON("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ nice_mode: e.target.checked }),
        });
      } catch (err) {
        $("settings-status").textContent = "Could not save.";
      }
    });

    $("settings-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const form = $("settings-form");
      const payload = {
        warn_seconds: Number(form.elements["warn_seconds"].value),
        roast_seconds: Number(form.elements["roast_seconds"].value),
        cooldown_seconds: Number(form.elements["cooldown_seconds"].value),
        delta_angle: Number(form.elements["delta_angle"].value),
        audio_route: form.elements["audio_route"].value,
        tts_mode: form.elements["tts_mode"].value,
        nice_mode: $("nice-mode").checked,
      };
      try {
        const result = await fetchJSON("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        $("settings-status").textContent = "Saved \u00b7 " + Object.keys(result.applied || {}).join(", ");
        fillSettings(result.settings);
      } catch (e) {
        $("settings-status").textContent = "Could not save.";
      }
    });

    // Reconnect a dropped MJPEG stream.
    $("feed").addEventListener("error", () => {
      setTimeout(() => {
        $("feed").src = "/video_feed?t=" + Date.now();
      }, 2000);
    });
  }

  function tickClock() {
    const clock = $("session-clock");
    if (!sessionStartedAt) {
      clock.textContent = "no session";
      return;
    }
    const total = Math.max(0, Math.round(Date.now() / 1000 - sessionStartedAt));
    clock.textContent = `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
  }

  /* ------------------------------------------------------------------ init */
  function init() {
    load();
    applyVisibility();
    if (!layoutFits()) {
      arrange();
      // Panel heights settle once the canvas and fonts have laid out, so one
      // more pass keeps the corner stacking exact on a first run.
      requestAnimationFrame(() => arrange());
    } else {
      clampAll();
    }

    initDrag();
    initResize();
    initPanelButtons();
    buildPanelMenu();
    applyDisplay();
    bindTopbar();
    bindControls();

    connect();
    fetchSettings();
    fetchHistory();
    fetchRoasts();
    fetchStats();
    fetchStatus();
    requestAnimationFrame(resizeChart);

    setInterval(fetchStats, 5000);
    setInterval(fetchStatus, 5000);
    setInterval(tickClock, 1000);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
