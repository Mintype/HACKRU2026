/* Posture Coach dashboard.
 * Pure vanilla JS + canvas: no build step, works offline on the Pi. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const els = {
    conn: $("conn"),
    connLabel: $("conn-label"),
    statePill: $("state-pill"),
    feed: $("feed"),
    feedMeta: $("feed-meta"),
    calib: $("calib-overlay"),
    calibBar: $("calib-bar"),
    caption: $("roast-caption"),
    sessionClock: $("session-clock"),
    neck: $("neck-value"),
    torso: $("torso-value"),
    score: $("score-value"),
    scoreBar: $("score-bar"),
    cooldownRow: $("cooldown-row"),
    cooldown: $("cooldown"),
    statGood: $("stat-good"),
    statSlouches: $("stat-slouches"),
    statRoasts: $("stat-roasts"),
    statStreak: $("stat-streak"),
    chart: $("chart"),
    btnCalibrate: $("btn-calibrate"),
    btnRoast: $("btn-roast"),
    niceMode: $("nice-mode"),
    settingsForm: $("settings-form"),
    settingsStatus: $("settings-status"),
    sysMode: $("sys-mode"),
    sysFps: $("sys-fps"),
    sysBt: $("sys-bt"),
    sysRoute: $("sys-route"),
    sysGemini: $("sys-gemini"),
    sysTts: $("sys-tts"),
    sysLocal: $("sys-local"),
    sysPool: $("sys-pool"),
    shame: $("shame-list"),
    footerUrl: $("footer-url"),
  };

  const STATE_CLASS = {
    away: "state-away",
    calibrating: "state-calibrating",
    good: "state-good",
    slouching: "state-slouching",
    warned: "state-warned",
    roasted: "state-roasted",
  };

  const series = [];          // { ts, score, state }
  const markers = [];         // { ts, text }
  const shame = [];           // most recent roasts first
  let sessionStartedAt = null;
  let state = "away";
  let lastReading = null;

  // ---------------------------------------------------------------- helpers
  function applyState(next) {
    if (!next || next === state) return;
    state = next;
    els.statePill.textContent = next;
    els.statePill.className = "state-pill " + (STATE_CLASS[next] || "state-away");
    if (next === "calibrating") {
      els.calib.classList.remove("hidden");
    } else {
      els.calib.classList.add("hidden");
    }
  }

  function fmt(value, digits = 0, suffix = "") {
    return value === null || value === undefined || Number.isNaN(value)
      ? "—"
      : value.toFixed(digits) + suffix;
  }

  function fmtClock(seconds) {
    seconds = Math.max(0, Math.round(seconds || 0));
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${m}:${String(s).padStart(2, "0")}`;
  }

  function setStatus(el, ok, textOk, textBad) {
    el.textContent = ok ? textOk : textBad;
    el.className = ok ? "ok" : "bad";
  }

  // ------------------------------------------------------------------ chart
  const ctx = els.chart.getContext("2d");

  function resizeChart() {
    const dpr = window.devicePixelRatio || 1;
    const rect = els.chart.getBoundingClientRect();
    els.chart.width = Math.max(300, rect.width * dpr);
    els.chart.height = Math.max(160, rect.height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    drawChart();
  }

  let chartQueued = false;
  function requestChart() {
    if (chartQueued) return;
    chartQueued = true;
    requestAnimationFrame(() => {
      chartQueued = false;
      drawChart();
    });
  }

  function drawChart() {
    const w = els.chart.clientWidth;
    const h = els.chart.clientHeight;
    const pad = { l: 34, r: 10, t: 10, b: 20 };
    const plotW = w - pad.l - pad.r;
    const plotH = h - pad.t - pad.b;

    ctx.clearRect(0, 0, w, h);
    if (!series.length) {
      ctx.fillStyle = "#8e99ad";
      ctx.font = "13px system-ui, sans-serif";
      ctx.fillText("No session data yet — sit down to start a session.", pad.l, h / 2);
      return;
    }

    const t0 = series[0].ts;
    const t1 = Math.max(series[series.length - 1].ts, t0 + 1);
    const xOf = (ts) => pad.l + ((ts - t0) / (t1 - t0)) * plotW;
    const yOf = (score) => pad.t + (1 - Math.max(0, Math.min(1, score))) * plotH;

    // grid + threshold guides
    ctx.strokeStyle = "#262d3b";
    ctx.lineWidth = 1;
    ctx.fillStyle = "#8e99ad";
    ctx.font = "10px system-ui, sans-serif";
    [0, 0.25, 0.5, 0.75, 1].forEach((v) => {
      const y = yOf(v);
      ctx.beginPath();
      ctx.moveTo(pad.l, y);
      ctx.lineTo(pad.l + plotW, y);
      ctx.stroke();
      ctx.fillText(v.toFixed(2), 4, y + 3);
    });
    [[0.8, "#4ade80"], [0.7, "#facc15"]].forEach(([v, color]) => {
      const y = yOf(v);
      ctx.save();
      ctx.setLineDash([5, 5]);
      ctx.strokeStyle = color;
      ctx.globalAlpha = 0.5;
      ctx.beginPath();
      ctx.moveTo(pad.l, y);
      ctx.lineTo(pad.l + plotW, y);
      ctx.stroke();
      ctx.restore();
    });

    // line
    ctx.strokeStyle = "#7c3aed";
    ctx.lineWidth = 2;
    ctx.beginPath();
    series.forEach((p, i) => {
      const x = xOf(p.ts);
      const y = yOf(p.score);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.stroke();

    // roast markers
    ctx.save();
    ctx.setLineDash([4, 4]);
    markers.forEach((m) => {
      const x = xOf(m.ts);
      if (x < pad.l || x > pad.l + plotW) return;
      ctx.strokeStyle = "rgba(248,113,113,.75)";
      ctx.beginPath();
      ctx.moveTo(x, pad.t);
      ctx.lineTo(x, pad.t + plotH);
      ctx.stroke();
      ctx.fillStyle = "#f87171";
      ctx.beginPath();
      ctx.arc(x, pad.t + 4, 3.5, 0, Math.PI * 2);
      ctx.fill();
    });
    ctx.restore();

    // trailing dot
    const last = series[series.length - 1];
    ctx.fillStyle = last.score >= 0.8 ? "#4ade80" : last.score >= 0.7 ? "#facc15" : "#f87171";
    ctx.beginPath();
    ctx.arc(xOf(last.ts), yOf(last.score), 4, 0, Math.PI * 2);
    ctx.fill();
  }

  // -------------------------------------------------------------- rendering
  function updateReading(reading) {
    lastReading = reading;
    if (reading.ts && !sessionStartedAt) sessionStartedAt = reading.ts;
    if (reading.neck_angle !== null && reading.neck_angle !== undefined) {
      els.neck.textContent = reading.neck_angle.toFixed(1);
    }
    if (reading.torso_angle !== null && reading.torso_angle !== undefined) {
      els.torso.textContent = reading.torso_angle.toFixed(1);
    }
    if (reading.score !== null && reading.score !== undefined) {
      els.score.textContent = reading.score.toFixed(2);
      els.scoreBar.style.width = Math.max(0, Math.min(1, reading.score)) * 100 + "%";
    }
    if (reading.fps !== undefined) els.feedMeta.textContent = `${reading.fps.toFixed(1)} fps`;
    if (reading.state) applyState(reading.state);

    if (reading.score !== null && reading.score !== undefined) {
      series.push({ ts: reading.ts, score: reading.score, state: reading.state });
      if (series.length > 4000) series.shift();
      requestChart();
    }
  }

  function addRoast(roast) {
    els.caption.textContent = roast.text;
    els.caption.parentElement.classList.remove("flash");
    void els.caption.parentElement.offsetWidth;
    els.caption.parentElement.classList.add("flash");
    if (roast.ts) markers.push({ ts: roast.ts, text: roast.text });
    shame.unshift(roast);
    renderShame();
    requestChart();
  }

  function renderShame() {
    if (!shame.length) {
      els.shame.innerHTML = '<li class="muted">No roasts yet. Sit up straight and nobody gets hurt.</li>';
      return;
    }
    els.shame.innerHTML = shame
      .slice(0, 8)
      .map((r) => {
        const angle = r.neck_angle !== null && r.neck_angle !== undefined ? ` · neck ${Number(r.neck_angle).toFixed(0)}°` : "";
        const tier = r.tier ? ` · ${r.tier}` : "";
        const src = r.source ? ` · ${r.source}` : "";
        return `<li>${escapeHtml(r.text)}<span class="meta">${escapeHtml(tier + src + angle)}</span></li>`;
      })
      .join("");
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function updateStats(stats) {
    if (!stats) return;
    const live = stats.live || {};
    if (live.state) applyState(live.state);
    if (live.session_started_at) sessionStartedAt = live.session_started_at;
    if (stats.reading) updateReading(Object.assign({}, stats.reading, { fps: stats.fps, state: stats.state }));

    if (stats.good_percent !== undefined) {
      els.statGood.textContent = `${fmt(stats.good_percent, 0)}%`;
      els.statSlouches.textContent = stats.slouch_count ?? "—";
      els.statRoasts.textContent = stats.roasts ?? "—";
      els.statStreak.textContent = fmt(stats.longest_good_streak, 0, "s");
    }

    const cooldown = live.cooldown_remaining || 0;
    if (cooldown > 0) {
      els.cooldownRow.classList.remove("hidden");
      els.cooldown.textContent = Math.ceil(cooldown);
    } else {
      els.cooldownRow.classList.add("hidden");
    }
    if (live.calibrating) {
      els.calib.classList.remove("hidden");
      els.calibBar.style.width = Math.round((live.calibration_progress || 0) * 100) + "%";
    } else {
      els.calib.classList.add("hidden");
    }
  }

  function updateStatus(status) {
    if (!status) return;
    const vision = status.vision || {};
    const audio = status.audio || {};
    const voice = status.voice || {};
    const pool = voice.pool || {};

    els.sysMode.textContent = vision.mode === "simulate" ? "simulated" : vision.source || "camera";
    els.sysFps.textContent = `${(vision.fps ?? 0).toFixed(1)} fps`;
    setStatus(els.sysBt, !!audio.bluetooth_connected, "connected", audio.bluetooth_mac ? "not connected" : "not configured");
    els.sysRoute.textContent = audio.route || "—";
    setStatus(els.sysGemini, !!voice.gemini_text, "ready", "fallback roasts");
    setStatus(els.sysTts, !!voice.gemini_tts, "ready", "local TTS");
    els.sysLocal.textContent = voice.local_tts || "unavailable";
    els.sysPool.textContent = `${pool.ready ?? 0} / ${pool.target ?? 0}`;
    els.footerUrl.textContent = `${status.server?.host || "0.0.0.0"}:${status.server?.port || 5000}`;
  }

  // ----------------------------------------------------------------- events
  function connect() {
    const source = new EventSource("/events");
    source.onopen = () => {
      els.conn.classList.add("online");
      els.conn.classList.remove("offline");
      els.connLabel.textContent = "live";
    };
    source.onerror = () => {
      els.conn.classList.remove("online");
      els.conn.classList.add("offline");
      els.connLabel.textContent = "reconnecting…";
    };
    source.onmessage = (message) => {
      let data;
      try {
        data = JSON.parse(message.data);
      } catch {
        return;
      }
      switch (data.kind) {
        case "reading":
          updateReading(data);
          break;
        case "event":
          if (data.event === "state") applyState(data.state);
          else if (data.state) applyState(data.state);
          if (data.event === "session_start") {
            series.length = 0;
            markers.length = 0;
            shame.length = 0;
            renderShame();
            sessionStartedAt = data.ts;
            requestChart();
          }
          if (data.event === "session_end") {
            fetchStats();
          }
          break;
        case "roast":
          addRoast(data);
          break;
        case "settings":
          fillSettings(data.settings);
          break;
        default:
          break;
      }
    };
  }

  // -------------------------------------------------------------------- data
  async function fetchJSON(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) throw new Error(`${url} -> ${response.status}`);
    return response.json();
  }

  async function fetchStats() {
    try {
      updateStats(await fetchJSON("/api/stats"));
    } catch (error) {
      /* server restarting; the SSE reconnect will catch up */
    }
  }

  async function fetchHistory() {
    try {
      const data = await fetchJSON("/api/history");
      series.length = 0;
      markers.length = 0;
      (data.readings || []).forEach((r) => {
        if (r.score !== null && r.score !== undefined) series.push({ ts: r.ts, score: r.score, state: r.state });
      });
      (data.roasts || []).forEach((r) => markers.push({ ts: r.ts, text: r.text }));
      requestChart();
    } catch (error) {
      /* ignore */
    }
  }

  async function fetchRoasts() {
    try {
      const data = await fetchJSON("/api/roasts");
      shame.length = 0;
      (data.roasts || []).forEach((r) => shame.push(r));
      renderShame();
      if (shame.length) els.caption.textContent = shame[0].text;
    } catch (error) {
      /* ignore */
    }
  }

  async function fetchStatus() {
    try {
      updateStatus(await fetchJSON("/api/status"));
    } catch (error) {
      /* ignore */
    }
  }

  // ---------------------------------------------------------------- settings
  function fillSettings(settings) {
    if (!settings) return;
    ["warn_seconds", "roast_seconds", "cooldown_seconds", "delta_angle"].forEach((key) => {
      const input = els.settingsForm.elements[key];
      if (input && settings[key] !== undefined) input.value = settings[key];
    });
    if (settings.audio_route) els.settingsForm.elements["audio_route"].value = settings.audio_route;
    if (settings.nice_mode !== undefined) els.niceMode.checked = !!settings.nice_mode;
  }

  async function fetchSettings() {
    try {
      fillSettings(await fetchJSON("/api/settings"));
    } catch (error) {
      /* ignore */
    }
  }

  async function saveSettings(event) {
    event.preventDefault();
    const payload = {
      warn_seconds: Number(els.settingsForm.elements["warn_seconds"].value),
      roast_seconds: Number(els.settingsForm.elements["roast_seconds"].value),
      cooldown_seconds: Number(els.settingsForm.elements["cooldown_seconds"].value),
      delta_angle: Number(els.settingsForm.elements["delta_angle"].value),
      audio_route: els.settingsForm.elements["audio_route"].value,
      nice_mode: els.niceMode.checked,
    };
    try {
      const response = await fetchJSON("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      els.settingsStatus.textContent = "Saved. " + Object.keys(response.applied || {}).join(", ");
      fillSettings(response.settings);
    } catch (error) {
      els.settingsStatus.textContent = "Could not save settings.";
    }
  }

  async function toggleNiceMode() {
    try {
      await fetchJSON("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ nice_mode: els.niceMode.checked }),
      });
      els.settingsStatus.textContent = els.niceMode.checked ? "Nice mode on." : "Nice mode off.";
    } catch (error) {
      els.settingsStatus.textContent = "Could not update nice mode.";
    }
  }

  async function calibrate() {
    els.btnCalibrate.disabled = true;
    try {
      await fetchJSON("/api/calibrate", { method: "POST" });
      els.calib.classList.remove("hidden");
      els.calibBar.style.width = "0%";
    } catch (error) {
      els.settingsStatus.textContent = "Calibrate needs an active session (sit down first).";
    } finally {
      setTimeout(() => (els.btnCalibrate.disabled = false), 1500);
    }
  }

  async function testRoast() {
    els.btnRoast.disabled = true;
    try {
      const response = await fetchJSON("/api/test-roast", { method: "POST" });
      if (response.text) addRoast({ text: response.text, tier: response.tier, source: response.source, ts: Date.now() / 1000 });
    } catch (error) {
      els.settingsStatus.textContent = "Player busy — try again in a moment.";
    } finally {
      setTimeout(() => (els.btnRoast.disabled = false), 800);
    }
  }

  // --------------------------------------------------------------------- init
  function tickClock() {
    if (!sessionStartedAt) {
      els.sessionClock.textContent = "no session";
      return;
    }
    els.sessionClock.textContent = `session ${fmtClock(Date.now() / 1000 - sessionStartedAt)}`;
  }

  function init() {
    els.btnCalibrate.addEventListener("click", calibrate);
    els.btnRoast.addEventListener("click", testRoast);
    els.settingsForm.addEventListener("submit", saveSettings);
    els.niceMode.addEventListener("change", toggleNiceMode);
    window.addEventListener("resize", resizeChart);

    // Reconnect the MJPEG stream if the browser drops it.
    els.feed.addEventListener("error", () => {
      setTimeout(() => {
        els.feed.src = `/video_feed?t=${Date.now()}`;
      }, 2000);
    });

    connect();
    fetchSettings();
    fetchHistory();
    fetchRoasts();
    fetchStats();
    fetchStatus();
    resizeChart();

    setInterval(fetchStats, 5000);
    setInterval(fetchStatus, 5000);
    setInterval(tickClock, 1000);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
