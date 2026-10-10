"""Hand-written roast + encouragement fallbacks.

Used when Gemini text generation is unavailable, and always available for a
fully offline demo.  Rules mirror the prompt: posture only, PG, short.
"""

from __future__ import annotations

import random
from typing import Dict, Iterable, List, Sequence

MILD = [
    "Sit up. Your future spine just sent a polite complaint.",
    "That posture is a suggestion, not a plan. Try again.",
    "Your chair called. It wants the real you back.",
    "Leaning in like the screen owes you money.",
    "Straighten up. You are not a question mark.",
    "The camera sees the slouch. The slouch sees everything.",
    "You were upright two minutes ago. It suited you.",
    "Slouch number {n}. The chair is keeping a list.",
    "Small lean, big tell. Sit tall.",
    "Your posture just left the group chat.",
]

MEDIUM = [
    "Slouch number {n}. Your spine has filed a complaint.",
    "That hunch has its own weather system. Sit up.",
    "You are folding like a lawn chair. Unfold.",
    "Your vertebrae would like a word. Preferably while upright.",
    "Posture check failed. Retake available immediately.",
    "Slouch {n}. The chair is drafting a resignation letter.",
    "You are auditioning for the role of Semicolon.",
    "Your back is writing a memoir titled 'Bent'. Edit it.",
    "Sit up before the desk claims you as furniture.",
    "The Nest has opinions and the first one is: sit tall.",
]

SPICY = [
    "Slouch number {n}. Your future self is watching and cringing.",
    "Your spine has entered its noodle era. Exit immediately.",
    "That posture is a group project and everyone is failing.",
    "Sit up. Your skeleton is not a suggestion box.",
    "You are one hunch away from becoming a croissant.",
    "Slouch {n}. The chiropractor just bought a boat.",
    "Your posture called. It quit. It left a note: 'stand tall'.",
    "If slouching were an olympic sport, you would still lose. Sit up.",
]

NICE = [
    "Great posture! Your spine is thriving.",
    "Nice and tall. Keep that up.",
    "You are sitting like you own the place. Excellent.",
    "Straight spine, sharp mind. Well done.",
    "That is the posture of someone who wins hackathons.",
    "Lovely alignment. Your future self says thanks.",
    "Sitting tall. The Nest approves quietly.",
    "Great catch. Realignment looks good on you.",
]

POOLS: Dict[str, List[str]] = {"mild": MILD, "medium": MEDIUM, "spicy": SPICY, "nice": NICE}
TIERS = ("mild", "medium", "spicy")


def tier_for(slouch_count: int) -> str:
    """Escalate in tiers: mild teasing first, dramatic for repeat offenders."""
    count = int(slouch_count or 1)
    if count <= 3:
        return "mild"
    if count <= 6:
        return "medium"
    return "spicy"


def tier_pool(tier: str, nice: bool = False) -> List[str]:
    if nice:
        return list(NICE)
    return list(POOLS.get(tier, MILD))


def fallback_batch(
    *,
    count: int,
    tier: str = "mild",
    nice: bool = False,
    exclude: Iterable[str] = (),
    rng: random.Random | None = None,
) -> List[Dict[str, str]]:
    """Return up to ``count`` fallback roasts, avoiding recent ones."""
    rng = rng or random
    used = {t.strip().lower() for t in exclude}
    pool = [line for line in tier_pool(tier, nice) if line.lower() not in used]
    if not pool:
        pool = tier_pool(tier, nice)
    rng.shuffle(pool)
    out: List[Dict[str, str]] = []
    for line in pool[: max(0, int(count))]:
        out.append({"text": line, "tier": "nice" if nice else tier, "source": "fallback"})
    return out


def fallback_one(*, tier: str = "mild", nice: bool = False, exclude: Sequence[str] = ()) -> str:
    batch = fallback_batch(count=1, tier=tier, nice=nice, exclude=exclude)
    if batch:
        return batch[0]["text"]
    return "Sit up straight, please."
