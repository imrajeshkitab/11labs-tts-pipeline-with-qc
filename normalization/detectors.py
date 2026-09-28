"""
Regex-based span detectors: find text that needs normalization before it reaches the model
(numbers, currency, dates, phone numbers, abbreviations, symbols) — but do NOT normalize it
here. Detection is free and deterministic; normalization is what costs an LLM call, so keeping
them separate is what makes the cache in `cache_store.py` actually pay off.

Extensibility: register a new detector with `@register_detector("category_name")` on a function
matching the `Detector` signature. `pipeline.py` iterates the registry — no other file needs to
change to add a new category.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable

Detector = Callable[[str], Iterable["Span"]]


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    text: str
    category: str

    def key(self) -> str:
        """Cache key: category-scoped so '100' as a currency vs a phone digit-group
        never collide in the cache even though the raw text is identical."""
        return f"{self.category}:{self.text}"


_REGISTRY: dict[str, Detector] = {}


def register_detector(category: str):
    def decorator(fn: Detector) -> Detector:
        _REGISTRY[category] = fn
        return fn
    return decorator


def all_detectors() -> dict[str, Detector]:
    return dict(_REGISTRY)


def find_spans(text: str) -> list[Span]:
    """Run every registered detector, merge results, drop overlaps (first match wins by
    registration order — order the registrations so more specific patterns run first)."""
    spans: list[Span] = []
    occupied = [False] * len(text)

    for category, detector in _REGISTRY.items():
        for span in detector(text):
            if any(occupied[span.start:span.end]):
                continue
            spans.append(span)
            for i in range(span.start, span.end):
                occupied[i] = True

    return sorted(spans, key=lambda s: s.start)


# --- built-in detectors -----------------------------------------------------------------

@register_detector("date")
def detect_dates(text: str) -> Iterable[Span]:
    # ISO (2024-01-01), slash (01/01/2024), or "January 1, 2024"-style is left to prose already
    pattern = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b")
    for m in pattern.finditer(text):
        yield Span(m.start(), m.end(), m.group(), "date")


@register_detector("phone")
def detect_phone_numbers(text: str) -> Iterable[Span]:
    pattern = re.compile(r"\b(\+?\d{1,3}[-.\s]?)?(\(?\d{3,4}\)?[-.\s]?){2,3}\d{3,4}\b")
    for m in pattern.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if len(digits) >= 7:  # avoid catching plain numbers/years as phone numbers
            yield Span(m.start(), m.end(), m.group(), "phone")


@register_detector("alnum_compound")
def detect_alnum_compounds(text: str) -> Iterable[Span]:
    # "omega-3", "COVID-19", "ओमेगा-3": the number's spoken form depends on the word it's attached
    # to ("omega three", never "omega-tīn"), so the whole compound is one span / one cache entry.
    # [\wऀ-ॿ]: Python's \w skips Devanagari vowel signs (matras), so "ओमेगा" needs the
    # explicit block or the compound is missed and "3" gets normalized alone ("ओमेगा-तीन").
    word = r"[^\W\d_ऀ-ॿ]|[ऀ-ॿ]"
    pattern = re.compile(rf"(?<![\wऀ-ॿ-])(?:{word})(?:[\wऀ-ॿ])*-\d+(?![\w-])")
    for m in pattern.finditer(text):
        yield Span(m.start(), m.end(), m.group(), "alnum_compound")


@register_detector("percent")
def detect_percentages(text: str) -> Iterable[Span]:
    pattern = re.compile(r"(?<![\w.])\d[\d,]*(\.\d+)?\s?%")
    for m in pattern.finditer(text):
        yield Span(m.start(), m.end(), m.group(), "percent")


@register_detector("currency")
def detect_currency(text: str) -> Iterable[Span]:
    pattern = re.compile(r"[$₹€£]\s?\d[\d,]*(\.\d+)?|\b\d[\d,]*(\.\d+)?\s?(USD|INR|EUR|GBP|Rs\.?)\b")
    for m in pattern.finditer(text):
        yield Span(m.start(), m.end(), m.group(), "currency")


@register_detector("measurement")
def detect_measurements(text: str) -> Iterable[Span]:
    units = r"km|m|cm|mm|kg|g|mg|lb|lbs|oz|ft|in|mph|kmh|km/h|%"
    pattern = re.compile(rf"\b\d[\d,]*(\.\d+)?\s?(?:{units})\b")
    for m in pattern.finditer(text):
        yield Span(m.start(), m.end(), m.group(), "measurement")


@register_detector("abbreviation")
def detect_abbreviations(text: str) -> Iterable[Span]:
    # Common title/honorific abbreviations that should be expanded, not spelled out letter-by-letter
    known = ["Dr.", "Mr.", "Mrs.", "Ms.", "Prof.", "St.", "Jr.", "Sr.", "vs.", "etc."]
    for word in known:
        for m in re.finditer(re.escape(word), text):
            yield Span(m.start(), m.end(), m.group(), "abbreviation")


@register_detector("latin_in_indic")
def detect_latin_in_indic(text: str) -> Iterable[Span]:
    # Latin-script tokens inside Devanagari text ("विटामिन G") are a pronunciation risk for Hindi
    # voices — they get transliterated to their Devanagari spoken form. No-op for non-Indic text.
    if not re.search(r"[ऀ-ॿ]", text):
        return
    # letters only (optionally one internal apostrophe) — never swallow surrounding quotes/stops
    for m in re.finditer(r"(?<![\w-])[A-Za-z]+(?:'[A-Za-z]+)?(?![\w-])", text):
        yield Span(m.start(), m.end(), m.group(), "latin_in_indic")


@register_detector("plain_number")
def detect_plain_numbers(text: str) -> Iterable[Span]:
    # Catches standalone numbers not already claimed by a more specific detector above
    # (years, quantities, page references, etc.)
    pattern = re.compile(r"\b\d[\d,]*(\.\d+)?\b")
    for m in pattern.finditer(text):
        yield Span(m.start(), m.end(), m.group(), "plain_number")
