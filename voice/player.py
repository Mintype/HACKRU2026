"""Audio playback to the Nest.

Primary route: Bluetooth Classic (the Pi appears as a phone to the Nest).
Backup route: Wi-Fi cast via ``pychromecast``, served by Flask.

Clips are played one at a time through a size-1 queue: if something is already
playing, the new clip is skipped rather than stacked.
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote

log = logging.getLogger(__name__)


class AudioPlayer(threading.Thread):
    def __init__(
        self,
        *,
        route: str = "bluetooth",
        host: str = "127.0.0.1",
        port: int = 5000,
        bluetooth_mac: Optional[str] = None,
        cast_name: Optional[str] = None,
        device: Optional[str] = None,
        no_audio: bool = False,
        log_fn=log.info,
    ):
        super().__init__(name="audio-player", daemon=True)
        self.route = "off" if no_audio else (route or "bluetooth")
        self.host = host
        self.port = int(port)
        self.bluetooth_mac = bluetooth_mac or os.environ.get("NEST_BLUETOOTH_MAC") or None
        self.cast_name = cast_name or os.environ.get("NEST_CAST_NAME") or None
        self.device = device or os.environ.get("POSTURE_BLUETOOTH_DEVICE") or None

        self._queue: "queue.Queue[Optional[object]]" = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._bt_checked_at = 0.0
        self._bt_connected = False
        self._playing = False

        self.play_count = 0
        self.skipped = 0
        self.failures = 0
        self.dropped_no_audio = 0
        self.last_text: Optional[str] = None
        self.last_route_used: Optional[str] = None
        self.last_played_at: Optional[float] = None
        self.log = log_fn

    # -- public API -------------------------------------------------------
    def play_clip(self, clip) -> bool:
        """Queue a clip.  Returns ``False`` if it was skipped (busy)."""
        if clip is None or not getattr(clip, "text", None):
            return False
        if self.route == "off" or getattr(clip, "path", None) is None:
            # No audio device / no audio file: still count it as delivered so
            # the caption and stats work on a laptop.
            self._record(clip.text, "silent")
            return True
        try:
            self._queue.put_nowait(clip)
        except queue.Full:
            self.skipped += 1
            self.log(f"[audio] busy, skipped: {clip.text}")
            return False
        return True

    def set_route(self, route: str) -> None:
        if route in {"bluetooth", "cast", "off"}:
            self.route = route
            self.log(f"[audio] route -> {route}")

    def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    def status(self) -> Dict:
        return {
            "route": self.route,
            "busy": self._queue.qsize() > 0 or self._playing,
            "bluetooth_connected": self.bluetooth_connected(),
            "bluetooth_mac": self.bluetooth_mac,
            "cast_name": self.cast_name,
            "play_count": self.play_count,
            "skipped": self.skipped,
            "failures": self.failures,
            "last_text": self.last_text,
            "last_route_used": self.last_route_used,
            "last_played_at": self.last_played_at,
        }

    # -- worker -----------------------------------------------------------
    def run(self) -> None:
        while not self._stop.is_set():
            try:
                clip = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if clip is None:
                break
            self._playing = True
            try:
                self._play(clip)
            finally:
                self._playing = False

    def _play(self, clip) -> None:
        path = getattr(clip, "path", None)
        ok = False
        if self.route == "bluetooth":
            ok = self._play_bluetooth(path)
            if not ok and self.cast_name:
                self.log("[audio] Bluetooth failed, trying Wi-Fi cast backup")
                ok = self._play_cast(path)
                if ok:
                    self._record(clip.text, "cast(backup)")
                    return
        elif self.route == "cast":
            ok = self._play_cast(path)
        if not ok:
            self.failures += 1
            self.log(f"[audio] could not play clip: {clip.text}")
            self._record(clip.text, self.route + "(failed)")
            return
        self._record(clip.text, self.route)

    def _record(self, text: str, route: str) -> None:
        self.last_text = text
        self.last_route_used = route
        self.last_played_at = time.time()
        self.play_count += 1
        self.log(f"[audio:{route}] {text}")

    # -- bluetooth --------------------------------------------------------
    def _player_command(self) -> Optional[List[str]]:
        override = os.environ.get("POSTURE_BLUETOOTH_PLAYER")
        if override:
            return override.split()
        for name, args in (
            ("pw-play", []),
            ("paplay", []),
            ("aplay", ["-q"]),
        ):
            if shutil.which(name):
                return [name] + args
        if shutil.which("ffplay"):
            return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]
        return None

    def _play_bluetooth(self, path: Optional[str]) -> bool:
        command = self._player_command()
        if not command or not path or not Path(path).exists():
            return False
        self.ensure_bluetooth()
        try:
            result = subprocess.run(command + [str(path)], capture_output=True, timeout=45)
            return result.returncode == 0
        except Exception as exc:
            log.warning("bluetooth playback failed: %s", exc)
            return False

    def ensure_bluetooth(self, force: bool = False) -> bool:
        """Reconnect before each clip: the link can drop when idle."""
        if not self.bluetooth_mac or not shutil.which("bluetoothctl"):
            return False
        now = time.time()
        if not force and (now - self._bt_checked_at) < 5.0:
            return self._bt_connected
        self._bt_checked_at = now
        try:
            info = subprocess.run(
                ["bluetoothctl", "info", self.bluetooth_mac],
                capture_output=True,
                text=True,
                timeout=8,
            ).stdout
            self._bt_connected = "Connected: yes" in info
            if not self._bt_connected:
                self.log(f"[audio] reconnecting Bluetooth {self.bluetooth_mac}")
                subprocess.run(
                    ["bluetoothctl", "connect", self.bluetooth_mac],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                info = subprocess.run(
                    ["bluetoothctl", "info", self.bluetooth_mac],
                    capture_output=True,
                    text=True,
                    timeout=8,
                ).stdout
                self._bt_connected = "Connected: yes" in info
        except Exception as exc:
            log.warning("bluetoothctl check failed: %s", exc)
            self._bt_connected = False
        return self._bt_connected

    def bluetooth_connected(self) -> bool:
        if not self.bluetooth_mac or not shutil.which("bluetoothctl"):
            return False
        return self.ensure_bluetooth()

    # -- wifi cast --------------------------------------------------------
    def _play_cast(self, path: Optional[str]) -> bool:
        if not path:
            return False
        try:
            import pychromecast
        except Exception as exc:
            log.warning("pychromecast not installed: %s", exc)
            return False
        try:
            if not self.cast_name:
                return False
            chromecasts, browser = pychromecast.get_chromecasts(timeout=6)
            match = next(
                (c for c in chromecasts if self.cast_name.lower() in (c.name or "").lower()),
                None,
            )
            if match is None:
                pychromecast.discovery.stop_discovery(browser)
                self.log(f"[audio] cast device {self.cast_name!r} not found")
                return False
            match.wait(timeout=8)
            url = f"http://{self.host}:{self.port}/media/roasts/{quote(Path(path).name)}"
            match.media_controller.play_media(url, "audio/wav")
            match.media_controller.block_until_active(timeout=12)
            pychromecast.discovery.stop_discovery(browser)
            return True
        except Exception as exc:
            log.warning("cast playback failed: %s", exc)
            return False
