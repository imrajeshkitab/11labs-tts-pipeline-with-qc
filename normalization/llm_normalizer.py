"""
LLM-backed normalizer: turns a batch of raw spans ("$42.50", "Dr.", "2024-01-01") into their
spoken-word form, in one call per batch — never one call per span, and never the whole
document. Only spans that missed the cache reach this file at all (see pipeline.py).

Behind an ABC so Gemini can be swapped for another provider (or a rule-based fallback) without
touching pipeline.py or cache_store.py.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod


class LLMNormalizer(ABC):
    @abstractmethod
    def normalize_batch(self, items: list[dict], language_hint: str = "en") -> dict[str, str]:
        """items: [{"key": "...", "text": "...", "category": "..."}, ...]
        returns: {key: normalized_text}"""
        ...


_PROMPT_TEMPLATE = """You are normalizing text for a text-to-speech engine. For each item below, \
convert the raw text into how it should be SPOKEN ALOUD, written out in words, in {language}.

Rules:
- Numbers, currency, dates, measurements, percentages, phone numbers: spell out in full words.
- Abbreviations/titles: expand to the full word (e.g. "Dr." -> "Doctor").
- "alnum_compound" items (e.g. "omega-3", "COVID-19"): say them the way a native speaker of \
{language} would in that context — often the English number is kept even in Hindi \
(e.g. "ओमेगा-3" -> "ओमेगा-थ्री").
- "latin_in_indic" items: Latin-script words inside Hindi text — write their spoken form in \
Devanagari (e.g. "G" -> "जी").
- Hindi numbers use Indian conventions (lakh/crore) written as Hindi words.
- Use each item's "context" only to decide the correct reading; return the spoken form of \
"text" only, never the surrounding context.
- Do not add extra commentary, punctuation-only changes, or explanation — only the spoken form.
- Preserve the language of the surrounding content ({language}).

Items (JSON array):
{items_json}

Respond with ONLY a JSON object mapping each item's "key" to its normalized spoken form, e.g.:
{{"currency:$42.50": "forty-two dollars and fifty cents"}}
"""


class GeminiNormalizer(LLMNormalizer):
    def __init__(self, api_key: str, model_name: str = "gemini-2.5-flash"):
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        self._model = genai.GenerativeModel(model_name)

    def normalize_batch(self, items: list[dict], language_hint: str = "en") -> dict[str, str]:
        if not items:
            return {}

        prompt = _PROMPT_TEMPLATE.format(
            language=language_hint,
            items_json=json.dumps(items, ensure_ascii=False),
        )
        response = self._model.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json"},
        )
        result = json.loads(response.text)
        if not isinstance(result, dict):
            raise ValueError(f"Expected a JSON object from the model, got: {type(result)}")
        return result


class NullNormalizer(LLMNormalizer):
    """Fallback/testing normalizer that returns spans unchanged. Useful for dry runs where you
    want to see what WOULD be sent to the LLM without spending an API call."""

    def normalize_batch(self, items: list[dict], language_hint: str = "en") -> dict[str, str]:
        return {item["key"]: item["text"] for item in items}
