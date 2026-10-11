#!/usr/bin/env python3
"""Report which voice providers Posture Coach can use, and optionally test them.

By default this spends nothing: it only reports what is configured. Pass --live to
make one real request per provider (that consumes ElevenLabs characters and Gemini
requests).

The Gemini probe calls each model **directly**, bypassing the fallback chain. Going
through `synthesize()` would report success whenever *any* model works, which hides
exactly the thing you are trying to find out.

Usage
    python probe_tts.py
    python probe_tts.py --live
    python probe_tts.py --live --text "Sit up straight."
"""

from __future__ import annotations

import argparse

import sys
import tempfile
from pathlib import Path


def project_root() -> Path:
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        if (parent / "run.py").exists() and (parent / "voice").is_dir():
            return parent
    raise SystemExit("could not locate the project root (expected run.py + voice/)")


ROOT = project_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from server.settings import Settings  # noqa: E402
from voice.eleven_tts import ElevenLabsTTS  # noqa: E402
from voice.gemini_text import RoastWriter  # noqa: E402
from voice.tts_local import LocalTTS  # noqa: E402

DEFAULT_TEXT = "Sit up. Your spine has filed a complaint."


def describe() -> int:
    config.load_env()
    settings = Settings()
    eleven = ElevenLabsTTS()
    local = LocalTTS()
    writer = RoastWriter()

    print("configuration (no requests made)")
    print("-" * 78)
    print(f"  tts_mode          : {settings.get('tts_mode')}")
    print(f"  ElevenLabs key    : {'yes' if eleven.available else 'NO'}")
    print(f"    voice / model   : {eleven.voice_name} ({eleven.voice_id}) / {eleven.model}")
    print(f"    output format   : {eleven.output_format}")
    print(f"  Gemini text key   : {'yes' if writer.available else 'NO'}")
    print(f"    preferred model : {writer.model}")
    print(f"    model chain     : {', '.join(writer.models())}")
    print(f"  local engine      : {local.engine or 'none'} (voice {local.voice})")
    print("-" * 78)
    print("  auto order: ElevenLabs -> local.  Roasts are written by Gemini.")
    return 0


def probe(text: str) -> int:
    config.load_env()
    eleven = ElevenLabsTTS()
    local = LocalTTS()

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "probe.wav"

        print(f"ElevenLabs ({eleven.voice_name} / {eleven.model})")
        if not eleven.available:
            print("  no ELEVENLABS_API_KEY, skipping")
        elif eleven.synthesize(text, out, "mild"):
            print(f"  OK      {out.stat().st_size:,} bytes")
            out.unlink()
        else:
            print(f"  FAILED  {eleven.last_error}")

        print("\nlocal engine")
        if not local.available:
            print(f"  none available (engine={local.engine})")
        elif local.synthesize(text, out):
            print(f"  OK      {out.stat().st_size:,} bytes via {local.engine}")
        else:
            print("  FAILED")

        print("\nGemini text (a tiny batch, to prove the chef can actually write)")
        writer = RoastWriter()
        if not writer.available:
            print("  no GEMINI_API_KEY, skipping")
        else:
            items = writer.generate(count=2, tier="mild")
            print(f"  wrote with {writer.model_used or 'offline fallbacks'}")
            for item in items:
                print(f"    - {item['text']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--live", action="store_true", help="make real API requests (costs quota)")
    parser.add_argument("--text", default=DEFAULT_TEXT, help="line to synthesize when probing")
    args = parser.parse_args(argv)
    return probe(args.text) if args.live else describe()


if __name__ == "__main__":
    raise SystemExit(main())
