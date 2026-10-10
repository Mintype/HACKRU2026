"""Test suite for Posture Coach. Run with:  python -m unittest discover -s tests

**Offline by policy.** The suite must never talk to Gemini or ElevenLabs: it would
be slow, non-deterministic and might spend real quota. Importing this package
therefore blanks the API keys, and does it by *setting them empty* rather than
deleting them, because ``config.load_env()`` uses ``override=False`` and
``os.environ.setdefault`` — so a test that loads ``.env`` cannot smuggle the real
keys back in and start spending money behind everyone's back.

Tests that want a key pass one explicitly (``RoastWriter(api_key="fake-key")``).
"""

import os

for _key in ("GEMINI_API_KEY", "ELEVENLABS_API_KEY"):
    os.environ[_key] = ""
