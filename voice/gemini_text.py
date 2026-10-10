"""Gemini text generation for roast batches (structured JSON output).

The prompt is built as a pure function so it can be unit-tested and reviewed.
Every response is validated (length, tone blocklist, duplicates) before use.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Dict, Iterable, List, Optional, Sequence

from .roasts import fallback_batch

log = logging.getLogger(__name__)

MAX_WORDS = 18

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
    "mild": "Tier: MILD. Gentle teasing, first slouches of the session.",
    "medium": "Tier: MEDIUM. Playfully dramatic, the person keeps doing it.",
    "spicy": "Tier: SPICY but still PG. Theatrical despair, repeat offender energy.",
    "nice": "Encouragement, not insults. Warm and genuinely motivating.",
}


def build_prompt(
    *,
    count: int = 5,
    tier: str = "mild",
    nice: bool = False,
    context: Optional[Dict] = None,
    exclude: Sequence[str] = (),
) -> str:
    context = context or {}
    lines = [
        'You write one-liners for "Posture Coach", a webcam that watches how '
        "someone sits and speaks to them out loud when they slouch.",
        "",
        "Hard rules:",
        "- Roast the slouch and the posture only.",
        "- NEVER mention appearance, body shape, weight, age, gender, race, "
        "identity, intelligence or ability.",
        "- PG, office-safe, playful. No profanity, no slurs, no cruelty.",
        f"- At most {MAX_WORDS} words each, speakable in one breath.",
        "- Vary the imagery. Do not reuse phrasing between lines.",
        f"- {TIER_BRIEF.get('nice' if nice else tier, TIER_BRIEF['mild'])}",
    ]
    if context:
        lines.append("")
        lines.append("Session context to weave in naturally:")
        if context.get("slouch_count") is not None:
            lines.append(f"- This is slouch number {context['slouch_count']} this session.")
        if context.get("session_minutes") is not None:
            lines.append(f"- The session has been running ~{context['session_minutes']} minutes.")
        if context.get("worst_neck_angle") is not None:
            lines.append(f"- Their worst forward-head angle so far is {context['worst_neck_angle']} degrees from vertical.")
    if exclude:
        lines.append("")
        lines.append("Do not reuse or closely paraphrase these recent lines:")
        for line in list(exclude)[:8]:
            lines.append(f"- {line}")
    lines += [
        "",
        f"Return exactly {count} lines.",
        'Respond with JSON only: {"roasts": [{"text": "...", "tier": "' + ("nice" if nice else tier) + '"}]}',
    ]
    return "\n".join(lines)


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
        # Last-ditch: pull quoted strings out of a JSON-ish blob.
        import re

        found = re.findall(r'"text"\s*:\s*"([^"]+)"', text)
        return [{"text": item, "tier": tier} for item in found]
    items: Iterable
    if isinstance(data, dict):
        items = data.get("roasts") or data.get("lines") or []
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
        """Return validated roasts, or hand-written fallbacks on any failure."""
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
            )
        return items[:count]

    def _call(self, prompt: str, count: int, tier: str, nice: bool) -> List[Dict[str, str]]:
        from google.genai import types

        client = self._get_client()
        config_kwargs: Dict = {"response_mime_type": "application/json", "temperature": 1.0}
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
