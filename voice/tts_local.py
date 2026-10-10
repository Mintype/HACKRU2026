"""Local, offline text-to-speech fallback.

Tries, in order: espeak-ng, espeak, piper, macOS ``say`` and Windows SAPI.
Lower quality than Gemini, but it means a slouch still gets a roast with the
internet unplugged.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)


class LocalTTS:
    def __init__(self, voice: str = "en", rate: int = 165):
        self.voice = voice
        self.rate = rate
        self.engine: Optional[str] = self._detect()

    def _detect(self) -> Optional[str]:
        if os.environ.get("POSTURE_DISABLE_LOCAL_TTS", "").strip().lower() in {"1", "true", "yes", "on"}:
            return None
        for name in ("espeak-ng", "espeak"):
            if shutil.which(name):
                return name
        if shutil.which("piper"):
            return "piper"
        if sys.platform == "darwin" and shutil.which("say"):
            return "say"
        if sys.platform.startswith("win") and shutil.which("powershell"):
            return "sapi"
        return None

    @property
    def available(self) -> bool:
        return self.engine is not None

    def synthesize(self, text: str, out_path: str | Path) -> bool:
        if not text.strip() or not self.engine:
            return False
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            command = self._command(text, path)
            if not command:
                return False
            stdin = text.encode("utf-8") if self.engine == "piper" else None
            subprocess.run(command, input=stdin, check=True, capture_output=True, timeout=60)
            if path.exists() and path.stat().st_size > 0:
                from . import wavutil

                wavutil.normalize(path)
                return True
        except Exception as exc:
            log.warning("local TTS (%s) failed: %s", self.engine, exc)
        return False

    def _command(self, text: str, path: Path) -> List[str]:
        if self.engine in {"espeak-ng", "espeak"}:
            return [self.engine, "-v", self.voice, "-s", str(self.rate), "-w", str(path), text]
        if self.engine == "piper":
            # piper reads text on stdin and writes a WAV; voice must be configured.
            return ["piper", "--output_file", str(path)]
        if self.engine == "say":
            return ["say", "-o", str(path), "--data-format=LEI16@22050", text]
        if self.engine == "sapi":
            script = (
                "Add-Type -AssemblyName System.Speech; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                f"$s.SetOutputToWaveFile('{path}'); "
                f"$s.Speak({_ps_quote(text)}); $s.Dispose()"
            )
            return ["powershell", "-NoProfile", "-Command", script]
        return []


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"
