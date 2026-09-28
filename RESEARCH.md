# ElevenLabs research findings (2026-09-23)

Four parallel research passes: Studio API, v3 long-form quality, Hindi, full API surface.
Labels: **[official]** = ElevenLabs docs/blog/changelog/help center; **[community]** = forums,
GitHub issues, practitioner write-ups, competitor benchmarks; **[unverified]** = could not
confirm, needs our own test. Decisions derived from this live in [PLAN.md](PLAN.md).

---

## 1. Eleven v3 core facts

- 5,000 char/request (~5 min audio); 70+ languages incl. Hindi. [official] [models](https://elevenlabs.io/docs/overview/models)
- GA on 2026-02-02: "out of alpha - more stable, accurate and has lower latency"; number/symbol
  errors down 68% (15.3% → 4.9%). [official] [changelog](https://elevenlabs.io/docs/changelog/2026/2/2), [GA blog](https://elevenlabs.io/blog/eleven-v3-is-now-generally-available)
- **No request stitching**: "Request stitching is not available for the `eleven_v3` model." API
  rejects `previous_text` with `unsupported_model`. [official] [stitching](https://elevenlabs.io/docs/eleven-api/guides/how-to/text-to-speech/request-stitching), [community] [voicebook PR #148](https://github.com/NeoVand/voicebook/pull/148)
- **No SSML breaks**: "Eleven v3 does not support SSML break tags. Use audio tags, punctuation
  (ellipses), and text structure to control pauses." [official] [best practices](https://elevenlabs.io/docs/overview/capabilities/text-to-speech/best-practices)
- **No TTS WebSocket** support for v3 (irrelevant for batch). [official] [websockets](https://elevenlabs.io/docs/eleven-api/guides/how-to/websockets/realtime-tts)
- **Stability = 3 presets**, "the most important setting in v3":
  Creative "prone to hallucinations" / Natural "closest to the original voice recording" /
  Robust "highly stable… less responsive to directional prompts… similar to v2". [official] [v3 prompting](https://elevenlabs.io/docs/best-practices/prompting/eleven-v3)
- **Speed is ignored on base v3** — 0.7 and 1.2 produced identical 2.88 s audio; only stability
  is effectively exposed. [community] [voicebook PR #148](https://github.com/NeoVand/voicebook/pull/148)
- **Seed**: "best effort to sample deterministically… Determinism is not guaranteed." Range
  0–4,294,967,295. [official] [API ref](https://elevenlabs.io/docs/api-reference/text-to-speech/convert)
- `language_code` (ISO 639-1) "used to enforce a language for the model and text normalization."
  [official] [API ref](https://elevenlabs.io/docs/api-reference/text-to-speech/convert)
- `apply_language_text_normalization` is "Currently only supported for Japanese" → no Hindi
  normalizer. [official] same page
- Lossless output (`pcm_*`, `wav_*` up to 48 kHz) requires **Pro tier+**. [official] same page
- Response headers `request-id`, `character-cost`, `x-trace-id` for manifests/cost. [official] [API intro](https://elevenlabs.io/docs/api-reference/introduction)

## 2. Chunk length (the key consistency lever)

- "The AI can sometimes switch languages or accents throughout a single generation, especially if
  that generation is longer… keep text generations under 800-900 characters." Also "Break text
  into sections under 800 characters" to avoid degradation. [official, not v3-specific] [troubleshooting](https://elevenlabs.io/docs/eleven-creative/troubleshooting)
- Alpha-era v3 guide: very short prompts "more likely to cause inconsistent outputs", recommended
  "prompts greater than 250 characters". Line since removed from the current page. [official, historical]
- → Working band: **~250–900 chars/request**. No published measurement of speaker similarity
  vs chunk length for v3 — must test ourselves.
- Studio "can generate several smaller audio segments… ensuring better quality and consistency".
  [official] troubleshooting

## 3. Voices

- "The most important parameter for Eleven v3 is the voice you choose." [official] [v3 prompting](https://elevenlabs.io/docs/best-practices/prompting/eleven-v3)
- "Professional Voice Clones (PVCs) are currently not fully optimized for Eleven v3… it would be
  best to find an Instant Voice Clone (IVC) or designed voice for your project if you need to use
  v3 features." [official] same page
- Voice Design v3 voices "are fully compatible with… Eleven v3". [official] [Voice Design v3](https://elevenlabs.io/blog/voice-design-v3)
- Library voices "may produce more variable results compared to the v2 and v2.5 models". [official]
- "Neutral" voices "tend to be more stable across languages and styles". [official] v3 prompting
- Accent comes from the voice; language from the text. Clone "speaking the language with the
  correct accent"; accent can't be changed after cloning. [official] [accent help](https://elevenlabs.io/docs/help-center/troubleshooting/why-does-my-voice-change-accent-or-language)
- Shared library voices can be removed — filter on `min_notice_period_days` / subscribe to
  `voice_removal_notice` webhook. [official] [shared voices](https://elevenlabs.io/docs/api-reference/voices/voice-library/get-shared)

## 4. Audio tags

- Place "immediately before… or immediately after" the segment they modify. Effectiveness depends
  on voice ("Don't expect a whispering voice to suddenly shout"). Test experimental tags. [official] [v3 prompting](https://elevenlabs.io/docs/best-practices/prompting/eleven-v3)
- Overuse (documented for break tags) → "speed up, or introduce additional noises or audio
  artifacts". [official] best practices
- `[Indian English]` accent tag exists — unnecessary on an already Indian-accented voice.
  [official] [accent blog](https://elevenlabs.io/blog/eleven-v3-audio-tags-emulating-accents-with-precision)
- Tag behavior inside Hindi text is undocumented. [unverified]

## 5. Pronunciation

- v3 inline IPA `/…/` across 70+ languages, "80-90% pronunciation consistency". [official] [controls](https://elevenlabs.io/docs/best-practices/prompting/controls)
- "Pronunciation dictionary phoneme tags only work with eleven_flash_v2 and eleven_v3 models." "If
  you want to use IPA and CMU pronunciations in languages other than English, you will have to
  switch to the eleven_v3 model." [official] [pronunciation dictionaries](https://elevenlabs.io/docs/eleven-api/guides/how-to/text-to-speech/pronunciation-dictionaries)
- Up to 3 dictionary locators per request, applied in order; every edit creates a new
  `version_id`. PLS matching is case-sensitive; "only the very first replacement is used".
  [official]
- No official Hindi/Devanagari PLS examples. [unverified]

## 6. Hindi specifics

- Devanagari is the only script in ElevenLabs' Hindi samples. [official] [Hindi page](https://elevenlabs.io/text-to-speech/hindi)
- Competitor benchmark: global TTS incl. ElevenLabs had higher CER on romanized text, code-mixing,
  numerics, named entities, abbreviations. [community/competitor] [Sarvam Bulbul v3](https://www.sarvam.ai/blogs/bulbul-v3)
- Blind preference: ElevenLabs v3 preferred over Bulbul v3 in full-band Hindi (Bulbul won only
  37.9%). [community] [Josh Talks evals](https://evals.ai.joshtalks.com/blog/indic-tts-evaluation-bulbul-v3-vs-others)
- Indian-style numbers (lakh/crore, digit-by-digit phone) weaker on ElevenLabs. [community] [caller.digital](https://caller.digital/blog/elevenlabs-alternatives-india-2026)
- Accent benchmark: Hindi retroflex collapse low (~1%); "commercial WER-leaders do not uniformly
  lead on retroflex or prosodic fidelity." [research] [arXiv 2604.25476](https://arxiv.org/abs/2604.25476)
- → QC focus areas for Hindi: numbers, Indian named entities, code-mixed spans, abbreviations,
  prosody.

## 7. QC tools

- **Forced Alignment API** `POST /v1/forced-alignment`: returns word/character timings + per-word
  `loss` ("average alignment loss/confidence score") + overall loss; 29 languages incl. Hindi;
  priced like STT. Assumes text is correct → can't alone prove audio matches. Loss thresholds
  undocumented. [official] [API](https://elevenlabs.io/docs/api-reference/forced-alignment/create), [overview](https://elevenlabs.io/docs/overview/capabilities/forced-alignment.md)
- **Scribe v2** `POST /v1/speech-to-text`: word/char timestamps, per-word `logprob`, `keyterms`,
  audio-event tagging (catches hallucinated noises). English "≤ 5% WER", Hindi "&gt;5% to ≤10%
  WER". [official] [API](https://elevenlabs.io/docs/api-reference/speech-to-text/convert), [capabilities](https://elevenlabs.io/docs/overview/capabilities/speech-to-text)
- Keyterm biasing can mask real TTS errors → run one pass with and one without. [inference]
- IndicWhisper (AI4Bharat): lowest WER on 39/59 Vistaar benchmarks — good Hindi second opinion.
  [research] [arXiv](https://arxiv.org/pdf/2305.15386)
- Whisper-style normalizers strip matras → "artificially improved performance metrics for Indic
  languages". Use Indic-aware normalization (NFC, nukta/chandrabindu folding). [research] [arXiv 2409.02449](https://arxiv.org/abs/2409.02449)
- Standard research practice: ASR-WER for "repetitive, endless, or omitted speech"; speaker
  similarity via WavLM/ECAPA embeddings. [research] [Seed-TTS](https://arxiv.org/html/2406.02430v1)
- ElevenLabs: regeneration fixes roughly half of quality issues; corrupt speech has no identified
  cause → "regenerate". Glitches between paragraphs: "regenerate the preceding paragraph, as the
  problem often originates there." [official] troubleshooting

## 8. Studio API (verdict: not our primary path)

- Exists (`/v1/studio/projects`, chapters, convert, snapshots, callbacks, character alignments on
  chapter snapshots), `from_content_json` with chapters → blocks → `tts_node`s. [official] [add-project](https://elevenlabs.io/docs/api-reference/studio/add-project)
- **Sales-gated**: "The ElevenCreative Studio API is only available upon request. To get access,
  contact sales." [official] [studio-api-information](https://elevenlabs.io/docs/api-reference/studio-api-information)
- Generates per paragraph (≤5,000 chars/paragraph). **No documented cross-paragraph context**,
  for v3 or any model. [official] [studio](https://elevenlabs.io/docs/eleven-creative/products/studio)
- Auto-Regenerate (checks "volume distortions, voice similarity, mispronunciations, missing or
  additional words", up to 2 free regenerations) is documented only for "convert your whole
  chapter or project in one step from the Export dialog" — **not documented for the API**.
  [official] [auto-regenerate](https://elevenlabs.io/docs/help-center/product/studio/studio/what-is-auto-regenerate)
- API smallest convertible unit is a chapter; no per-block regenerate; no per-block voice
  settings; no seed. `default_model_id=eleven_v3` via API not explicitly documented. Studio page
  says "Phoneme tags are only compatible with 'Eleven Flash v2' model." [official/unverified]
- `volume_normalization` (formerly `acx_volume_normalization`) → ACX-style loudness. [official]
- Community: chapters stuck converting; breaths/bleed at paragraph boundaries. [community]
- **Only worth a sales call** to confirm (a) v3 via API and (b) whether auto-regenerate QC runs on
  API conversions. If both yes, pilot on one book.

## 9. Other useful APIs

| API | Use for us | Notes |
|---|---|---|
| TTS `/with-timestamps` | chunk generation + char alignment for trimming | v3 support not named in docs — **test once** [unverified] |
| Sound Effects `/v1/sound-generation` | chimes, ambience | 0.5–30 s, `loop` on `eleven_text_to_sound_v2`, 40 credits/s |
| Eleven Music | intro/outro beds | 3 s–5 min, `music_v2_5` default since 2026-09-14; **read commercial licence terms before shipping** |
| Voice Design `/v1/text-to-voice/design` | create narrator voice | `eleven_ttv_v3` |
| Shared voice library search | find Hindi / Indian-English narrators | filter language/accent/`min_notice_period_days` |
| Text to Dialogue | only for future multi-narrator books | v3, 2,000 chars total |
| History / usage | audit & cost | `/v1/usage/character-stats` deprecated |
| Dubbing, Voice Isolator, Voice Changer | — | not useful (Dubbing is alpha; translate + generate directly instead) |
| Agents, Reception AI, agent env vars | — | conversational phone agents, irrelevant to batch audiobooks |

## 10. Post-production standards

- ACX numbers: RMS −23 to −18 dB; peaks < −3 dB; noise floor < −60 dB RMS; room tone at head/tail;
  MP3 ≥192 kbps CBR 44.1 kHz. [official] [ACX](https://help.acx.com/s/article/acx-audio-submission-requirements)
- **ACX prohibits AI/TTS narration** ("Unauthorized use of text-to-speech, AI… in ACX titles is
  prohibited") — use specs as a quality target only; Audible via ACX is not a channel. [official] same page
- Silence conventions: ~0.5 s head, ~0.5 s between sentences, slightly longer between paragraphs
  (0.8–1.2 s), 2–3.5 s at section/chapter breaks, 1–3.5 s tail; fill with room tone, not digital
  zero. [community] [Narrator's Roadmap](https://www.narratorsroadmap.com/standards-for-silence-in-the-book/)
- MP3 encoder delay/padding varies → gaps at joins; edit in WAV/PCM, encode once. [community] [Hydrogenaudio](https://wiki.hydrogenaudio.org/index.php?title=Gapless_playback)
- Overlap-and-trim (1–2 sentence overlap, trim, 10–50 ms crossfade) suggested by practitioners.
  [community, weak] [Arti-Trends](https://arti-trends.com/community/troubleshooting-elevenlabs-artifacts-on-long-form-narration/)

## 11. Open questions — answer with our own tests

1. ~~Does `/with-timestamps` work with `eleven_v3`?~~ **Confirmed 2026-09-23**: returns
   `alignment` + `normalized_alignment` (character-level, exact match to input, timings consistent
   with audio duration). `pcm_44100` output worked → plan tier supports lossless.
   **`quality_check` field (investigated):** sent by the server (seen in raw HTTP, not an SDK
   artifact), but undocumented — absent from the API reference and from the live OpenAPI spec's
   `AudioWithTimestampsResponseModel`; no request parameter enables it; zero public GitHub code
   references it. Always `null` across v3 Natural, v3 Creative with deliberately glitch-prone text,
   and Multilingual v2. Only documented "quality check" is Studio's `quality_check_on` project flags.
   Conclusion: internal/unexposed plumbing — do not rely on it; log it in the manifest in case it
   ever populates; QC stays our own (Stage 5). (A web page claiming `get_voice_audio_quality` /
   `output_format='with_metrics'` is fabricated — neither exists in the spec.)
   Response headers confirmed: `request-id`, `character-cost`, `x-trace-id`, `x-region`.
   Same seed + settings gave identical duration (11.84 s) across mp3 and pcm calls.
2. Chunk-length sweep (e.g. 300 / 600 / 900 chars): which gives best speaker-similarity + lowest
   WER for each of our two voices?
3. Natural vs Robust: drift/error rate vs expressiveness on our voices.
4. Does explicit `language_code` measurably reduce accent drift (esp. Hindi words in Indian English)?
5. Does overlap-and-trim beat plain chunking on join smoothness (blind A/B)?
6. Forced-alignment `loss` and Scribe WER/CER thresholds that separate good vs bad chunks.
7. ~~Are our two voices IVC, PVC, or library?~~ **Checked 2026-09-23**: English
   `ancient-wisdom-speaker` = `generated` (Voice Design — v3-compatible). Hindi = "Kuber J – Calm,
   Devotional & Meditative", `professional` (library PVC — "not fully optimized for Eleven v3").
   `use_pvc_as_ivc` is **rejected on v3** ("Providing use_pvc_as_ivc is not supported with the
   'eleven_v3' model.") — no workaround; if Kuber J sounds degraded on v3, replace with an IVC or a
   Voice Design v3 Hindi voice.
8. ~~Which plan tier are we on?~~ **Pro** (lossless PCM works).
9. (Optional, sales) Studio API: v3 accepted? auto-regenerate runs on API converts?
