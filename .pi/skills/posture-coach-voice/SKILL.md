---
name: posture-coach-voice
description: Work on the spoken roasts in Posture Coach — prompt text and moods, ElevenLabs and Gemini text-to-speech, the local offline voice, quota handling, the never-repeat insult ledger and the roast pool. Use when roasts repeat, sound wrong, are silent, or when changing prompts, TTS providers, voices or audio playback.
compatibility: Cloud providers need API keys in .env; the local voice (espeak-ng/piper/SAPI) always works offline.
---

# Posture Coach — roasts and voice

Read `posture-coach` first for layout and contracts.

## Where the words and the delivery live

| File | What it controls |
| ---- | ---------------- |
| `voice/prompts/roast.txt` | normal mode: actually insulting, still PG |
| `voice/prompts/nice.txt` | nice mode: encouragement |
| `voice/prompts/voice.txt` | spoken delivery: a shared base plus `[mood mild\|medium\|spicy\|nice]` sections |

Templates use `{{TOKEN}}` placeholders (`{{CONTEXT}}`, `{{EXCLUDE}}`, `{{COUNT}}`,
`{{TIER}}`, `{{TIER_BRIEF}}`) substituted with plain `str.replace` — **not**
`str.format`, because the templates contain literal JSON braces. Every file has a
built-in fallback (a missing file must never break the app), and `voice.txt` is
re-read per clip, so the persona can be tuned while running.

## Providers and quota reality

`tts_mode` (Controls → Voice) is `auto | elevenlabs | gemini | local`. `auto` walks:

1. **ElevenLabs** (`ELEVENLABS_API_KEY`) — free tier is **characters per month**,
   which suits these one-liners far better than a per-day request cap. Mood maps to
   `voice_settings`: stability falls and style rises as the tier escalates.
2. **Gemini** (`GEMINI_API_KEY`) — free tier is **~10 requests/day per model**. The
   quota is per *model*, so `voice/gemini_tts.py` walks a chain of TTS models and
   cools down each one that returns 429. This is why a pinned, exhausted model in
   `.env` used to make every clip fall back to the local voice.
3. **Local** — `espeak-ng` / `piper` / macOS `say` / Windows SAPI: unlimited, offline.

Forcing an engine stays **silent** rather than mixing in another voice when it is
unavailable. Diagnose with:

```bash
python run.py --test-voice "Sit up. Your spine has filed a complaint."
python run.py --test-voice "..." --mood spicy
python .pi/skills/posture-coach-voice/scripts/probe_tts.py        # per provider/model status
```

`probe_tts.py` calls each provider **directly**, bypassing the fallback chain — the
chain makes `synthesize()` succeed if *any* model works, which hides which one is
actually dead.

## Never repeat, stay original

`data/insults.txt` is the ledger: one insult per line, appended when a roast is
actually played. It is enforced in four places, and all four must keep working:

- the most recent lines are passed to the model as banned (`{{EXCLUDE}}`)
- `validate()` drops any generated line that matches
- `fallback_batch()` skips used lines
- the pool refuses to queue a line that is already said **or already queued**

Exclusions are ordered **most relevant first** (just played → queued → history)
because only the head of the list is shown to the model. The hand-written fallback
pool is finite, so once a tier is exhausted it degrades to a repeat rather than
going silent — the model is what provides unlimited originals.

## Invariants

- **Never bake session state into a pooled clip.** The pool voices text in the
  background at startup, before any session exists — the literal `{n}` that once got
  spoken came from exactly this. Session counts belong in the *prompt context*, not
  in the text of a pre-generated clip.
- **Clips are cached by text hash + engine** (`roast_<hash>_elevenlabs.wav`), so a
  voice can never be repaired after the fact, only regenerated. `_voice_from_path()`
  derives the engine from the filename — tag clips created outside the pool too
  (live roasts and the fallback path), or the database records `voice=NULL`.
- **Audio never runs on the vision thread.** `RoastService` generates live roasts on
  its own worker and plays through a size-1 queue that skips rather than stacks.
- **Every clip needs ~0.5 s of leading silence**, because a Bluetooth speaker clips
  the first syllable while the link wakes up (`voice/wavutil.py` does this).
- **Tests must not touch the network.** Use `offline_writer()`, `offline_tts()` and
  `offline_eleven()` from `tests/test_voice.py` when constructing a `RoastService`.
- A quota 429 does **not** consume quota, but it does cost a round trip, so a model
  that fails is skipped for its retry window rather than retried per clip.

## Playback

`voice/player.py` plays one clip at a time: Bluetooth first (reconnecting before each
clip, since the link drops when idle), then the Wi-Fi cast backup via
`/media/roasts/<name>`, then a local player (`pw-play`/`paplay`/`aplay`/`ffplay`) —
which is why a laptop hears roasts through its own speakers. `--no-audio` prints
instead, which is what CI and the smoke test use.
