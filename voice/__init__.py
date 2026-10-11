"""Voice layer: Gemini roasts + TTS, a pre-generated clip pool, and playback.

Gemini is deliberately kept out of the real-time path: roasts are written and
voiced *ahead* of time into a pool, so a slouch gets audio in about a second.
"""

from .pool import Clip, RoastPool
from .roasts import TIERS, fallback_batch, fallback_one, tier_for

__all__ = ["Clip", "RoastPool", "TIERS", "fallback_batch", "fallback_one", "tier_for"]
