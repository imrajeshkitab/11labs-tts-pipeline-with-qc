"""
Remote QC signals from ElevenLabs:
  - Scribe v2 transcript (independent check that the audio says the script) + audio-event tags
    (catches hallucinated laughs/music/noise). Run WITHOUT keyterms so script names can't bias
    the ASR into "hearing" a word the TTS actually mangled.
  - Forced Alignment per-word `loss` (high loss = garbled / dropped / mispronounced word).

Results are cached on disk keyed by the audio file's hash, so re-running QC never re-pays.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


class ElevenLabsQC:
    def __init__(self, api_key: str, cache_dir: Path):
        from elevenlabs.client import ElevenLabs
        self._client = ElevenLabs(api_key=api_key)
        self.cache_dir = cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)

    def _cached(self, kind: str, wav: Path, fn):
        path = self.cache_dir / f"{wav.stem}.{kind}.{_file_hash(wav)}.json"
        if path.exists():
            return json.loads(path.read_text())
        from net import with_retries
        result = with_retries(fn, label=kind)
        path.write_text(json.dumps(result, ensure_ascii=False))
        return result

    def transcribe(self, wav: Path, lang: str) -> dict:
        def call():
            with open(wav, "rb") as f:
                r = self._client.speech_to_text.convert(
                    model_id="scribe_v2", file=f, language_code=lang,
                    tag_audio_events=True, timestamps_granularity="word",
                )
            d = r.dict() if hasattr(r, "dict") else dict(r)
            words = [
                {"text": w.get("text"), "type": w.get("type"), "start": w.get("start"),
                 "end": w.get("end"), "logprob": w.get("logprob")}
                for w in (d.get("words") or [])
            ]
            return {"text": d.get("text", ""), "language_code": d.get("language_code"), "words": words}
        return self._cached("scribe", wav, call)

    def align(self, wav: Path, text: str) -> dict:
        def call():
            with open(wav, "rb") as f:
                r = self._client.forced_alignment.create(file=f, text=text)
            d = r.dict() if hasattr(r, "dict") else dict(r)
            words = [{"text": w.get("text"), "start": w.get("start"), "end": w.get("end"),
                      "loss": w.get("loss")} for w in (d.get("words") or [])]
            return {"loss": d.get("loss"), "words": words}
        return self._cached("align", wav, call)
