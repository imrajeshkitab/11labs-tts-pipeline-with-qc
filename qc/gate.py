"""
QC gate: every generated chunk -> pass | warn | fail, with reasons.

  fail -> regenerate automatically (new seed, max N) — content errors or hallucinated sounds
  warn -> put on the human listening list — voice/delivery outliers, suspicious words
  pass -> accept

Thresholds are initial values, meant to be calibrated on pilot audio (PLAN.md Stage 5).
Per-title outlier checks (z-scores) need >= MIN_CHUNKS_FOR_STATS chunks; below that only
absolute thresholds apply.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np

from qc import acoustics
from qc.elevenlabs_checks import ElevenLabsQC
from qc.text_compare import compare

_TAG = re.compile(r"\[[^\[\]]{1,200}\]")


def spoken_text(text: str) -> str:
    """Script as it should be HEARD: direction tags removed (a tag heard in the transcript =
    extra words = 'tag read aloud' -> caught as an insertion)."""
    return re.sub(r"\s+", " ", _TAG.sub(" ", text)).strip()

THRESHOLDS = {
    "en": {"fail_error_rate": 0.03, "warn_error_rate": 0.01, "metric": "wer"},
    # Hindi also uses artifact-filtered WER: raw CER counts Latin-vs-Devanagari loanword spelling
    "hi": {"fail_error_rate": 0.05, "warn_error_rate": 0.02, "metric": "wer"},
    "fail_max_run": 2,                 # >=2 consecutive words skipped or inserted
    "fail_speaker_sim": 0.70,          # vs anchor
    "warn_speaker_sim": 0.80,
    "warn_z": 2.5,                     # pitch / rate / loudness / similarity outliers
    "warn_join_pitch_jump": 0.06,      # register jump across a chunk join
    "fail_join_pitch_jump": 0.10,
    "warn_align_loss_z": 4.5,          # per-word forced-alignment loss outliers (robust z) —
                                       # uncalibrated: no known-bad sample yet, kept loose
}
MIN_CHUNKS_FOR_STATS = 4


@dataclass
class ChunkQC:
    chunk_id: str
    verdict: str = "pass"
    reasons: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    diffs: list[dict] = field(default_factory=list)

    def flag(self, level: str, reason: str) -> None:
        self.reasons.append(f"[{level}] {reason}")
        if level == "fail" or (level == "warn" and self.verdict == "pass"):
            self.verdict = level


def _robust_z(values: np.ndarray) -> np.ndarray:
    med = np.nanmedian(values)
    mad = np.nanmedian(np.abs(values - med)) * 1.4826
    return (values - med) / mad if mad > 1e-9 else np.zeros_like(values)


def run_qc(run_dir: Path, chunks: list, lang: str, api_key: str, anchor_wav: Path | None = None) -> list[ChunkQC]:
    remote = ElevenLabsQC(api_key, run_dir / "qc_cache")
    t = THRESHOLDS[lang]
    results, feats = [], []

    for c in chunks:
        wav = run_dir / "chunks" / f"{c.id}.wav"
        q = ChunkQC(c.id)
        if not wav.exists():
            q.flag("fail", "audio missing")
            results.append(q); feats.append(None)
            continue

        script = spoken_text(c.text)
        stt = remote.transcribe(wav, lang)
        al = remote.align(wav, script)
        cmp = compare(script, stt["text"], lang)
        rate = getattr(cmp, t["metric"])
        q.metrics.update({"wer": round(cmp.wer, 4), "cer": round(cmp.cer, 4),
                          "script_words": cmp.script_words, "align_loss": al.get("loss")})
        q.diffs = [asdict(d) for d in cmp.diffs]

        if cmp.max_run_deleted >= THRESHOLDS["fail_max_run"]:
            q.flag("fail", f"{cmp.max_run_deleted} consecutive script words not heard (skipped?)")
        if cmp.max_run_inserted >= THRESHOLDS["fail_max_run"]:
            q.flag("fail", f"{cmp.max_run_inserted} consecutive extra words heard (repeat, hallucination, or a tag read aloud?)")
        if rate > t["fail_error_rate"]:
            q.flag("fail", f"{t['metric'].upper()} {rate:.1%} > {t['fail_error_rate']:.0%}")
        elif rate > t["warn_error_rate"]:
            q.flag("warn", f"{t['metric'].upper()} {rate:.1%} — review diffs")
        if cmp.numeric_review:
            q.flag("warn", f"ASR wrote digits {cmp.numeric_review} — confirm spoken number is right")

        events = [w["text"] for w in stt["words"] if w.get("type") == "audio_event"]
        if events:
            q.flag("fail", f"audio events detected: {events}")

        # alignment loss spikes on sentence-final words (it tracks the following pause), so only
        # mid-sentence words are checked; whitespace tokens are ignored entirely
        mid = [w for w in al["words"] if (w.get("text") or "").strip() and w.get("loss") is not None
               and not re.search(r"[.!?।,;:—\"”’)]$", w["text"].strip())]
        # REPORT-ONLY until calibrated against a known-bad sample: on the pilot title it mostly
        # flagged short function words (है, का, को, "on"), i.e. noise, not errors.
        if len(mid) >= 5:
            z = _robust_z(np.array([w["loss"] for w in mid], dtype=float))
            q.metrics["align_outlier_words"] = [mid[i]["text"] for i in np.where(z > THRESHOLDS["warn_align_loss_z"])[0]][:8]

        f = acoustics.extract(wav, al["words"], script, lang)
        q.metrics.update({"pitch_hz": round(f.pitch_median_hz, 1), "lufs": round(f.loudness_lufs, 2),
                          "speech_rms_db": round(f.speech_rms_db, 2),
                          "words_per_sec": round(f.words_per_sec, 2) if f.words_per_sec else None,
                          "syllables_per_sec": round(f.syllables_per_sec, 2) if f.syllables_per_sec else None,
                          "pause_ratio": round(f.pause_ratio, 3) if f.pause_ratio is not None else None})
        results.append(q); feats.append(f)

    # voice consistency: similarity to anchor (default = first chunk that generated)
    valid = [(q, f) for q, f in zip(results, feats) if f is not None]
    if valid:
        anchor = acoustics.extract(anchor_wav).embedding if anchor_wav else valid[0][1].embedding
        for q, f in valid:
            sim = acoustics.cosine(anchor, f.embedding)
            q.metrics["speaker_sim_anchor"] = round(sim, 4)
            if sim < THRESHOLDS["fail_speaker_sim"]:
                q.flag("fail", f"voice similarity to anchor {sim:.3f} < {THRESHOLDS['fail_speaker_sim']}")
            elif sim < THRESHOLDS["warn_speaker_sim"]:
                q.flag("warn", f"voice similarity to anchor {sim:.3f} < {THRESHOLDS['warn_speaker_sim']}")

    if len(valid) >= MIN_CHUNKS_FOR_STATS:
        for key in ("pitch_hz", "syllables_per_sec", "pause_ratio", "speech_rms_db", "speaker_sim_anchor"):
            vals = np.array([q.metrics.get(key) if q.metrics.get(key) is not None else np.nan
                             for q, _ in valid], dtype=float)
            z = _robust_z(vals)
            for (q, _), zi in zip(valid, z):
                if key == "speaker_sim_anchor" and zi > 0:
                    continue  # being MORE similar to the anchor is never a problem
                if abs(zi) > THRESHOLDS["warn_z"]:
                    q.flag("warn", f"{key} outlier (z={zi:+.1f}, value {q.metrics.get(key)})")

    # join check: register jump between the END of one chunk and the START of the next (same
    # section). Calibrated against the voice's OWN intonation resets at pauses inside chunks —
    # a fixed threshold flagged natural prosody (inside-chunk resets measured -15%..+21%).
    # Warn-only: pitch alone never triggers regeneration; the listen list decides.
    internal = []
    for c in chunks:
        wav = run_dir / "chunks" / f"{c.id}.wav"
        if wav.exists():
            internal += [abs(x) for x in acoustics.internal_pitch_changes(wav)]
    limit = max(THRESHOLDS["warn_join_pitch_jump"], float(np.percentile(internal, 90))) if len(internal) >= 5 \
        else THRESHOLDS["fail_join_pitch_jump"]
    for prev, cur, qcur in zip(chunks, chunks[1:], results[1:]):
        if prev.section != cur.section:
            continue
        a = run_dir / "chunks" / f"{prev.id}.wav"
        b = run_dir / "chunks" / f"{cur.id}.wav"
        if not (a.exists() and b.exists()):
            continue
        p_end, p_start = acoustics.edge_pitch(a, "end"), acoustics.edge_pitch(b, "start")
        if not (p_end and p_start):
            continue
        jump = p_start / p_end - 1
        qcur.metrics["join_pitch_jump"] = round(jump, 3)
        qcur.metrics["join_pitch_limit"] = round(limit, 3)
        if abs(jump) > limit:
            qcur.flag("warn", f"pitch jump {jump:+.0%} at the join from {prev.id} exceeds this voice's "
                              f"own natural variation (±{limit:.0%}) — listen")

    report = {"lang": lang, "thresholds": THRESHOLDS, "chunks": [asdict(q) for q in results]}
    (run_dir / "qc_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=float))
    return results
