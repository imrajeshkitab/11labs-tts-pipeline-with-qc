# ElevenLabs TTS pipeline

See [PLAN.md](PLAN.md) for the full production plan and open blockers. This is the two
unblocked pieces from that plan, implemented and tested: **text normalization** and
**pronunciation dictionary tooling**. TTS generation itself is wired up (`tts/client.py`,
verified against the installed SDK) but not yet run against real book content.

## Layout

```
config.py                    single source of truth for env vars / paths
normalization/
  detectors.py                regex span detection (dates, currency, phone, measurements,
                               abbreviations, numbers) — extensible via @register_detector
  cache_store.py               persistent (category, term) -> normalized_text cache, JSON-backed
  llm_normalizer.py            Gemini call, one batch per run, only for cache misses
  pipeline.py                  orchestrates the above + offset-safe text reconstruction
pronunciation/
  dictionary.py                local JSON source of truth -> PLS (XML) -> ElevenLabs upload
tts/
  client.py                    thin ElevenLabs wrapper (generation only, no normalization logic)
cli.py                         glue: normalize / generate subcommands
data/                          normalization_cache.json, pronunciation_dictionary.json (gitignored)
```

## Why it's built this way

- **Normalization is detect-then-substitute, not detect-and-rewrite.** Regex finds spans
  (free, instant). Only spans not already in the cache go to Gemini, in one batched call —
  never the whole document, never one call per span. Reconstruction substitutes each span back
  at its exact character offset (right-to-left) rather than a naive string `.replace()`, so
  "100" meaning a room number and "100" meaning a page number never get conflated just because
  they're the same text.
- **The cache is the editable artifact.** `data/normalization_cache.json` is a flat
  `{"category:raw_text": "normalized_text"}` map. Fix an entry by hand, re-run — no LLM call
  happens for that term again, anywhere, ever (verified: a second identical run costs 0 LLM
  calls).
- **Pronunciation dictionaries are a separate concern from normalization.** Normalization fixes
  *how numbers/symbols are spoken*; the pronunciation dictionary fixes *how specific words are
  pronounced* (names, brand terms). Confirmed against live ElevenLabs docs (2026-09-22):
  `eleven_v3` supports phoneme-tag entries directly — it's actually the *best*-supported model
  for this (only v3 has multi-language IPA/CMU), not a limitation. Non-flash-v2/v3 models fall
  back to alias (plain substitution) entries, handled separately by `AliasApplier`.
- **Every module is decoupled behind an interface** so a piece can be swapped without touching
  the others: `NormalizationCache` (JSON file today, could be a DB table later), `LLMNormalizer`
  (Gemini today, swappable), `tts/client.py` (knows nothing about normalization or dictionaries,
  just takes final text + locator IDs).

## What's NOT done yet

- No real book content run through this — `data/*.json` is empty/test data only.
- Pronunciation dictionary has zero entries — needs Kitab-specific terms (brand names, author
  names, Sanskrit/Hindi words) from the content team. The mechanism is built; the content isn't.
- No segment-stitching/post-production step (LUFS normalization, crossfade joins) wired up yet —
  see PLAN.md Phase 4. The crossfade/mix technique from `elevenlabs-best/audio-library/scripts/build.py`
  is directly reusable here when that's built.
- No DB/RMS integration — deliberately out of scope for now per your instruction; local-only.

## Try it

```bash
pip install -r requirements.txt

# detection only, no LLM call, no cache writes
python3 cli.py normalize "Dr. Smith owes \$42.50 as of 2024-01-01." --dry-run

# real normalization (uses GEMINI_API_KEY from ../.env)
python3 cli.py normalize "Dr. Smith owes \$42.50 as of 2024-01-01."

# generate one audio segment (uses ELEVEN_LABS_API_KEY + a voice id from ../.env)
python3 cli.py generate "Doctor Smith owes forty-two dollars." \
    --voice ancient_wisdom_speaker_english_indian --out data/test.mp3
```
