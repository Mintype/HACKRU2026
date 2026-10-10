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
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .roasts import fallback_batch, pick_topic

log = logging.getLogger(__name__)

#: Drop generated lines longer than this (kept just above the 6-12 the prompt asks for).
MAX_WORDS = 14

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
PROMPT_FILES = {False: "roast.txt", True: "nice.txt"}

# Fallbacks, kept deliberately close to the files above.
DEFAULT_ROAST_PROMPT = """\
You write one-line insults for a webcam that watches how someone sits. When they
slouch, a speaker in the room says your line out loud, in front of other people.

Be genuinely funny and actually roast them. A polite reminder is a failure.

HARD RULES
- Attack the slouch, never the person: no appearance, body shape, weight, age,
  gender, race, identity, intelligence or ability. Ever.
- PG. No profanity, no slurs.
- SHORT: 6-12 words. One breath. No emoji, no quotation marks.
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
You write one-line encouragement for a webcam that watches how someone sits.
When they sit up well, a speaker in the room says your line out loud.

Warm, specific, and a little bit funny. No filler like "great job".
Never comment on appearance, body, identity or ability. PG. Kind.
SHORT: 5-12 words. No emoji, no quotation marks.

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
    if context.get("phone"):
        lines.append(
            "- This is not a posture problem: they are staring down at a phone or device."
        )
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
        self.model = model or os.environ.get("GEMINI_TEXT_MODEL", "gemini-2.5-flash")
        self.max_words = max_words
        self._client = None
        self.last_error: Optional[str] = None

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
            try:
                items = self._call(prompt, count, tier, nice)
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                log.warning("Gemini text generation failed (%s); using fallbacks", exc)
        items = validate(items, tier="nice" if nice else tier, exclude=list(exclude))[:count]
        if len(items) < count:
            items += fallback_batch(
                count=count - len(items),
                tier=tier,
                nice=nice,
                exclude=list(exclude) + [i["text"] for i in items],
                part=(context or {}).get("slouch_part"),
                fault="phone" if (context or {}).get("phone") else None,
            )
        return items[:count]

    def _call(self, prompt: str, count: int, tier: str, nice: bool) -> List[Dict[str, str]]:
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
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(**config_kwargs),
        )
        raw = getattr(response, "text", None) or ""
        return parse_roasts(raw, tier="nice" if nice else tier)
