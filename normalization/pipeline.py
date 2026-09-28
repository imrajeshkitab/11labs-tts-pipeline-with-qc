"""
Orchestrator: text -> normalized text, spending an LLM call only on spans never seen before.

    1. detectors.find_spans(text)              — free, regex, deterministic
    2. split spans into cached vs uncached      — cache lookup, free
    3. llm.normalize_batch(uncached)            — ONE call for however many are new
    4. cache.set(...) for every new result      — persisted, so step 3 never repeats for these
    5. reconstruct the string by substituting each span at its exact offset, right-to-left
       so earlier offsets stay valid — NOT a global str.replace (see PLAN.md for why)

Editing normalization_cache.json by hand and re-running only re-does step 5 (instant, free) —
no LLM call happens unless a genuinely new span shows up. This is what "don't regenerate the
whole thing for a couple of fixes" means in practice.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .cache_store import NormalizationCache
from .detectors import Span, find_spans
from .llm_normalizer import LLMNormalizer


@dataclass
class NormalizationResult:
    original_text: str
    normalized_text: str
    term_map: dict[str, str] = field(default_factory=dict)  # "category:raw text" -> normalized
    new_terms: int = 0
    cached_terms: int = 0


class NormalizationPipeline:
    def __init__(self, cache: NormalizationCache, llm: LLMNormalizer, language_hint: str = "en"):
        self.cache = cache
        self.llm = llm
        self.language_hint = language_hint

    def normalize(self, text: str) -> NormalizationResult:
        spans = find_spans(text)
        if not spans:
            return NormalizationResult(text, text)

        cached = self.cache.get_many([self._key(s) for s in spans])

        uncached: dict[str, Span] = {}
        for s in spans:
            k = self._key(s)
            if k not in cached and k not in uncached:
                uncached[k] = s

        fresh: dict[str, str] = {}
        if uncached:
            batch = [
                {"key": k, "text": s.text, "category": s.category, "context": self._context(text, s)}
                for k, s in uncached.items()
            ]
            fresh = self.llm.normalize_batch(batch, language_hint=self.language_hint)
            for k, s in uncached.items():
                self.cache.set(k, fresh.get(k, s.text))  # fall back to raw text if model omits it
            self.cache.flush()

        term_map = {**cached, **fresh}
        normalized_text = self._apply_spans(text, spans, term_map, self._key)

        return NormalizationResult(
            original_text=text,
            normalized_text=normalized_text,
            term_map={s.text: term_map.get(self._key(s), s.text) for s in spans},
            new_terms=len(uncached),
            cached_terms=len({self._key(s) for s in spans}) - len(uncached),
        )

    def _key(self, span: Span) -> str:
        # language-scoped: "30" is "thirty" in en and "तीस" in hi — never share a cache entry
        return f"{self.language_hint}:{span.key()}"

    @staticmethod
    def _context(text: str, span: Span, width: int = 40) -> str:
        return text[max(0, span.start - width): span.end + width].replace("\n", " ")

    @staticmethod
    def _apply_spans(text: str, spans: list[Span], term_map: dict[str, str], key_fn) -> str:
        result = text
        for span in sorted(spans, key=lambda s: s.start, reverse=True):
            replacement = term_map.get(key_fn(span), span.text)
            result = result[: span.start] + replacement + result[span.end :]
        return result
