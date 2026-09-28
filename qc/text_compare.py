"""
Script vs. ASR transcript comparison — catches skipped, repeated, inserted and misread words.

Normalization is language-aware: generic lowercase + punctuation strip for English; for Hindi
also NFC, nukta removal, chandrabindu->anusvara, ZWJ/ZWNJ removal. (Whisper-style normalizers
strip matras and inflate Indic accuracy — RESEARCH.md §7 — so we don't use them.)

Tokens containing digits in the transcript are NOT counted as errors (ASR writes "30%" where the
script says "thirty percent"); they're listed separately for review instead.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field

_ZW = dict.fromkeys(map(ord, "‌‍"), None)


def normalize_for_compare(text: str, lang: str) -> list[str]:
    t = unicodedata.normalize("NFC", text).lower()
    if lang == "hi":
        t = t.translate(_ZW).replace("़", "").replace("ँ", "ं")
        t = unicodedata.normalize("NFC", t)
    t = t.replace("-", " ").replace("—", " ").replace("–", " ")
    t = "".join(ch if not unicodedata.category(ch).startswith("P") else " " for ch in t)
    return t.split()


@dataclass
class DiffItem:
    op: str               # "delete" (in script, not heard) | "insert" (heard, not in script) | "replace"
    script: str
    heard: str
    script_index: int     # word index in the script, for locating it in audio


@dataclass
class TextComparison:
    wer: float
    cer: float
    script_words: int
    diffs: list[DiffItem] = field(default_factory=list)
    numeric_review: list[str] = field(default_factory=list)
    max_run_deleted: int = 0
    max_run_inserted: int = 0


_LATIN_PHONETIC = [("sci", "si"), ("sce", "se"), ("ce", "se"), ("ci", "si"), ("cy", "sy"),
                   ("gn", "n"), ("ge", "je"), ("gi", "ji"), ("gy", "jy"), ("ph", "f"), ("ck", "k"), ("sh", "s"), ("th", "t"), ("dh", "d"), ("kh", "k"),
                   ("gh", "g"), ("bh", "b"), ("ch", "c"), ("q", "k"), ("x", "ks"), ("w", "v"),
                   ("z", "j"), ("c", "k")]


def _skeleton(latin: str) -> str:
    """Consonant skeleton of a romanized word: phonetic folding, drop vowels, collapse repeats.
    'stress'->'strs', 'sTresa'->'strs', 'cortisol'->'krtsl', 'korTisola'->'krtsl'."""
    s = re.sub(r"[^a-z]", "", latin.replace("M", "n").lower())  # ITRANS anusvara 'M' ~ n
    for a, b in _LATIN_PHONETIC:
        s = s.replace(a, b)
    s = re.sub(r"[aeiouy]", "", s)
    return re.sub(r"(.)\1+", r"\1", s)


def _romanize_devanagari(text: str) -> str:
    from indic_transliteration import sanscript
    return sanscript.transliterate(text, sanscript.DEVANAGARI, sanscript.ITRANS)


# Hindi spellings that are pronounced the same (or interchangeably) and that ASR normalizes to
# one form — differences between them are transcription artifacts, not TTS errors.
_HI_VARIANTS = [{"ये", "यह", "येह"}, {"वो", "वह", "वोह"}, {"न", "ना"}, {"नहीं", "नही"},
                {"गई", "गयी"}, {"गए", "गये"}, {"लिए", "लिये"}, {"हुई", "हुयी"}, {"कई", "कयी"}]


def _hi_variant_equal(a: str, b: str) -> bool:
    wa, wb = a.split(), b.split()
    if len(wa) != len(wb):
        return False
    return all(x == y or any(x in s and y in s for s in _HI_VARIANTS) for x, y in zip(wa, wb))


def is_equivalent(script: str, heard: str, lang: str) -> bool:
    """True if a diff is a transcription artifact, not a TTS error:
      - joined/split words ("green space" vs "greenspace", "आस पास" vs "आसपास")
      - Hindi loanwords the ASR wrote in Latin ("stress" for "स्ट्रेस")"""
    if not script or not heard:
        return False
    if script.replace(" ", "") == heard.replace(" ", ""):
        return True
    if lang == "hi" and _hi_variant_equal(script, heard):
        return True
    if lang == "hi" and re.search(r"[a-z]", heard) and re.search(r"[ऀ-ॿ]", script):
        a, b = _skeleton(_romanize_devanagari(script)), _skeleton(heard)
        if a and b:
            return difflib.SequenceMatcher(None, a, b).ratio() >= 0.75
    return False


def _cer(a: str, b: str) -> float:
    if not a:
        return 0.0 if not b else 1.0
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    edits = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal")
    return edits / len(a)


def compare(script: str, transcript: str, lang: str) -> TextComparison:
    ref = normalize_for_compare(script, lang)
    hyp_all = normalize_for_compare(transcript, lang)
    numeric = [w for w in hyp_all if re.search(r"\d", w)]
    hyp = [w for w in hyp_all if not re.search(r"\d", w)]

    sm = difflib.SequenceMatcher(None, ref, hyp, autojunk=False)
    diffs = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            op = {"delete": "delete", "insert": "insert"}.get(tag, "replace")
            diffs.append(DiffItem(op, " ".join(ref[i1:i2]), " ".join(hyp[j1:j2]), i1))

    # numeric tokens were removed from hyp, so script words they stood for ("thirty percent")
    # show up as short deletions — excuse up to one short deletion per numeric token
    if numeric:
        explained = [d for d in diffs if d.op == "delete" and len(d.script.split()) <= 4][: len(numeric)]
        diffs = [d for d in diffs if d not in explained]

    artifacts = [d for d in diffs if d.op == "replace" and is_equivalent(d.script, d.heard, lang)]
    diffs = [d for d in diffs if d not in artifacts]

    def _n(s: str) -> int:
        return len(s.split())

    errors = sum(max(_n(d.script), _n(d.heard)) for d in diffs)
    max_del = max((_n(d.script) for d in diffs if d.op == "delete"), default=0)
    max_ins = max((_n(d.heard) for d in diffs if d.op == "insert"), default=0)

    return TextComparison(
        wer=errors / max(len(ref), 1),
        cer=_cer(" ".join(ref), " ".join(hyp)),
        script_words=len(ref),
        diffs=diffs,
        numeric_review=numeric,
        max_run_deleted=max_del,
        max_run_inserted=max_ins,
    )
