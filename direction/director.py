"""
Performance direction (PLAN.md Stage 2.5): one structured LLM pass per title + language that plans
the emotional arc of every thought, before any audio is generated.

The LLM may reshape punctuation and add a trajectory tag per chunk — it may NOT change words.
`validate()` enforces that mechanically; any chunk that fails falls back to its plain text.
Output is written to work/<title>/<lang>/direction.json for human review/editing.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from qc.text_compare import normalize_for_compare

MAX_TAGS_PER_CHUNK = 4
TAG_RE = re.compile(r"\[[^\[\]]{1,200}\]")


class TitleProfile(BaseModel):
    genre: str
    audience: str
    narrator_persona: str = Field(description="One consistent narrator character for the whole title")
    base_register: str = Field(description="Default delivery, e.g. 'warm, calm, curious'")
    allowed_tags: list[str] = Field(description="8-12 delivery descriptors; tags vary intensity/trajectory, never persona")
    energy_range: str
    pacing_notes: str


class ChunkDirection(BaseModel):
    chunk_id: str
    arc: str = Field(description="One line: what this thought does emotionally, and how it continues from the previous chunk")
    lead_tag: str = Field(description="Trajectory tag in English, without brackets, e.g. 'warm, curious — gradually building'")
    performance_text: str = Field(description="The chunk's exact words with reshaped punctuation and line breaks, starting with the bracketed opening tag; further [tags] only where the emotion shifts")
    intended_pace: Literal["slower", "normal", "faster"]
    intended_energy: Literal["low", "medium", "high"]


class PronunciationCandidate(BaseModel):
    term: str
    why: str
    suggestion: str = Field(description="Respelling or IPA to use if the TTS mispronounces it")


class SfxSuggestion(BaseModel):
    chunk_id: str
    anchor_phrase: str
    sound: str
    duration_sec: float


class TitleDirection(BaseModel):
    profile: TitleProfile
    chunks: list[ChunkDirection]
    pronunciation_candidates: list[PronunciationCandidate]
    sfx_suggestions: list[SfxSuggestion]


_PROMPT = """You are the performance director for an audiobook narrated by ElevenLabs Eleven v3.
Title: "{title}" by {author}. Language of the narration: {language_name}.
The script is split into chunks; each chunk is generated as ONE separate TTS request and the TTS
cannot hear the previous chunk, so continuity across chunks must come from your direction.

THE PROBLEM TO SOLVE: without direction, the TTS gives every sentence its own mini-performance
(high -> low, reset at each full stop: H↓ H↓ H↓). We want PROSODIC CONTINUITY: the emotional
contour carries across sentence boundaries — a thought starts, rises, carries forward, dips,
rises again, resolves — like H → M → H → L → M → H → L across a passage.

HOW TO DIRECT (follow the reference example below closely):
- Do NOT tag every sentence. Place a tag only WHERE THE EMOTION NEEDS TO SHIFT. One tag governs
  a whole stretch of several sentences — the "emotional journey" of that stretch.
- Tags must describe a PERFORMABLE, AUDIBLE change in the voice — something a voice actor can
  physically do — plus its trajectory. Build them from these dimensions:
    pace: unhurried / slower / easing into / picking up slightly / let it land
    volume & weight: softer / quieter / fuller / leaning in close
    brightness & warmth: lighter, brighter / warmer / darker, more serious
    audible attitude: a small smile in the voice / gentle gravity / open curiosity / quiet wonder
  Examples: "[softer and slower — gently concerned, letting it sink in]",
  "[brighter, lighter, a small smile in the voice]",
  "[leaning in, unhurried — quiet wonder, let it land]",
  "[warm and encouraging — picking up slightly]".
  NEVER use an abstract label alone ("authoritative", "practical", "informative", "formal",
  "clear") — the TTS cannot perform a job description; always say how it SOUNDS.
- A TAG DESCRIBES DELIVERY ONLY — never the content or the purpose of the text. After the "—"
  write only a delivery trajectory ("gradually building", "easing off", "let it land").
  FORBIDDEN: "— explaining the problem of stress", "— introducing the key idea", "— citing
  studies", "— offering advice". Those are READ ALOUD by the TTS in English narration.
- CONTRAST: when the meaning shifts (problem -> solution -> evidence -> invitation -> wonder),
  neighbouring stretches must sound noticeably different in pace, volume or brightness. Do not
  give consecutive stretches near-identical tags.
- A chunk usually has 1–3 tags (as many as it has real emotional movements, max 4). Every chunk
  MUST open with a tag, because each chunk is a fresh generation; for chunks after the first
  in a section, that opening tag says how it CONTINUES from the previous chunk.
- Put each tag on its own line, then a blank line, then the text it governs — a blank line is a
  real emotional transition.
- PUNCTUATION IS THE MUSIC NOTATION. You may reshape it freely:
    ","  keep the thought moving        "…"  hold / emotional suspension
    "—"  the thought turns              "." / "।"  moderate reset
    blank line  actual emotional transition      [tag]  change of the overall trajectory
  Join sentences that belong to one thought with commas, "…" and "—" so they flow as one line
  instead of resetting at every full stop.
- BREATH POINTS: also add "—" or "," where a narrator naturally breathes even if the written text
  has no punctuation there — between a long subject and its predicate, before a list, after a long
  introductory phrase, before a long "कि"/"that" clause. Example (Hindi):
  "कोर्टिसोल का लगातार बढ़ा हुआ लेवल चिंता, डिप्रेशन…" -> "कोर्टिसोल का लगातार बढ़ा हुआ लेवल — चिंता, डिप्रेशन…"
  (otherwise "लेवल चिंता" runs together like one word). Only where a narrator would really breathe.
- Stay inside ONE narrator persona for the whole title. This is non-fiction: sincere and
  grounded, but NOT flat — let curiosity, concern, reassurance, wonder and quiet conviction
  genuinely rise and fall with the meaning, audibly.
- Tags are always written in ENGLISH, even when the narration is Hindi.
- Never use sound-effect or reaction tags ([laughs], [sighs], [applause]) or accent tags.

ABSOLUTE RULE — WORDS: never add, remove, reorder, translate, respell or replace any word. Every
word must stay exactly as written (numbers are already spelled out — keep them; headings like
"Key Idea one of six" stay as they are). Keep the script (Devanagari stays Devanagari). You may
only change punctuation, whitespace/line breaks, and add [tags].

REFERENCE EXAMPLE (English, other content — copy the PHILOSOPHY and FORMAT, not the words):

[warm, intimate, naturally conversational — begin softly, gradually build curiosity]

You can have insane chemistry with someone… and still have no idea how to build a life together. You can talk for hours, lose track of time, feel that instant pull toward each other… and before you know it, you’re thinking, “This has to be it.”

[subtle emotional shift — keep the same conversational flow, gradually becoming more grounded]

But then real life starts. You want to talk things through, and they shut down. You want a committed relationship, but they’re still figuring out what they want. You want the same future… and they want a completely different life.

[quiet realization — slow slightly, let the realization emerge rather than announcing it]

And suddenly, all that chemistry isn’t solving anything. Because chemistry tells you how you feel around someone… compatibility tells you whether your lives actually work together. And those are not the same thing.

OUTPUT per chunk: arc (one line), lead_tag (the opening tag's text, no brackets),
performance_text (the full directed text, starting with the opening [tag]),
intended_pace and intended_energy relative to the narrator's normal delivery.
Also: pronunciation_candidates (names, foreign/technical terms) and at most one subtle
sfx_suggestion per key idea where the text clearly evokes a sound (empty list is fine).

CHUNKS (id, section, text):
{chunks}
"""

_LANG_NAMES = {"en": "English (Indian-accented narrator)", "hi": "Hindi"}


_CONTENT_CLAUSE = re.compile(
    r"\s*[—–-]\s*(explaining|introducing|presenting|revealing|citing|describing|offering|posing|"
    r"setting up|stating|linking|delivering|summarizing|summarising|illustrating|defining|listing|"
    r"giving|sharing|making|moving to|shifting to|transitioning|returning to|continuing the)\b[^\]]*",
    re.IGNORECASE)


def sanitize_tags(text: str) -> tuple[str, int]:
    """Drop content/purpose clauses from tags ("[warm — explaining the science]" -> "[warm]").
    In English narration v3 reads such clauses aloud (heard by QC on the pilot)."""
    count = 0

    def fix(m):
        nonlocal count
        inner = m.group(1)
        cleaned = _CONTENT_CLAUSE.sub("", inner).strip(" ,—–-")
        if cleaned != inner:
            count += 1
        return f"[{cleaned or inner}]"
    return TAG_RE.sub(lambda m: fix(re.match(r"\[(.*)\]", m.group(0), re.S)), text), count


def _strip_tags(text: str) -> str:
    return TAG_RE.sub(" ", text)


def validate(original: str, directed: ChunkDirection, lang: str, max_chars: int) -> list[str]:
    problems = []
    text = directed.performance_text.strip()
    if not TAG_RE.match(text):
        problems.append("does not start with a lead [tag]")
    if len(TAG_RE.findall(text)) > MAX_TAGS_PER_CHUNK:
        problems.append(f"more than {MAX_TAGS_PER_CHUNK} tags")
    if normalize_for_compare(_strip_tags(text), lang) != normalize_for_compare(original, lang):
        a, b = normalize_for_compare(original, lang), normalize_for_compare(_strip_tags(text), lang)
        first = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
        problems.append(f"words changed near word {first}: {a[first:first + 4]} -> {b[first:first + 4]}")
    if len(text) > max_chars:
        problems.append(f"too long after direction ({len(text)} > {max_chars})")
    return problems


class GeminiDirector:
    # Pro first (creative judgment); Flash as fallback when Pro is overloaded (503 "high demand")
    def __init__(self, api_key: str, models: tuple[str, ...] = ("gemini-2.5-pro", "gemini-2.5-flash")):
        from google import genai
        self._client = genai.Client(api_key=api_key)
        self.models = models
        self.model_used: str | None = None

    def direct(self, title: str, author: str, lang: str, chunks: list) -> TitleDirection:
        from google.genai import types
        from net import with_retries

        listing = "\n\n".join(f"<<{c.id}>> [{c.section}]\n{c.text}" for c in chunks)
        prompt = _PROMPT.format(title=title, author=author, language_name=_LANG_NAMES.get(lang, lang),
                                chunks=listing)
        config = types.GenerateContentConfig(response_mime_type="application/json",
                                             response_schema=TitleDirection, temperature=0.4)
        last_exc = None
        for model in self.models:
            try:
                resp = with_retries(lambda: self._client.models.generate_content(
                    model=model, contents=prompt, config=config), attempts=4, label=f"direction/{model}")
                self.model_used = model
                return resp.parsed
            except Exception as exc:  # noqa: BLE001 — fall through to the next model
                last_exc = exc
                print(f"  {model} unavailable ({type(exc).__name__}); trying next model")
        raise last_exc


def build_direction(work_dir: Path, chunks: list, title: str, author: str, api_key: str,
                    max_chars: int = 1500) -> dict:  # tags add characters, not speech
    """Run the director, validate every chunk, write direction.json. Returns the saved document."""
    lang = chunks[0].lang
    director = GeminiDirector(api_key)
    result = director.direct(title, author, lang, chunks)
    by_id = {d.chunk_id: d for d in result.chunks}
    sanitized = 0
    for d in result.chunks:
        d.performance_text, n = sanitize_tags(d.performance_text)
        d.lead_tag = sanitize_tags(f"[{d.lead_tag}]")[0][1:-1]
        sanitized += n
    if sanitized:
        print(f"  sanitized {sanitized} tag(s): removed content/purpose clauses")

    out_chunks = []
    for c in chunks:
        d = by_id.get(c.id)
        problems = ["missing from LLM output"] if d is None else validate(c.text, d, lang, max_chars)
        out_chunks.append({
            "chunk_id": c.id,
            "accepted": not problems,
            "problems": problems,
            "text_to_speak": d.performance_text.strip() if d and not problems else c.text,
            "lead_tag": d.lead_tag if d else None,
            "arc": d.arc if d else None,
            "intended_pace": d.intended_pace if d and not problems else "normal",
            "intended_energy": d.intended_energy if d else "medium",
        })

    doc = {"lang": lang, "model": director.model_used, "profile": result.profile.model_dump(), "chunks": out_chunks,
           "pronunciation_candidates": [p.model_dump() for p in result.pronunciation_candidates],
           "sfx_suggestions": [s.model_dump() for s in result.sfx_suggestions]}
    (work_dir / "direction.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    return doc


def apply_direction(chunks: list, direction_path: Path) -> tuple[list, dict[str, str]]:
    """Chunks with text replaced by the accepted performance text, + intended pace per chunk."""
    from dataclasses import replace
    doc = json.loads(Path(direction_path).read_text())
    by_id = {d["chunk_id"]: d for d in doc["chunks"]}
    directed = [replace(c, text=by_id[c.id]["text_to_speak"]) if c.id in by_id else c for c in chunks]
    pace = {cid: d["intended_pace"] for cid, d in by_id.items()}
    return directed, pace
