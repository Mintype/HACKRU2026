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

Templates use `{{TOKEN}}` placeholders (`{{CONTEXT}}`, `{{EXCLUDE}}`, `{{COUNT}}`,
`{{TIER}}`, `{{TIER_BRIEF}}`, `{{TOPIC}}`) substituted with plain `str.replace` —
**not** `str.format`, because the templates contain literal JSON braces. Each file
has a built-in fallback, so a missing file can never break the app.

The spoken **mood** is not a text prompt any more: ElevenLabs takes
`voice_settings`, so the tier maps to numbers in `voice/eleven_tts.py:
TIER_SETTINGS` (stability falls and style rises as the roast escalates).

## Providers and quota reality

**Gemini writes the roasts; ElevenLabs speaks them.**

Text generation walks a **model chain** the same way the voice does:
`RoastWriter.models()` tries the configured `GEMINI_TEXT_MODEL` and then known
fallbacks, and a model that answers 404 (retired) or 429 (quota) is skipped for its
backoff window with the reason logged. This matters: `gemini-2.5-flash` now answers
*"no longer available to new users"*, so a pinned old model means **every roast
silently comes from the offline pools** — which reads as "the insults are unoriginal
and repeat". Check the log for `Gemini is writing roasts with …`, or run
`probe_tts.py --live`, which prints the model that answered and a sample line.

The chef persona lives in `voice/prompts/roast.txt`: a furious chef inspecting the
posture as a badly plated dish, required to land one vivid food/kitchen comparison
per line. `nice.txt` is the same chef, delighted.

`tts_mode` (Controls → Voice) is `auto | elevenlabs | local`, and `auto` is simply:

1. **ElevenLabs** (`ELEVENLABS_API_KEY`) — free tier is **characters per month**,
   which suits these one-liners far better than a per-day request cap.
2. **Local** — `espeak-ng` / `piper` / macOS `say` / Windows SAPI: unlimited, offline.

Forcing an engine stays **silent** rather than mixing in another voice when it is
unavailable. Two ElevenLabs quirks that are handled for you: free plans cannot use
"library" voices through the API (HTTP 402 — the client then lists the account's
own voices and uses one), and some models reject the optional `style` setting (a
422 retries without it). The default voice is **Sarah**. Diagnose with:

```bash
python run.py --test-voice "Sit up. Your spine has filed a complaint."
python .pi/skills/posture-coach-voice/scripts/probe_tts.py        # config, spends nothing
python .pi/skills/posture-coach-voice/scripts/probe_tts.py --live # one real request
```

## Never repeat, stay original

`data/insults.txt` is the ledger: one insult per line, appended when a roast is
actually played. It is enforced in four places, and all four must keep working:

- the most recent lines (40 of them) are passed to the model as banned
- `validate()` drops any generated line that matches
- `fallback_batch()` skips used lines
- the pool refuses to queue a line that is already said **or already queued**

Exclusions are ordered **most relevant first** (just played → queued → history)
because only the head of the list is shown to the model.

Originality comes from two more levers:

- **A writing angle per batch.** `gemini_text.build_prompt` fills the `{{TOPIC}}`
  token with a random entry from `voice/roasts.py:TOPICS` (write as the chair,
  blame gravity, write as their back's warranty department…), so batches do not
  converge on the same joke. Add angles by adding to `TOPICS`.
- **Substantial offline pools.** ~20 lines per tier plus 10 neck-, 10 torso- and
  10 phone-specific lines, because the hand-written set is finite: once a tier is
  exhausted it degrades to a repeat rather than going silent. Gemini is what
  provides unlimited originals.
- **Aimed batches.** `fallback_batch(part=…)` mixes in neck/torso lines, and
  `fault="phone"` overrides that with the phone pool when the fault is not the
  posture at all (`FAULT_LINES`). `RoastService` passes both from the context, and
  `gemini_text` does the same so the model gets a phone line in its context.

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
