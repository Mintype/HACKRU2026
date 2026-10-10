#!/usr/bin/env python3
"""Posture Coach.

Two ways to run it:

* **server + website** (default): the program runs here on this machine (camera,
  pose detection, state machine, audio) and serves the web app to any browser.
* **standalone preview** (``--preview``): a plain desktop window with the
  annotated feed, no browser -- handy for aiming the side-view camera.

    python run.py                  # real webcam (side view), serves the website
    python run.py --preview        # standalone window, no browser
    python run.py --list-cameras   # show which camera indices work
    python run.py --simulate       # laptop demo, no camera
    python run.py --video clip.mp4 # develop vision against a recording
    python run.py --no-audio       # print roasts instead of playing them
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

_VENV_ENV = "POSTURE_VENV_BOOTSTRAPPED"


def _relaunch_in_venv() -> None:
    """Re-exec inside ``.venv`` when launched with a Python that lacks the deps.

    ``python`` is often not the interpreter the dependencies were installed
    into; this keeps ``python run.py`` working without manual activation.
    """
    if os.environ.get(_VENV_ENV) == "1":
        return
    root = Path(__file__).resolve().parent
    venv_python = next(
        (
            candidate
            for candidate in (
                root / ".venv" / "Scripts" / "python.exe",
                root / ".venv" / "bin" / "python",
            )
            if candidate.exists()
        ),
        None,
    )
    if venv_python is None:
        return
    try:
        import flask  # noqa: F401  (already inside an environment with the deps?)
        return
    except Exception:
        pass
    if Path(sys.executable).resolve() == venv_python.resolve():
        return
    print(f"[posture-coach] using virtualenv interpreter: {venv_python}")
    env = dict(os.environ, **{_VENV_ENV: "1"})
    raise SystemExit(
        subprocess.call([str(venv_python), str(root / "run.py"), *sys.argv[1:]], env=env)
    )


_relaunch_in_venv()

import config  # noqa: E402  (imported after the venv check)
from server.app import create_app  # noqa: E402

BANNER = r"""
  ___         _              ___         _
 | _ \___ ___| |_ _ _ _ ___  / __|___  __| |___
 |  _/ _ \_ _|  _| '_/ _ \  | (__/ _ \/ _` / -_)
 |_| \___/__| \__|_| \___/   \___\___/\__,_\___|
"""

SIDE_VIEW_HINT = (
    "  side view: mount the camera at shoulder height, 1-2 m to the person's side,\n"
    "             roughly level, with the whole head and hip visible in frame."
)


def _tls_available() -> bool:
    """Can Flask serve HTTPS with a throw-away certificate?

    A phone browser refuses ``getUserMedia`` on a plain ``http://`` LAN origin, so
    mobile mode wants TLS; ``cryptography`` is what Werkzeug's adhoc context needs.
    """
    try:
        import cryptography  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def mobile_urls(port: int, secure: bool = True) -> list:
    """The addresses a phone can actually open, so nobody has to guess an IP."""
    import socket

    scheme = "https" if secure else "http"
    urls: list = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in urls:
                urls.append(ip)
    except Exception:  # noqa: BLE001
        pass
    if not urls:
        # A UDP socket to a public address sends nothing, but it does tell the OS
        # which interface would be used - which is the one the phone can reach.
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("8.8.8.8", 80))
            urls.append(probe.getsockname()[0])
            probe.close()
        except Exception:  # noqa: BLE001
            pass

    lines = ["  phone  : open this on the phone (same WiFi as this computer):"]
    if urls:
        lines += [f"           {scheme}://{ip}:{port}/m" for ip in urls]
    else:
        lines.append(f"           {scheme}://<this-computer's-LAN-IP>:{port}/m")
    if secure:
        lines.append("           the certificate is self-signed: tap Advanced -> Proceed once")
    else:
        lines.append("           no HTTPS: the phone browser will block the camera (see the README)")
    return lines


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Posture Coach: side-view posture coach that roasts you.")
    parser.add_argument("--simulate", "--fake", dest="simulate", action="store_true",
                        help="run the scripted fake readings instead of a camera")
    parser.add_argument("--mobile", action="store_true",
                        help="phone-as-camera mode: no local camera, phones post frames to /m "
                             "and get a live roster of everyone in view (see the README)")
    parser.add_argument("--mobile-people", dest="mobile_people", type=int, default=None, metavar="N",
                        help="with --mobile, how many people to track at once (default: the setting, 3)")
    parser.add_argument("--camera", type=int, default=None,
                        help="camera index to use (default: remembered setting, usually 0)")
    parser.add_argument("--camera-backend", dest="camera_backend", type=str, default="auto",
                        choices=["auto", "dshow", "msmf", "v4l2", "avfoundation"],
                        help="camera backend; Windows 'auto' = DirectShow (fast)")
    parser.add_argument("--list-cameras", dest="list_cameras", action="store_true",
                        help="probe camera indices, print them and exit")
    parser.add_argument("--list-snapshots", dest="list_snapshots", action="store_true",
                        help="with --list-cameras, save one snapshot per camera to data/camera-scan/")
    parser.add_argument("--test-voice", dest="test_voice", nargs="?",
                        const="Sit up straight. Your spine has filed a complaint.",
                        default=None, metavar="TEXT",
                        help="synthesize one line with the configured TTS, play it and exit")
    parser.add_argument("--preview", action="store_true",
                        help="open a standalone preview window instead of the website")
    parser.add_argument("--demo", action="store_true",
                        help="short thresholds (roast 10s, cooldown 15s) so the whole cycle is visible fast")
    parser.add_argument("--video", type=str, default=None, help="use a video file instead of the webcam")
    parser.add_argument("--model", type=str, default=None, choices=["lite", "full", "heavy"],
                        help="MediaPipe pose model (default lite on the Pi)")
    parser.add_argument("--no-audio", dest="no_audio", action="store_true",
                        help="print roasts instead of playing them (laptop / testing)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="bind address (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5000, help="port (default 5000)")
    parser.add_argument("--timers", nargs="?", const="http://127.0.0.1:5000", default=None, metavar="URL",
                        help="watch the roast countdown from a running server (Ctrl+C to stop)")
    parser.add_argument("--settings", type=str, default=None, help="path to a settings JSON file")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    return parser.parse_args(argv)


def list_cameras_command(args: argparse.Namespace) -> int:
    """Probe camera indices so the side camera can be picked by number."""
    from vision.source import list_cameras, resolve_backend

    backend = resolve_backend(args.camera_backend)
    snapshot_dir = (config.DATA_DIR / "camera-scan") if args.list_snapshots else None
    print("Probing camera indices 0..5 (a camera light may blink briefly)...\n")
    found = 0
    for info in list_cameras(max_index=6, backend=backend, snapshot_dir=snapshot_dir):
        if not info["opened"]:
            continue
        found += 1
        resolution = (
            f"{info['width']}x{info['height']}" if info["width"] else "no frame"
        )
        maximum = (
            f"max {info['max_width']}x{info['max_height']}" if info["max_width"] else ""
        )
        snapshot = f"  snapshot: {info['snapshot']}" if info.get("snapshot") else ""
        print(f"  index {info['index']}: OPEN  {resolution}  {maximum}  ({info['ms']} ms){snapshot}")
    print(f"\n{found} camera(s) found.")
    print("Pick one with:   python run.py --camera <index>")
    print("Aim it with:     python run.py --camera <index> --preview")
    if snapshot_dir:
        print(f"Open the snapshots in {snapshot_dir} to tell the cameras apart.")
    return 0


def preview_loop(coach) -> int:
    """Standalone desktop preview: annotated feed in a window, no browser."""
    try:
        import cv2
    except Exception as exc:  # pragma: no cover
        print(f"preview needs OpenCV: {exc}")
        return 2

    window = "Posture Coach - preview"
    queue = coach.broker.subscribe()
    print("  preview : a window will open; press q or Esc to quit\n")

    coach.start()
    try:
        while True:
            frame = coach.vision.latest_frame()
            if frame is not None:
                try:
                    cv2.imshow(window, frame)
                    if cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                        break
                except Exception:
                    break
            if (cv2.waitKey(1) & 0xFF) in (ord("q"), 27):
                break

            event = coach.broker.drain(queue, timeout=0)
            while event is not None:
                kind = event.get("kind")
                if kind == "roast":
                    print(f"  ROAST  -> {event.get('text')}")
                elif kind == "event" and event.get("event") in {
                    "session_start", "calibrated", "good", "session_end",
                }:
                    print(f"  [{event.get('event')}] state={event.get('state')}")
                event = coach.broker.drain(queue, timeout=0)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            cv2.destroyAllWindows()
        except Exception:
            pass
        coach.stop()
    return 0


def test_voice_command(text: str) -> int:
    """Voice one line using the configured provider order, then play it."""
    from server.settings import Settings
    from voice.eleven_tts import ElevenLabsTTS
    from voice.tts_local import LocalTTS

    mode = str(Settings().get("tts_mode") or "auto").lower()
    eleven, local = ElevenLabsTTS(), LocalTTS()
    out = config.ROAST_DIR / "voice-test.wav"

    print(f"  voice mode   : {mode}")
    if eleven.available:
        print(f"  ElevenLabs   : key found (voice {eleven.voice_name})")
    else:
        print("  ElevenLabs   : no ELEVENLABS_API_KEY")
    print(f"  local engine : {local.engine or 'none'}")
    print(f"  line         : {text!r}\n")

    # Same order RoastService uses, so this tests what the app will actually do.
    attempts = []
    if mode in {"auto", "elevenlabs"}:
        attempts.append(("ElevenLabs", lambda: eleven.synthesize(text, out, "medium")))
    if mode in {"auto", "local"}:
        attempts.append(("local", lambda: local.synthesize(text, out)))

    for name, run in attempts:
        if run():
            engine = eleven.voice_name if name == "ElevenLabs" else local.engine
            print(f"  spoke with   : {name} ({engine})")
            print(f"  wrote        : {out}  ({out.stat().st_size} bytes)")
            _play(out)
            return 0
        detail = eleven.last_error if name == "ElevenLabs" else ""
        print(f"  {name:11s} failed" + (f": {str(detail)[:160]}" if detail else ""))

    print("  nothing could synthesize audio: add ELEVENLABS_API_KEY or install espeak-ng/piper")
    return 1


def _play(path) -> None:
    from voice.player import AudioPlayer

    player = AudioPlayer(route="bluetooth")
    if player.play_file(path):
        print("  played       : yes")
    else:
        print(f"  played       : no audio player found - open it yourself: {path}")


def timers_command(url: str) -> int:
    """Live countdown to the next roast, polled from a running server.

    Shows the state, the score, how long the current slouch has lasted, and how
    long until a roast fires — plus what is holding it back, which is the part
    that is otherwise invisible.
    """
    import json
    import urllib.request

    base = url.rstrip("/")
    print(f"  watching {base}/api/stats   (Ctrl+C to stop)")
    print("  " + "-" * 78)
    try:
        while True:
            try:
                with urllib.request.urlopen(f"{base}/api/stats", timeout=3) as response:
                    data = json.loads(response.read().decode("utf-8"))
            except Exception as exc:  # noqa: BLE001 - a debug tool should not crash
                print(f"\r  cannot reach {base}: {str(exc)[:56]:<56}", end="", flush=True)
                time.sleep(1.0)
                continue

            live = data.get("live") or {}
            reading = data.get("reading") or {}
            next_roast = live.get("next_roast") or {}
            score = reading.get("score")
            score_text = f"{score:.2f}" if isinstance(score, (int, float)) else " -- "
            part = str(reading.get("slouch_part") or "-")
            slouched = float(live.get("slouch_seconds") or 0.0)

            if next_roast.get("seconds") is None:
                countdown = "not slouching"
            else:
                countdown = f"roast in {next_roast['seconds']:5.1f}s"
                if next_roast.get("blocked_by"):
                    countdown += f"  (held by {next_roast['blocked_by']})"
                else:
                    countdown += f"  ({next_roast.get('trigger')})"

            print(
                f"\r  {str(data.get('state') or '?'):<12} score {score_text}  part {part:<6}"
                f"  slouched {slouched:5.1f}s   {countdown:<46}",
                end="",
                flush=True,
            )
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    config.load_env()
    config.ensure_dirs()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    print(BANNER)

    if args.list_cameras:
        return list_cameras_command(args)

    if args.test_voice is not None:
        return test_voice_command(args.test_voice)

    if args.timers is not None:
        return timers_command(args.timers)

    app = create_app(
        simulate=args.simulate,
        camera=args.camera,
        video=args.video,
        camera_backend=args.camera_backend,
        model=args.model,
        no_audio=args.no_audio,
        host=args.host,
        port=args.port,
        settings_path=args.settings,
        mobile=args.mobile,
        mobile_max_people=args.mobile_people,
        settings_overrides=(
            {
                "roast_seconds": 10,
                "cooldown_seconds": 15,
                "calibration_seconds": 3,
                "roast_pool_size": 2,
            }
            if args.demo
            else None
        ),
    )
    coach = app.coach
    if args.demo:
        print("  demo   : roast 10s / cooldown 15s (not saved unless you press Save)")

    if coach.mobile_mode:
        mode = f"MOBILE - up to {coach.mobile_max_people} people, camera in the browser"
    elif coach.simulate:        mode = "SIMULATED feed"
    elif args.video is not None:
        mode = f"video file: {Path(args.video).name}"
    else:
        mode = f"camera {coach.camera_index} (side view)"
    print(f"  mode   : {mode}")
    print(f"  audio  : {coach.player.route}")
    if coach.mobile_mode:
        secure = _tls_available()
        for line in mobile_urls(args.port, secure):
            print(line)
    if not coach.simulate and args.video is None and not coach.mobile_mode:
        print(SIDE_VIEW_HINT)

    if args.preview:
        return preview_loop(coach)

    if not coach.mobile_mode:
        print(f"  website: http://localhost:{args.port}  (http://posture.local:{args.port} on the LAN)")
    print("  press Ctrl+C to stop\n")

    coach.start()
    try:
        # The reloader is off on purpose: it starts the app twice and the second
        # copy cannot open the camera.
        app.run(
            host=args.host,
            port=args.port,
            threaded=True,
            debug=False,
            use_reloader=False,
            ssl_context=("adhoc" if coach.mobile_mode and _tls_available() else None),
        )
    except KeyboardInterrupt:
        pass
    finally:
        coach.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
