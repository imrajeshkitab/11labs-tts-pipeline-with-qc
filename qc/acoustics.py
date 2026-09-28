"""
Local acoustic QC signals per chunk — voice consistency, not content:
  - speaker embedding (Resemblyzer d-vector) for similarity to the anchor / title mean
  - median pitch (librosa pYIN) over voiced frames
  - speaking rate (words/sec) from forced-alignment word timings
  - loudness (LUFS, pyloudnorm) and speech-only RMS
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
import soundfile as sf

warnings.filterwarnings("ignore")


@dataclass
class AcousticFeatures:
    embedding: np.ndarray
    pitch_median_hz: float
    loudness_lufs: float
    speech_rms_db: float
    words_per_sec: float | None   # articulation rate: words / (speech span minus pauses > 250 ms)
    pause_ratio: float | None     # share of the speech span spent in pauses > 250 ms
    syllables_per_sec: float | None = None  # pace metric: text-length independent (unlike words/s)


_DEV_CONS = r"[क-हक़-य़]"
_DEV_VOWEL_SIGN = r"[ा-ौॢॣ]"


def count_syllables(text: str, lang: str) -> int:
    """Approximate spoken syllables. Words/sec over-reads passages with short words as 'fast'
    (verified on the pilot title), so pace is measured in syllables/sec instead.
      en: vowel groups per word (min 1)
      hi: independent vowels + vowel signs + consonants carrying the inherent vowel
          (not followed by a vowel sign or virama; word-final schwa is deleted in Hindi)"""
    import re
    if lang == "hi":
        n = len(re.findall(r"[ऄ-औ]", text)) + len(re.findall(_DEV_VOWEL_SIGN, text))
        for word in re.findall(r"[ऀ-ॿ]+", text):
            cons = [m.end() for m in re.finditer(_DEV_CONS, word)]
            for i, end in enumerate(cons):
                nxt = word[end:end + 1]
                if nxt and re.match(rf"{_DEV_VOWEL_SIGN}|्|़", nxt):
                    continue
                if i == len(cons) - 1 and end == len(word) and len(cons) > 1:
                    continue  # schwa deletion at word end
                n += 1
        return max(n, 1)
    return sum(max(1, len(re.findall(r"[aeiouy]+", w.lower()))) for w in re.findall(r"[A-Za-z']+", text))


@lru_cache(maxsize=1)
def _encoder():
    from resemblyzer import VoiceEncoder
    return VoiceEncoder(verbose=False)


def speech_rms_db(y: np.ndarray, sr: int, floor_margin_db: float = 20.0) -> float:
    frame = int(sr * 0.03)
    if len(y) < frame:
        return float("-inf")
    frames = y[: len(y) // frame * frame].reshape(-1, frame)
    rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
    db = 20 * np.log10(rms)
    speech = db[db > db.max() - floor_margin_db - 20]  # frames within 40 dB of the loudest
    return float(20 * np.log10(np.sqrt((10 ** (speech / 10)).mean())))


def extract(wav: Path, align_words: list[dict] | None = None, text: str | None = None,
            lang: str = "en") -> AcousticFeatures:
    import librosa
    import pyloudnorm
    from resemblyzer import preprocess_wav

    y, sr = sf.read(str(wav), dtype="float32")
    if y.ndim > 1:
        y = y.mean(axis=1)

    emb = _encoder().embed_utterance(preprocess_wav(y, source_sr=sr))

    y16 = librosa.resample(y, orig_sr=sr, target_sr=16000)
    f0, voiced, _ = librosa.pyin(y16, fmin=60, fmax=400, sr=16000, frame_length=1024)
    f0v = f0[voiced & ~np.isnan(f0)] if f0 is not None else np.array([])
    pitch = float(np.median(f0v)) if f0v.size else float("nan")

    lufs = float(pyloudnorm.Meter(sr).integrated_loudness(y)) if len(y) > sr * 0.5 else float("nan")

    wps = pause_ratio = sps = None
    if align_words:
        words = [w for w in align_words if w.get("start") is not None and (w.get("text") or "").strip()]
        if len(words) > 1:
            span = words[-1]["end"] - words[0]["start"]
            pauses = sum(g for g in (words[i + 1]["start"] - words[i]["end"] for i in range(len(words) - 1)) if g > 0.25)
            if span > pauses >= 0:
                wps = len(words) / (span - pauses)
                pause_ratio = pauses / span
                if text:
                    sps = count_syllables(text, lang) / (span - pauses)

    return AcousticFeatures(emb, pitch, lufs, speech_rms_db(y, sr), wps, pause_ratio, sps)


def edge_pitch(wav: Path, edge: str, seconds: float = 6.0) -> float | None:
    """Median pitch of the first or last `seconds` of speech in a chunk (for join checks)."""
    import librosa
    y, sr = sf.read(str(wav), dtype="float32")
    if y.ndim > 1:
        y = y.mean(axis=1)
    frame = int(sr * 0.02)
    db = 20 * np.log10(np.sqrt((y[: len(y) // frame * frame].reshape(-1, frame) ** 2).mean(1)) + 1e-10)
    act = np.where(db > db.max() - 40)[0]
    if act.size == 0:
        return None
    s, e = act[0] * frame, (act[-1] + 1) * frame
    n = int(seconds * sr)
    seg = y[s:s + n] if edge == "start" else y[max(s, e - n):e]
    seg16 = librosa.resample(seg, orig_sr=sr, target_sr=16000)
    f0, voiced, _ = librosa.pyin(seg16, fmin=60, fmax=400, sr=16000, frame_length=1024)
    f0 = f0[voiced & ~np.isnan(f0)] if f0 is not None else np.array([])
    return float(np.median(f0)) if f0.size else None


def internal_pitch_changes(wav: Path, window: float = 6.0, min_pause: float = 0.40) -> list[float]:
    """Pitch change across pauses INSIDE one generation — the voice's own natural intonation
    resets. Used to calibrate what a join may do (measured on the pilot: -15%..+21%, median +6%)."""
    import librosa
    y, sr = sf.read(str(wav), dtype="float32")
    if y.ndim > 1:
        y = y.mean(axis=1)
    frame = int(sr * 0.02)
    db = 20 * np.log10(np.sqrt((y[: len(y) // frame * frame].reshape(-1, frame) ** 2).mean(1)) + 1e-10)
    quiet = db < db.max() - 50

    def p(seg):
        s16 = librosa.resample(seg, orig_sr=sr, target_sr=16000)
        f0, v, _ = librosa.pyin(s16, fmin=60, fmax=400, sr=16000, frame_length=1024)
        f0 = f0[v & ~np.isnan(f0)]
        return float(np.median(f0)) if f0.size else None

    out, i, dur = [], 0, len(y) / sr
    while i < len(quiet):
        if quiet[i]:
            j = i
            while j < len(quiet) and quiet[j]:
                j += 1
            a, b = i * 0.02, j * 0.02
            if b - a >= min_pause and a > window and b < dur - window:
                pa, pb = p(y[int((a - window) * sr):int(a * sr)]), p(y[int(b * sr):int((b + window) * sr)])
                if pa and pb:
                    out.append(pb / pa - 1)
            i = j
        else:
            i += 1
    return out


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))
