#!/usr/bin/env node
/**
 * Keep the mobile page open with a working (fake) camera, so a *second* browser
 * can be pointed at the desktop HUD while frames are actually flowing.
 *
 * `screenshot.mjs` closes its browser as soon as it has the shot, which makes it
 * impossible to check anything that needs the phone to be streaming meanwhile -
 * most importantly "the phone drives /video_feed on the desktop page".
 *
 * Usage:
 *   node keep_phone.mjs [url] [seconds] [width] [height]
 *   node keep_phone.mjs http://127.0.0.1:5000/m 25 390 844
 *
 * It needs plain http on localhost (a secure context) - see the mobile SKILL.md
 * for why the HTTPS form cannot be camera-tested headlessly.
 */
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import net from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [url = "http://127.0.0.1:5000/m", seconds = "25", width = "390", height = "844"] =
  process.argv.slice(2);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function findBrowser() {
  const candidates = [
    process.env.CHROME,
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  ].filter(Boolean);
  const found = candidates.find((path) => existsSync(path));
  if (!found) throw new Error("no Chrome/Edge found - set CHROME=/path/to/browser");
  return found;
}

async function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

async function pageTarget(port, attempts = 80) {
  for (let i = 0; i < attempts; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      const page = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
      if (page) return page.webSocketDebuggerUrl;
    } catch {
      /* still starting */
    }
    await sleep(250);
  }
  throw new Error("the browser never offered a debug target");
}

const port = await freePort();
const profile = mkdtempSync(join(tmpdir(), "hud-phone-"));
const child = spawn(
  findBrowser(),
  [
    "--headless=new",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--ignore-certificate-errors",
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    `--window-size=${width},${height}`,
    "about:blank",
  ],
  { stdio: "ignore" }
);

const ws = new WebSocket(await pageTarget(port));
let seq = 0;
const pending = new Map();
ws.addEventListener("message", (event) => {
  const message = JSON.parse(event.data);
  const entry = pending.get(message.id);
  if (!entry) return;
  pending.delete(message.id);
  message.error ? entry.reject(new Error(JSON.stringify(message.error))) : entry.resolve(message.result);
});
await new Promise((resolve) => ws.addEventListener("open", resolve));
const send = (method, params = {}) =>
  new Promise((resolve, reject) => {
    const id = ++seq;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });

try {
  await send("Page.enable");
  await send("Runtime.enable");
  await send("Emulation.setDeviceMetricsOverride", {
    width: Number(width),
    height: Number(height),
    deviceScaleFactor: 2,
    mobile: true,
  });
  await send("Page.navigate", { url });
  await sleep(2500);
  const started = await send("Runtime.evaluate", {
    expression: "__pc.startCamera(); 'started'",
    awaitPromise: true,
    returnByValue: true,
  });
  console.log(`phone page streaming for ${seconds}s (${started.result.value})`);
  await sleep(Number(seconds) * 1000);
  const stats = await send("Runtime.evaluate", {
    expression: "JSON.stringify({hint: document.getElementById('hint').textContent, net: document.getElementById('net').textContent})",
    returnByValue: true,
  });
  console.log(`phone says: ${stats.result.value}`);
} finally {
  try {
    ws.close();
  } catch {
    /* ignore */
  }
  child.kill();
  await sleep(400);
  try {
    rmSync(profile, { recursive: true, force: true });
  } catch {
    /* the OS will clean up */
  }
}
