"""
Chunk WAVs -> one seamless, mastered narration track (PLAN.md Stage 6).

Per chunk:
  1. energy-based trim of the model's own lead/tail silence (v3's alignment is stretched to the
     full file, so it can't be used for edges), keeping the natural decay of the last word
  2. cap over-long pauses inside the chunk (v3 sometimes pauses 2x longer in one chunk)
  3. pace match: time-stretch (Rubber Band, pitch-preserving) toward the title's median
     articulation rate, clamped to ±MAX_TEMPO_DEVIATION
  4. speech-only loudness match to the title median, clamped
  5. micro-fades at the cut points
Join with controlled pauses by boundary type, filled with room tone at the chunks' own noise
floor (so gaps sound like the pauses inside chunks), then one glue-mastering pass over the whole
track and an ACX-style measurement (RMS / peak / noise floor).
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import soundfile as sf

PAUSES_SEC = {"head": 0.5, "sentence": 0.5, "paragraph": 0.9, "section": 2.5, "end": 2.0}
MAX_INNER_PAUSE_SEC = 1.0      # pauses inside a chunk longer than this are shortened to it
MAX_TEMPO_DEVIATION = 0.10     # never stretch a chunk more than ±10% (Rubber Band stays transparent for speech)
TEMPO_DEADBAND = 0.02          # don't touch chunks already within ±2% of the target pace
MAX_GAIN_DB = 4.0
PACE_INTENT = {"slower": 0.94, "normal": 1.0, "faster": 1.06}  # directed pace vs title median
FRAME_SEC = 0.02
ACX = {"rms_db": (-23.0, -18.0), "peak_db_max": -3.0, "noise_floor_db_max": -60.0}


@dataclass
class ChunkEdit:
    chunk_id: str
    trimmed_lead_sec: float
    trimmed_tail_sec: float
    pauses_shortened: int
    tempo: float
    gain_db: float
    duration_sec: float
    pace_deviation: float = 0.0     # how far off the title pace the chunk was before correction
    regenerate_suggested: bool = False  # needed more stretch than allowed -> regenerate instead
    pauses_extended: int = 0             # sentence/clause pauses lengthened to the minimum


def _frames_db(y: np.ndarray, sr: int) -> np.ndarray:
    n = int(sr * FRAME_SEC)
    f = y[: len(y) // n * n].reshape(-1, n)
    return 20 * np.log10(np.sqrt((f ** 2).mean(axis=1)) + 1e-10)


def _activity(y: np.ndarray, sr: int, below_peak_db: float = 45.0) -> np.ndarray:
    db = _frames_db(y, sr)
    return db > (db.max() - below_peak_db)


def trim(y: np.ndarray, sr: int, pre_roll=0.03, tail_keep=0.30) -> tuple[np.ndarray, float, float]:
    act = np.where(_activity(y, sr))[0]
    if act.size == 0:
        return y, 0.0, 0.0
    n = int(sr * FRAME_SEC)
    start = max(0, act[0] * n - int(sr * pre_roll))
    end = min(len(y), (act[-1] + 1) * n + int(sr * tail_keep))
    return y[start:end], start / sr, (len(y) - end) / sr


def cap_inner_pauses(y: np.ndarray, sr: int, max_pause=MAX_INNER_PAUSE_SEC) -> tuple[np.ndarray, int]:
    act = _activity(y, sr)
    n = int(sr * FRAME_SEC)
    keep, shortened, i = [], 0, 0
    max_frames = int(max_pause / FRAME_SEC)
    while i < len(act):
        j = i
        while j < len(act) and act[j] == act[i]:
            j += 1
        run = j - i
        if not act[i] and run > max_frames and i > 0 and j < len(act):
            # keep the first and last halves of the allowed pause (preserves breath/decay shape)
            half = max_frames // 2
            keep += [(i * n, (i + half) * n), ((j - half) * n, j * n)]
            shortened += 1
        else:
            keep.append((i * n, j * n))
        i = j
    tail = y[len(act) * n:]
    out = np.concatenate([y[a:b] for a, b in keep] + [tail])
    return out, shortened


MIN_PAUSE_AFTER = {"sentence": 0.55, "hold": 0.45, "clause": 0.25}  # seconds; extended to at least this
_SENT_END = ("।", ".", "?", "!", "॥")
_CLAUSE_END = (",", "—", "–", ";", ":")
_HOLD_END = ("…", "...")


def _alignment_for(wav: Path) -> list[dict] | None:
    """Forced-alignment words cached by the QC stage for exactly this audio file."""
    import hashlib
    h = hashlib.sha256(wav.read_bytes()).hexdigest()[:16]
    p = wav.parent.parent / "qc_cache" / f"{wav.stem}.align.{h}.json"
    if not p.exists():
        return None
    return [w for w in json.loads(p.read_text())["words"] if (w.get("text") or "").strip()]


def extend_pauses(y: np.ndarray, sr: int, words: list[dict], floor_db: float,
                  rng: np.random.Generator) -> tuple[np.ndarray, int]:
    """Lengthen too-short pauses after sentence/clause ends (v3 voices can barely pause — the
    main reason a narration feels rushed).

    The insertion point is SNAPPED TO REAL SILENCE measured from the audio, never taken from the
    alignment midpoint: alignment word boundaries ran 70-140 ms late on this voice, and mid-gap
    inserts landed inside the next word's onset ("ज|जब", "अ|अपना"). The gap length is also
    measured from the audio. If there is no truly silent spot, nothing is inserted."""
    from generation.overlap import silent_run
    inserts = []
    for w, nxt in zip(words, words[1:]):
        t = w["text"].strip()
        kind = ("hold" if t.endswith(_HOLD_END) else "sentence" if t.endswith(_SENT_END)
                else "clause" if t.endswith(_CLAUSE_END) else None)
        if not kind:
            continue
        run = silent_run(y, sr, w["end"] - 0.40, max(nxt["start"], w["end"]) + 0.10,
                         below_peak_db=50.0, min_run_sec=0.03)
        if run is None:
            continue
        center, length = run
        need = MIN_PAUSE_AFTER[kind] - length
        if need > 0.02:
            inserts.append((center, need))
    if not inserts:
        return y, 0
    out, last = [], 0
    for at, dur in sorted(inserts):
        i = min(int(at * sr), len(y))
        if i < last:
            continue
        out += [y[last:i], room_tone(dur, sr, floor_db, rng)]
        last = i
    out.append(y[last:])
    return np.concatenate(out), len(inserts)


def time_stretch(y: np.ndarray, sr: int, tempo: float) -> np.ndarray:
    """Pitch-preserving tempo change via ffmpeg's Rubber Band filter. tempo>1 = faster."""
    if abs(tempo - 1.0) < 1e-3:
        return y
    with tempfile.TemporaryDirectory() as td:
        src, dst = Path(td) / "in.wav", Path(td) / "out.wav"
        sf.write(src, y, sr, subtype="PCM_24")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                        "-af", f"rubberband=tempo={tempo:.4f}:pitchq=quality:formant=preserved",
                        str(dst)], check=True)
        out, _ = sf.read(dst, dtype="float32")
    return out


def speech_rms_db(y: np.ndarray, sr: int) -> float:
    db = _frames_db(y, sr)
    speech = db[db > db.max() - 25]
    return float(10 * np.log10(np.mean(10 ** (speech / 10))))


def fade(y: np.ndarray, sr: int, fade_in=0.01, fade_out=0.08) -> np.ndarray:
    y = y.copy()
    a, b = int(sr * fade_in), int(sr * fade_out)
    if a:
        y[:a] *= np.linspace(0, 1, a)
    if b:
        y[-b:] *= np.linspace(1, 0, b) ** 2
    return y


def room_tone(seconds: float, sr: int, level_db: float, rng: np.random.Generator) -> np.ndarray:
    n = int(sr * seconds)
    white = rng.standard_normal(n)
    # gentle low-pass (one-pole) so it reads as air, not hiss
    pink = np.empty(n)
    acc = 0.0
    for i in range(n):
        acc = 0.97 * acc + 0.03 * white[i]
        pink[i] = acc
    pink /= (np.sqrt((pink ** 2).mean()) + 1e-12)
    return (pink * 10 ** (level_db / 20)).astype(np.float32)


def measure(path: Path) -> dict:
    y, sr = sf.read(str(path), dtype="float32")
    db = _frames_db(y, sr)
    win = int(0.5 / FRAME_SEC)
    rolling = np.convolve(10 ** (db / 10), np.ones(win) / win, mode="valid")
    floor = float(10 * np.log10(rolling.min() + 1e-12))
    rms = float(20 * np.log10(np.sqrt((y ** 2).mean()) + 1e-12))
    peak = float(20 * np.log10(np.abs(y).max() + 1e-12))
    ok = (ACX["rms_db"][0] <= rms <= ACX["rms_db"][1] and peak <= ACX["peak_db_max"]
          and floor <= ACX["noise_floor_db_max"])
    return {"rms_db": round(rms, 2), "peak_db": round(peak, 2), "noise_floor_db": round(floor, 2),
            "duration_sec": round(len(y) / sr, 2), "acx_ok": ok}


def master(src: Path, dst_wav: Path, dst_mp3: Path, target_lufs: float = -20.0) -> None:
    """One glue chain over the whole joined track, then two-pass loudnorm."""
    pre = "highpass=f=60,deesser=i=0.3,acompressor=threshold=-22dB:ratio=2:attack=15:release=200:makeup=1"
    probe = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(src), "-af",
         f"{pre},loudnorm=I={target_lufs}:TP=-3.5:LRA=9:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, check=True).stderr
    m = json.loads(probe[probe.rfind("{"): probe.rfind("}") + 1])
    ln = (f"loudnorm=I={target_lufs}:TP=-3.5:LRA=9:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
          f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-af", f"{pre},{ln}",
                    "-ar", "44100", "-ac", "1", "-c:a", "pcm_s24le", str(dst_wav)], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(dst_wav),
                    "-c:a", "libmp3lame", "-b:a", "192k", "-abr", "0", str(dst_mp3)], check=True)


def assemble(run_dir: Path, chunks: list, qc_metrics: dict[str, dict] | None = None,
             out_name: str = "narration", intended_pace: dict[str, str] | None = None,
             pause_extension: bool = False) -> dict:
    qc_metrics = qc_metrics or {}
    intended_pace = intended_pace or {}
    rng = np.random.default_rng(0)
    loaded = []
    for c in chunks:
        y, sr = sf.read(str(run_dir / "chunks" / f"{c.id}.wav"), dtype="float32")
        loaded.append((c, y, sr))
    sr = loaded[0][2]

    rates = [qc_metrics.get(c.id, {}).get("syllables_per_sec") for c, _, _ in loaded]
    known = [r for r in rates if r]
    target_rate = float(np.median(known)) if len(known) >= 2 else None

    edits, pieces = [], []
    floors = []
    for (c, y, _), rate in zip(loaded, rates):
        extended = 0
        words = _alignment_for(run_dir / "chunks" / f"{c.id}.wav") if pause_extension else None
        if words:  # raw-chunk timings, so extend before any trimming/stretching
            y, extended = extend_pauses(y, sr, words, float(np.percentile(_frames_db(y, sr), 5)), rng)
        y, lead, tail = trim(y, sr)
        y, shortened = cap_inner_pauses(y, sr)
        tempo, raw = 1.0, 1.0
        if target_rate and rate:
            # a directed 'slower'/'faster' thought is matched to its intended pace, not flattened
            raw = target_rate * PACE_INTENT.get(intended_pace.get(c.id, "normal"), 1.0) / rate
            if abs(raw - 1) > TEMPO_DEADBAND:
                tempo = float(np.clip(raw, 1 - MAX_TEMPO_DEVIATION, 1 + MAX_TEMPO_DEVIATION))
                y = time_stretch(y, sr, tempo)
        db = _frames_db(y, sr)
        floors.append(np.percentile(db, 5))
        pieces.append([c, y])
        edits.append(ChunkEdit(c.id, round(lead, 3), round(tail, 3), shortened, round(tempo, 4), 0.0, 0.0,
                               pace_deviation=round(1 / raw - 1, 3) if raw else 0.0,
                               regenerate_suggested=abs(raw - 1) > MAX_TEMPO_DEVIATION + 0.02,
                               pauses_extended=extended))

    levels = [speech_rms_db(y, sr) for _, y in pieces]
    target_level = float(np.median(levels))
    for i, ((c, y), lvl) in enumerate(zip(pieces, levels)):
        g = float(np.clip(target_level - lvl, -MAX_GAIN_DB, MAX_GAIN_DB))
        y = fade(y * 10 ** (g / 20), sr)
        pieces[i][1] = y
        edits[i].gain_db = round(g, 2)
        edits[i].duration_sec = round(len(y) / sr, 3)

    tone_db = float(np.clip(np.median(floors), -90.0, -65.0))
    out = [room_tone(PAUSES_SEC["head"], sr, tone_db, rng)]
    for c, y in pieces:
        out += [y, room_tone(PAUSES_SEC.get(c.boundary_after, PAUSES_SEC["paragraph"]), sr, tone_db, rng)]
    track = np.concatenate(out)

    joined = run_dir / f"{out_name}_joined.wav"
    sf.write(joined, track, sr, subtype="PCM_24")
    mastered_wav, mastered_mp3 = run_dir / f"{out_name}_mastered.wav", run_dir / f"{out_name}_mastered.mp3"
    master(joined, mastered_wav, mastered_mp3)

    report = {"target_words_per_sec": target_rate, "target_speech_rms_db": round(target_level, 2),
              "room_tone_db": round(tone_db, 1), "pauses_sec": PAUSES_SEC,
              "chunks": [asdict(e) for e in edits], "final": measure(mastered_wav),
              "outputs": {"joined": str(joined), "mastered_wav": str(mastered_wav), "mastered_mp3": str(mastered_mp3)}}
    (run_dir / f"{out_name}_assembly_report.json").write_text(json.dumps(report, indent=2))
    return report
