"""
Sections -> generation chunks sized for eleven_v3 consistency.

Rules (PLAN.md Stage 3, RESEARCH.md §2):
  - a section (intro / key idea / summary) is a hard boundary — chunks never span two
  - the heading is spoken as the first sentence of the section's first chunk (so it's never a
    tiny standalone generation)
  - paragraph = natural chunk unit; paragraphs under MIN are merged with a neighbour, paragraphs
    over MAX are split at sentence ends into equal-sized parts (no short tail chunk)
  - never split mid-sentence

Input text should already be normalized — lengths here are what actually gets sent to the API.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, asdict

MIN_CHARS = 250
MAX_CHARS = 950  # keeps every paragraph of the pilot title whole (longest ~916); see PLAN.md

_SENTENCE_END = re.compile(r"(?<=[.!?।])[\"'”’)\]]*\s+")


@dataclass
class Chunk:
    id: str                # "<section_slug>_<nn>"
    lang: str
    section: str           # section slug
    order: int             # global order within the title+lang
    text: str
    boundary_after: str    # "paragraph" | "sentence" | "section" | "end"

    @property
    def chars(self) -> int:
        return len(self.text)

    def content_hash(self, params_fingerprint: str = "") -> str:
        return hashlib.sha256(f"{self.text}\x00{params_fingerprint}".encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {**asdict(self), "chars": self.chars}


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_END.split(text) if s.strip()]


def _balanced_split(paragraph: str, max_chars: int) -> list[str]:
    """Split one over-long paragraph into the fewest near-equal parts, at sentence ends."""
    sentences = split_sentences(paragraph)
    n_parts = -(-len(paragraph) // max_chars)  # ceil
    target = len(paragraph) / n_parts
    parts, cur = [], ""
    for s in sentences:
        if cur and len(cur) + 1 + len(s) > target * 1.15 and len(parts) < n_parts - 1:
            parts.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        parts.append(cur)
    return parts


def chunk_section(heading: str, paragraphs: list[str],
                  min_chars: int = MIN_CHARS, max_chars: int = MAX_CHARS) -> list[tuple[str, str]]:
    """Returns [(text, boundary_after)] for one section; last item's boundary is set by caller."""
    units: list[tuple[str, str]] = []  # (text, boundary_after_within_section)
    paras = list(paragraphs)
    if heading:
        paras[0] = f"{heading}\n{paras[0]}" if paras else heading

    for p in paras:
        if len(p) > max_chars:
            pieces = _balanced_split(p, max_chars)
            units += [(x, "sentence") for x in pieces[:-1]] + [(pieces[-1], "paragraph")]
        else:
            units.append((p, "paragraph"))

    # merge undersized units into the following one (or the previous one, at the section end)
    merged: list[tuple[str, str]] = []
    for text, boundary in units:
        if merged and len(merged[-1][0]) < min_chars and len(merged[-1][0]) + len(text) + 1 <= max_chars:
            prev_text, _ = merged.pop()
            merged.append((f"{prev_text}\n\n{text}", boundary))
        else:
            merged.append((text, boundary))
    if len(merged) > 1 and len(merged[-1][0]) < min_chars and \
            len(merged[-2][0]) + len(merged[-1][0]) + 1 <= max_chars:
        last_text, last_b = merged.pop()
        prev_text, _ = merged.pop()
        merged.append((f"{prev_text}\n\n{last_text}", last_b))
    return merged


def chunk_title(sections_by_slug: list[tuple[str, str, list[str]]], lang: str,
                min_chars: int = MIN_CHARS, max_chars: int = MAX_CHARS) -> list[Chunk]:
    """sections_by_slug: [(slug, heading, paragraphs)] in reading order."""
    chunks: list[Chunk] = []
    order = 0
    for s_i, (slug, heading, paragraphs) in enumerate(sections_by_slug):
        pieces = chunk_section(heading, paragraphs, min_chars, max_chars)
        for p_i, (text, boundary) in enumerate(pieces):
            is_last_in_section = p_i == len(pieces) - 1
            if is_last_in_section:
                boundary = "end" if s_i == len(sections_by_slug) - 1 else "section"
            chunks.append(Chunk(f"{slug}_{p_i + 1:02d}", lang, slug, order, text, boundary))
            order += 1
    return chunks
