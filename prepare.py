"""
Stage 2+3 orchestration: content -> normalized, alias-applied, chunked text per language.

Writes work/<title-slug>/<lang>/chunks.json — the exact text that will be sent to the API, for
human review before any credits are spent.
"""
from __future__ import annotations

import json
from pathlib import Path

from chunking.segmenter import Chunk, chunk_title
from config import PACKAGE_ROOT, load_settings
from content.kitab_loader import Title
from normalization.cache_store import JSONFileCache
from normalization.llm_normalizer import GeminiNormalizer
from normalization.pipeline import NormalizationPipeline
from pronunciation.dictionary import AliasApplier, PronunciationDictionary

WORK_ROOT = PACKAGE_ROOT / "work"


def work_dir(title: Title, lang: str) -> Path:
    return WORK_ROOT / title.slug / lang


def prepare_language(title: Title, lang: str, normalize: bool = True,
                     pronunciation: bool = True) -> list[Chunk]:
    settings = load_settings()
    normalizer = None
    if normalize:
        cache = JSONFileCache(settings.normalization_cache_path)
        normalizer = NormalizationPipeline(
            cache, GeminiNormalizer(settings.gemini_api_key, settings.gemini_model), language_hint=lang
        )
    aliases = (AliasApplier(PronunciationDictionary.load(settings.pronunciation_dictionary_path))
               if pronunciation else None)

    def prep(text: str) -> str:
        if normalizer is not None:
            text = normalizer.normalize(text).normalized_text
        if aliases is not None:
            text = aliases.apply(text)
        return text

    sections = [
        (s.slug, prep(s.heading), [prep(p) for p in s.paragraphs])
        for s in title.sections[lang]
    ]
    chunks = chunk_title(sections, lang)

    out = work_dir(title, lang)
    out.mkdir(parents=True, exist_ok=True)
    (out / "chunks.json").write_text(json.dumps(
        {"title": title.title, "author": title.author, "source_id": title.source_id,
         "lang": lang, "chunks": [c.to_dict() for c in chunks]},
        indent=2, ensure_ascii=False))
    return chunks


def load_chunks(path: Path) -> list[Chunk]:
    data = json.loads(Path(path).read_text())
    return [Chunk(c["id"], c["lang"], c["section"], c["order"], c["text"], c["boundary_after"])
            for c in data["chunks"]]
