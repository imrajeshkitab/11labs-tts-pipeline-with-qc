"""
Overlap-and-trim: v3's substitute for request stitching (which it doesn't support).

Each chunk is a fresh generation that can't hear the previous one, so it starts in the voice's
default register — after an emotionally directed chunk that ends low and firm, the next one jumps
back up (measured: +15% pitch at a join, heard as a different speaker). Fix: prepend the previous
chunk's LAST sentence (with the tag that governs it) as a lead-in, so the model enters the new
chunk from the same register — then cut the lead-in off using the character timestamps.
Only applied within a section; a new key idea starts fresh (a chime and a long pause cover it).
"""
from __future__ import annotations

import re

_TAG = re.compile(r"\[[^\[\]]{1,200}\]")
_SENT_SPLIT = re.compile(r"(?<=[.!?।…])[\"'”’)\]]*\s+")
MAX_LEAD_IN_CHARS = 260


def lead_in_for(prev_text: str) -> str | None:
    """Last sentence of the previous chunk, preceded by the tag in force at that point."""
    body = prev_text.strip()
    tags = list(_TAG.finditer(body))
    spoken = _TAG.sub(" ", body)
    sentences = [s.strip() for s in _SENT_SPLIT.split(spoken) if s.strip()]
    if not sentences:
        return None
    last = sentences[-1]
    if len(last) > MAX_LEAD_IN_CHARS:  # very long final sentence: keep its tail clause
        cut = last[-MAX_LEAD_IN_CHARS:]
        last = cut[cut.find(" ") + 1:]
    tag = tags[-1].group() if tags else ""
    return f"{tag}\n{last}\n\n" if tag else f"{last}\n\n"


def _spoken_mask(text: str) -> list[bool]:
    mask = [not ch.isspace() for ch in text]
    for m in _TAG.finditer(text):
        for i in range(m.start(), m.end()):
            mask[i] = False
    return mask


def cut_time(full_text: str, prefix_len: int, alignment: dict) -> float | None:
    """Seconds at which the real chunk starts: midway between the last spoken character of the
    lead-in and the first spoken character of the chunk."""
    chars = alignment.get("characters") or []
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    if "".join(chars) != full_text:
        return None
    mask = _spoken_mask(full_text)
    prefix_ends = [ends[i] for i in range(min(prefix_len, len(chars))) if mask[i]]
    chunk_starts = [starts[i] for i in range(prefix_len, len(chars)) if mask[i]]
    if not prefix_ends or not chunk_starts:
        return None
    a, b = max(prefix_ends), min(chunk_starts)
    return (a + b) / 2 if b >= a else b


def shift_alignment(alignment: dict, prefix_len: int, t0: float) -> dict:
    """Alignment for the chunk alone, re-based to the trimmed audio."""
    return {
        "characters": alignment["characters"][prefix_len:],
        "character_start_times_seconds": [max(0.0, t - t0) for t in alignment["character_start_times_seconds"][prefix_len:]],
        "character_end_times_seconds": [max(0.0, t - t0) for t in alignment["character_end_times_seconds"][prefix_len:]],
    }


# ---- look-ahead + silence snapping (added 2026-09-24) ---------------------------------------
# v3 ends each generation while the last word is still sounding (measured: files end at
# -18..-26 dB, no decay) -> audible "cut a few ms early". Giving the model the next sentence
# makes our real last word decay naturally; we then cut in the silence before the look-ahead.
# All cut points are snapped to real silence measured from the audio, because alignment word
# boundaries were measured 70-140 ms late on this voice (a mid-gap cut landed inside the next
# word's onset: "ज|जब").

FALLBACK_TAIL = {"en": "And that is where we pause for a moment.", "hi": "और यहीं हम थोड़ा रुकते हैं।"}


def look_ahead_for(next_text: str | None, lang: str) -> str:
    """First sentence of the next chunk (tags removed), or a neutral sentence at the very end."""
    if next_text:
        spoken = _TAG.sub(" ", next_text).strip()
        sentences = [s.strip() for s in _SENT_SPLIT.split(spoken) if s.strip()]
        if sentences:
            first = sentences[0]
            if len(first) > MAX_LEAD_IN_CHARS:
                first = first[:MAX_LEAD_IN_CHARS].rsplit(" ", 1)[0]
            return f"\n\n{first}"
    return f"\n\n{FALLBACK_TAIL.get(lang, FALLBACK_TAIL['en'])}"


def snap_to_silence(samples, sr: int, t_lo: float, t_hi: float,
                    below_peak_db: float = 50.0, min_run_sec: float = 0.04) -> float | None:
    """Center of the longest truly silent run inside [t_lo, t_hi] (seconds), or None."""
    r = silent_run(samples, sr, t_lo, t_hi, below_peak_db, min_run_sec)
    return r[0] if r else None


def silent_run(samples, sr: int, t_lo: float, t_hi: float,
               below_peak_db: float = 50.0, min_run_sec: float = 0.04) -> tuple[float, float] | None:
    """(center_sec, length_sec) of the longest truly silent run inside [t_lo, t_hi], or None."""
    import numpy as np
    frame = int(sr * 0.01)
    lo, hi = max(0, int(t_lo * sr)), min(len(samples), int(t_hi * sr))
    if hi - lo < frame * 2:
        return None
    x = samples[lo:hi].astype(np.float64)
    f = x[: len(x) // frame * frame].reshape(-1, frame)
    db = 20 * np.log10(np.sqrt((f ** 2).mean(axis=1)) + 1e-10)
    peak = 20 * np.log10(np.sqrt((samples.astype(np.float64) ** 2).max()) + 1e-10)
    quiet = db < peak - below_peak_db
    best, best_len, i = None, 0, 0
    while i < len(quiet):
        if quiet[i]:
            j = i
            while j < len(quiet) and quiet[j]:
                j += 1
            if j - i > best_len:
                best, best_len = (i + j) / 2, j - i
            i = j
        else:
            i += 1
    if best is None or best_len * 0.01 < min_run_sec:
        return None
    return (lo + best * frame) / sr, best_len * 0.01


def _index_map(full_text: str, chars: list[str]) -> list[int | None]:
    """full_text index -> alignment index, tolerant of small differences in the characters
    ElevenLabs returns (quotes, dashes, Unicode composition), instead of requiring an exact match."""
    import difflib
    aligned = "".join(chars)
    out: list[int | None] = [None] * len(full_text)
    if aligned == full_text:
        return list(range(len(full_text)))
    for a, b, n in difflib.SequenceMatcher(None, full_text, aligned, autojunk=False).get_matching_blocks():
        for k in range(n):
            out[a + k] = b + k
    return out


def spoken_char_times(full_text: str, alignment: dict, lo: int, hi: int) -> tuple[float, float] | None:
    """(first start, last end) of spoken characters in full_text[lo:hi]."""
    chars = alignment.get("characters") or []
    if not chars:
        return None
    idx_map = _index_map(full_text, chars)
    mask = _spoken_mask(full_text)
    st, en = alignment["character_start_times_seconds"], alignment["character_end_times_seconds"]
    idx = [idx_map[i] for i in range(lo, min(hi, len(full_text))) if mask[i] and idx_map[i] is not None]
    return (st[idx[0]], en[idx[-1]]) if idx else None


def nearest_silence(samples, sr: int, around: float, search: float = 1.2,
                    below_peak_db: float = 50.0, min_run_sec: float = 0.08) -> float | None:
    """Center of the silent run (>= min_run_sec) closest to `around`, within ±search seconds."""
    import numpy as np
    frame = int(sr * 0.01)
    lo, hi = max(0, int((around - search) * sr)), min(len(samples), int((around + search) * sr))
    x = samples[lo:hi].astype(np.float64)
    if len(x) < frame * 4:
        return None
    f = x[: len(x) // frame * frame].reshape(-1, frame)
    db = 20 * np.log10(np.sqrt((f ** 2).mean(axis=1)) + 1e-10)
    peak = 20 * np.log10(np.abs(samples.astype(np.float64)).max() + 1e-10)
    quiet = db < peak - below_peak_db
    best, best_dist, i = None, 1e9, 0
    while i < len(quiet):
        if quiet[i]:
            j = i
            while j < len(quiet) and quiet[j]:
                j += 1
            if (j - i) * 0.01 >= min_run_sec:
                c = (lo + (i + j) / 2 * frame) / sr
                if abs(c - around) < best_dist:
                    best, best_dist = c, abs(c - around)
            i = j
        else:
            i += 1
    return best
