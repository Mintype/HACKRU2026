"""Gemini text generation for roast / encouragement batches (structured JSON).

The prompt wording lives in editable text files rather than in this module:

    voice/prompts/roast.txt   normal mode — actually insulting, still PG
    voice/prompts/nice.txt    nice mode — genuine encouragement

Both are templates with ``{{TOKEN}}`` placeholders (``{{CONTEXT}}``,
``{{EXCLUDE}}``, ``{{COUNT}}``, ``{{TIER}}``, ``{{TIER_BRIEF}}``).  Plain
``str.replace`` is used rather than ``str.format`` so the JSON example braces in
the templates stay literal.  If a file is missing or empty the built-in
constants below are used, so the app can never break on a deleted file.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .roasts import fallback_batch, pick_topic

log = logging.getLogger(__name__)

#: Drop generated lines longer than this. The prompt asks for 3-9 words; this is
#: the slack allowed before a line is thrown away as a rambling one.
MAX_WORDS = 11

#: How long to skip a text model that failed because it is gone or out of quota.
MODEL_SKIP_SECONDS = 6 * 3600

#: Text models in preference order. They get retired (gemini-2.5-flash answers
#: 404 "no longer available to new users" for newer accounts), so the chain is the
#: safety net: if the configured one is gone or rate-limited, the next answers.
DEFAULT_TEXT_MODEL = "gemini-3.8-flash"
FALLBACK_TEXT_MODELS = (
    "gemini-3.8-flash",
    "gemini-flash-latest",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-2.5-flash",
)


def is_quota_error(message: str) -> bool:
    text = (message or "").upper()
    return "RESOURCE_EXHAUSTED" in text or "429" in text or "QUOTA" in text


def is_model_gone(message: str) -> bool:
    text = (message or "").upper()
    return "NOT_FOUND" in text or "404" in text or "NO LONGER AVAILABLE" in text


def model_backoff_seconds(message: str, default: float = 3600.0) -> float:
    """How long to skip this model: the quota retry hint, or a long rest if gone."""
    if is_model_gone(message):
        return MODEL_SKIP_SECONDS
    if is_quota_error(message):
        match = re.search(r"retryDelay'?\s*:\s*'?(\d+)s", message or "")
        if match:
            return min(float(match.group(1)), MODEL_SKIP_SECONDS)
        match = re.search(r"retry in (\d+)h(\d+)m", message or "")
        if match:
            return min(int(match.group(1)) * 3600 + int(match.group(2)) * 60, MODEL_SKIP_SECONDS)
        return min(default, MODEL_SKIP_SECONDS)
    return 60.0

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
PROMPT_FILES = {False: "roast.txt", True: "nice.txt"}

# Fallbacks, kept deliberately close to the files above.
DEFAULT_ROAST_PROMPT = """\
You write one-line insults for a webcam that watches how someone sits. When they
slouch, a speaker in the room says your line out loud, in front of other people.

YOU ARE THE CHEF
A furious, world-famous chef, mid-service, and the dish is their posture. It is
RAW. Furious about the plate, never cruel about the person. Think a chef shouting
across a hot kitchen, not a posture pamphlet, not an HR memo.

Be genuinely funny and actually roast them. A polite reminder is a failure.
Name the part that is actually giving way, and land one sharp image when it earns
its place: food and kitchens are the house speciality, but a garnish rather than
the whole dish, and a plain furious line beats a forced food joke.

HARD RULES
- Attack the slouch, never the person: no appearance, body shape, weight, age,
  gender, race, identity, intelligence or ability. Ever.
- PG. No profanity, no slurs.
- SHORT: 3-9 words. One breath, one hit. No emoji, no quotation marks.
- Never explain the joke.

TIER FOR THIS BATCH
{{TIER}} — {{TIER_BRIEF}}

SESSION CONTEXT (use only if it makes the line funnier)
{{CONTEXT}}

ALREADY USED — do not reuse or paraphrase
{{EXCLUDE}}

OUTPUT
Return exactly {{COUNT}} lines, JSON only:
{"roasts":[{"text":"...","tier":"{{TIER}}"}]}
"""

DEFAULT_NICE_PROMPT = """\
You write one-line praise for a webcam that watches how someone sits. When they
sit up well, a speaker in the room says your line out loud.

Same chef, but the dish is perfect and you are delighted: warm, specific, a little
theatrical, never condescending.

Never comment on appearance, body, identity or ability. PG. Kind.
SHORT: 3-9 words. No emoji, no quotation marks. A food comparison now and then is
welcome; never force one.

SESSION CONTEXT (optional colour)
{{CONTEXT}}

ALREADY USED — do not reuse or paraphrase
{{EXCLUDE}}

OUTPUT
Return exactly {{COUNT}} lines, JSON only:
{"roasts":[{"text":"...","tier":"nice"}]}
"""

BLOCKLIST = {
    "fat",
    "ugly",
    "stupid",
    "idiot",
    "idiotic",
    "dumb",
    "moron",
    "loser",
    "hate",
    "kill",
    "die",
    "worthless",
    "disgusting",
    "gross",
    "pig",
    "cow",
    "whale",
    "lazy",
    "failure",
    "pathetic",
}

TIER_BRIEF = {
    "mild": "MILD — light teasing; they have only just sat down badly.",
    "medium": "MEDIUM — sharper and more theatrical; they keep doing it.",
    "spicy": "SPICY — repeat offender; commit fully, still PG.",
    "nice": "Encouragement, not insults. Warm, specific, never condescending.",
}


def load_prompt_template(nice: bool = False) -> str:
    """Read the prompt template for the mode, falling back to the built-in text."""
    path = PROMPTS_DIR / PROMPT_FILES[bool(nice)]
    try:
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        log.debug("prompt file %s missing; using built-in prompt", path)
    return DEFAULT_NICE_PROMPT if nice else DEFAULT_ROAST_PROMPT


def _context_lines(context: Dict) -> List[str]:
    lines: List[str] = []
    if context.get("slouch_count") is not None:
        lines.append(f"- This is slouch number {context['slouch_count']} this session.")
    if context.get("session_minutes") is not None:
        lines.append(f"- The session has been running about {context['session_minutes']} minutes.")

    part = context.get("slouch_part")
    if part == "neck":
        lines.append("- What is actually failing: the head is jutting forward off the neck.")
    elif part == "torso":
        lines.append("- What is actually failing: the upper back is hunching forward.")
    elif part == "even":
        lines.append("- What is actually failing: everything is folding forward at once.")

    if context.get("worst_neck_angle") is not None:
        lines.append(f"- Worst forward-head angle so far: {context['worst_neck_angle']} degrees from vertical.")
    if context.get("sleeping"):
        lines.append(
            "- IMPORTANT: they have nodded off - slumped, motionless, head down. "
            "The line must be about falling asleep at the desk and must tell them to wake up."
        )
    if context.get("person"):
        # Mobile mode watches several people at once, so the insult has to be
        # aimed: the phone speaks it aloud in a room with other people in it.
        lines.append(
            f"- You are talking about {context['person']} in a shared room. "
            "Address them by that name so everyone knows who it is for."
        )
    if context.get("metric"):
        lines.append(f"- What the camera measures on them right now: {context['metric']}.")
    if context.get("worst_torso_angle"):
        lines.append(f"- Worst torso angle so far: {context['worst_torso_angle']} degrees from vertical.")
    if context.get("neck_drift") is not None and context.get("torso_drift") is not None:
        lines.append(
            "- Drift past their own upright calibration right now: "
            f"neck {context['neck_drift']} deg, torso {context['torso_drift']} deg."
        )
    return lines


def build_prompt(
    *,
    count: int = 5,
    tier: str = "mild",
    nice: bool = False,
    context: Optional[Dict] = None,
    exclude: Sequence[str] = (),
    topic: Optional[str] = None,
) -> str:
    """Render the prompt template for this batch."""
    context = context or {}
    template = load_prompt_template(nice)
    tier_key = "nice" if nice else tier

    context_text = "\n".join(_context_lines(context)) or "- First slouch; no session history yet."
    exclude_text = "\n".join(f"- {line}" for line in list(exclude)[:40]) or "- (nothing yet)"

    return (
        template.replace("{{COUNT}}", str(int(count)))
        .replace("{{TIER}}", tier_key)
        .replace("{{TIER_BRIEF}}", TIER_BRIEF.get(tier_key, TIER_BRIEF["mild"]))
        .replace("{{CONTEXT}}", context_text)
        .replace("{{EXCLUDE}}", exclude_text)
        .replace("{{TOPIC}}", topic or pick_topic())
    )


def _strip_code_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    return text.strip()


def parse_roasts(raw: str, *, tier: str = "mild") -> List[Dict[str, str]]:
    """Parse a Gemini response into ``[{"text", "tier"}]`` (tolerant)."""
    text = _strip_code_fences(raw)
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        import re

        found = re.findall(r'"text"\s*:\s*"([^"]+)"', text)
        return [{"text": item, "tier": tier} for item in found]
    if isinstance(data, dict):
        items: Iterable = data.get("roasts") or data.get("lines") or []
    elif isinstance(data, list):
        items = data
    else:
        items = []
    out: List[Dict[str, str]] = []
    for item in items:
        if isinstance(item, str):
            out.append({"text": item, "tier": tier})
        elif isinstance(item, dict) and item.get("text"):
            out.append({"text": str(item["text"]), "tier": str(item.get("tier") or tier)})
    return out


def validate(items: Sequence[Dict], *, tier: str = "mild", exclude: Sequence[str] = ()) -> List[Dict[str, str]]:
    """Drop anything too long, empty, blocklisted or repeated."""
    seen = {t.strip().lower() for t in exclude}
    out: List[Dict[str, str]] = []
    for item in items:
        text = " ".join(str(item.get("text", "")).split())
        if not text:
            continue
        if len(text.split()) > MAX_WORDS:
            continue
        lower = text.lower()
        words = set(lower.replace(",", " ").replace(".", " ").replace("!", " ").split())
        if words & BLOCKLIST:
            continue
        if lower in seen:
            continue
        seen.add(lower)
        out.append({"text": text, "tier": str(item.get("tier") or tier)})
    return out


class RoastWriter:
    """Thin wrapper over ``google-genai`` with a graceful fallback."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        max_words: int = MAX_WORDS,
    ):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY") or ""
        #: Preferred model; the rest of FALLBACK_TEXT_MODELS is tried after it.
        self.model = model or os.environ.get("GEMINI_TEXT_MODEL") or DEFAULT_TEXT_MODEL
        self.max_words = max_words
        self._client = None
        self.last_error: Optional[str] = None
        #: model -> epoch until which it is skipped (retired, or out of quota)
        self._model_cooldown: Dict[str, float] = {}
        #: Which model actually answered last.
        self.model_used: Optional[str] = None

    def models(self) -> List[str]:
        """Preferred model first, then the known fallbacks."""
        seen: set = set()
        out: List[str] = []
        for model in [self.model, *FALLBACK_TEXT_MODELS]:
            if model and model not in seen:
                seen.add(model)
                out.append(model)
        return out

    def _cooling_down(self, model: str) -> bool:
        until = self._model_cooldown.get(model, 0.0)
        if not until:
            return False
        if time.time() >= until:
            self._model_cooldown.pop(model, None)
            return False
        return True

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _get_client(self):
        if self._client is None:
            from google import genai  # lazy: only needed when a key exists

            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def generate(
        self,
        *,
        count: int = 5,
        tier: str = "mild",
        context: Optional[Dict] = None,
        nice: bool = False,
        exclude: Sequence[str] = (),
    ) -> List[Dict[str, str]]:
        """Return validated lines, or hand-written fallbacks on any failure."""
        prompt = build_prompt(count=count, tier=tier, nice=nice, context=context, exclude=exclude)
        items: List[Dict[str, str]] = []
        if self.available:
            for model in self.models():
                if self._cooling_down(model):
                    continue
                try:
                    items = self._call(prompt, count, tier, nice, model)
                except Exception as exc:
                    message = str(exc)
                    delay = model_backoff_seconds(message)
                    self._model_cooldown[model] = time.time() + delay
                    self.last_error = message
                    log.warning(
                        "Gemini text model %s failed (%s); skipping it for %.0f min",
                        model,
                        message[:90],
                        delay / 60.0,
                    )
                    items = []
                    continue
                if not items:
                    # Answered but gave nothing usable; let the next model try.
                    self.last_error = "empty response"
                    self._model_cooldown[model] = time.time() + 60.0
                    continue
                if self.model_used != model:
                    log.info("Gemini is writing roasts with %s", model)
                self.model_used = model
                self.last_error = None
                break
        items = validate(items, tier="nice" if nice else tier, exclude=list(exclude))[:count]
        if len(items) < count:
            items += fallback_batch(
                count=count - len(items),
                tier=tier,
                nice=nice,
                exclude=list(exclude) + [i["text"] for i in items],
                part=(context or {}).get("slouch_part"),
                fault="sleeping" if (context or {}).get("sleeping") else None,
            )
        return items[:count]

    def _call(
        self, prompt: str, count: int, tier: str, nice: bool, model: Optional[str] = None
    ) -> List[Dict[str, str]]:
        from google.genai import types

        client = self._get_client()
        config_kwargs: Dict = {"response_mime_type": "application/json", "temperature": 1.1}
        schema = None
        try:
            schema = types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "roasts": types.Schema(
                        type=types.Type.ARRAY,
                        items=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "text": types.Schema(type=types.Type.STRING),
                                "tier": types.Schema(type=types.Type.STRING),
                            },
                            required=["text", "tier"],
                        ),
                    )
                },
                required=["roasts"],
            )
        except Exception:
            schema = None  # older/newer SDKs: fall back to JSON mime type only
        if schema is not None:
            config_kwargs["response_schema"] = schema

        response = client.models.generate_content(
            model=model or self.model,
            contents=prompt,
            config=types.GenerateContentConfig(**config_kwargs),
        )
        raw = getattr(response, "text", None) or ""
        return parse_roasts(raw, tier="nice" if nice else tier)
