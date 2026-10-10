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
    "The chair is keeping a list, and you are on it.",
    "You were upright a minute ago. It suited you.",
    "Sit up. Your chair has opinions now.",
    "That lean is running a full marketing campaign.",
    "Posture: pending. Attitude: hopeful.",
    "You are slowly becoming a comma.",
    "Sit up. The whole room can see you.",
    "That slouch was a rumour. It is now true.",
    "Straighten up. Gravity is winning the argument.",
    "Your shoulders called. They want to come back.",
    "Sit up. This is a chair, not a hammock.",
    "You drifted. The chair did not.",
    "That posture is RAW. Sit up.",
    "You are plating a slump. Start again.",
    "That is undercooked. Straighten up.",
    "Where is the seasoning? Where is the spine?",
    "This dish is a question mark. Fix it.",
]

MEDIUM = [
    "Your spine just filed a complaint.",
    "You are one hunch away from becoming furniture.",
    "Sit up. Your vertebrae are unionising.",
    "That posture has a sequel. Do not make it.",
    "You are writing a memoir called Bent. Stop.",
    "The chair is drafting its resignation letter.",
    "You are bent like a comma and twice as dramatic.",
    "Sit up. Your skeleton is not a suggestion box.",
    "That is not sitting, that is surrendering to gravity.",
    "The desk says you live there now.",
    "Your spine has started a support group.",
    "Sit up before your back claims squatter's rights.",
    "You are the reason ergonomics was invented.",
    "That hunch has a postcode now.",
    "Your posture is on a downward trend line.",
    "Sit up. The chiropractor is warming up.",
    "You are folded like a letter nobody wants.",
    "Even the chair is leaning away from you.",
    "That is a slump with a business plan.",
    "Sit up before your ribs meet your hips.",
    "That slouch is a soggy bottom. Unacceptable.",
    "You have overcooked the spine and undercooked the point.",
    "This plate is a crime against the chair.",
    "That is not sitting, that is a collapsed souffle.",
    "You are folding like badly rolled pastry.",
]

SPICY = [
    "Your future self is watching this in horror.",
    "Your spine has entered its noodle era. Leave it.",
    "Sit up. The chiropractor just bought a boat.",
    "You are auditioning to be a croissant. You got the part.",
    "That is not posture, that is a resignation letter.",
    "Your back is writing cheques your discs cannot cash.",
    "Even the chair looks disappointed.",
    "Sit up before your spine starts charging rent.",
    "You have become a speed bump with a laptop.",
    "Your posture called. It quit.",
    "Your vertebrae are drafting a hostile takeover.",
    "Sit up. Your spine is considering early retirement.",
    "That slouch is now a load-bearing structure.",
    "Your back has filed for independence.",
    "Sit up or your discs will unionise and strike.",
    "You are folding in real time. Impressive. Stop.",
    "Your spine has left the building.",
    "Sit up before the desk files a missing person report.",
    "That hunch has tenure now.",
    "Your posture is a cautionary tale with a keyboard.",
    "Your spine is a raw chicken. Sit up.",
    "That posture would not pass the pass. Out of my kitchen.",
    "You are one degree from being sent back to the kitchen.",
    "Today's special is disappointment, and you are plating it.",
    "This dish is burnt, and so is your posture.",
]

#: Aimed at whichever part is actually giving way, mixed into the batch.
NECK = [
    "Your head is running a forward project.",
    "Chin back. That is not a diving board.",
    "Your neck is auditioning for a longer life.",
    "Head forward again. Neck optional, apparently.",
    "Sit up. Your head is ahead of schedule.",
    "That neck is leading the parade.",
    "Your chin is doing the work of a keel.",
    "Head back. Your neck has somewhere to be.",
    "You are peering like the screen owes you money.",
    "Your head has left the rest of you behind.",
    "Chin back. Your neck is overproofed dough.",
    "Sit up. Your neck is a limp noodle.",
    "That head is jutting like an unplated garnish.",
]

TORSO = [
    "Your back is doing a prawn impression.",
    "Straighten the spine, not just the story.",
    "That hunch is load-bearing now.",
    "Shoulders back. The hunch has had its turn.",
    "Your ribcage is migrating toward the desk.",
    "Sit tall. Your torso is not a beanbag.",
    "That back has folded like a beach chair.",
    "Your spine is doing an impression of a banana.",
    "Sit up. Your ribs deserve better than this.",
    "Your back wants a better chair, not a rescue.",
    "Straighten up. Your spine is a collapsed souffle.",
    "That back is a folded napkin, not a spine.",
    "Your torso is slumping like a bad custard.",
]

#: Phone use is its own fault: these are picked when the head is down.
PHONE = [
    "Put the phone down. That is not the dish I ordered.",
    "Look up. Whatever is on that screen is undercooked.",
    "That is a phone, not a garnish. Look at the room.",
    "Your neck is paying for that screen, and it is RAW.",
    "You are folded over a rectangle again. Badly plated.",
    "Heads up. Literally. That plate is going cold.",
    "The phone won this round. Your posture is burnt.",
    "Your phone has seen enough of your face for one day.",
    "Sit up and look at the room, not your lap.",
    "Your screen is not going anywhere. Neither are you, apparently.",
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
    "That spine is doing honest work.",
    "Sitting tall suits you. Stay there.",
    "Beautiful alignment. Keep breathing.",
    "Your back just sent a thank you note.",
    "That is a posture worth keeping.",
    "Strong, tall, and quietly excellent.",
    "Finally, something I would serve.",
    "That is a dish with a spine. Beautiful.",
    "Now that is cooked to perfection.",
    "Seasoned, aligned, and ready to serve.",
]

#: A fresh angle per batch, so consecutive roasts do not sound the same.
TOPICS = (
    "Write from the point of view of the chair they are sitting on.",
    "Write from the point of view of their spine.",
    "Blame physics, gravity and structural engineering.",
    "Write as their future self, looking back on this exact moment.",
    "Write as a nature documentary narrator describing a strange creature.",
    "Write as a sports commentator calling the play.",
    "Write as a chiropractor sending an invoice.",
    "Write as an office furniture catalogue trying to sell them a better chair.",
    "Compare their posture to an everyday object that bends or folds.",
    "Write as a health-and-safety inspector filing a report.",
    "Write as their back's warranty department voiding the policy.",
    "Write as a personal trainer who has given up on small talk.",
    "Write about the desk slowly claiming them as its own.",
    "Write as a building inspector condemning a structure.",
    "Write as a playwright describing a tragedy in one line.",
    "Write as the camera, which has seen all of this before.",
    "Write about the phone or screen they are staring at.",
    "Write as a furious chef inspecting a badly plated dish.",
    "Compare the posture to a badly cooked piece of food.",
    "Compare the spine to an overcooked noodle.",
    "Write as a chef sending the dish back to the kitchen.",
    "Compare the slouch to a collapsed souffle.",
    "Write as a chef who has just tasted something awful.",
    "Compare the posture to the soggy bottom of a pie.",
)

POOLS: Dict[str, List[str]] = {"mild": MILD, "medium": MEDIUM, "spicy": SPICY, "nice": NICE}
PART_LINES: Dict[str, List[str]] = {"neck": NECK, "torso": TORSO}
FAULT_LINES: Dict[str, List[str]] = {"phone": PHONE}
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


def pick_topic(rng: random.Random | None = None) -> str:
    """One writing angle for a batch, so roasts do not all look alike."""
    rng = rng or random
    return rng.choice(TOPICS)


def fallback_batch(
    *,
    count: int,
    tier: str = "mild",
    nice: bool = False,
    exclude: Iterable[str] = (),
    part: str | None = None,
    fault: str | None = None,
    rng: random.Random | None = None,
) -> List[Dict[str, str]]:
    """Return up to ``count`` fallback lines, avoiding recent ones.

    ``part`` ("neck"/"torso") aims part of the batch at whatever is actually
    giving way, and ``fault`` ("phone") overrides it when the problem is not the
    posture at all.
    """
    rng = rng or random
    count = max(0, int(count))
    used = {t.strip().lower() for t in exclude}

    def fresh(lines: List[str]) -> List[str]:
        keep = [line for line in lines if line.lower() not in used]
        return keep or list(lines)

    chosen: List[str] = []
    aimed_lines = FAULT_LINES.get(fault) if fault else None
    if aimed_lines is None and part:
        aimed_lines = PART_LINES.get(part)
    if not nice and aimed_lines:
        aimed = fresh(aimed_lines)
        rng.shuffle(aimed)
        chosen = aimed[: max(1, count // 2)]

    tier_lines = fresh(tier_pool(tier, nice))
    rng.shuffle(tier_lines)
    for line in tier_lines:
        if len(chosen) >= count:
            break
        if line not in chosen:
            chosen.append(line)

    return [
        {"text": line, "tier": "nice" if nice else tier, "source": "fallback"}
        for line in chosen[:count]
    ]


def fallback_one(
    *,
    tier: str = "mild",
    nice: bool = False,
    exclude: Sequence[str] = (),
    part: str | None = None,
    fault: str | None = None,
) -> str:
    batch = fallback_batch(count=1, tier=tier, nice=nice, exclude=exclude, part=part, fault=fault)
    if batch:
        return batch[0]["text"]
    return "Sit up straight, please."
