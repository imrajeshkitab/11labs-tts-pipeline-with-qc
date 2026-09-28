"""
Pronunciation dictionary tooling: a local, human-editable source of truth (JSON) that renders
to ElevenLabs' required PLS (XML) format and uploads via their API.

Confirmed against live ElevenLabs docs (2026-09-22): eleven_v3 supports phoneme-tag entries
directly (in fact it's the only model with multi-language IPA/CMU support — better supported
than most other models, not worse). Other non-flash-v2 models fall back to alias (spelling
substitution) entries instead of phoneme entries.

PLS is case-sensitive, so every phoneme entry is rendered twice automatically (as written, and
Title-cased) unless the term is explicitly all-lowercase-only (e.g. a common word, not a name).

The JSON source has no content yet — Kitab-specific terms (brand names, author names,
Sanskrit/Hindi words) need to come from the content team. This just builds the mechanism.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring
from defusedxml.minidom import parseString as safe_parse_string


@dataclass
class PhonemeEntry:
    grapheme: str
    phoneme: str
    alphabet: str = "ipa"  # "ipa" or "cmu-arpabet"
    add_case_variants: bool = True  # auto-add a Title-cased twin entry


@dataclass
class AliasEntry:
    grapheme: str
    alias: str  # the substitute spelling to force correct pronunciation on non-v3/flash-v2 models


@dataclass
class PronunciationDictionary:
    phoneme_entries: list[PhonemeEntry] = field(default_factory=list)
    alias_entries: list[AliasEntry] = field(default_factory=list)

    # --- local JSON source of truth ---------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "PronunciationDictionary":
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text())
        return cls(
            phoneme_entries=[PhonemeEntry(**e) for e in raw.get("phoneme_entries", [])],
            alias_entries=[AliasEntry(**e) for e in raw.get("alias_entries", [])],
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "phoneme_entries": [e.__dict__ for e in self.phoneme_entries],
            "alias_entries": [e.__dict__ for e in self.alias_entries],
        }, indent=2, ensure_ascii=False))

    def add_phoneme(self, grapheme: str, phoneme: str, alphabet: str = "ipa") -> None:
        self.phoneme_entries.append(PhonemeEntry(grapheme, phoneme, alphabet))

    def add_alias(self, grapheme: str, alias: str) -> None:
        self.alias_entries.append(AliasEntry(grapheme, alias))

    # --- PLS (XML) rendering ------------------------------------------------------------

    def to_pls(self, alphabet: str = "ipa") -> str:
        """Render phoneme entries as PLS XML (the format ElevenLabs' upload API expects).
        Alias entries aren't part of PLS phoneme rules — they're applied as plain text
        substitution before the request is sent (see AliasApplier below)."""
        lexicon = Element("lexicon", {
            "version": "1.0",
            "xmlns": "http://www.w3.org/2005/01/pronunciation-lexicon",
            "alphabet": alphabet,
            "xml:lang": "en-US",
        })

        seen_graphemes: set[str] = set()

        def add_lexeme(grapheme: str, phoneme: str) -> None:
            if grapheme in seen_graphemes:
                return
            seen_graphemes.add(grapheme)
            lexeme = SubElement(lexicon, "lexeme")
            SubElement(lexeme, "grapheme").text = grapheme
            SubElement(lexeme, "phoneme").text = phoneme

        for entry in self.phoneme_entries:
            if entry.alphabet != alphabet:
                continue
            add_lexeme(entry.grapheme, entry.phoneme)
            if entry.add_case_variants:
                title_case = entry.grapheme[:1].upper() + entry.grapheme[1:]
                add_lexeme(title_case, entry.phoneme)

        raw = tostring(lexicon, encoding="unicode")
        return safe_parse_string(raw).toprettyxml(indent="  ")

    def write_pls_file(self, path: Path, alphabet: str = "ipa") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_pls(alphabet=alphabet))
        return path

    # --- upload to ElevenLabs ------------------------------------------------------------

    def upload(self, api_key: str, name: str, pls_path: Path) -> dict:
        """Uploads the PLS file, returns {"id": ..., "version_id": ...} to plug into
        tts.client.ElevenLabsClient.generate(pronunciation_dictionary_locators=...)."""
        from elevenlabs.client import ElevenLabs

        client = ElevenLabs(api_key=api_key)
        with open(pls_path, "rb") as f:
            result = client.pronunciation_dictionaries.create_from_file(file=f, name=name)
        return {"id": result.id, "version_id": result.version_id}


class AliasApplier:
    """Alias entries aren't uploaded — they're a plain-text substitution pass applied to the
    script text before it's sent to models that don't support phoneme tags. Kept separate from
    normalization/pipeline.py: normalization fixes *how numbers/symbols are spoken*, this fixes
    *how specific words are pronounced* — different concerns, same substitution mechanics."""

    def __init__(self, dictionary: PronunciationDictionary):
        self._map = {e.grapheme: e.alias for e in dictionary.alias_entries}

    def apply(self, text: str) -> str:
        import re
        if not self._map:
            return text
        pattern = re.compile("|".join(re.escape(k) for k in self._map), )
        return pattern.sub(lambda m: self._map[m.group()], text)
