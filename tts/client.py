"""
Thin ElevenLabs TTS wrapper around a frozen eleven_v3 preset (PLAN.md Stage 1).

It knows nothing about normalization, chunking or dictionaries as concepts — it takes final text
plus a preset and returns audio + alignment + request metadata. Verified against the installed
SDK (elevenlabs==2.68.0, 2026-09-23).
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, asdict, field

# v3 exposes stability as three presets; the API takes the underlying float.
STABILITY_PRESETS = {"creative": 0.0, "natural": 0.5, "robust": 1.0}


@dataclass(frozen=True)
class VoicePreset:
    """Everything that shapes the generated voice, frozen per title. Its fingerprint goes into
    every chunk's content hash, so changing any field invalidates (regenerates) affected chunks."""
    voice_id: str
    language_code: str                    # always explicit: "en" | "hi"
    model_id: str = "eleven_v3"
    stability: str = "natural"            # "natural" | "robust" (never "creative" for audiobooks)
    similarity_boost: float = 0.75
    style: float = 0.0                    # ElevenLabs: keep at 0
    seed: int = 20260923                  # one per title; retries override with a new seed
    apply_text_normalization: str = "off"  # we normalize upstream
    output_format: str = "pcm_44100"      # lossless end to end
    use_pvc_as_ivc: bool = False          # API rejects this on eleven_v3 (verified 2026-09-23)
    pronunciation_dictionary_locators: tuple = field(default_factory=tuple)  # ((id, version_id), ...)

    def __post_init__(self):
        if self.use_pvc_as_ivc and self.model_id == "eleven_v3":
            raise ValueError("use_pvc_as_ivc is not supported with eleven_v3 (API: unsupported_model)")
        if self.stability not in STABILITY_PRESETS:
            raise ValueError(f"stability must be one of {list(STABILITY_PRESETS)}")

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, default=list).encode()).hexdigest()[:16]

    @property
    def sample_rate(self) -> int:
        return int(self.output_format.split("_")[1])


@dataclass
class GenerationResult:
    pcm: bytes                      # 16-bit mono PCM at preset.sample_rate
    alignment: dict | None          # character timings for the text as sent
    normalized_alignment: dict | None
    request_id: str | None
    character_cost: int | None
    quality_check: object           # undocumented field, logged in case it ever populates
    seed: int


class ElevenLabsClient:
    def __init__(self, api_key: str):
        from elevenlabs.client import ElevenLabs
        self._client = ElevenLabs(api_key=api_key)

    def generate(self, text: str, preset: VoicePreset, seed: int | None = None) -> GenerationResult:
        from elevenlabs.types import VoiceSettings, PronunciationDictionaryVersionLocator

        seed = preset.seed if seed is None else seed
        locators = [
            PronunciationDictionaryVersionLocator(pronunciation_dictionary_id=i, version_id=v)
            for i, v in preset.pronunciation_dictionary_locators
        ] or None

        from net import with_retries

        resp = with_retries(lambda: self._client.text_to_speech.with_raw_response.convert_with_timestamps(
            voice_id=preset.voice_id,
            text=text,
            model_id=preset.model_id,
            language_code=preset.language_code,
            output_format=preset.output_format,
            seed=seed,
            apply_text_normalization=preset.apply_text_normalization,
            use_pvc_as_ivc=preset.use_pvc_as_ivc,
            voice_settings=VoiceSettings(
                stability=STABILITY_PRESETS[preset.stability],
                similarity_boost=preset.similarity_boost,
                style=preset.style,
            ),
            pronunciation_dictionary_locators=locators,
        ), label="tts")
        data = resp.data
        as_dict = data.dict() if hasattr(data, "dict") else dict(data)

        def _align(a):
            return a if a is None or isinstance(a, dict) else a.dict()

        cost = resp.headers.get("character-cost")
        return GenerationResult(
            pcm=base64.b64decode(as_dict["audio_base64"]),
            alignment=_align(as_dict.get("alignment")),
            normalized_alignment=_align(as_dict.get("normalized_alignment")),
            request_id=resp.headers.get("request-id"),
            character_cost=int(cost) if cost and cost.isdigit() else None,
            quality_check=as_dict.get("quality_check"),
            seed=seed,
        )
