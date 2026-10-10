#!/usr/bin/env node
/**
 * Screenshot a page by driving headless Chrome over CDP.
 *
 * Why not `chrome --screenshot`? The Posture Coach page holds an open
 * server-sent-events connection, so it never goes idle and the one-shot
 * screenshot flag hangs forever. This drives the browser explicitly instead.
 *
 * Usage:
 *   node screenshot.mjs [url] [out.png] [delayMs] [width] [height] [options]
 *   node screenshot.mjs http://127.0.0.1:5000/ hud.png 25000 1600 900
 *   node screenshot.mjs http://127.0.0.1:5000/m phone.png 3000 390 844 --mobile
 *   node screenshot.mjs ... --eval "__pc.handleFrame({...})"   # render a state first
 *   node screenshot.mjs ... --fake-camera --eval "__pc.startCamera()"
 *
 * Options:
 *   --mobile        emulate a phone viewport (device pixel ratio 2, touch, no hover)
 *   --fake-camera   give the page a synthetic camera + auto-accepted permission, so
 *                   getUserMedia really runs (use with --eval "__pc.startCamera()")
 *   --eval <js>     run JS in the page just before capturing, e.g. to inject state
 *
 * Env:
 *   CHROME=/path/to/chrome   override browser discovery
 *   CDP_PORT=9222            attach to an already-running browser instead of launching
 */
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import net from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";

const argv = process.argv.slice(2);
const options = { mobile: false, fakeCamera: false, eval: null };
const positional = [];
for (let i = 0; i < argv.length; i++) {
  const arg = argv[i];
  if (arg === "--mobile") options.mobile = true;
  else if (arg === "--fake-camera") options.fakeCamera = true;
  else if (arg === "--eval") options.eval = argv[++i];
  else positional.push(arg);
}

const [
  url = "http://127.0.0.1:5000/",
  out = "hud.png",
  delayMs = "9000",
  width = "1600",
  height = "900",
] = positional;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function findBrowser() {
  const candidates = [
    process.env.CHROME,
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  ].filter(Boolean);
  const found = candidates.find((path) => existsSync(path));
  if (!found) throw new Error("no Chrome/Edge found - set CHROME=/path/to/browser");
  return found;
}

function freePort() {
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
      /* browser still starting */
    }
    await sleep(250);
  }
  throw new Error("the browser never offered a debug target");
}

async function main() {
  const reusePort = process.env.CDP_PORT;
  const port = reusePort ? Number(reusePort) : await freePort();
  let child = null;
  let profile = null;

  if (!reusePort) {
    profile = mkdtempSync(join(tmpdir(), "hud-shot-"));
    const browserArgs = [
      "--headless=new",
      "--disable-gpu",
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-extensions",
      // Mobile mode serves HTTPS with a throw-away certificate, so the phone
      // (and this) has to accept a self-signed one.
      "--ignore-certificate-errors",
      `--remote-debugging-port=${port}`,
      `--user-data-dir=${profile}`,
      `--window-size=${width},${height}`,
    ];
    if (options.fakeCamera) {
      // A synthetic camera feed, and no permission prompt to click.
      browserArgs.push("--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream");
    }
    browserArgs.push("about:blank");
    child = spawn(findBrowser(), browserArgs, { stdio: "ignore" });
  }

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
      deviceScaleFactor: options.mobile ? 2 : 1,
      mobile: options.mobile,
    });
    if (options.mobile) {
      await send("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 5 });
    }
    await send("Page.navigate", { url });
    await sleep(Number(delayMs));
    if (options.eval) {
      const result = await send("Runtime.evaluate", {
        expression: options.eval,
        awaitPromise: true,
        returnByValue: true,
      });
      const value = result && result.result ? result.result.value : undefined;
      if (result && result.exceptionDetails) {
        console.error(`--eval threw: ${JSON.stringify(result.exceptionDetails.text || result.exceptionDetails)}`);
      } else if (value !== undefined) {
        console.log(`--eval -> ${JSON.stringify(value).slice(0, 200)}`);
      }
      await sleep(600); // let the injected state paint before the shot
    }
    const { data } = await send("Page.captureScreenshot", { format: "png" });
    writeFileSync(out, Buffer.from(data, "base64"));
    console.log(`wrote ${out} (${width}x${height}, waited ${delayMs}ms)`);
  } finally {
    try {
      ws.close();
    } catch {
      /* ignore */
    }
    if (child) {
      child.kill();
      await sleep(500);
    }
    if (profile) {
      try {
        rmSync(profile, { recursive: true, force: true });
      } catch {
        /* the browser may still be holding it; the OS will clean up */
      }
    }
  }
}

main().catch((error) => {
  console.error(String((error && error.message) || error));
  process.exit(1);
});
