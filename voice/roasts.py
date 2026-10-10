"""Hand-written roasts and encouragement.

Used when Gemini text generation is unavailable, and always available for a
fully offline demo.  Rules mirror the prompt files in ``voice/prompts/``:
roast the *posture* only, PG, one breath, and actually funny.

``{n}`` is replaced with the slouch number for the session by
:class:`voice.service.RoastService`.
"""

from __future__ import annotations

import random
from typing import Dict, Iterable, List, Sequence

MILD = [
    "Sit up. You are not a question mark.",
    "That is a slouch. Even the chair noticed.",
    "You are folding like a cheap lawn chair.",
    "Spine check: failed. Retake it now.",
    "Straighten up. The camera is judging you.",
    "You are leaning like the WiFi dropped.",
    "Sit up before the desk adopts you.",
    "That hunch is doing numbers. Bad ones.",
    "Slouch {n}. The chair is keeping a list.",
    "You were upright a minute ago. It suited you.",
]

MEDIUM = [
    "Slouch {n}. Your spine just filed a complaint.",
    "You are one hunch away from becoming furniture.",
    "Sit up. Your vertebrae are unionising.",
    "That posture has a sequel. Do not make it.",
    "You are writing a memoir called Bent. Stop.",
    "The chair is drafting its resignation letter.",
    "You are bent like a comma and twice as dramatic.",
    "Sit up. Your skeleton is not a suggestion box.",
    "That is not sitting, that is surrendering to gravity.",
    "Slouch {n}. The desk says you live there now.",
]

SPICY = [
    "Slouch {n}. Your future self is watching in horror.",
    "Your spine has entered its noodle era. Leave it.",
    "Sit up. The chiropractor just bought a boat.",
    "You are auditioning to be a croissant. You got the part.",
    "That is not posture, that is a resignation letter.",
    "Your back is writing cheques your discs cannot cash.",
    "Slouch {n}. Even the chair looks disappointed.",
    "Sit up before your spine starts charging rent.",
    "You have become a speed bump with a laptop.",
    "Slouch {n}. Your posture called. It quit.",
]

NICE = [
    "Sit tall. That is the good stuff.",
    "Spine aligned, mind sharp. Carry on.",
    "That is the posture of someone winning.",
    "Nice. Your future self says thanks.",
    "Upright and unstoppable. Keep it.",
    "Great posture. Hold exactly that.",
    "You are sitting like you mean it.",
    "Perfect. The chair is proud of you.",
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
    """Return up to ``count`` fallback lines, avoiding recent ones."""
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
