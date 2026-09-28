# ElevenLabs Audiobook Pipeline — Plan

Last updated: 2026-09-23. Evidence and sources for every decision: [RESEARCH.md](RESEARCH.md).

**Goal:** audiobook narration in English (Indian-accented) and Hindi that listeners genuinely
like — near-zero errors (no skipped/repeated/misread words, no hallucinated sounds, no
mispronunciations) and one consistent voice across the whole piece.

**Content shape:** ~15-minute non-fiction summaries, split into ~6 key ideas (treated as
chapters). ~13–14k chars English; most key ideas fit in one 5,000-char request, some exceed it.

---

## Decisions

| # | Decision | Why |
|---|---|---|
| D1 | **Model: `eleven_v3` only.** No Multilingual v2. | Only model with audio tags + multi-language IPA/phoneme dictionaries (needed for Hindi/Sanskrit terms). |
| D2 | **Raw TTS API (`/with-timestamps`), not Studio API.** | Studio API is sales-gated, converts per chapter (no per-block regenerate), no seed, no documented cross-paragraph context for v3, and its auto-regenerate QC is only documented for the UI export. We rebuild that QC ourselves with more control. Optional sales call to re-evaluate (RESEARCH §8). |
| D3 | **Chunks of ~250–900 chars**, not near 5,000. | ElevenLabs: accent/language drift and degradation above ~800–900 chars; inconsistency below ~250. |
| D4 | **One take per chunk; regenerate only on QC failure.** | Cost. QC gate decides, max 2 automatic regenerations, then human. |
| D5 | **Voice: neutral, consistently-delivered IVC or Voice Design v3 voice. Separate native voice per language.** | PVCs "not fully optimized for Eleven v3". Accent comes from the voice — an English clone speaking Hindi carries an English accent. |
| D6 | **We normalize text ourselves (Gemini, already built); ElevenLabs normalization off.** | No Hindi normalizer exists; lakh/crore and Indian numbers are a documented weak spot. |
| D7 | **Lossless (WAV/PCM 44.1 kHz) end to end; encode MP3 once at the end.** | MP3 encoder padding causes gaps/hiccups at joins. Needs Pro tier. |
| D8 | **Two-stage automated QC: Forced Alignment + Scribe v2 (+ speaker similarity).** | Alignment catches garbled/dropped words via per-word loss; Scribe independently verifies the audio says the script; embeddings catch voice drift. |
| D9 | **Use ACX specs as the quality target, not as a channel.** | ACX prohibits AI narration. |

---

## Stage 1 — Voice & settings lock (once per voice)

1. Confirm voice types of `ancient_wisdom_speaker_english_indian` / `ancient_wisdom_speaker_hindi`
   (IVC / PVC / library). Replace any PVC with IVC or Voice Design v3.
2. For library voices, check `min_notice_period_days` (library voices can be withdrawn).
3. Generate a calibration set on real script passages; pick **Natural** (default) vs **Robust**
   (if drift/hallucinations). Never **Creative**.
4. Approve one **anchor clip** per voice (reference for speaker-similarity QC).
5. Freeze the preset. Recorded per title in the manifest:

```
model_id               = eleven_v3            (+ model version reported by API)
voice_id               = <per language>
stability              = natural | robust     (the only setting that really works on v3)
similarity_boost       = 0.75
style                  = 0
language_code          = en | hi              (always explicit)
apply_text_normalization = off                (we normalize upstream)
seed                   = <one fixed int per title>   (best-effort; retries use a new seed)
output_format          = wav_44100 / pcm_44100
pronunciation_dictionary_locators = [<id, pinned version_id>]
```

Note: `speed` is ignored on base v3 — pacing comes from text + post-production.

## Stage 2 — Text preparation

1. **Normalization** (built: `normalization/`): numbers, dates, currency, measurements,
   abbreviations → spoken words. Hindi: spell out in Hindi words incl. lakh/crore
   ("दो लाख पचास हज़ार", "बयालीस रुपये").
2. **Pronunciation** (built: `pronunciation/`):
   - Hindi: prefer alias rules with a Devanagari respelling; IPA phoneme rules only where alias
     fails. NFC-normalize text and graphemes.
   - English: aliases or IPA for Sanskrit/Hindi terms and Indian names inside English text.
   - v3 IPA is 80–90% consistent → QC still verifies.
3. **Hindi script**: Devanagari. Keep a per-title style sheet for loanwords (Devanagari vs Latin),
   decided by A/B listening.
4. **Tags**: superseded by Stage 2.5 — one trajectory tag at the start of a chunk (a chunk is one
   thought, so the tag *should* colour the whole generation), rare inline tags only at real turns,
   no SFX/experimental tags, no accent tag on an already-accented voice, written in English.

## Stage 2.5 — Performance direction (planned 2026-09-24)

**Problem:** v3 is generated with no emotional direction, so narration is correct and consistent
but flat — every sentence gets its own small rise-and-fall (H→L, H→L, H→L) instead of one thought
carrying across sentences. Reference: `../emotion-tagging-example.md`.

**Principle (from the example, and consistent with ElevenLabs' tag guidance):**
- Direct the **arc of a whole thought**, not each sentence. One trajectory tag at the start of a
  chunk (a chunk ≈ a paragraph ≈ a thought), e.g. `[warm, curious — gradually building]`. No
  per-sentence emotion tags.
- **Punctuation is the score:** `,` keep moving · `…` hold/suspension · `—` the thought turns ·
  `.`/`।` moderate reset · blank line = real transition · `[tag]` = change of trajectory.
- Tag continuity across chunks: since v3 has no stitching, each chunk's tag says how it *continues*
  from the previous one ("continuing the same calm thought, now more grounded"), because the LLM
  sees the whole section even though ElevenLabs sees one chunk.
- **Stay inside one persona.** Tags vary intensity and trajectory, never the character. The
  per-title narrator persona and a small allowed tag vocabulary keep the emotional range inside a
  band. This is what protects the consistency we fought for.

**One LLM "direction pass" per title + language, structured output (JSON schema):**
Gemini (`google.genai` — also replaces the deprecated `google.generativeai`), `response_schema` =
Pydantic model, a stronger model (e.g. Gemini 2.5 Pro) since this is a creative-judgment step.
It sees the whole title (all chunks with ids), so it plans the arc across sections.

```
TitleDirection
  profile:          genre, audience, narrator_persona, base_register,
                    allowed_tags[] (≈8–12), energy_range, pacing_notes
  chunks[]:         chunk_id,
                    arc (1 line: what this thought does emotionally),
                    lead_tag (trajectory tag for the chunk start),
                    inline_tags[] (rare: ≤1 per chunk, only at a real turn),
                    performance_text (same words, reshaped punctuation + tags),
                    intended_pace: slower | normal | faster,
                    intended_energy: low | medium | high
  pronunciation_candidates[]: term, lang, why, suggested alias / IPA   -> Stage 2 dictionary
  sfx_suggestions[]: chunk_id, anchor_phrase, sound, duration_sec      -> audio-library cue-sheet
```

This consolidates the "understand the title" LLM work into one call: emotion, pronunciation
candidates and SFX cue ideas (which were done by hand for *Together*). **Number/abbreviation
normalization stays separate** (span-based, cached, deterministic, cheap) — merging it into a
whole-text rewrite would bring back the regenerate-everything cost we designed out.

**Order:** normalize → chunk → **direction pass** → validate → human review → generate.

**Guardrails (automatic, before any credits are spent):**
1. **Same words, guaranteed:** strip tags + punctuation from `performance_text` and from the
   chunk text → the word sequences must be identical. Any added/removed/changed word → rejected,
   that chunk falls back to plain text. The LLM may only shape delivery, never content.
2. Tags must come from `allowed_tags` (or close variants); ≤1 lead tag + ≤1 inline tag per chunk.
3. Chunk still within size limits after reshaping (tags add characters).
4. Tags written in English even in Hindi text (tag behavior inside Hindi is undocumented).
5. Output saved as `work/<title>/<lang>/direction.json` — human-reviewable and editable. Edit one
   chunk's direction → only that chunk regenerates (its text hash changes).

**Downstream changes this requires:**
- QC: strip tags from the script before comparing with the transcript; any bracket word heard in
  the transcript = "tag read aloud" → fail/regenerate. Voice identity (speaker similarity) stays
  strict; pitch/pace/energy checks become per-chunk **relative to intended_pace/energy** instead of
  "everything near the median" (otherwise QC would flag the emotion we asked for).
- Joiner: pace matching targets each chunk's `intended_pace`, not the global median — otherwise
  it would flatten a deliberately slowed or quickened passage.
- Stability: Natural responds moderately to tags; Robust barely does; Creative responds most but
  is "prone to hallucinations". With the QC gate as a safety net, test Natural vs Creative.

**Experiment before adopting (Hindi Key Idea 1, new voice, blind A/B):**
- A: plain text (current approach)
- B: punctuation reshaping only
- C: punctuation + trajectory tags, Natural stability
- D: C with Creative stability
Judge by ear (does it flow across sentences? still one narrator?), plus QC: speaker similarity at
joins, tag-read-aloud, content errors. ~9k chars for all four.

## Stage 3 — Chunking (to build: `chunking/segmenter.py`)

| Rule | Value |
|---|---|
| Hard boundary | Every key idea (a chime + long pause covers the seam) |
| Target size | ~600–800 chars |
| Hard max | 900 chars (headroom for tags/context) |
| Min | ~250 chars — merge short paragraphs |
| Split points | Paragraph end preferred, else sentence end. Never mid-sentence, never right after a tag or quote |
| Balance | Split a key idea into **equal-size** chunks (e.g. 2,000 → 3 × ~670), never leave a short tail |
| Each chunk carries | `key_idea`, `index`, `text`, `boundary_before/after` type (sentence / paragraph / key idea), content hash |

A ~14k-char title → roughly 18–24 chunks.

**Experiment (pilot):** overlap-and-trim — generate `[prev last sentence] + chunk + [next first
sentence]`, then trim at the pause boundaries using character timestamps. Keeps chunks from
starting "cold" or ending on a final cadence. Evidence is community-only, so A/B it blind against
plain chunking before adopting. Skip at key-idea boundaries.

## Stage 4 — Generation (to build: `generation/generator.py`)

- `POST /v1/text-to-speech/{voice_id}/with-timestamps` with the frozen preset.
  **Confirmed working with `eleven_v3` (2026-09-23)** — character-level alignment, and
  `pcm_44100` lossless output works on our plan. Test audio: `data/test_v3_timestamps/`.
- Write per chunk: `chunk_NNN.wav`, alignment JSON, manifest entry (params, seed, request-id,
  character-cost, attempt number, content hash).
- **Idempotent:** skip a chunk if its content hash (text + params) is unchanged — editing one
  paragraph regenerates only that chunk.

## Stage 5 — QC gate (to build: `qc/`)

Per chunk, all automatic:

| Check | Tool | Fail signal |
|---|---|---|
| Content correct | **Scribe v2** transcript vs script. English WER; Hindi CER after Indic-aware normalization (NFC, nukta/chandrabindu folding). One pass with `keyterms` (names), one without. | Any insertion/deletion of ≥2 consecutive words; WER/CER above calibrated threshold |
| Word-level health | **Forced Alignment API** per-word `loss` | Words with loss above calibrated threshold |
| Hallucinated sounds | Scribe v2 audio-event tags | Any unexpected event (laughter, music, noise) |
| Voice drift | Speaker embedding (ECAPA/WavLM) vs anchor clip | Similarity > ~2σ below the title's mean |
| Delivery drift | Pitch median, speaking rate (chars/s from timestamps), loudness vs title median | Outliers |
| Hindi second opinion | IndicWhisper | Divergence on numbers / named entities → human |

On failure: regenerate with a new seed (max 2), then flag for a human.
On a glitch at a join: also regenerate the **preceding** chunk (ElevenLabs: problems often
originate there).
Thresholds are calibrated on the pilot title (known-good vs known-bad chunks, both languages).

## Stage 6 — Assembly & mastering (to build: `assembly/joiner.py`)

1. Stay in WAV/PCM throughout.
2. Trim each chunk's model-generated leading/trailing silence at the noise floor (keep the natural
   tail/breath of the last word, ~50–100 ms before fade).
3. Re-insert **controlled pauses** by boundary type:

| Boundary | Pause |
|---|---|
| Head of file | 0.5 s |
| Sentence (inside overlap-trim joins) | ~0.5 s |
| Paragraph | 0.8–1.2 s |
| Key idea | 2–3 s (+ chime) |
| Tail of file | 1–3.5 s |

4. Fill every pause with **room tone** (looped quietest slice of the audio), never digital zero.
5. 5–20 ms micro-fades at zero crossings on every cut; crossfade only over room tone, never over
   speech.
6. Per-chunk loudness match measured on **speech only** (exclude silence).
7. One **glue mastering chain** over the joined file (light compression, de-ess, gentle EQ).
8. Master to ACX numbers: RMS −23 to −18 dB, peak ≤ −3 dB, noise floor ≤ −60 dB.
9. Hand off to `elevenlabs-best/audio-library/scripts/build.py` for intro/outro music, chimes,
   SFX, cover art — then single final encode.

## Stage 7 — Assets (optional upgrade)

- Sound Effects API for chimes/ambience (0.5–30 s, loopable), Eleven Music for intro/outro beds.
  **Read the Eleven Music commercial licence terms before shipping.**

---

## Pilot plan (before scaling)

One real title, both languages:
1. Test call: `/with-timestamps` + `eleven_v3`.
2. Chunk-length sweep (300 / 600 / 900 chars) × Natural vs Robust → measure speaker similarity +
   WER/CER.
3. `language_code` on vs auto on a Hindi-word-heavy English passage.
4. Blind A/B: overlap-and-trim vs plain chunking.
5. Calibrate QC thresholds from the pilot's good/bad chunks.
6. One uninterrupted full listen per language — the final judge.

## Open items / blockers

- Confirm voice types (IVC/PVC/library). (Plan tier confirmed: lossless PCM works.)
- Pronunciation dictionary content (names, Sanskrit/Hindi terms) — from content team.
- Tests listed in RESEARCH §11.
- Optional: sales call re Studio API (v3 + auto-regenerate via API).
- DB/RMS integration — deferred; local-only for now.
- Code already built still defaults to `eleven_multilingual_v2` in `tts/client.py` → switch to
  the Stage 1 preset when Stage 4 is built.

## Built so far

- `normalization/` — detect → cache → Gemini (misses only) → offset-safe substitution. Tested.
- `pronunciation/` — JSON source of truth → PLS → upload; alias applier. Tested.
- `tts/client.py` — thin generation wrapper (to be updated per Stage 1/4).
- `elevenlabs-best/audio-library/` — post-production (intro/outro beds, SFX, cover) via cue-sheets.
