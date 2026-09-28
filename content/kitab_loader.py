"""
Kitab summary JSON -> clean, speakable sections per language.

Input shape (one record): {"id", "source_id", "title", "author", "content": {"en": md, "hi": md}}
where each markdown body is "### heading" blocks: an intro, "Key Idea N of M" sections, and a
final summary. Output is structure only — no normalization or chunking happens here, so this is
the only file that changes if the content source/format changes.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

_KEY_IDEA_EN = re.compile(r"key\s+idea\s+(\d+)\s+of\s+(\d+)", re.I)
_KEY_IDEA_HI = re.compile(r"मुख्य\s+विचार\s+(\d+)\s+में\s+से\s+(\d+)")


@dataclass
class Section:
    kind: str           # "intro" | "key_idea" | "summary"
    index: int          # key idea number; 0 for intro, 99 for summary
    heading: str        # cleaned heading, spoken form ("Key Idea 1 of 6. Use Vitamin G ...")
    paragraphs: list[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return {"intro": "intro", "summary": "summary"}.get(self.kind, f"key_idea_{self.index}")


@dataclass
class Title:
    id: str
    source_id: str
    title: str
    author: str
    sections: dict[str, list[Section]]  # lang -> sections in order

    @property
    def slug(self) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", self.title.lower()).strip("-")
        return f"{self.source_id}-{base}"


def clean_markdown(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)          # bold
    text = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", text)  # italics
    text = text.replace("*", "")
    text = re.sub(r"([?!])\1+", r"\1", text)              # "??" -> "?"
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _classify(heading: str, position: int, total: int) -> tuple[str, int]:
    m = _KEY_IDEA_EN.search(heading)
    if m:
        return "key_idea", int(m.group(1))
    m = _KEY_IDEA_HI.search(heading)
    if m:
        # Hindi phrasing is "मुख्य विचार <total> में से <n>" — the index is the SECOND number
        return "key_idea", int(m.group(2))
    return ("intro", 0) if position == 0 else ("summary", 99)


def _spoken_heading(heading: str) -> str:
    # "Key Idea 1 of 6: Use X" -> "Key Idea 1 of 6. Use X" — a colon reads poorly aloud; a full
    # stop gives the natural beat a narrator puts after a chapter number.
    h = clean_markdown(heading.lstrip("#").strip())
    stop = "।" if re.search(r"[ऀ-ॿ]", h) else "."
    h = re.sub(r"\s*:\s*", f"{stop} ", h, count=1)
    if h and h[-1] not in ".?!।":
        h += stop
    return h


def parse_markdown(md: str) -> list[Section]:
    parts = re.split(r"(?m)^(#{1,6}.*)$", md)
    headed = [(parts[i], parts[i + 1]) for i in range(1, len(parts), 2)]
    sections = []
    for pos, (heading, body) in enumerate(headed):
        kind, index = _classify(heading, pos, len(headed))
        paragraphs = [clean_markdown(p) for p in re.split(r"\n\s*\n", body) if p.strip()]
        sections.append(Section(kind, index, _spoken_heading(heading), paragraphs))
    return sections


def title_from_record(rec: dict) -> Title:
    """Build a Title from a raw content record (a local JSON file or an RMS DB row).
    Both share the shape: id, source_id, title, author, content={lang: markdown}."""
    return Title(
        id=rec["id"],
        source_id=rec.get("source_id") or rec["id"][:8],
        title=rec["title"],
        author=rec.get("author") or "",
        sections={lang: parse_markdown(md) for lang, md in (rec.get("content") or {}).items() if md},
    )


def load_kitab_json(path: Path) -> Title:
    raw = json.loads(Path(path).read_text())
    rec = raw[0] if isinstance(raw, list) else raw
    return title_from_record(rec)
